from dataclasses import dataclass
from datetime import date, datetime
import re
from struct import pack


@dataclass
class DeviceUser:
    uid: int
    user_id: str
    name: str
    privilege: int = 0
    card: int = 0


@dataclass
class AttendanceRecord:
    user_id: str
    uid: int | None
    timestamp: datetime
    status: int = 0
    punch: int = 0


def _iso(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def serialize_user(user):
    return {
        "uid": getattr(user, "uid", None),
        "employee_code": str(getattr(user, "user_id", "")),
        "name": getattr(user, "name", "") or "",
        "privilege": int(getattr(user, "privilege", 0) or 0),
        "card": int(getattr(user, "card", 0) or 0),
    }


def serialize_attendance(record):
    return {
        "user_id": str(getattr(record, "user_id", "")),
        "uid": getattr(record, "uid", None),
        "timestamp": _iso(getattr(record, "timestamp")),
        "status": int(getattr(record, "status", 0) or 0),
        "punch": int(getattr(record, "punch", 0) or 0),
        "source": "device",
    }


def parse_attendance_photo_name(name):
    stem = str(name).strip().removesuffix(".jpg").removesuffix(".JPG")
    match = re.fullmatch(r"(\d{14})-([A-Za-z0-9_-]+)", stem)
    if not match:
        return None
    try:
        timestamp = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return {
        "filename": f"{stem}.jpg",
        "employee_code": match.group(2),
        "timestamp": timestamp.isoformat(),
    }


class ZKDevice:
    def __init__(self, host="10.10.9.60", port=4370, timeout=8, password=0, force_udp=False):
        self.host = host
        self.port = int(port)
        self.timeout = int(timeout)
        self.password = int(password)
        self.force_udp = force_udp

    def _zk(self):
        try:
            from zk import ZK
        except ImportError as exc:
            raise RuntimeError("pyzk belum terinstall") from exc
        return ZK(
            self.host,
            port=self.port,
            timeout=self.timeout,
            password=self.password,
            force_udp=self.force_udp,
            ommit_ping=False,
        )

    def _with_conn(self, callback):
        conn = None
        try:
            conn = self._zk().connect()
            return callback(conn)
        finally:
            if conn:
                conn.disconnect()

    def status(self):
        def read(conn):
            return {
                "connected": True,
                "host": self.host,
                "port": self.port,
                "device_name": _safe(conn, "get_device_name"),
                "serial_number": _safe(conn, "get_serialnumber"),
                "firmware_version": _safe(conn, "get_firmware_version"),
                "platform": _safe(conn, "get_platform"),
                "mac": _safe(conn, "get_mac"),
                "time": _iso(_safe(conn, "get_time")),
                "network": _safe(conn, "get_network_params"),
            }

        return self._with_conn(read)

    def users(self):
        return self._with_conn(lambda conn: [serialize_user(user) for user in conn.get_users()])

    def attendance(self):
        return self._with_conn(lambda conn: [serialize_attendance(record) for record in conn.get_attendance()])

    def attendance_photos(self, start, end, known_names=()):
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
        known_names = set(known_names)

        def read(conn):
            encoding = getattr(conn, "encoding", "UTF-8")
            names = _read_photo_data(conn, 0x7E0, pack("<i", 0)).rstrip(b"\x00").decode(encoding, errors="ignore")
            photos = []
            matched = 0
            for name in names.replace("\n", "\t").split("\t"):
                photo = parse_attendance_photo_name(name)
                if not photo or not start_date <= datetime.fromisoformat(photo["timestamp"]).date() <= end_date:
                    continue
                matched += 1
                if photo["filename"] in known_names:
                    continue
                data = _read_photo_data(conn, 0x7DE, photo["filename"].encode(encoding) + b"\x00")
                if not data.startswith(b"\xff\xd8"):
                    raise RuntimeError(f"data foto tidak valid: {photo['filename']}")
                photos.append({**photo, "data": data})
            return {"matched": matched, "photos": photos}

        return self._with_conn(read)

    def set_time(self, timestamp=None):
        timestamp = timestamp or datetime.now()

        def write(conn):
            result = conn.set_time(timestamp)
            conn.refresh_data()
            return result

        return self._with_conn(write)

    def set_user(self, employee):
        def write(conn):
            uid = employee.get("uid")
            if uid is None:
                matched = [user for user in conn.get_users() if str(user.user_id) == str(employee["employee_code"])]
                uid = matched[0].uid if matched else conn.next_uid
            from zk import const
            from zk.exception import ZKErrorResponse

            encoding = getattr(conn, "encoding", "UTF-8")
            payload = pack(
                "<HB8s24sIB4H24s",
                int(uid),
                int(employee.get("privilege") or 0),
                employee.get("password", "").encode(encoding),
                employee["name"].encode(encoding),
                int(employee.get("card") or 0),
                1,
                1,
                1,
                0,
                0,
                str(employee["employee_code"]).encode(encoding),
            )
            response = conn._ZK__send_command(
                const.CMD_USER_WRQ,
                payload,
                1024,
            )
            if not response.get("status"):
                raise ZKErrorResponse("Can't set user")
            conn.refresh_data()
            return True

        return self._with_conn(write)


def _safe(conn, method_name):
    try:
        return getattr(conn, method_name)()
    except Exception as exc:
        return {"error": str(exc)}


def _read_photo_data(conn, command, payload):
    from zk import const
    from zk.exception import ZKErrorResponse

    response = conn._ZK__send_command(command, payload, 1024)
    if not response.get("status"):
        raise ZKErrorResponse(f"device menolak perintah foto {command}")
    if response["code"] in {const.CMD_PREPARE_DATA, const.CMD_DATA}:
        return conn._ZK__recieve_chunk()
    return conn._ZK__data
