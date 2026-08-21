import os
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import TestCase


class ApiTest(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ATTENDANCE_DB"] = str(Path(self.tmp.name) / "test.sqlite3")
        os.environ["ADMIN_PASSWORD"] = "secret"

        from attendance_app.api import create_app

        from fastapi.testclient import TestClient

        self.app = create_app()
        self.client = TestClient(self.app)

    def tearDown(self):
        self.tmp.cleanup()

    def test_env_file_loader_sets_missing_values_without_overriding_existing_env(self):
        from attendance_app.api import load_env

        env_file = Path(self.tmp.name) / ".env"
        env_file.write_text("ZK_HOST=1.2.3.4\nADMIN_PASSWORD=file-password\n")
        os.environ.pop("ZK_HOST", None)
        os.environ["ADMIN_PASSWORD"] = "secret"

        load_env(env_file)

        self.assertEqual(os.environ["ZK_HOST"], "1.2.3.4")
        self.assertEqual(os.environ["ADMIN_PASSWORD"], "secret")

    def test_auth_required_then_employee_create_and_csv_report(self):
        blocked = self.client.get("/api/employees")
        self.assertEqual(blocked.status_code, 401)

        login = self.client.post("/api/auth/login", json={"password": "secret"})
        self.assertEqual(login.status_code, 200)

        created = self.client.post("/api/employees", json={"employee_code": "1001", "name": "Ayu"})
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["employee_code"], "1001")

        csv_response = self.client.get("/api/reports/export.csv?start=2026-07-21&end=2026-07-21")
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn("text/csv", csv_response.headers["content-type"])

        xls_response = self.client.get("/api/reports/export.xls?start=2026-07-21&end=2026-07-21")
        self.assertEqual(xls_response.status_code, 200)
        self.assertIn("application/vnd.ms-excel", xls_response.headers["content-type"])
        self.assertIn("laporan-absensi-harian.xls", xls_response.headers["content-disposition"])

    def test_user_role_is_read_only_and_admin_can_manage_notes(self):
        from fastapi.testclient import TestClient

        login = self.client.post("/api/auth/login", json={"username": "admin", "password": "secret"})
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["user"]["role"], "admin")

        self.assertEqual(
            self.client.post("/api/employees", json={"employee_code": "1001", "name": "Ayu"}).status_code,
            200,
        )
        user = self.client.post(
            "/api/users",
            json={"username": "viewer", "name": "Viewer", "role": "user", "password": "viewerpass"},
        )
        self.assertEqual(user.status_code, 200)

        note = self.client.post(
            "/api/attendance-notes",
            json={
                "employee_code": "1001",
                "kind": "cuti",
                "status": "approved",
                "start_date": "2026-07-21",
                "end_date": "2026-07-21",
                "note": "Tahunan",
            },
        )
        self.assertEqual(note.status_code, 200)

        readonly = TestClient(self.app)
        self.assertEqual(readonly.post("/api/auth/login", json={"username": "viewer", "password": "viewerpass"}).status_code, 200)
        self.assertEqual(readonly.get("/api/employees").status_code, 200)
        self.assertEqual(readonly.get("/api/attendance-notes").status_code, 200)
        self.assertEqual(readonly.patch(f"/api/attendance-notes/{note.json()['id']}", json={"status": "cancelled"}).status_code, 403)
        self.assertEqual(readonly.post("/api/employees", json={"employee_code": "1002", "name": "Bima"}).status_code, 403)

    def test_start_enrollment_endpoint_is_not_exposed(self):
        paths = {route.path for route in self.app.routes}

        self.assertNotIn("/api/employees/{employee_id}/start-enrollment", paths)

    def test_auto_sync_returns_inserted_count_and_today_rows(self):
        class FakeDevice:
            host = "device"
            port = 4370

            def attendance(self):
                return [
                    {
                        "user_id": "1001",
                        "uid": 1,
                        "timestamp": datetime.now().replace(hour=8, minute=0, second=0, microsecond=0).isoformat(),
                        "status": 15,
                        "punch": 255,
                        "source": "device",
                    }
                ]

        from attendance_app.api import create_app
        from fastapi.testclient import TestClient

        client = TestClient(create_app(device=FakeDevice()))
        client.post("/api/auth/login", json={"password": "secret"})

        response = client.post("/api/device/auto-sync-attendance")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["inserted"], 1)
        self.assertEqual(len(response.json()["today"]), 1)

    def test_photo_sync_stores_and_serves_matching_attendance_photo(self):
        import inspect

        from attendance_app.api import create_app
        from attendance_app.db import Database
        from fastapi.testclient import TestClient

        class FakeDevice:
            host = "device"
            port = 4370

            def attendance_photos(self, start, end, known_names):
                self.range = (start, end)
                return {
                    "matched": 1,
                    "photos": [
                        {
                            "filename": "20260821084531-1001.jpg",
                            "employee_code": "1001",
                            "timestamp": "2026-08-21T08:45:31",
                            "data": b"\xff\xd8photo\xff\xd9",
                        }
                    ],
                }

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            db.init()
            db.upsert_attendance(
                [{"user_id": "1001", "uid": 1, "timestamp": "2026-08-21T08:45:31", "status": 1, "punch": 0}]
            )
            device = FakeDevice()
            self.assertIn("photo_dir", inspect.signature(create_app).parameters)
            client = TestClient(create_app(db=db, device=device, photo_dir=Path(tmp) / "photos"))
            client.post("/api/auth/login", json={"password": "secret"})

            synced = client.post("/api/device/sync-attendance-photos?start=2026-08-21&end=2026-08-21")

            self.assertEqual(synced.status_code, 200)
            self.assertEqual(synced.json(), {"ok": True, "matched": 1, "downloaded": 1, "skipped": 0})
            self.assertEqual(device.range, ("2026-08-21", "2026-08-21"))
            photo_url = client.get("/api/attendance?start=2026-08-21&end=2026-08-21").json()[0]["photo_url"]
            self.assertEqual(photo_url, "/api/attendance-photos/20260821084531-1001.jpg")
            photo = client.get(photo_url)
            self.assertEqual(photo.status_code, 200)
            self.assertEqual(photo.content, b"\xff\xd8photo\xff\xd9")
            self.assertEqual(photo.headers["content-type"], "image/jpeg")
