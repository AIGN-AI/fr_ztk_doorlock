# MiniAC Plus Attendance

Web lokal untuk admin absensi ZKTeco/MiniAC Plus via `pyzk`.

## Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm install
cp .env.example .env
```

Edit `.env` kalau IP/port/password device berubah:

```env
ZK_HOST=10.10.9.60
ZK_PORT=4370
ZK_PASSWORD=0
ZK_TIMEOUT=8
ZK_FORCE_UDP=false
ADMIN_PASSWORD=admin
ATTENDANCE_DB=data/attendance.sqlite3
```

## Run

Terminal 1, API:

```bash
.venv/bin/python run_api.py
```

Terminal 2, GUI:

```bash
npm run dev
```

Buka `http://127.0.0.1:5173`.

## Notes

- Absensi dilakukan di mesin; app menarik log dan membuat report.
- Tombol `Tarik foto` mengambil foto sesuai rentang tanggal dan menyimpannya di `data/attendance_photos`.
- Enrollment biometrik dilakukan manual dari layar mesin.
- App tidak menyimpan template wajah/fingerprint; foto absensi hanya dapat dibuka setelah login.
- Jangan pakai endpoint destructive pyzk seperti `clear_attendance` atau `clear_data`.
