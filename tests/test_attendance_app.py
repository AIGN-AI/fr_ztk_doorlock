import tempfile
from datetime import datetime
from pathlib import Path
from unittest import TestCase

from attendance_app.db import Database, rows_to_excel_html
from attendance_app.device import (
    AttendanceRecord,
    DeviceUser,
    ZKDevice,
    pack_user_payload,
    serialize_attendance,
    serialize_user,
)


class AttendanceAppTest(TestCase):
    def test_attendance_upsert_deduplicates_by_user_time_status_and_punch(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "attendance.sqlite3")
            db.init()
            db.upsert_attendance(
                [
                    {
                        "user_id": "1001",
                        "uid": 7,
                        "timestamp": "2026-07-21T08:01:00",
                        "status": 1,
                        "punch": 0,
                    },
                    {
                        "user_id": "1001",
                        "uid": 7,
                        "timestamp": "2026-07-21T08:01:00",
                        "status": 1,
                        "punch": 0,
                    },
                ]
            )

            logs = db.list_attendance()

            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0]["employee_code"], "1001")

    def test_report_groups_first_and_last_scan_per_employee(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "attendance.sqlite3")
            db.init()
            db.upsert_employee({"employee_code": "1001", "name": "Ayu", "uid": 7})
            db.upsert_attendance(
                [
                    {"user_id": "1001", "uid": 7, "timestamp": "2026-07-21T17:05:00", "status": 1, "punch": 1},
                    {"user_id": "1001", "uid": 7, "timestamp": "2026-07-21T08:01:00", "status": 1, "punch": 0},
                ]
            )

            rows = db.daily_report("2026-07-21", "2026-07-21")

            self.assertEqual(rows[0]["employee_name"], "Ayu")
            self.assertEqual(rows[0]["first_in"], "2026-07-21T08:01:00")
            self.assertEqual(rows[0]["last_out"], "2026-07-21T17:05:00")

    def test_excel_export_preserves_text_and_escapes_formula_like_values(self):
        html = rows_to_excel_html(
            [
                {
                    "employee_code": "0123",
                    "employee_name": "=SUM(1,1)",
                }
            ]
        )

        self.assertIn("0123", html)
        self.assertIn("&#x27;=SUM(1,1)", html)
        self.assertIn("mso-number-format", html)

    def test_device_objects_are_serialized_without_biometric_data(self):
        user = DeviceUser(uid=2, user_id="1002", name="Bima", privilege=0, card=0)
        attendance = AttendanceRecord(
            user_id="1002",
            uid=2,
            timestamp=datetime(2026, 7, 21, 9, 0),
            status=1,
            punch=0,
        )

        self.assertEqual(serialize_user(user)["employee_code"], "1002")
        self.assertEqual(serialize_attendance(attendance)["timestamp"], "2026-07-21T09:00:00")

    def test_push_user_uses_default_access_group(self):
        payload = pack_user_payload(
            packet_size=72,
            uid=1,
            name="Ayu",
            privilege=0,
            password="",
            group_id="1",
            user_id="1001",
            card=0,
            encoding="UTF-8",
        )

        self.assertEqual(payload[40:47].rstrip(b"\x00"), b"1")

    def test_user_payload_enables_first_time_period_for_zk6(self):
        payload = pack_user_payload(
            packet_size=28,
            uid=1,
            name="Ayu",
            privilege=0,
            password="",
            group_id="1",
            user_id="1001",
            card=0,
            encoding="UTF-8",
        )

        self.assertEqual(payload[22], 1)

    def test_user_payload_enables_first_time_period_for_zk8(self):
        payload = pack_user_payload(
            packet_size=72,
            uid=1,
            name="Ayu",
            privilege=0,
            password="",
            group_id="1",
            user_id="1001",
            card=0,
            encoding="UTF-8",
        )

        self.assertEqual(payload[39], 1)
