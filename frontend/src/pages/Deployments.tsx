import { useEffect, useState } from "react";
import { api } from "../lib/api";

interface HealthCheck {
  category: string;
  check_name: string;
  passed: boolean;
  detail: string | null;
  checked_at: string;
}

interface DeploymentRecord {
  id: string;
  change_request_id: string;
  device_id: string;
  snapshot_id: string | null;
  protocol: string;
  status: "queued" | "in_progress" | "succeeded" | "failed" | "rolled_back";
  error_message: string | null;
  created_at: string;
  health_checks: HealthCheck[];
}

const STATUS_STYLES: Record<DeploymentRecord["status"], string> = {
  queued: "bg-slate-100 text-slate-600",
  in_progress: "bg-blue-100 text-blue-700",
  succeeded: "bg-green-100 text-green-700",
  failed: "bg-red-100 text-red-700",
  rolled_back: "bg-amber-100 text-amber-700",
};

export default function Deployments() {
  const [deployments, setDeployments] = useState<DeploymentRecord[]>([]);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = () => {
    api.get<DeploymentRecord[]>("/deployments").then((res) => {
      setDeployments(res.data);
      setLoading(false);
    });
  };

  useEffect(() => {
    load();
    // Deployments run asynchronously (Celery, one task per target device --
    // see backend app/tasks.py), so this page polls rather than expecting a
    // single request/response to carry the final result.
    const interval = setInterval(load, 5000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div>
      <h1 className="text-2xl font-bold text-navy">Deployments</h1>
      <p className="text-sm text-slate-500 mt-1">
        Snapshot → Deploy → Health Monitor → Success / Rollback, per target device. Refreshes automatically.
      </p>

      <div className="mt-6 bg-white border border-slate-200 rounded-xl overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-navy text-white">
            <tr>
              <th className="text-left px-4 py-3 font-semibold">Started</th>
              <th className="text-left px-4 py-3 font-semibold">Change Request</th>
              <th className="text-left px-4 py-3 font-semibold">Device</th>
              <th className="text-left px-4 py-3 font-semibold">Protocol</th>
              <th className="text-left px-4 py-3 font-semibold">Status</th>
              <th className="text-left px-4 py-3 font-semibold">Health Checks</th>
            </tr>
          </thead>
          <tbody>
            {!loading && deployments.length === 0 && (
              <tr>
                <td colSpan={6} className="text-center text-slate-400 py-8">
                  No deployments yet. Approve a change request to kick one off.
                </td>
              </tr>
            )}
            {deployments.map((d, i) => (
              <>
                <tr
                  key={d.id}
                  className={`cursor-pointer ${i % 2 ? "bg-slate-50" : "bg-white"}`}
                  onClick={() => setExpanded(expanded === d.id ? null : d.id)}
                >
                  <td className="px-4 py-3 text-slate-500">{new Date(d.created_at).toLocaleString()}</td>
                  <td className="px-4 py-3 font-mono text-xs text-slate-500">{d.change_request_id.slice(0, 8)}</td>
                  <td className="px-4 py-3 font-mono text-xs text-slate-500">{d.device_id.slice(0, 8)}</td>
                  <td className="px-4 py-3 text-slate-600 uppercase text-xs">{d.protocol}</td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-1 rounded-full text-xs font-medium ${STATUS_STYLES[d.status]}`}>
                      {d.status.replace(/_/g, " ")}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-slate-500">
                    {d.health_checks.length > 0
                      ? `${d.health_checks.filter((c) => c.passed).length}/${d.health_checks.length} passed`
                      : "—"}
                  </td>
                </tr>
                {expanded === d.id && (
                  <tr className="bg-slate-50">
                    <td colSpan={6} className="px-4 py-3">
                      {d.error_message && (
                        <p className="text-riskcrit text-xs mb-2">Error: {d.error_message}</p>
                      )}
                      {d.health_checks.length > 0 ? (
                        <ul className="space-y-1">
                          {d.health_checks.map((c, idx) => (
                            <li key={idx} className="text-xs flex gap-2">
                              <span className={c.passed ? "text-green-700" : "text-riskcrit"}>
                                {c.passed ? "✓" : "✗"}
                              </span>
                              <span className="font-medium">{c.category}/{c.check_name}</span>
                              {c.detail && <span className="text-slate-400">— {c.detail}</span>}
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <p className="text-xs text-slate-400">No health check results yet.</p>
                      )}
                    </td>
                  </tr>
                )}
              </>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
