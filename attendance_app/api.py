import os
import secrets
from datetime import date, datetime
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from attendance_app.db import Database, rows_to_csv, rows_to_excel_html, today_range
from attendance_app.device import ZKDevice, parse_attendance_photo_name


def load_env(path=".env"):
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("\"'")
        os.environ.setdefault(key.strip(), value)


load_env()


class LoginRequest(BaseModel):
    username: str | None = None
    password: str


class UserRequest(BaseModel):
    username: str
    name: str
    role: str = "user"
    password: str
    active: bool = True


class UserPatch(BaseModel):
    username: str | None = None
    name: str | None = None
    role: str | None = None
    password: str | None = None
    active: bool | None = None


class EmployeeRequest(BaseModel):
    employee_code: str
    name: str
    uid: int | None = None
    card: int = 0
    privilege: int = 0
    active: bool = True


class EmployeePatch(BaseModel):
    employee_code: str | None = None
    name: str | None = None
    uid: int | None = None
    card: int | None = None
    privilege: int | None = None
    active: bool | None = None


class AttendanceNoteRequest(BaseModel):
    employee_code: str
    kind: str
    status: str = "approved"
    start_date: str
    end_date: str
    note: str = ""


class AttendanceNotePatch(BaseModel):
    employee_code: str | None = None
    kind: str | None = None
    status: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    note: str | None = None


