import tempfile
from datetime import datetime
from pathlib import Path
from unittest import TestCase

from attendance_app.db import Database, rows_to_excel_html
from attendance_app import device as device_module
from attendance_app.device import (
    AttendanceRecord,
    DeviceUser,
    ZKDevice,
    serialize_attendance,
    serialize_user,
)


class AttendanceAppTest(TestCase):
    def test_photo_name_parser_extracts_employee_and_timestamp(self):
        parser = getattr(device_module, "parse_attendance_photo_name", lambda _name: None)

        self.assertEqual(
            parser("20260821084531-0226060023"),
            {
                "filename": "20260821084531-0226060023.jpg",
                "employee_code": "0226060023",
                "timestamp": "2026-08-21T08:45:31",
            },
        )
        self.assertIsNone(parser("../../foto.jpg"))

    def test_device_downloads_only_unseen_photos_in_selected_dates(self):
        class FakeConnection:
            def __init__(self):
                self._ZK__data = b""
                self.pending = b""

            def _ZK__send_command(self, command, payload=b"", response_size=8):
                if command == 0x7E0 and payload == b"\x00\x00\x00\x00":
                    self.pending = (
                        b"20260820235959-1001\t"
                        b"20260821084531-1001\t"
                        b"20260821090000-1002\t\n\x00"
                    )
                    return {"status": True, "code": 1500}
                if command == 0x7DE and payload == b"20260821090000-1002.jpg\x00":
                    self.pending = b"\xff\xd8photo\xff\xd9"
                    return {"status": True, "code": 1500}
                raise AssertionError((command, payload, response_size))

            def _ZK__recieve_chunk(self):
                return self.pending

        device = ZKDevice()
        device._with_conn = lambda callback: callback(FakeConnection())
        self.assertTrue(hasattr(device, "attendance_photos"))

        result = device.attendance_photos(
            "2026-08-21",
            "2026-08-21",
            known_names={"20260821084531-1001.jpg"},
        )

        self.assertEqual(result["matched"], 2)
        self.assertEqual(
            result["photos"],
            [
                {
                    "filename": "20260821090000-1002.jpg",
                    "employee_code": "1002",
                    "timestamp": "2026-08-21T09:00:00",
                    "data": b"\xff\xd8photo\xff\xd9",
                }
            ],
        )

    def test_attendance_rows_include_matching_photo_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "attendance.sqlite3")
            db.init()
            db.upsert_attendance(
                [{"user_id": "1001", "uid": 7, "timestamp": "2026-08-21T08:45:31", "status": 1, "punch": 0}]
            )
            self.assertTrue(hasattr(db, "upsert_attendance_photos"))
            db.upsert_attendance_photos(
                [
                    {
                        "filename": "20260821084531-1001.jpg",
                        "employee_code": "1001",
                        "timestamp": "2026-08-21T08:45:31",
                    }
                ]
            )

            self.assertEqual(
                db.list_attendance()[0]["photo_url"],
                "/api/attendance-photos/20260821084531-1001.jpg",
            )

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

    def test_reports_include_approved_manual_attendance_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "attendance.sqlite3")
            db.init()
            db.upsert_employee({"employee_code": "1001", "name": "Ayu", "uid": 7})
            db.upsert_employee({"employee_code": "1002", "name": "Bima", "uid": 8})
            db.upsert_employee({"employee_code": "1003", "name": "Citra", "uid": 9})
            db.upsert_attendance(
                [
                    {"user_id": "1001", "uid": 7, "timestamp": "2026-07-22T08:01:00", "status": 1, "punch": 0},
                    {"user_id": "1001", "uid": 7, "timestamp": "2026-07-22T17:05:00", "status": 1, "punch": 1},
                ]
            )
            db.create_attendance_note(
                {
                    "employee_code": "1001",
                    "kind": "izin",
                    "status": "approved",
                    "start_date": "2026-07-22",
                    "end_date": "2026-07-22",
                    "note": "Urus dokumen",
                }
            )
            db.create_attendance_note(
                {
                    "employee_code": "1002",
                    "kind": "cuti",
                    "status": "approved",
                    "start_date": "2026-07-21",
                    "end_date": "2026-07-22",
                    "note": "Tahunan",
                }
            )
            db.create_attendance_note(
                {
                    "employee_code": "1002",
                    "kind": "tugas_unit",
                    "status": "cancelled",
                    "start_date": "2026-07-23",
                    "end_date": "2026-07-23",
                    "note": "Tidak jadi",
                }
            )

            rows = db.daily_report("2026-07-21", "2026-07-22")
            bima_rows = [row for row in rows if row["employee_code"] == "1002"]
            ayu_rows = [row for row in rows if row["employee_code"] == "1001"]
            citra_rows = [row for row in rows if row["employee_code"] == "1003"]
            monthly = db.monthly_report("2026-07")

            self.assertEqual(len(bima_rows), 2)
            self.assertTrue(all(row["day_status"] == "Cuti" for row in bima_rows))
            self.assertTrue(all(row["scans"] == 0 for row in bima_rows))
            self.assertEqual(ayu_rows[0]["day_status"], "Hadir")
            self.assertEqual(ayu_rows[0]["note_kind"], "izin")
            self.assertEqual(ayu_rows[0]["note"], "Urus dokumen")
            self.assertEqual(len(citra_rows), 2)
            self.assertTrue(all(row["day_status"] == "Tidak ada data" for row in citra_rows))

            bima_month = next(row for row in monthly if row["employee_code"] == "1002")
            ayu_month = next(row for row in monthly if row["employee_code"] == "1001")
            self.assertEqual(bima_month["leave_days"], 2)
            self.assertEqual(bima_month["unit_task_days"], 0)
            self.assertEqual(ayu_month["present_days"], 1)
            self.assertEqual(ayu_month["permission_days"], 1)

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

    def test_push_user_sets_group_and_personal_time_period(self):
        class FakeConnection:
            def set_user(self, **kwargs):
                return True

            def _ZK__send_command(self, command, payload, response_size):
                self.user_command = (command, payload, response_size)
                return {"status": True}

            def refresh_data(self):
                self.refreshed = True

        connection = FakeConnection()
        device = ZKDevice()
        device._with_conn = lambda callback: callback(connection)

        result = device.set_user(
            {
                "uid": 7,
                "employee_code": "1001",
                "name": "Ayu",
                "privilege": 0,
                "password": "",
                "card": 23,
            }
        )

        self.assertTrue(result)
        command, payload, response_size = connection.user_command
        self.assertEqual(command, 8)
        self.assertEqual(response_size, 1024)
        self.assertEqual(len(payload), 72)
        self.assertEqual(payload[:3], b"\x07\x00\x00")
        self.assertEqual(payload[35:39], b"\x17\x00\x00\x00")
        self.assertEqual(payload[39], 1)
        self.assertEqual(payload[40:48], b"\x01\x00\x01\x00\x00\x00\x00\x00")
        self.assertEqual(payload[48:72].rstrip(b"\x00"), b"1001")
        self.assertTrue(connection.refreshed)

    def test_sync_time_refreshes_device(self):
        class FakeConnection:
            def set_time(self, timestamp):
                self.timestamp = timestamp
                return True

            def refresh_data(self):
                self.refreshed = True

        connection = FakeConnection()
        device = ZKDevice()
        device._with_conn = lambda callback: callback(connection)
        timestamp = datetime(2026, 9, 1, 13, 15)

        result = device.set_time(timestamp)

        self.assertTrue(result)
        self.assertEqual(connection.timestamp, timestamp)
        self.assertTrue(connection.refreshed)
