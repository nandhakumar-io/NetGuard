import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";

interface JobTask {
  id: string | null;
  name: string | null;
  args: unknown;
  kwargs: unknown;
  eta: string | null;
  time_start: number | null;
}

interface WorkerJobs {
  active: JobTask[];
  reserved: JobTask[];
  scheduled: JobTask[];
}

interface BeatScheduleEntry {
  name: string;
  task: string;
  cadence: string;
}

interface JobsResponse {
  checked_at: string;
  workers_online: number;
  workers: Record<string, WorkerJobs>;
  beat_schedule: BeatScheduleEntry[];
  inspect_error: string | null;
}

function Card({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <div className={`bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-2xl shadow-sm ${className}`}>{children}</div>;
}

function TaskRow({ task }: { task: JobTask }) {
  return (
    <div className="flex items-center justify-between gap-3 text-sm py-2 border-b border-slate-100 dark:border-slate-700 last:border-0">
      <div className="min-w-0">
        <span className="font-medium text-slate-700 dark:text-slate-200 truncate block">{task.name || "unknown task"}</span>
        <span className="text-[11px] text-slate-400 truncate block">{task.id}</span>
      </div>
      {task.eta && <span className="text-[11px] text-slate-400 shrink-0">ETA {new Date(task.eta).toLocaleString()}</span>}
    </div>
  );
}

export default function Jobs() {
  const [data, setData] = useState<JobsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = () => {
    api
      .get<JobsResponse>("/jobs")
      .then((res) => {
        setData(res.data);
        setError(null);
      })
      .catch((err) => setError(err?.response?.data?.detail || "Failed to load job queue status."))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
    const interval = setInterval(load, 15000);
    return () => clearInterval(interval);
  }, []);

  const workerEntries = data ? Object.entries(data.workers) : [];
  const totalActive = workerEntries.reduce((n, [, w]) => n + w.active.length, 0);
  const totalReserved = workerEntries.reduce((n, [, w]) => n + w.reserved.length, 0);
  const totalScheduled = workerEntries.reduce((n, [, w]) => n + w.scheduled.length, 0);

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-bold text-navy dark:text-white">Background Jobs</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            Live Celery worker activity and the periodic sweep schedule. Per-device deployment retries live on the{" "}
            <Link to="/deployments" className="text-brandblue hover:underline">Deployments</Link> page.
          </p>
        </div>
        <button
          onClick={() => {
            setLoading(true);
            load();
          }}
          className="px-3 py-2 text-xs font-semibold text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200 border border-slate-200 dark:border-slate-700 rounded-lg"
        >
          Refresh
        </button>
      </div>

      {error && (
        <Card className="p-4 mb-6 border-red-200 bg-red-50 text-sm text-red-700">{error}</Card>
      )}

      {data?.inspect_error && (
        <Card className="p-4 mb-6 border-amber-200 bg-amber-50 text-sm text-amber-700">
          Couldn't reach any Celery workers: {data.inspect_error}. The beat schedule below is still shown, but live
          task state is unavailable.
        </Card>
      )}

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
        <Card className="p-4">
          <p className="text-2xl font-bold text-slate-800 dark:text-white">{loading ? "…" : data?.workers_online ?? 0}</p>
          <p className="text-[11px] font-semibold text-slate-400 uppercase tracking-wide mt-1">Workers Online</p>
        </Card>
        <Card className="p-4">
          <p className="text-2xl font-bold text-slate-800 dark:text-white">{loading ? "…" : totalActive}</p>
          <p className="text-[11px] font-semibold text-slate-400 uppercase tracking-wide mt-1">Active Tasks</p>
        </Card>
        <Card className="p-4">
          <p className="text-2xl font-bold text-slate-800 dark:text-white">{loading ? "…" : totalReserved}</p>
          <p className="text-[11px] font-semibold text-slate-400 uppercase tracking-wide mt-1">Reserved / Queued</p>
        </Card>
        <Card className="p-4">
          <p className="text-2xl font-bold text-slate-800 dark:text-white">{loading ? "…" : totalScheduled}</p>
          <p className="text-[11px] font-semibold text-slate-400 uppercase tracking-wide mt-1">Scheduled (ETA)</p>
        </Card>
      </div>

      <Card className="p-6 mb-6">
        <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-4">Workers</p>
        {workerEntries.length === 0 ? (
          <p className="text-sm text-slate-400 italic py-6 text-center">
            No Celery workers reporting in right now. Deployments and other async work won't run until one comes up.
          </p>
        ) : (
          <div className="space-y-5">
            {workerEntries.map(([name, w]) => (
              <div key={name}>
                <p className="text-sm font-semibold text-slate-700 dark:text-slate-200 mb-2">{name}</p>
                {w.active.length === 0 && w.reserved.length === 0 && w.scheduled.length === 0 ? (
                  <p className="text-xs text-slate-400 italic">Idle -- no active, reserved, or scheduled tasks.</p>
                ) : (
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                    <div>
                      <p className="text-[11px] text-slate-400 mb-1">Active ({w.active.length})</p>
                      {w.active.map((t) => <TaskRow key={t.id} task={t} />)}
                    </div>
                    <div>
                      <p className="text-[11px] text-slate-400 mb-1">Reserved ({w.reserved.length})</p>
                      {w.reserved.map((t) => <TaskRow key={t.id} task={t} />)}
                    </div>
                    <div>
                      <p className="text-[11px] text-slate-400 mb-1">Scheduled ({w.scheduled.length})</p>
                      {w.scheduled.map((t) => <TaskRow key={t.id} task={t} />)}
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card className="p-6">
        <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-4">Periodic Schedule</p>
        <div className="space-y-2">
          {(data?.beat_schedule ?? []).map((entry) => (
            <div key={entry.name} className="flex items-center justify-between gap-2 text-sm py-1.5 border-b border-slate-100 dark:border-slate-700 last:border-0">
              <div className="min-w-0">
                <span className="font-medium text-slate-700 dark:text-slate-200">{entry.name}</span>
                <span className="text-[11px] text-slate-400 ml-2 truncate">{entry.task}</span>
              </div>
              <span className="text-[11px] text-slate-400 shrink-0">{entry.cadence}</span>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}