def create_app(db=None, device=None, photo_dir=None):
    app = FastAPI(title="MiniAC Plus Attendance")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    database = db or Database(os.getenv("ATTENDANCE_DB", "data/attendance.sqlite3"))
    database.init()
    database.ensure_admin_user(os.getenv("ADMIN_PASSWORD", "admin"))
    photo_root = Path(photo_dir or "data/attendance_photos")
    zk_device = device or ZKDevice(
        host=os.getenv("ZK_HOST", "10.10.9.60"),
        port=int(os.getenv("ZK_PORT", "4370")),
        timeout=int(os.getenv("ZK_TIMEOUT", "8")),
        password=int(os.getenv("ZK_PASSWORD", "0")),
        force_udp=os.getenv("ZK_FORCE_UDP", "").lower() in {"1", "true", "yes"},
    )
    sessions = {}

    def require_auth(attendance_session: str | None = Cookie(default=None)):
        user = sessions.get(attendance_session)
        if not user:
            raise HTTPException(status_code=401, detail="login required")
        return user

    def require_admin(user=Depends(require_auth)):
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="admin required")
        return user

    @app.post("/api/auth/login")
    def login(payload: LoginRequest, response: Response):
        username = payload.username or "admin"
        user = database.authenticate_user(username, payload.password)
        if not user:
            raise HTTPException(status_code=401, detail="password salah")
        token = secrets.token_urlsafe(32)
        sessions[token] = user
        response.set_cookie(
            "attendance_session",
            token,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=60 * 60 * 12,
        )
        database.log_audit("login", username)
        return {"ok": True, "user": user}

    @app.get("/api/auth/me")
    def me(user=Depends(require_auth)):
        return user

    @app.post("/api/auth/logout", dependencies=[Depends(require_auth)])
    def logout(response: Response, attendance_session: str | None = Cookie(default=None)):
        sessions.pop(attendance_session, None)
        response.delete_cookie("attendance_session")
        return {"ok": True}

    @app.get("/api/device/status", dependencies=[Depends(require_auth)])
    def device_status():
        try:
            status = zk_device.status()
            status["clock_warning"] = _clock_warning(status.get("time"))
            return status
        except Exception as exc:
            return {"connected": False, "host": zk_device.host, "port": zk_device.port, "error": str(exc)}

    @app.post("/api/device/sync-time", dependencies=[Depends(require_admin)])
    def sync_time():
        try:
            ok = bool(zk_device.set_time(datetime.now()))
            database.log_sync("time", ok, "device time synced")
            database.log_audit("sync_time", "manual sync")
            return {"ok": ok}
        except Exception as exc:
            database.log_sync("time", False, str(exc))
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/device/sync-users", dependencies=[Depends(require_admin)])
    def sync_users():
        try:
            users = zk_device.users()
            for user in users:
                database.upsert_employee(user)
            database.log_sync("users", True, "users synced", pulled=len(users), inserted=len(users))
            return {"ok": True, "pulled": len(users)}
        except Exception as exc:
            database.log_sync("users", False, str(exc))
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/device/sync-attendance", dependencies=[Depends(require_admin)])
    def sync_attendance():
        return _sync_attendance(database, zk_device)

    @app.post("/api/device/auto-sync-attendance", dependencies=[Depends(require_admin)])
    def auto_sync_attendance():
        try:
            result = _sync_attendance(database, zk_device)
            today, _ = today_range()
            return {
                **result,
                "today": database.daily_report(today, today),
                "last_sync": database.last_sync(),
            }
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/device/sync-attendance-photos", dependencies=[Depends(require_admin)])
    def sync_attendance_photos(start: str | None = None, end: str | None = None):
        start, end = _photo_date_range(start, end)
        try:
            return _sync_attendance_photos(database, zk_device, photo_root, start, end)
        except Exception as exc:
            database.log_sync("attendance_photos", False, str(exc))
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/employees", dependencies=[Depends(require_auth)])
    def list_employees():
        return database.list_employees()

    @app.post("/api/employees", dependencies=[Depends(require_admin)])
    def create_employee(payload: EmployeeRequest):
        employee = _save_or_400(lambda: database.upsert_employee(payload.model_dump()))
        database.log_audit("employee_create", employee["employee_code"])
        return employee

    @app.patch("/api/employees/{employee_id}", dependencies=[Depends(require_admin)])
    def update_employee(employee_id: int, payload: EmployeePatch):
        data = {key: value for key, value in payload.model_dump().items() if value is not None}
        employee = _save_or_400(lambda: database.update_employee(employee_id, data))
        if not employee:
            raise HTTPException(status_code=404, detail="employee tidak ditemukan")
        database.log_audit("employee_update", employee["employee_code"])
        return employee

    @app.post("/api/employees/{employee_id}/push-to-device", dependencies=[Depends(require_admin)])
    def push_employee(employee_id: int):
        employee = database.get_employee(employee_id)
        if not employee:
            raise HTTPException(status_code=404, detail="employee tidak ditemukan")
        try:
            ok = zk_device.set_user(employee)
            database.log_audit("push_to_device", employee["employee_code"])
            return {"ok": bool(ok)}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/users", dependencies=[Depends(require_auth)])
    def list_users():
        return database.list_users()

    @app.post("/api/users", dependencies=[Depends(require_admin)])
    def create_user(payload: UserRequest):
        user = _save_or_400(lambda: database.create_user(payload.model_dump()))
        database.log_audit("user_create", user["username"])
        return user

    @app.patch("/api/users/{user_id}", dependencies=[Depends(require_admin)])
    def update_user(user_id: int, payload: UserPatch):
        data = {key: value for key, value in payload.model_dump().items() if value is not None}
        user = _save_or_400(lambda: database.update_user(user_id, data))
        if not user:
            raise HTTPException(status_code=404, detail="user tidak ditemukan")
        database.log_audit("user_update", user["username"])
        return user

    @app.get("/api/attendance-notes", dependencies=[Depends(require_auth)])
    def attendance_notes(start: str | None = None, end: str | None = None, employee_code: str | None = None, kind: str | None = None, status: str | None = None):
        return database.list_attendance_notes(start, end, employee_code, kind, status)

    @app.post("/api/attendance-notes", dependencies=[Depends(require_admin)])
    def create_attendance_note(payload: AttendanceNoteRequest):
        note = _save_or_400(lambda: database.create_attendance_note(payload.model_dump()))
        database.log_audit("attendance_note_create", f"{note['employee_code']} {note['kind']}")
        return note

    @app.patch("/api/attendance-notes/{note_id}", dependencies=[Depends(require_admin)])
    def update_attendance_note(note_id: int, payload: AttendanceNotePatch):
        data = {key: value for key, value in payload.model_dump().items() if value is not None}
        note = _save_or_400(lambda: database.update_attendance_note(note_id, data))
        if not note:
            raise HTTPException(status_code=404, detail="catatan tidak ditemukan")
        database.log_audit("attendance_note_update", f"{note['employee_code']} {note['kind']}")
        return note

    @app.get("/api/attendance", dependencies=[Depends(require_auth)])
    def attendance(start: str | None = None, end: str | None = None, employee_code: str | None = None):
        return database.list_attendance(_start_iso(start), _end_iso(end), employee_code)

    @app.get("/api/attendance-photos/{filename}", dependencies=[Depends(require_auth)])
    def attendance_photo(filename: str):
        if not parse_attendance_photo_name(filename):
            raise HTTPException(status_code=404, detail="foto tidak ditemukan")
        path = photo_root / filename
        if not path.is_file():
            raise HTTPException(status_code=404, detail="foto tidak ditemukan")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/reports/daily", dependencies=[Depends(require_auth)])
    def daily(start: str | None = None, end: str | None = None):
        if not start or not end:
            start, end = today_range()
        return database.daily_report(start, end)

    @app.get("/api/reports/monthly", dependencies=[Depends(require_auth)])
    def monthly(month: str | None = None):
        month = month or datetime.now().strftime("%Y-%m")
        return database.monthly_report(month)

    @app.get("/api/reports/export.csv", dependencies=[Depends(require_auth)])
    def export_csv(start: str | None = None, end: str | None = None):
        if not start or not end:
            start, end = today_range()
        return PlainTextResponse(rows_to_csv(database.daily_report(start, end)), media_type="text/csv")

    @app.get("/api/reports/export.xls", dependencies=[Depends(require_auth)])
    def export_xls(start: str | None = None, end: str | None = None):
        if not start or not end:
            start, end = today_range()
        return Response(
            rows_to_excel_html(database.daily_report(start, end)),
            media_type="application/vnd.ms-excel",
            headers={"Content-Disposition": 'attachment; filename="laporan-absensi-harian.xls"'},
        )

    @app.get("/api/summary", dependencies=[Depends(require_auth)])
    def summary():
        today, _ = today_range()
        return {
            "employees": len(database.list_employees()),
            "today": database.daily_report(today, today),
            "last_sync": database.last_sync(),
        }

    dist_dir = Path(__file__).resolve().parent.parent / "dist"
    if dist_dir.is_dir():
        app.mount("/", StaticFiles(directory=dist_dir, html=True), name="static")

    return app


