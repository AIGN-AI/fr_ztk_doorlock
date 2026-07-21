import { FormEvent, useEffect, useMemo, useState } from "react";

type DeviceStatus = {
  connected: boolean;
  host: string;
  port: number;
  device_name?: string;
  serial_number?: string;
  firmware_version?: string;
  time?: string;
  clock_warning?: string | null;
  error?: string;
};

type Employee = {
  id: number;
  employee_code: string;
  name: string;
  uid?: number | null;
  active: number;
};

type AttendanceLog = {
  id: number;
  employee_code: string;
  employee_name: string;
  timestamp: string;
  status: number;
  punch: number;
};

type DailyReport = {
  work_date: string;
  employee_code: string;
  employee_name: string;
  first_in: string;
  last_out: string;
  scans: number;
};

type MonthlyReport = {
  month: string;
  employee_code: string;
  employee_name: string;
  present_days: number;
  scans: number;
};

type Summary = {
  employees: number;
  today: DailyReport[];
  last_sync: Array<{ kind: string; ok: number; message: string; pulled: number; inserted: number; created_at: string }>;
};

type AutoSyncResult = {
  pulled: number;
  inserted: number;
  today: DailyReport[];
  last_sync: Summary["last_sync"];
};

const tabs = [
  { key: "Dashboard", label: "Dashboard" },
  { key: "Employees", label: "Karyawan" },
  { key: "Attendance", label: "Absensi" },
  { key: "Reports", label: "Laporan" }
] as const;
type Tab = (typeof tabs)[number]["key"];

async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...options,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {})
    }
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || response.statusText);
  }
  return response.json();
}

export default function App() {
  const [loggedIn, setLoggedIn] = useState(false);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [tab, setTab] = useState<Tab>("Dashboard");

  async function login(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      await api("/api/auth/login", { method: "POST", body: JSON.stringify({ password }) });
      setLoggedIn(true);
    } catch (err) {
      setError(String((err as Error).message));
    }
  }

  if (!loggedIn) {
    return (
      <main className="login-shell">
        <form className="login-panel" onSubmit={login}>
          <div>
            <p className="eyebrow">MiniAC Plus</p>
            <h1>Absensi Lokal</h1>
            <p className="muted">Masuk sebagai admin untuk sinkron user, log absensi, dan report.</p>
          </div>
          <label>
            Password admin
            <input
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              type="password"
              autoComplete="current-password"
              autoFocus
            />
          </label>
          {error && <p className="error">{error}</p>}
          <button className="primary" type="submit">Login</button>
        </form>
      </main>
    );
  }

  return (
    <div className="app-shell">
      <aside>
        <div className="brand">
          <span>MiniAC</span>
          <strong>Attendance</strong>
        </div>
        <nav aria-label="Navigasi utama">
          {tabs.map((item) => (
            <button key={item.key} className={tab === item.key ? "active" : ""} onClick={() => setTab(item.key)}>
              {item.label}
            </button>
          ))}
        </nav>
      </aside>
      <main className="workspace">
        {tab === "Dashboard" && <Dashboard />}
        {tab === "Employees" && <Employees />}
        {tab === "Attendance" && <Attendance />}
        {tab === "Reports" && <Reports />}
      </main>
    </div>
  );
}

