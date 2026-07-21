import argparse
import json
import os
from datetime import date, datetime


BASIC_FIELDS = (
    ("device_name", "get_device_name"),
    ("serial_number", "get_serialnumber"),
    ("firmware_version", "get_firmware_version"),
    ("platform", "get_platform"),
    ("mac", "get_mac"),
    ("time", "get_time"),
    ("network", "get_network_params"),
)


def jsonable(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def collect_basic_info(conn):
    info = {}
    for key, method_name in BASIC_FIELDS:
        try:
            info[key] = jsonable(getattr(conn, method_name)())
        except Exception as exc:
            info[key] = {"error": str(exc)}
    return info


def connect_and_probe(args):
    try:
        from zk import ZK
    except ImportError as exc:
        raise SystemExit("pyzk belum terinstall. Jalankan: python3 -m pip install -r requirements.txt") from exc

    zk = ZK(
        args.host,
        port=args.port,
        timeout=args.timeout,
        password=args.password,
        force_udp=args.force_udp,
        ommit_ping=args.no_ping,
    )
    conn = None
    try:
        conn = zk.connect()
        return {
            "connected": True,
            "host": args.host,
            "port": args.port,
            "basic": collect_basic_info(conn),
        }
    finally:
        if conn:
            conn.disconnect()


def build_parser():
    parser = argparse.ArgumentParser(description="Read-only ZKTeco/pyzk connection probe.")
    parser.add_argument("--host", default=os.getenv("ZK_HOST"), help="IP mesin, bisa juga via env ZK_HOST")
    parser.add_argument("--port", type=int, default=int(os.getenv("ZK_PORT", "4370")))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("ZK_TIMEOUT", "5")))
    parser.add_argument("--password", type=int, default=int(os.getenv("ZK_PASSWORD", "0")))
    parser.add_argument("--force-udp", action="store_true")
    parser.add_argument("--no-ping", action="store_true", help="Lewati ping bawaan pyzk")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.host:
        parser.error("--host wajib diisi atau set env ZK_HOST")
    print(json.dumps(connect_and_probe(args), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
