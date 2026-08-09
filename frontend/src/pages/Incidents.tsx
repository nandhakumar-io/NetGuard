import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../lib/api";

interface Incident {
  id: string;
  title: string;
  summary: string | null;
  severity: string;
  status: string;
  alert_ids: string[];
  detected_at: string | null;
  resolved_at: string | null;
  root_cause_summary: string | null;
  impact_summary: string | null;
  action_items: string | null;
  created_by: string | null;
  created_at: string;
}

interface TimelineEvent {
  id: string;
  event_type: string;
  description: string;
  actor: string | null;
  occurred_at: string;
}

const SEVERITY_BADGE: Record<string, string> = {
  critical: "bg-red-100 text-red-700",
  major: "bg-amber-100 text-amber-700",
  minor: "bg-slate-100 text-slate-600",
};

const STATUS_BADGE: Record<string, string> = {
  open: "bg-red-100 text-red-700",
  mitigated: "bg-amber-100 text-amber-700",
  resolved: "bg-blue-100 text-blue-700",
  postmortem_due: "bg-purple-100 text-purple-700",
  closed: "bg-green-100 text-green-700",
};

const STATUS_FLOW = ["open", "mitigated", "resolved", "postmortem_due", "closed"];

export default function Incidents() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [timeline, setTimeline] = useState<TimelineEvent[]>([]);
  const [detail, setDetail] = useState<Incident | null>(null);
  const [newNote, setNewNote] = useState("");
  const [postmortem, setPostmortem] = useState({ root_cause_summary: "", impact_summary: "", action_items: "" });
  const [preview, setPreview] = useState<{ root_cause_alert_id: string; alert_count: number; alerts: any[] } | null>(null);
  const [newTitle, setNewTitle] = useState("");
  const [creating, setCreating] = useState(false);

  const load = () => {
    setLoading(true);
    api
      .get<Incident[]>("/incidents")
      .then((res) => setIncidents(res.data))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  useEffect(() => {
    const fromAlert = searchParams.get("from_alert");
    if (!fromAlert) return;
    api.get(`/incidents/from-alert/${fromAlert}`).then((res) => {
      setPreview(res.data);
      setNewTitle(res.data.alerts?.[0]?.category ? `${res.data.alerts[0].category} — ${res.data.alert_count} alerts` : "New Incident");
    });
  }, [searchParams]);

  const createFromPreview = async () => {
    if (!preview) return;
    setCreating(true);
    try {
      const res = await api.post("/incidents", {
        title: newTitle || "New Incident",
        root_cause_alert_id: preview.root_cause_alert_id,
      });
      setPreview(null);
      searchParams.delete("from_alert");
      setSearchParams(searchParams);
      load();
      openDetail(res.data.id);
    } finally {
      setCreating(false);
    }
  };

  const openDetail = (id: string) => {
    setSelectedId(id);
    api.get(`/incidents/${id}`).then((res) => {
      setDetail(res.data);
      setTimeline(res.data.timeline || []);
      setPostmortem({
        root_cause_summary: res.data.root_cause_summary || "",
        impact_summary: res.data.impact_summary || "",
        action_items: res.data.action_items || "",
      });
    });
  };

  const addNote = async () => {
    if (!selectedId || !newNote.trim()) return;
    await api.post(`/incidents/${selectedId}/timeline`, { event_type: "note", description: newNote });
    setNewNote("");
    openDetail(selectedId);
  };

  const advanceStatus = async () => {
    if (!selectedId || !detail) return;
    const idx = STATUS_FLOW.indexOf(detail.status);
    const next = STATUS_FLOW[idx + 1];
    if (!next) return;
    await api.patch(`/incidents/${selectedId}/status`, null, { params: { status: next } });
    openDetail(selectedId);
    load();
  };

  const savePostmortem = async () => {
    if (!selectedId) return;
    await api.put(`/incidents/${selectedId}`, postmortem);
    openDetail(selectedId);
  };

  return (
    <div>
      <div>
        <h1 className="text-2xl font-bold text-navy dark:text-white">Incidents</h1>
        <p className="text-sm text-slate-500 mt-1">
          Correlated alert groups promoted to formal incidents for retros — timeline, root cause, and follow-ups.
        </p>
      </div>

      {preview && (
        <div className="bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-900 rounded-xl p-4 mt-4 flex items-center justify-between gap-4 flex-wrap">
          <div>
            <p className="text-sm font-semibold text-navy dark:text-white">
              Open incident from {preview.alert_count} correlated alert(s)?
            </p>
            <input
              className="border border-slate-300 dark:border-noc-border dark:bg-noc-panel rounded-lg px-3 py-1.5 text-sm mt-2 w-80"
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
              placeholder="Incident title"
            />
          </div>
          <div className="flex gap-2">
            <button
              onClick={createFromPreview}
              disabled={creating}
              className="text-xs bg-brandblue text-white rounded-lg px-3 py-1.5 font-semibold hover:bg-navy disabled:opacity-50"
            >
              {creating ? "Creating…" : "Create Incident"}
            </button>
            <button
              onClick={() => {
                setPreview(null);
                searchParams.delete("from_alert");
                setSearchParams(searchParams);
              }}
              className="text-xs bg-white dark:bg-noc-panel border border-slate-300 dark:border-noc-border rounded-lg px-3 py-1.5 font-semibold"
            >
              Dismiss
            </button>
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5 mt-5">
        <div className="bg-white dark:bg-noc-panel border border-slate-200 dark:border-noc-border rounded-xl overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-navy text-white">
              <tr>
                <th className="text-left px-4 py-3 font-semibold">Title</th>
                <th className="text-left px-4 py-3 font-semibold">Severity</th>
                <th className="text-left px-4 py-3 font-semibold">Status</th>
                <th className="text-left px-4 py-3 font-semibold">Detected</th>
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr>
                  <td colSpan={4} className="text-center text-slate-400 py-8">Loading incidents…</td>
                </tr>
              )}
              {!loading && incidents.length === 0 && (
                <tr>
                  <td colSpan={4} className="text-center text-slate-400 py-8">
                    No incidents yet. Open one from a correlated alert group in Alert Center.
                  </td>
                </tr>
              )}
              {incidents.map((inc, i) => (
                <tr
                  key={inc.id}
                  onClick={() => openDetail(inc.id)}
                  className={`cursor-pointer ${selectedId === inc.id ? "bg-brandblue/10" : i % 2 ? "bg-slate-50 dark:bg-white/5" : ""}`}
                >
                  <td className="px-4 py-3 font-medium text-navy dark:text-white">{inc.title}</td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-1 rounded-full text-xs font-medium ${SEVERITY_BADGE[inc.severity] || ""}`}>
                      {inc.severity}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-1 rounded-full text-xs font-medium ${STATUS_BADGE[inc.status] || ""}`}>
                      {inc.status.replace(/_/g, " ")}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-slate-500 whitespace-nowrap">
                    {inc.detected_at ? new Date(inc.detected_at).toLocaleString() : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="bg-white dark:bg-noc-panel border border-slate-200 dark:border-noc-border rounded-xl p-5">
          {!detail && <p className="text-sm text-slate-400">Select an incident to view its timeline and postmortem.</p>}
          {detail && (
            <div className="space-y-5">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h2 className="font-semibold text-navy dark:text-white">{detail.title}</h2>
                  <p className="text-xs text-slate-500 mt-1">{detail.alert_ids.length} correlated alert(s) · opened by {detail.created_by || "—"}</p>
                </div>
                {STATUS_FLOW.indexOf(detail.status) < STATUS_FLOW.length - 1 && (
                  <button
                    onClick={advanceStatus}
                    className="text-xs bg-brandblue text-white rounded-lg px-3 py-1.5 font-semibold hover:bg-navy whitespace-nowrap"
                  >
                    Mark {STATUS_FLOW[STATUS_FLOW.indexOf(detail.status) + 1].replace(/_/g, " ")}
                  </button>
                )}
              </div>

              <div>
                <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-2">Timeline</h3>
                <ul className="space-y-2 max-h-48 overflow-y-auto">
                  {timeline.map((e) => (
                    <li key={e.id} className="text-sm border-l-2 border-slate-200 dark:border-noc-border pl-3">
                      <span className="text-slate-400 text-xs">{new Date(e.occurred_at).toLocaleString()}</span>
                      <span className="ml-2 uppercase text-[10px] font-semibold text-brandblue">{e.event_type}</span>
                      <p className="text-slate-700 dark:text-noc-text">{e.description}</p>
                    </li>
                  ))}
                </ul>
                <div className="flex gap-2 mt-2">
                  <input
                    className="border border-slate-300 dark:border-noc-border dark:bg-noc-panel rounded-lg px-3 py-1.5 text-sm flex-1"
                    placeholder="Add a timeline note…"
                    value={newNote}
                    onChange={(e) => setNewNote(e.target.value)}
                  />
                  <button onClick={addNote} className="text-xs bg-slate-100 dark:bg-white/10 rounded-lg px-3 py-1.5 font-semibold">
                    Add
                  </button>
                </div>
              </div>

              <div>
                <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-2">Postmortem</h3>
                <div className="space-y-2">
                  <textarea
                    className="w-full border border-slate-300 dark:border-noc-border dark:bg-noc-panel rounded-lg px-3 py-2 text-sm"
                    rows={2}
                    placeholder="Root cause summary"
                    value={postmortem.root_cause_summary}
                    onChange={(e) => setPostmortem({ ...postmortem, root_cause_summary: e.target.value })}
                  />
                  <textarea
                    className="w-full border border-slate-300 dark:border-noc-border dark:bg-noc-panel rounded-lg px-3 py-2 text-sm"
                    rows={2}
                    placeholder="Impact summary"
                    value={postmortem.impact_summary}
                    onChange={(e) => setPostmortem({ ...postmortem, impact_summary: e.target.value })}
                  />
                  <textarea
                    className="w-full border border-slate-300 dark:border-noc-border dark:bg-noc-panel rounded-lg px-3 py-2 text-sm"
                    rows={2}
                    placeholder="Action items (one per line)"
                    value={postmortem.action_items}
                    onChange={(e) => setPostmortem({ ...postmortem, action_items: e.target.value })}
                  />
                  <button onClick={savePostmortem} className="text-xs bg-brandblue text-white rounded-lg px-3 py-1.5 font-semibold hover:bg-navy">
                    Save postmortem
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}