def _clock_warning(value):
    if not isinstance(value, str) or len(value) < 4:
        return "Jam device tidak terbaca"
    try:
        year = int(value[:4])
    except ValueError:
        return "Jam device tidak valid"
    return "Jam device perlu disinkronkan" if year < 2024 else None


def _sync_attendance(database, zk_device):
    try:
        logs = zk_device.attendance()
        inserted = database.upsert_attendance(logs)
        database.log_sync("attendance", True, "attendance synced", pulled=len(logs), inserted=inserted)
        return {"ok": True, "pulled": len(logs), "inserted": inserted}
    except Exception as exc:
        database.log_sync("attendance", False, str(exc))
        raise exc


def _sync_attendance_photos(database, zk_device, photo_root, start, end):
    known_names = database.attendance_photo_names()
    result = zk_device.attendance_photos(start, end, known_names)
    photo_root.mkdir(parents=True, exist_ok=True)
    records = []
    for photo in result["photos"]:
        (photo_root / photo["filename"]).write_bytes(photo["data"])
        records.append({key: photo[key] for key in ("filename", "employee_code", "timestamp")})
    downloaded = database.upsert_attendance_photos(records)
    matched = result["matched"]
    database.log_sync("attendance_photos", True, "attendance photos synced", pulled=matched, inserted=downloaded)
    return {"ok": True, "matched": matched, "downloaded": downloaded, "skipped": matched - downloaded}


def _photo_date_range(start, end):
    if not start or not end:
        start, end = today_range()
    try:
        if date.fromisoformat(start) > date.fromisoformat(end):
            raise ValueError
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="rentang tanggal tidak valid") from exc
    return start, end


def _start_iso(value):
    return f"{value}T00:00:00" if value and len(value) == 10 else value


def _end_iso(value):
    return f"{value}T23:59:59" if value and len(value) == 10 else value


def _save_or_400(callback):
    try:
        return callback()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        message = str(exc)
        if "UNIQUE constraint failed" in message:
            raise HTTPException(status_code=400, detail="data sudah ada") from exc
        raise


app = create_app()
