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