function Dashboard() {
  const [status, setStatus] = useState<DeviceStatus | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [recentLogs, setRecentLogs] = useState<AttendanceLog[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  async function load() {
    setMessage("");
    const today = new Date().toISOString().slice(0, 10);
    const [device, data, logs] = await Promise.all([
      api<DeviceStatus>("/api/device/status"),
      api<Summary>("/api/summary"),
      api<AttendanceLog[]>(`/api/attendance?start=${today}&end=${today}`)
    ]);
    setStatus(device);
    setSummary(data);
    setRecentLogs(logs.slice(0, 5));
  }

  useEffect(() => {
    load().catch((err) => setMessage(String((err as Error).message)));
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => {
      const today = new Date().toISOString().slice(0, 10);
      Promise.all([
        api<Summary>("/api/summary"),
        api<AttendanceLog[]>(`/api/attendance?start=${today}&end=${today}`)
      ])
        .then(([data, logs]) => {
          setSummary(data);
          setRecentLogs(logs.slice(0, 5));
        })
        .catch(() => undefined);
    }, 10000);
    return () => window.clearInterval(timer);
  }, []);

  async function syncTime() {
    setBusy(true);
    setMessage("");
    try {
      await api("/api/device/sync-time", { method: "POST" });
      await load();
      setMessage("Jam device sudah disinkronkan.");
    } catch (err) {
      setMessage(String((err as Error).message));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <Header title="Dashboard" action={<button onClick={load}>Refresh</button>} />
      {message && <p className="notice">{message}</p>}
      <div className="metrics">
        <Metric
          label="Device"
          value={<StatusChip tone={status?.connected ? "ok" : "bad"}>{status?.connected ? "Online" : "Offline"}</StatusChip>}
          detail={status?.device_name || status?.error || "-"}
        />
        <Metric
          label="Live sync"
          value={<StatusChip tone="live">Aktif</StatusChip>}
          detail={summary?.last_sync[0]?.created_at ? `terakhir ${formatUtcDateTime(summary.last_sync[0].created_at)}` : "menunggu sync"}
        />
        <Metric label="Karyawan" value={String(summary?.employees ?? "-")} detail="tersimpan lokal" />
        <Metric label="Hadir hari ini" value={String(summary?.today.length ?? "-")} detail="berdasarkan log device" />
      </div>
      {status?.clock_warning && (
        <div className="warning-row">
          <div>
            <strong>{status.clock_warning}</strong>
            <span>Device time: {status.time || "-"}</span>
          </div>
          <button className="primary" disabled={busy} onClick={syncTime}>Sync time</button>
        </div>
      )}
      <DataTable
        title="Scan terbaru"
        columns={["Waktu", "Kode", "Nama", "Metode", "Tipe"]}
        rows={recentLogs.map((log) => [
          formatDateTime(log.timestamp),
          log.employee_code,
          log.employee_name,
          <DeviceValue label={statusLabel(log.status)} raw={log.status} />,
          <DeviceValue label={punchLabel(log.punch)} raw={log.punch} />
        ])}
        emptyText="Belum ada scan hari ini."
      />
      <DataTable
        title="Absensi hari ini"
        columns={["Tanggal", "Kode", "Nama", "Masuk", "Keluar", "Scan"]}
        rows={(summary?.today || []).map((row) => [formatDate(row.work_date), row.employee_code, row.employee_name, timeOnly(row.first_in), timeOnly(row.last_out), row.scans])}
      />
      <DataTable
        title="Sync terakhir"
        columns={["Tipe", "Status", "Pulled", "Inserted", "Waktu"]}
        rows={(summary?.last_sync || []).map((row) => [syncKindLabel(row.kind), <StatusChip tone={row.ok ? "ok" : "bad"}>{row.ok ? "OK" : "Gagal"}</StatusChip>, row.pulled, row.inserted, formatUtcDateTime(row.created_at)])}
      />
    </section>
  );
}

function Employees() {
  const [employees, setEmployees] = useState<Employee[]>([]);
  const [form, setForm] = useState({ employee_code: "", name: "" });
  const [search, setSearch] = useState("");
  const [message, setMessage] = useState("");
  const filteredEmployees = useMemo(() => {
    const keyword = search.trim().toLowerCase();
    if (!keyword) return employees;
    return employees.filter((employee) =>
      `${employee.employee_code} ${employee.name}`.toLowerCase().includes(keyword)
    );
  }, [employees, search]);

  async function load() {
    setEmployees(await api<Employee[]>("/api/employees"));
  }

  useEffect(() => {
    load().catch((err) => setMessage(String((err as Error).message)));
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setMessage("");
    try {
      await api<Employee>("/api/employees", { method: "POST", body: JSON.stringify(form) });
      setForm({ employee_code: "", name: "" });
      await load();
      setMessage("Karyawan tersimpan.");
    } catch (err) {
      setMessage(String((err as Error).message));
    }
  }

  async function action(path: string, success: string) {
    setMessage("");
    try {
      await api(path, { method: "POST" });
      setMessage(success);
    } catch (err) {
      setMessage(String((err as Error).message));
    }
  }

  return (
    <section>
      <Header title="Karyawan" action={<button onClick={() => action("/api/device/sync-users", "User dari device sudah ditarik.").then(load)}>Tarik user dari device</button>} />
      {message && <p className="notice">{message}</p>}
      <form className="inline-form" onSubmit={submit}>
        <label>
          Kode
          <input value={form.employee_code} onChange={(event) => setForm({ ...form, employee_code: event.target.value })} required />
        </label>
        <label>
          Nama
          <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} required />
        </label>
        <button className="primary" type="submit">Simpan</button>
      </form>
      <div className="toolbar">
        <label>
          Cari karyawan
          <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Nama atau kode" />
        </label>
        <span className="muted">{filteredEmployees.length} dari {employees.length} karyawan</span>
      </div>
      <div className="table-block">
        <h2>Daftar karyawan</h2>
        <table>
          <thead>
            <tr><th scope="col">Kode</th><th scope="col">Nama</th><th scope="col">UID device</th><th scope="col">Device</th></tr>
          </thead>
          <tbody>
            {filteredEmployees.length === 0 ? (
              <tr><td colSpan={4}>Tidak ada karyawan yang cocok.</td></tr>
            ) : filteredEmployees.map((employee) => (
              <tr key={employee.id}>
                <td>{employee.employee_code}</td>
                <td>{employee.name}</td>
                <td>{employee.uid ? <StatusChip tone="ok">UID {employee.uid}</StatusChip> : <StatusChip tone="neutral">Belum tersinkron</StatusChip>}</td>
                <td><button onClick={() => action(`/api/employees/${employee.id}/push-to-device`, "User dikirim ke device.")}>Kirim ke device</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Attendance() {
  const today = new Date().toISOString().slice(0, 10);
  const [start, setStart] = useState(today);
  const [end, setEnd] = useState(today);
  const [logs, setLogs] = useState<AttendanceLog[]>([]);
  const [message, setMessage] = useState("");
  const [autoSyncAt, setAutoSyncAt] = useState("");

  async function load() {
    setLogs(await api<AttendanceLog[]>(`/api/attendance?start=${start}&end=${end}`));
  }

  useEffect(() => {
    load().catch((err) => setMessage(String((err as Error).message)));
  }, []);

  useEffect(() => {
    let stopped = false;
    async function autoSync() {
      try {
        const result = await api<AutoSyncResult>("/api/device/auto-sync-attendance", { method: "POST" });
        if (stopped) return;
        setAutoSyncAt(new Date().toLocaleTimeString("id-ID", { hour: "2-digit", minute: "2-digit", second: "2-digit" }));
        if (result.inserted > 0) {
          setMessage(`Auto-sync: ${result.inserted} data baru masuk.`);
        }
        await load();
      } catch {
        if (!stopped) setAutoSyncAt("gagal");
      }
    }
    const timer = window.setInterval(autoSync, 5000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [start, end]);

  async function sync() {
    setMessage("");
    try {
      const result = await api<{ pulled: number; inserted: number }>("/api/device/sync-attendance", { method: "POST" });
      await load();
      setMessage(`Pulled ${result.pulled}, inserted ${result.inserted}.`);
    } catch (err) {
      setMessage(String((err as Error).message));
    }
  }

  return (
    <section>
      <Header
        title="Absensi"
        action={
          <div className="header-actions">
            <StatusChip tone={autoSyncAt === "gagal" ? "bad" : "live"}>Live sync setiap 5 detik{autoSyncAt && autoSyncAt !== "gagal" ? `, terakhir ${autoSyncAt}` : ""}</StatusChip>
            <button className="primary" onClick={sync}>Tarik data</button>
          </div>
        }
      />
      {message && <p className="notice">{message}</p>}
      <Filters start={start} end={end} setStart={setStart} setEnd={setEnd} onApply={load} />
      <DataTable
        title="Log absensi"
        columns={["Waktu", "Kode", "Nama", "Metode", "Tipe"]}
        rows={logs.map((log) => [
          formatDateTime(log.timestamp),
          log.employee_code,
          log.employee_name,
          <DeviceValue label={statusLabel(log.status)} raw={log.status} />,
          <DeviceValue label={punchLabel(log.punch)} raw={log.punch} />
        ])}
        emptyText="Belum ada absensi untuk tanggal ini."
        emptyAction={<button className="primary" onClick={sync}>Tarik data sekarang</button>}
      />
    </section>
  );
}

function Reports() {
  const today = new Date().toISOString().slice(0, 10);
  const monthNow = today.slice(0, 7);
  const [start, setStart] = useState(today);
  const [end, setEnd] = useState(today);
  const [month, setMonth] = useState(monthNow);
  const [daily, setDaily] = useState<DailyReport[]>([]);
  const [monthly, setMonthly] = useState<MonthlyReport[]>([]);
  const exportCsvUrl = useMemo(() => `/api/reports/export.csv?start=${start}&end=${end}`, [start, end]);
  const exportExcelUrl = useMemo(() => `/api/reports/export.xls?start=${start}&end=${end}`, [start, end]);

  async function load() {
    const [dailyRows, monthlyRows] = await Promise.all([
      api<DailyReport[]>(`/api/reports/daily?start=${start}&end=${end}`),
      api<MonthlyReport[]>(`/api/reports/monthly?month=${month}`)
    ]);
    setDaily(dailyRows);
    setMonthly(monthlyRows);
  }

  useEffect(() => {
    load().catch(() => undefined);
  }, []);

  return (
    <section>
      <Header
        title="Laporan"
        action={
          <div className="header-actions">
            <a className="button" href={exportCsvUrl}>Export CSV</a>
            <a className="button primary" href={exportExcelUrl}>Export Excel</a>
          </div>
        }
      />
      <div className="filters report-filters">
        <label>
          Dari
          <input type="date" value={start} onChange={(event) => setStart(event.target.value)} />
        </label>
        <label>
          Sampai
          <input type="date" value={end} onChange={(event) => setEnd(event.target.value)} />
        </label>
        <label>
          Bulan
          <input type="month" value={month} onChange={(event) => setMonth(event.target.value)} />
        </label>
        <button onClick={load}>Terapkan filter</button>
      </div>
      <DataTable
        title="Ringkasan harian"
        columns={["Tanggal", "Kode", "Nama", "Masuk", "Keluar", "Scan"]}
        rows={daily.map((row) => [formatDate(row.work_date), row.employee_code, row.employee_name, timeOnly(row.first_in), timeOnly(row.last_out), row.scans])}
      />
      <DataTable
        title="Ringkasan bulanan"
        columns={["Bulan", "Kode", "Nama", "Hari hadir", "Scan"]}
        rows={monthly.map((row) => [row.month, row.employee_code, row.employee_name, row.present_days, row.scans])}
      />
    </section>
  );
}

function Header({ title, action }: { title: string; action?: React.ReactNode }) {
  return (
    <header className="page-header">
      <h1>{title}</h1>
      {action}
    </header>
  );
}

function Metric({ label, value, detail }: { label: string; value: React.ReactNode; detail: string }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

function Filters({ start, end, setStart, setEnd, onApply }: {
  start: string;
  end: string;
  setStart: (value: string) => void;
  setEnd: (value: string) => void;
  onApply: () => void;
}) {
  return (
    <div className="filters">
      <label>
        Dari
        <input type="date" value={start} onChange={(event) => setStart(event.target.value)} />
      </label>
      <label>
        Sampai
        <input type="date" value={end} onChange={(event) => setEnd(event.target.value)} />
      </label>
      <button onClick={onApply}>Terapkan filter</button>
    </div>
  );
}

function DataTable({
  title,
  columns,
  rows,
  emptyText = "Tidak ada data.",
  emptyAction
}: {
  title: string;
  columns: string[];
  rows: Array<Array<React.ReactNode>>;
  emptyText?: string;
  emptyAction?: React.ReactNode;
}) {
  return (
    <div className="table-block">
      <h2>{title}</h2>
      <table>
        <thead>
          <tr>{columns.map((column) => <th scope="col" key={column}>{column}</th>)}</tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={columns.length}>
                <div className="empty-state">
                  <span>{emptyText}</span>
                  {emptyAction}
                </div>
              </td>
            </tr>
          ) : rows.map((row, index) => (
            <tr key={index}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell ?? "-"}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function StatusChip({ tone = "neutral", children }: { tone?: "ok" | "bad" | "live" | "neutral"; children: React.ReactNode }) {
  return <span className={`status-chip ${tone}`}>{children}</span>;
}

function DeviceValue({ label, raw }: { label: string; raw: number }) {
  return (
    <span className="device-value">
      <span>{label}</span>
      <small>raw {raw}</small>
    </span>
  );
}

function timeOnly(value: string) {
  return value ? new Date(value).toLocaleTimeString("id-ID", { hour: "2-digit", minute: "2-digit" }) : "-";
}

function formatDate(value: string) {
  return value ? new Date(`${value}T00:00:00`).toLocaleDateString("id-ID", { day: "2-digit", month: "short", year: "numeric" }) : "-";
}

function formatDateTime(value: string) {
  return value ? new Date(value.replace(" ", "T")).toLocaleString("id-ID", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "-";
}

function formatUtcDateTime(value: string) {
  return value ? new Date(`${value.replace(" ", "T")}Z`).toLocaleString("id-ID", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "-";
}

function statusLabel(value: number) {
  const labels: Record<number, string> = {
    1: "Sidik jari",
    4: "Kartu",
    15: "Wajah"
  };
  return labels[value] || "Metode device";
}

function punchLabel(value: number) {
  const labels: Record<number, string> = {
    0: "Masuk",
    1: "Keluar",
    2: "Istirahat mulai",
    3: "Istirahat selesai",
    4: "Lembur mulai",
    5: "Lembur selesai",
    255: "Scan otomatis"
  };
  return labels[value] || "Tipe device";
}

function syncKindLabel(value: string) {
  const labels: Record<string, string> = {
    attendance: "Absensi",
    users: "User",
    time: "Jam device"
  };
  return labels[value] || value;
}
