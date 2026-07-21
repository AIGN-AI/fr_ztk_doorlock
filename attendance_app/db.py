import csv
import html
import sqlite3
from contextlib import closing
from datetime import datetime
from io import StringIO
from pathlib import Path


SCHEMA = """
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
            SELECT a.*, COALESCE(e.name, a.employee_code) AS employee_name
            FROM attendance_logs a
            LEFT JOIN employees e ON e.employee_code = a.employee_code
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY a.timestamp DESC"
        with closing(self.connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def daily_report(self, start_date, end_date):
        with closing(self.connect()) as conn:
            rows = conn.execute(
                """
                SELECT
                  date(a.timestamp) AS work_date,
                  a.employee_code,
                  COALESCE(e.name, a.employee_code) AS employee_name,
                  MIN(a.timestamp) AS first_in,
                  MAX(a.timestamp) AS last_out,
                  COUNT(*) AS scans
                FROM attendance_logs a
                LEFT JOIN employees e ON e.employee_code = a.employee_code
                WHERE date(a.timestamp) BETWEEN date(?) AND date(?)
                GROUP BY work_date, a.employee_code
                ORDER BY work_date DESC, employee_name
                """,
                (start_date, end_date),
            ).fetchall()
        return [dict(row) for row in rows]

    def monthly_report(self, month):
        with closing(self.connect()) as conn:
            rows = conn.execute(
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
        return [dict(row) for row in rows]

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
