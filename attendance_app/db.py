import csv
import hashlib
import hmac
import html
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path

PASSWORD_ITERATIONS = 260_000
NOTE_KINDS = {"cuti": "Cuti", "izin": "Izin", "tugas_unit": "Tugas Unit"}
NOTE_STATUSES = {"approved", "rejected", "cancelled"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
  password_hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS employees (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  uid INTEGER,
  card INTEGER DEFAULT 0,
  privilege INTEGER DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS attendance_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_code TEXT NOT NULL,
  uid INTEGER,
  timestamp TEXT NOT NULL,
  status INTEGER DEFAULT 0,
  punch INTEGER DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'device',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(employee_code, timestamp, status, punch)
);

CREATE TABLE IF NOT EXISTS attendance_photos (
  filename TEXT PRIMARY KEY,
  employee_code TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(employee_code, timestamp)
);

CREATE TABLE IF NOT EXISTS attendance_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_code TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('cuti', 'izin', 'tugas_unit')),
  status TEXT NOT NULL CHECK (status IN ('approved', 'rejected', 'cancelled')),
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(employee_code) REFERENCES employees(employee_code)
);

CREATE TABLE IF NOT EXISTS sync_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  ok INTEGER NOT NULL,
  message TEXT NOT NULL DEFAULT '',
  pulled INTEGER NOT NULL DEFAULT 0,
  inserted INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path="attendance.sqlite3"):
        self.path = Path(path)

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self):
        with closing(self.connect()) as conn:
            with conn:
                conn.executescript(SCHEMA)

    def ensure_admin_user(self, password, username="admin", name="Admin"):
        with closing(self.connect()) as conn:
            existing = conn.execute("SELECT COUNT(*) AS total FROM users").fetchone()["total"]
        if existing:
            return None
        return self.create_user({"username": username, "name": name, "role": "admin", "password": password, "active": True})

    def create_user(self, data):
        username = str(data["username"]).strip()
        name = str(data["name"]).strip()
        role = str(data.get("role") or "user").strip()
        password = str(data.get("password") or "")
        if not username or not name or not password:
            raise ValueError("username, name, dan password wajib diisi")
        if role not in {"admin", "user"}:
            raise ValueError("role harus admin atau user")
        with closing(self.connect()) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO users (username, name, role, password_hash, active, updated_at)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (username, name, role, hash_password(password), 1 if data.get("active", True) else 0),
                )
        return _public_user(self.get_user_by_username(username))

    def update_user(self, user_id, data):
        current = self.get_user(user_id)
        if not current:
            return None
        updates = {
            "username": str(data.get("username", current["username"])).strip(),
            "name": str(data.get("name", current["name"])).strip(),
            "role": str(data.get("role", current["role"])).strip(),
            "active": 1 if data.get("active", bool(current["active"])) else 0,
        }
        if not updates["username"] or not updates["name"]:
            raise ValueError("username dan name wajib diisi")
        if updates["role"] not in {"admin", "user"}:
            raise ValueError("role harus admin atau user")
        assignments = ["username = ?", "name = ?", "role = ?", "active = ?", "updated_at = CURRENT_TIMESTAMP"]
        params = [updates["username"], updates["name"], updates["role"], updates["active"]]
        if data.get("password"):
            assignments.insert(3, "password_hash = ?")
            params.insert(3, hash_password(str(data["password"])))
        params.append(user_id)
        with closing(self.connect()) as conn:
            with conn:
                conn.execute(f"UPDATE users SET {', '.join(assignments)} WHERE id = ?", params)
        return _public_user(self.get_user(user_id))

    def authenticate_user(self, username, password):
        user = self.get_user_by_username(username)
        if not user or not user["active"] or not verify_password(password, user["password_hash"]):
            return None
        user.pop("password_hash", None)
        return user

    def get_user(self, user_id):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_user_by_username(self, username):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (str(username).strip(),)).fetchone()
        return dict(row) if row else None

    def list_users(self):
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT id, username, name, role, active, created_at, updated_at FROM users ORDER BY active DESC, role, name"
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_employee(self, data):
        code = str(data["employee_code"]).strip()
        name = str(data["name"]).strip()
        if not code or not name:
            raise ValueError("employee_code dan name wajib diisi")
        with closing(self.connect()) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO employees (employee_code, name, uid, card, privilege, active, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(employee_code) DO UPDATE SET
                      name=excluded.name,
                      uid=COALESCE(excluded.uid, employees.uid),
                      card=excluded.card,
                      privilege=excluded.privilege,
                      active=excluded.active,
                      updated_at=CURRENT_TIMESTAMP
                    """,
                    (
                        code,
                        name,
                        data.get("uid"),
                        int(data.get("card") or 0),
                        int(data.get("privilege") or 0),
                        1 if data.get("active", True) else 0,
                    ),
                )
        return self.get_employee_by_code(code)

    def update_employee(self, employee_id, data):
        current = self.get_employee(employee_id)
        if not current:
            return None
        merged = {**current, **data}
        employee = self.upsert_employee(merged)
        return self.get_employee(employee["id"])

    def get_employee(self, employee_id):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
        return dict(row) if row else None

    def get_employee_by_code(self, code):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM employees WHERE employee_code = ?", (str(code),)).fetchone()
        return dict(row) if row else None

    def list_employees(self):
        with closing(self.connect()) as conn:
            rows = conn.execute("SELECT * FROM employees ORDER BY active DESC, name").fetchall()
        return [dict(row) for row in rows]

    def create_attendance_note(self, data):
        payload = _clean_note(data)
        if not self.get_employee_by_code(payload["employee_code"]):
            raise ValueError("employee tidak ditemukan")
        with closing(self.connect()) as conn:
            with conn:
                cursor = conn.execute(
                    """
                    INSERT INTO attendance_notes (employee_code, kind, status, start_date, end_date, note, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        payload["employee_code"],
                        payload["kind"],
                        payload["status"],
                        payload["start_date"],
                        payload["end_date"],
                        payload["note"],
                    ),
                )
                note_id = cursor.lastrowid
        return self.get_attendance_note(note_id)

    def update_attendance_note(self, note_id, data):
        current = self.get_attendance_note(note_id)
        if not current:
            return None
        payload = _clean_note({**current, **data})
        if not self.get_employee_by_code(payload["employee_code"]):
            raise ValueError("employee tidak ditemukan")
        with closing(self.connect()) as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE attendance_notes
                    SET employee_code = ?, kind = ?, status = ?, start_date = ?, end_date = ?, note = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        payload["employee_code"],
                        payload["kind"],
                        payload["status"],
                        payload["start_date"],
                        payload["end_date"],
                        payload["note"],
                        note_id,
                    ),
                )
        return self.get_attendance_note(note_id)

    def get_attendance_note(self, note_id):
        with closing(self.connect()) as conn:
            row = conn.execute(
                """
                SELECT n.*, COALESCE(e.name, n.employee_code) AS employee_name
                FROM attendance_notes n
                LEFT JOIN employees e ON e.employee_code = n.employee_code
                WHERE n.id = ?
                """,
                (note_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_attendance_notes(self, start=None, end=None, employee_code=None, kind=None, status=None):
        where = []
        params = []
        if start:
            where.append("n.end_date >= date(?)")
            params.append(start)
        if end:
            where.append("n.start_date <= date(?)")
            params.append(end)
        if employee_code:
            where.append("n.employee_code = ?")
            params.append(str(employee_code))
        if kind:
            where.append("n.kind = ?")
            params.append(kind)
        if status:
            where.append("n.status = ?")
            params.append(status)
        sql = """
            SELECT n.*, COALESCE(e.name, n.employee_code) AS employee_name
            FROM attendance_notes n
            LEFT JOIN employees e ON e.employee_code = n.employee_code
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY n.start_date DESC, employee_name"
        with closing(self.connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def upsert_attendance(self, records):
        inserted = 0
        with closing(self.connect()) as conn:
            with conn:
                for record in records:
                    before = conn.total_changes
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO attendance_logs (employee_code, uid, timestamp, status, punch, source)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(record["user_id"]),
                            record.get("uid"),
                            record["timestamp"],
                            int(record.get("status") or 0),
                            int(record.get("punch") or 0),
                            record.get("source", "device"),
                        ),
                    )
                    inserted += conn.total_changes - before
        return inserted

    def attendance_photo_names(self):
        with closing(self.connect()) as conn:
            rows = conn.execute("SELECT filename FROM attendance_photos").fetchall()
        return {row["filename"] for row in rows}

    def upsert_attendance_photos(self, photos):
        inserted = 0
        with closing(self.connect()) as conn:
            with conn:
                for photo in photos:
                    before = conn.total_changes
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO attendance_photos (filename, employee_code, timestamp)
                        VALUES (?, ?, ?)
                        """,
                        (photo["filename"], str(photo["employee_code"]), photo["timestamp"]),
                    )
                    inserted += conn.total_changes - before
        return inserted

    def list_attendance(self, start=None, end=None, employee_code=None):
        where = []
        params = []
        if start:
            where.append("a.timestamp >= ?")
            params.append(start)
        if end:
            where.append("a.timestamp <= ?")
            params.append(end)
        if employee_code:
            where.append("a.employee_code = ?")
            params.append(str(employee_code))
        sql = """
            SELECT
              a.*,
              COALESCE(e.name, a.employee_code) AS employee_name,
              CASE WHEN p.filename IS NULL THEN NULL ELSE '/api/attendance-photos/' || p.filename END AS photo_url
            FROM attendance_logs a
            LEFT JOIN employees e ON e.employee_code = a.employee_code
            LEFT JOIN attendance_photos p
              ON p.employee_code = a.employee_code AND p.timestamp = a.timestamp
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY a.timestamp DESC"
        with closing(self.connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def daily_report(self, start_date, end_date):
        with closing(self.connect()) as conn:
            attendance_rows = conn.execute(
                """
                SELECT
                  date(a.timestamp) AS work_date,
                  a.employee_code,
                  COALESCE(e.name, a.employee_code) AS employee_name,
                  MIN(a.timestamp) AS first_in,
                  MAX(a.timestamp) AS last_out,
                  COUNT(*) AS scans,
                  NULL AS note_kind,
                  NULL AS note_status,
                  '' AS note
                FROM attendance_logs a
                LEFT JOIN employees e ON e.employee_code = a.employee_code
                WHERE date(a.timestamp) BETWEEN date(?) AND date(?)
                GROUP BY work_date, a.employee_code
                """,
                (start_date, end_date),
            ).fetchall()
            note_rows = conn.execute(
                """
                SELECT
                  n.employee_code,
                  COALESCE(e.name, n.employee_code) AS employee_name,
                  n.kind,
                  n.status,
                  n.start_date,
                  n.end_date,
                  n.note
                FROM attendance_notes n
                LEFT JOIN employees e ON e.employee_code = n.employee_code
                WHERE n.status = 'approved'
                  AND date(n.end_date) >= date(?)
                  AND date(n.start_date) <= date(?)
                """,
                (start_date, end_date),
            ).fetchall()
            employee_rows = conn.execute("SELECT employee_code, name AS employee_name FROM employees WHERE active = 1").fetchall()
        rows = {}
        for row in attendance_rows:
            data = dict(row)
            data["day_status"] = "Hadir"
            rows[(data["work_date"], data["employee_code"])] = data
        for row in note_rows:
            note = dict(row)
            for work_date in _date_span(max(note["start_date"], start_date), min(note["end_date"], end_date)):
                key = (work_date, note["employee_code"])
                if key not in rows:
                    rows[key] = {
                        "work_date": work_date,
                        "employee_code": note["employee_code"],
                        "employee_name": note["employee_name"],
                        "first_in": None,
                        "last_out": None,
                        "scans": 0,
                        "note_kind": note["kind"],
                        "note_status": note["status"],
                        "note": note["note"],
                        "day_status": NOTE_KINDS[note["kind"]],
                    }
                else:
                    rows[key]["note_kind"] = note["kind"]
                    rows[key]["note_status"] = note["status"]
                    rows[key]["note"] = note["note"]
        for work_date in _date_span(start_date, end_date):
            for employee in employee_rows:
                employee = dict(employee)
                rows.setdefault(
                    (work_date, employee["employee_code"]),
                    {
                        "work_date": work_date,
                        "employee_code": employee["employee_code"],
                        "employee_name": employee["employee_name"],
                        "first_in": None,
                        "last_out": None,
                        "scans": 0,
                        "note_kind": None,
                        "note_status": None,
                        "note": "",
                        "day_status": "Tidak ada data",
                    },
                )
        return sorted(rows.values(), key=lambda row: (-date.fromisoformat(row["work_date"]).toordinal(), row["employee_name"]))

    def monthly_report(self, month):
        with closing(self.connect()) as conn:
            attendance_rows = conn.execute(
                """
                SELECT
                  substr(date(a.timestamp), 1, 7) AS month,
                  a.employee_code,
                  COALESCE(e.name, a.employee_code) AS employee_name,
                  COUNT(DISTINCT date(a.timestamp)) AS present_days,
                  COUNT(*) AS scans
                FROM attendance_logs a
                LEFT JOIN employees e ON e.employee_code = a.employee_code
                WHERE substr(date(a.timestamp), 1, 7) = ?
                GROUP BY month, a.employee_code
                ORDER BY employee_name
                """,
                (month,),
            ).fetchall()
            note_rows = conn.execute(
                """
                SELECT
                  n.employee_code,
                  COALESCE(e.name, n.employee_code) AS employee_name,
                  n.kind,
                  n.start_date,
                  n.end_date
                FROM attendance_notes n
                LEFT JOIN employees e ON e.employee_code = n.employee_code
                WHERE n.status = 'approved'
                  AND substr(date(n.end_date), 1, 7) >= ?
                  AND substr(date(n.start_date), 1, 7) <= ?
                """,
                (month, month),
            ).fetchall()
        rows = {}
        for row in attendance_rows:
            data = dict(row)
            data.update({"leave_days": 0, "permission_days": 0, "unit_task_days": 0})
            rows[data["employee_code"]] = data
        for row in note_rows:
            note = dict(row)
            data = rows.setdefault(
                note["employee_code"],
                {
                    "month": month,
                    "employee_code": note["employee_code"],
                    "employee_name": note["employee_name"],
                    "present_days": 0,
                    "scans": 0,
                    "leave_days": 0,
                    "permission_days": 0,
                    "unit_task_days": 0,
                },
            )
            days = sum(1 for value in _date_span(note["start_date"], note["end_date"]) if value.startswith(month))
            if note["kind"] == "cuti":
                data["leave_days"] += days
            elif note["kind"] == "izin":
                data["permission_days"] += days
            elif note["kind"] == "tugas_unit":
                data["unit_task_days"] += days
        return sorted(rows.values(), key=lambda row: row["employee_name"])

    def log_sync(self, kind, ok, message="", pulled=0, inserted=0):
        with closing(self.connect()) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO sync_runs (kind, ok, message, pulled, inserted) VALUES (?, ?, ?, ?, ?)",
                    (kind, 1 if ok else 0, message, pulled, inserted),
                )

    def last_sync(self):
        with closing(self.connect()) as conn:
            rows = conn.execute("SELECT * FROM sync_runs ORDER BY created_at DESC LIMIT 5").fetchall()
        return [dict(row) for row in rows]

    def log_audit(self, action, detail=""):
        with closing(self.connect()) as conn:
            with conn:
                conn.execute("INSERT INTO audit_logs (action, detail) VALUES (?, ?)", (action, detail))


def rows_to_csv(rows):
    output = StringIO()
    keys = list(rows[0].keys()) if rows else ["work_date", "employee_code", "employee_name", "first_in", "last_out", "scans"]
    writer = csv.DictWriter(output, fieldnames=keys)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def rows_to_excel_html(rows):
    keys = list(rows[0].keys()) if rows else ["work_date", "employee_code", "employee_name", "first_in", "last_out", "scans"]
    header = "".join(f"<th>{html.escape(key)}</th>" for key in keys)
    body = []
    for row in rows:
        cells = "".join(f'<td style="mso-number-format:\\@">{html.escape(_excel_text(row.get(key)))}</td>' for key in keys)
        body.append(f"<tr>{cells}</tr>")
    return (
        '<html><head><meta charset="utf-8"></head><body>'
        "<table>"
        f"<thead><tr>{header}</tr></thead>"
        f"<tbody>{''.join(body)}</tbody>"
        "</table>"
        "</body></html>"
    )


def _excel_text(value):
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


def today_range():
    today = datetime.now().date().isoformat()
    return today, today


def hash_password(password, salt=None):
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", str(password).encode(), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        algorithm, iterations, salt, expected = str(stored).split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", str(password).encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(digest.hex(), expected)
    except Exception:
        return False


def _public_user(user):
    if user:
        user.pop("password_hash", None)
    return user


def _clean_note(data):
    payload = {
        "employee_code": str(data["employee_code"]).strip(),
        "kind": str(data.get("kind") or "").strip(),
        "status": str(data.get("status") or "approved").strip(),
        "start_date": str(data.get("start_date") or "").strip(),
        "end_date": str(data.get("end_date") or "").strip(),
        "note": str(data.get("note") or "").strip(),
    }
    if not payload["employee_code"] or not payload["start_date"] or not payload["end_date"]:
        raise ValueError("employee_code, start_date, dan end_date wajib diisi")
    if payload["kind"] not in NOTE_KINDS:
        raise ValueError("jenis catatan tidak valid")
    if payload["status"] not in NOTE_STATUSES:
        raise ValueError("status catatan tidak valid")
    start = date.fromisoformat(payload["start_date"])
    end = date.fromisoformat(payload["end_date"])
    if start > end:
        raise ValueError("tanggal selesai harus setelah tanggal mulai")
    return payload


def _date_span(start, end):
    current = date.fromisoformat(start)
    last = date.fromisoformat(end)
    while current <= last:
        yield current.isoformat()
        current += timedelta(days=1)
