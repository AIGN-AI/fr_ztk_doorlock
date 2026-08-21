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
        return self._with_conn(lambda conn: conn.set_time(timestamp))

    def set_user(self, employee):
        def write(conn):
            return set_user_with_time_period(
                conn,
                uid=employee.get("uid"),
                name=employee["name"],
                privilege=int(employee.get("privilege") or 0),
                password=employee.get("password", ""),
                group_id="1",
                user_id=str(employee["employee_code"]),
                card=int(employee.get("card") or 0),
                time_period=1,
            )

        return self._with_conn(write)


def set_user_with_time_period(conn, uid=None, name="", privilege=0, password="", group_id="1", user_id="", card=0, time_period=1):
    from zk import const
    from zk.exception import ZKErrorResponse

    users = conn.get_users()
    if uid is None and user_id:
        matched = [user for user in users if str(user.user_id) == str(user_id)]
        uid = matched[0].uid if matched else None
    if uid is None:
        uid = conn.next_uid
    if not user_id:
        user_id = str(uid)
    if privilege not in [const.USER_DEFAULT, const.USER_ADMIN]:
        privilege = const.USER_DEFAULT

    payload = pack_user_payload(
        packet_size=int(conn.user_packet_size),
        uid=int(uid),
        name=name,
        privilege=int(privilege),
        password=password,
        group_id=group_id,
        user_id=str(user_id),
        card=int(card or 0),
        encoding=conn.encoding,
        time_period=int(time_period or 1),
    )
    response = conn._ZK__send_command(const.CMD_USER_WRQ, payload, 1024)
    if not response.get("status"):
        raise ZKErrorResponse("Can't set user")
    conn.refresh_data()
    return True


def pack_user_payload(packet_size, uid, name, privilege, password, group_id, user_id, card, encoding, time_period=1):
    if int(packet_size) == 28:
        return pack(
            "HB5s8sIxBHI",
            uid,
            privilege,
            str(password).encode(encoding, errors="ignore"),
            str(name).encode(encoding, errors="ignore"),
            int(card),
            int(group_id or 0),
            int(time_period or 1),
            int(user_id),
        )

    name_pad = str(name).encode(encoding, errors="ignore").ljust(24, b"\x00")[:24]
    group_pad = str(group_id or "1").encode(encoding, errors="ignore").ljust(7, b"\x00")[:7]
    user_id_pad = str(user_id).encode(encoding, errors="ignore").ljust(24, b"\x00")[:24]
    return pack(
        "<HB8s24sIB7sx24s",
        uid,
        privilege,
        str(password).encode(encoding, errors="ignore"),
        name_pad,
        int(card),
        int(time_period or 1),
        group_pad,
        user_id_pad,
    )


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
