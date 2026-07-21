from datetime import datetime
from unittest import TestCase

from zk_probe import collect_basic_info


class FakeConn:
    def get_device_name(self):
        return "Face-01"

    def get_serialnumber(self):
        return "SN123"

    def get_firmware_version(self):
        return "FW1"

    def get_platform(self):
        raise RuntimeError("unsupported")

    def get_mac(self):
        return "00:11:22:33:44:55"

    def get_time(self):
        return datetime(2026, 7, 21, 9, 30, 0)

    def get_network_params(self):
        return {"ip": "10.10.10.50"}


class CollectBasicInfoTest(TestCase):
    def test_keeps_supported_values_and_errors(self):
        info = collect_basic_info(FakeConn())

        self.assertEqual(info["device_name"], "Face-01")
        self.assertEqual(info["serial_number"], "SN123")
        self.assertEqual(info["firmware_version"], "FW1")
        self.assertEqual(info["platform"]["error"], "unsupported")
        self.assertEqual(info["time"], "2026-07-21T09:30:00")
