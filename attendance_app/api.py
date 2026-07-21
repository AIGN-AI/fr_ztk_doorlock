import os
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from attendance_app.db import Database, rows_to_csv, rows_to_excel_html, today_range
from attendance_app.device import ZKDevice


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
    password: str


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


def create_app(db=None, device=None):
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
    zk_device = device or ZKDevice(
        host=os.getenv("ZK_HOST", "10.10.9.60"),
        port=int(os.getenv("ZK_PORT", "4370")),
        timeout=int(os.getenv("ZK_TIMEOUT", "8")),
        password=int(os.getenv("ZK_PASSWORD", "0")),
        force_udp=os.getenv("ZK_FORCE_UDP", "").lower() in {"1", "true", "yes"},
    )
    sessions = set()

    def require_auth(attendance_session: str | None = Cookie(default=None)):
        if attendance_session not in sessions:
            raise HTTPException(status_code=401, detail="login required")

    @app.post("/api/auth/login")
    def login(payload: LoginRequest, response: Response):
        expected = os.getenv("ADMIN_PASSWORD", "admin")
        if not secrets.compare_digest(payload.password, expected):
            raise HTTPException(status_code=401, detail="password salah")
        token = secrets.token_urlsafe(32)
        sessions.add(token)
        response.set_cookie(
            "attendance_session",
            token,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=60 * 60 * 12,
        )
        database.log_audit("login", "admin login")
        return {"ok": True}

    @app.post("/api/auth/logout", dependencies=[Depends(require_auth)])
    def logout(response: Response, attendance_session: str | None = Cookie(default=None)):
        sessions.discard(attendance_session)
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

    @app.post("/api/device/sync-time", dependencies=[Depends(require_auth)])
    def sync_time():
        try:
            ok = bool(zk_device.set_time(datetime.now()))
            database.log_sync("time", ok, "device time synced")
            database.log_audit("sync_time", "manual sync")
            return {"ok": ok}
        except Exception as exc:
            database.log_sync("time", False, str(exc))
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/device/sync-users", dependencies=[Depends(require_auth)])
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

    @app.post("/api/device/sync-attendance", dependencies=[Depends(require_auth)])
    def sync_attendance():
        return _sync_attendance(database, zk_device)

    @app.post("/api/device/auto-sync-attendance", dependencies=[Depends(require_auth)])
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

    @app.get("/api/employees", dependencies=[Depends(require_auth)])
    def list_employees():
        return database.list_employees()

    @app.post("/api/employees", dependencies=[Depends(require_auth)])
    def create_employee(payload: EmployeeRequest):
        employee = database.upsert_employee(payload.model_dump())
        database.log_audit("employee_create", employee["employee_code"])
        return employee

    @app.patch("/api/employees/{employee_id}", dependencies=[Depends(require_auth)])
    def update_employee(employee_id: int, payload: EmployeePatch):
        data = {key: value for key, value in payload.model_dump().items() if value is not None}
        employee = database.update_employee(employee_id, data)
        if not employee:
            raise HTTPException(status_code=404, detail="employee tidak ditemukan")
        database.log_audit("employee_update", employee["employee_code"])
        return employee

    @app.post("/api/employees/{employee_id}/push-to-device", dependencies=[Depends(require_auth)])
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

    @app.get("/api/attendance", dependencies=[Depends(require_auth)])
    def attendance(start: str | None = None, end: str | None = None, employee_code: str | None = None):
        return database.list_attendance(_start_iso(start), _end_iso(end), employee_code)

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


def _start_iso(value):
    return f"{value}T00:00:00" if value and len(value) == 10 else value


def _end_iso(value):
    return f"{value}T23:59:59" if value and len(value) == 10 else value


app = create_app()
