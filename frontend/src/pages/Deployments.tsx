import React, { useEffect, useMemo, useState, useRef } from "react";
import { api } from "../lib/api";
import { DeploymentRecord, DeploymentLog } from "../lib/types";

const STATUS_STYLES: Record<DeploymentRecord["status"], string> = {
  queued: "bg-slate-100 text-slate-600",
  in_progress: "bg-blue-100 text-blue-700",
  succeeded: "bg-green-100 text-green-700",
  failed: "bg-red-100 text-red-700",
  rolled_back: "bg-amber-100 text-amber-700",
};

const STATUS_FILTERS: { value: DeploymentRecord["status"] | "all"; label: string }[] = [
  { value: "all", label: "All" },
  { value: "queued", label: "Queued" },
  { value: "in_progress", label: "In Progress" },
  { value: "succeeded", label: "Succeeded" },
  { value: "failed", label: "Failed" },
  { value: "rolled_back", label: "Rolled Back" },
];

function DeploymentDetails({ deployment }: { deployment: DeploymentRecord }) {
  const [logs, setLogs] = useState<DeploymentLog[]>([]);
  const [loading, setLoading] = useState(true);
  const logEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // 1. Fetch historical logs
    api.get<DeploymentLog[]>(`/deployments/${deployment.id}/logs`).then((res) => {
      setLogs(res.data);
      setLoading(false);
    });

    // 2. Subscribe to realtime logs via WS
    const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    // If backend is running locally usually port 8000
    const host = window.location.hostname === "localhost" ? "localhost:8000" : window.location.host;
    const wsUrl = `${wsProtocol}//${host}/api/v1/deployments/ws`;
    const ws = new WebSocket(wsUrl);

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.event === "deployment_log" && data.deployment_id === deployment.id) {
        setLogs((prev) => [...prev, data.log]);
      }
    };

    return () => ws.close();
  }, [deployment.id]);

  useEffect(() => {
    if (logEndRef.current) {
      logEndRef.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [logs]);

  // Derived progress steps
  const steps = [
    { label: "Snapshot", key: "SNAPSHOT" },
    { label: "Deploy", key: "DEPLOY" },
    { label: "Verify", key: "VERIFY" },
    { label: "Complete / Rollback", key: "COMPLETE_ROLLBACK" }, 
  ];

  const currentStep = logs.length > 0 ? logs[logs.length - 1].step : "PRE-FLIGHT";

  function getStepStatus(stepKey: string) {
    if (deployment.status === "succeeded" && stepKey === "COMPLETE_ROLLBACK") return "passed";
    if (deployment.status === "rolled_back" && stepKey === "COMPLETE_ROLLBACK") return "rolled_back";
    if (deployment.status === "failed" && stepKey === "COMPLETE_ROLLBACK") return "failed";
    
    const hasLog = logs.some((l) => {
      if (stepKey === "COMPLETE_ROLLBACK") return l.step === "COMPLETE" || l.step === "ROLLBACK";
      return l.step === stepKey;
    });

    const isCurrent = currentStep === stepKey;
    const isPast = logs.findIndex(l => {
      if (stepKey === "COMPLETE_ROLLBACK") return l.step === "COMPLETE" || l.step === "ROLLBACK";
      return l.step === stepKey;
    }) !== -1 && !isCurrent;

    if (hasLog && deployment.status === "failed") return "failed";
    if (hasLog) return "passed";
    return "pending";
  }

  return (
    <div className="flex flex-col md:flex-row gap-6 p-4 bg-slate-50 border-t border-slate-200">
      
      {/* LEFT PANE: Progress & Health Checks */}
      <div className="w-full md:w-1/3 flex flex-col gap-6 p-2">
        <div>
          <h4 className="text-xs uppercase font-bold text-slate-500 mb-3 tracking-wider">Timeline</h4>
          <div className="relative pl-5 border-l-2 border-slate-200/60 pb-2 space-y-5 ml-2">
            {steps.map((st, idx) => {
              const status = getStepStatus(st.key);
              let icon = <div className="w-[11px] h-[11px] rounded-full bg-slate-300 absolute -left-[7px] top-1" />;
              let textStyle = "text-slate-400";
              
              if (status === "passed") {
                icon = <div className="w-[15px] h-[15px] rounded-full bg-green-500 absolute -left-[9px] top-[2px] ring-4 ring-slate-50 shadow-sm" />;
                textStyle = "text-green-700 font-medium";
              } else if (status === "failed") {
                icon = <div className="w-[15px] h-[15px] rounded-full bg-red-500 absolute -left-[9px] top-[2px] ring-4 ring-slate-50 shadow-sm" />;
                textStyle = "text-red-700 font-medium";
              } else if (status === "rolled_back") {
                icon = <div className="w-[15px] h-[15px] rounded-full bg-amber-500 absolute -left-[9px] top-[2px] ring-4 ring-slate-50 shadow-sm" />;
                textStyle = "text-amber-700 font-medium";
              }

              return (
                <div key={idx} className="relative">
                  {icon}
                  <span className={`text-[13px] ${textStyle} -mt-1 block`}>{st.label}</span>
                </div>
              );
            })}
          </div>
        </div>

        <div>
          <h4 className="text-xs uppercase font-bold text-slate-500 mb-2 tracking-wider">Health Sweeps</h4>
          {deployment.health_checks.length > 0 ? (
            <ul className="space-y-1">
              {deployment.health_checks.map((c, idx) => (
                <li key={idx} className="text-xs flex gap-2 items-center bg-white border border-slate-100 p-1.5 rounded shadow-sm">
                  <span className={c.passed ? "text-green-600 font-bold" : "text-riskcrit font-bold"}>
                    {c.passed ? "✓" : "✗"}
                  </span>
                  <span className="font-medium text-slate-700">
                    {c.category}/{c.check_name}
                  </span>
                  {c.detail && <span className="text-slate-400 truncate w-32" title={c.detail}>— {c.detail}</span>}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-slate-400 italic">No health check results yet.</p>
          )}
        </div>
      </div>

      {/* RIGHT PANE: Live Logs Terminal */}
      <div className="w-full md:w-2/3 flex flex-col">
        <h4 className="text-xs uppercase font-bold text-slate-500 mb-2 tracking-wider flex justify-between">
          <span>Live Logs</span>
          {(deployment.status === "queued" || deployment.status === "in_progress") && (
            <span className="text-brandblue flex items-center gap-1.5 normal-case font-medium">
              <span className="w-1.5 h-1.5 rounded-full bg-brandblue animate-pulse" /> Tailing...
            </span>
          )}
        </h4>
        <div className="bg-[#1e1e1e] border-4 border-slate-800 rounded-lg h-72 overflow-y-auto p-4 font-mono text-[11px] text-slate-300 relative shadow-inner leading-relaxed">
          {loading ? (
            <p className="text-slate-500 italic">Fetching terminal logs...</p>
          ) : logs.length === 0 ? (
            <p className="text-slate-500 italic">No logs available.</p>
          ) : (
            <div className="space-y-1">
              {logs.map((log) => (
                <div key={log.id} className="flex gap-3 hover:bg-slate-800/80 px-1 -mx-1 rounded transition-colors break-all">
                  <span className="text-slate-500 shrink-0 select-none">
                    {new Date(log.timestamp).toISOString().split('T')[1].slice(0, -1)}
                  </span>
                  <span className={`shrink-0 w-16 text-left font-bold ${
                    log.level === 'ERROR' ? 'text-red-400' :
                    log.level === 'WARN' ? 'text-amber-400' :
                    'text-blue-400'
                  }`}>
                    [{log.step}]
                  </span>
                  <span className={log.level === 'ERROR' ? 'text-red-300' : log.level === 'WARN' ? 'text-amber-200' : 'text-slate-300'}>
                    {log.message}
                  </span>
                </div>
              ))}
              <div ref={logEndRef} className="h-2" />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default function Deployments() {
  const [deployments, setDeployments] = useState<DeploymentRecord[]>([]);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<DeploymentRecord["status"] | "all">("all");
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  const load = () => {
    api.get<DeploymentRecord[]>("/deployments").then((res) => {
      setDeployments(res.data);
      setLoading(false);
      setLastUpdated(new Date());
    });
  };

  useEffect(() => {
    load();
    const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = window.location.hostname === "localhost" ? "localhost:8000" : window.location.host;
    const wsUrl = `${wsProtocol}//${host}/api/v1/deployments/ws`;
    const ws = new WebSocket(wsUrl);

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.event === "deployment_status_changed") {
        setTimeout(load, 500); 
      }
    };
    
    const interval = setInterval(load, 30000);
    return () => {
      clearInterval(interval);
      ws.close();
    };
  }, []);

  const filtered = useMemo(
    () => (statusFilter === "all" ? deployments : deployments.filter((d) => d.status === statusFilter)),
    [deployments, statusFilter]
  );

  const hasActive = deployments.some((d) => d.status === "queued" || d.status === "in_progress");

  return (
    <div className="pb-16 flex flex-col gap-6 md:p-2">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-3xl font-bold text-navy">Deployments & Logs</h1>
          <p className="text-sm text-slate-500 mt-1">
            Real-time pipeline monitoring and historical live logs.
          </p>
        </div>
        <div className="text-right text-xs text-slate-400">
          {hasActive && (
            <span className="inline-flex items-center gap-1.5 text-brandblue font-medium mr-2 bg-blue-50 px-2 py-1 rounded">
              <span className="w-1.5 h-1.5 rounded-full bg-brandblue animate-pulse" /> Active Deployments Running
            </span>
          )}
          {lastUpdated && <span className="mr-3">Updated {lastUpdated.toLocaleTimeString()}</span>}
          <button onClick={load} className="text-brandblue font-medium hover:text-navy bg-white border border-brandblue hover:bg-slate-50 px-3 py-1 rounded-full transition shadow-sm">
            ↻ Refresh now
          </button>
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        {STATUS_FILTERS.map((f) => (
          <button
            key={f.value}
            onClick={() => setStatusFilter(f.value)}
            className={`px-4 py-1.5 rounded-full text-[13px] font-semibold border transition-all shadow-sm ${
              statusFilter === f.value
                ? "bg-navy text-white border-navy"
                : "bg-white text-slate-600 border-slate-200 hover:border-slate-300 hover:bg-slate-50"
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      <div className="bg-white border border-slate-200 rounded-xl overflow-hidden shadow-sm">
        <table className="w-full text-sm">
          <thead className="bg-slate-100/80 border-b border-slate-200">
            <tr>
              <th className="text-left px-5 py-3.5 font-bold text-slate-600 uppercase text-xs tracking-wider">Started</th>
              <th className="text-left px-5 py-3.5 font-bold text-slate-600 uppercase text-xs tracking-wider">Change Request</th>
              <th className="text-left px-5 py-3.5 font-bold text-slate-600 uppercase text-xs tracking-wider">Device</th>
              <th className="text-left px-5 py-3.5 font-bold text-slate-600 uppercase text-xs tracking-wider">Protocol</th>
              <th className="text-left px-5 py-3.5 font-bold text-slate-600 uppercase text-xs tracking-wider">Status</th>
              <th className="text-left px-5 py-3.5 font-bold text-slate-600 uppercase text-xs tracking-wider">Health Checks</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {loading && (
              <tr>
                <td colSpan={6} className="text-center text-slate-400 py-12">
                  <div className="inline-block w-5 h-5 border-2 border-slate-200 border-t-brandblue rounded-full animate-spin mb-2" />
                  <p>Loading deployments…</p>
                </td>
              </tr>
            )}
            {!loading && filtered.length === 0 && (
              <tr>
                <td colSpan={6} className="text-center text-slate-400 py-12">
                  {deployments.length === 0
                    ? "No deployments yet. Approve a change request to trigger the pipeline."
                    : "No deployments match the selected filter."}
                </td>
              </tr>
            )}
            {filtered.map((d) => (
              <React.Fragment key={d.id}>
                <tr
                  className={`cursor-pointer transition-colors hover:bg-slate-50/70 border-l-4 ${
                    expanded === d.id ? "bg-slate-50 border-l-brandblue" : "border-l-transparent bg-white"
                  }`}
                  onClick={() => setExpanded(expanded === d.id ? null : d.id)}
                >
                  <td className="px-5 py-4 text-slate-600 whitespace-nowrap font-medium">{new Date(d.created_at).toLocaleString()}</td>
                  <td className="px-5 py-4 font-mono text-xs text-brandblue font-semibold">{d.change_request_id.slice(0, 8)}</td>
                  <td className="px-5 py-4 font-mono text-xs text-slate-500 font-semibold">{d.device_id.slice(0, 8)}</td>
                  <td className="px-5 py-4 text-slate-600 font-bold tracking-wide">{d.protocol.toUpperCase()}</td>
                  <td className="px-5 py-4">
                    <span className={`px-2.5 py-1 rounded-md text-[11px] font-bold uppercase tracking-wider shadow-sm ${STATUS_STYLES[d.status]}`}>
                      {d.status.replace(/_/g, " ")}
                    </span>
                  </td>
                  <td className="px-5 py-4 text-slate-500 font-medium whitespace-nowrap">
                    {d.health_checks.length > 0
                      ? <span className="bg-slate-100 px-2 py-0.5 rounded-full text-slate-700 text-xs shadow-sm font-semibold border border-slate-200">{d.health_checks.filter((c) => c.passed).length}/{d.health_checks.length} passed</span>
                      : <span className="text-slate-300 italic">Pending</span>}
                    <span className={`text-slate-400 ml-4 inline-block transition-transform duration-200 ${expanded === d.id ? "rotate-90 text-brandblue" : "rotate-0"}`}>▶</span>
                  </td>
                </tr>
                {expanded === d.id && (
                  <tr>
                    <td colSpan={6} className="p-0 border-b-2 border-slate-200">
                      <DeploymentDetails deployment={d} />
                    </td>
                  </tr>
                )}
              </React.Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}