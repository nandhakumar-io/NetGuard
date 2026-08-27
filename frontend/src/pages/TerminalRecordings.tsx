import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { TerminalRecordingRecord, TerminalSessionRecording } from "../lib/types";
import { Terminal } from "xterm";
import { FitAddon } from "xterm-addon-fit";
import "xterm/css/xterm.css";

// Reviewer-only page (SECURITY / NETWORK_ADMIN) mirroring the backend gate on
// app.api.terminal_recordings -- same bar as the audit log and credential
// rotation. Two parts: a filterable list of TerminalSessionRecording rows,
// and a playback modal that replays the JSON-Lines transcript through a
// read-only xterm.js instance (the same terminal lib WebTerminal.tsx uses
// for the *live* session, just fed pre-recorded output on a timer instead of
// a WebSocket).

function formatDuration(startedAt: string, endedAt: string | null): string {
  const start = new Date(startedAt).getTime();
  const end = endedAt ? new Date(endedAt).getTime() : Date.now();
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  if (mins < 60) return `${mins}m ${secs}s`;
  const hrs = Math.floor(mins / 60);
  return `${hrs}h ${mins % 60}m`;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

const PROTOCOL_BADGE: Record<string, string> = {
  ssh: "bg-green-100 text-green-700",
  telnet: "bg-amber-100 text-amber-700",
  demo: "bg-slate-100 text-slate-600",
};

function PlaybackModal({ recording, onClose }: { recording: TerminalSessionRecording; onClose: () => void }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const [records, setRecords] = useState<TerminalRecordingRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [cursor, setCursor] = useState(0); // index of next record to play
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    api
      .get<{ id: string; records: TerminalRecordingRecord[] }>(`/terminal-recordings/${recording.id}/transcript`)
      .then((res) => setRecords(res.data.records))
      .catch(() => setError("Failed to load transcript."));
  }, [recording.id]);

  useEffect(() => {
    if (!containerRef.current || termRef.current) return;
    const term = new Terminal({
      disableStdin: true, // playback only -- never send keystrokes anywhere
      cursorBlink: false,
      fontFamily: 'Menlo, Monaco, "Courier New", monospace',
      fontSize: 13,
      theme: { background: "#1e1e2e", foreground: "#cdd6f4", cursor: "#f38ba8" },
    });
    // Without a fit addon this stayed at xterm's default 80x24 cell grid
    // sized off whatever font metrics were available at construction time
    // (often before webfonts/layout settled), which could leave the
    // canvas laid out at ~0 visible rows inside the 380px container --
    // the terminal was technically receiving and buffering every write,
    // just not rendering any of it. Matches the sizing WebTerminal.tsx
    // (the live session view) already does for the same reason.
    const fitAddon = new FitAddon();
    term.loadAddon(fitAddon);
    term.open(containerRef.current);
    fitAddon.fit();
    const handleResize = () => fitAddon.fit();
    window.addEventListener("resize", handleResize);
    termRef.current = term;
    return () => {
      window.removeEventListener("resize", handleResize);
      term.dispose();
      termRef.current = null;
    };
  }, []);

  const writeUpTo = (idx: number) => {
    const term = termRef.current;
    if (!term || !records) return;
    for (let i = 0; i < idx; i++) {
      if (records[i].dir === "out") term.write(records[i].data);
    }
  };

  // Render the full session the moment its transcript loads, rather than
  // leaving the terminal blank until the reviewer clicks Play. A reviewer
  // opening a recording wants to see what happened -- requiring an extra
  // click before anything appears (with no visual difference between
  // "nothing captured" and "captured but not played yet") is exactly what
  // reads as "playback shows an empty terminal" even though the transcript
  // (confirmed by the raw .jsonl download) has real content. Play/Reset
  // below still work for scrubbing through the timeline from here.
  useEffect(() => {
    if (!records || !records.length || !termRef.current) return;
    writeUpTo(records.length);
    setCursor(records.length);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [records]);

  const reset = () => {
    if (timerRef.current) clearTimeout(timerRef.current);
    termRef.current?.reset();
    setCursor(0);
    setPlaying(false);
  };

  const step = (from: number) => {
    if (!records || from >= records.length) {
      setPlaying(false);
      return;
    }
    const rec = records[from];
    if (rec.dir === "out") termRef.current?.write(rec.data);
    const next = from + 1;
    setCursor(next);
    if (next >= records.length) {
      setPlaying(false);
      return;
    }
    const gap = Math.max(0, records[next].t - rec.t);
    timerRef.current = setTimeout(() => step(next), (gap * 1000) / speed);
  };

  const togglePlay = () => {
    if (!records) return;
    if (playing) {
      if (timerRef.current) clearTimeout(timerRef.current);
      setPlaying(false);
      return;
    }
    // Transcript is auto-rendered up to the end on load, so cursor already
    // sits at records.length the first time someone presses the button --
    // without this, "Replay" would immediately no-op (from >= length) and
    // look exactly like the "empty terminal" bug this is fixing.
    if (cursor >= records.length) {
      termRef.current?.reset();
      setPlaying(true);
      step(0);
      return;
    }
    setPlaying(true);
    step(cursor);
  };

  const seekFraction = (frac: number) => {
    if (!records) return;
    if (timerRef.current) clearTimeout(timerRef.current);
    setPlaying(false);
    termRef.current?.reset();
    const idx = Math.round(frac * records.length);
    writeUpTo(idx);
    setCursor(idx);
  };

  useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current); }, []);

  const totalDuration = records && records.length ? records[records.length - 1].t : 0;
  const currentTime = records && cursor > 0 && cursor <= records.length ? records[Math.min(cursor, records.length) - 1].t : 0;
  const progress = totalDuration > 0 ? currentTime / totalDuration : 0;

  return (
    <div className="fixed inset-0 bg-black/60 z-50 flex items-center justify-center p-4" onClick={onClose}>
      <div
        className="bg-navy rounded-xl w-full max-w-3xl shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 bg-slate-900/40 border-b border-white/10">
          <div>
            <p className="text-white font-semibold text-sm">
              {recording.device_hostname || recording.device_id} · {recording.actor_email}
            </p>
            <p className="text-slate-400 text-xs mt-0.5">
              {new Date(recording.started_at).toLocaleString()} · {formatDuration(recording.started_at, recording.ended_at)}
              {recording.redacted && <span className="ml-2 text-amber-400">Redacted content detected</span>}
            </p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-white text-xl leading-none px-2">
            ×
          </button>
        </div>

        <div ref={containerRef} className="px-3 py-2" style={{ height: 380 }} />

        {error && <p className="text-red-400 text-xs px-4 pb-2">{error}</p>}
        {!error && !records && <p className="text-slate-400 text-xs px-4 pb-2">Loading transcript…</p>}

        {records && records.length === 0 && (
          <p className="text-slate-400 text-xs px-4 pb-3">No output captured for this session.</p>
        )}

        {records && records.length > 0 && (
          <div className="px-4 pb-3 pt-1 flex items-center gap-3">
            <button
              onClick={togglePlay}
              className="bg-brandblue text-white rounded-lg px-3 py-1.5 text-xs font-semibold hover:opacity-90 min-w-[64px]"
            >
              {playing ? "Pause" : cursor >= records.length ? "Replay" : "Play"}
            </button>
            <button onClick={reset} className="text-slate-300 hover:text-white text-xs font-medium">
              ↺ Reset
            </button>
            <input
              type="range"
              min={0}
              max={1000}
              value={Math.round(progress * 1000)}
              onChange={(e) => seekFraction(Number(e.target.value) / 1000)}
              className="flex-1 accent-brandblue"
            />
            <span className="text-slate-400 text-xs whitespace-nowrap tabular-nums">
              {Math.round(currentTime)}s / {Math.round(totalDuration)}s
            </span>
            <select
              value={speed}
              onChange={(e) => setSpeed(Number(e.target.value))}
              className="bg-slate-800 text-slate-200 text-xs rounded px-1.5 py-1 border border-white/10"
            >
              {[0.5, 1, 2, 4, 8].map((s) => (
                <option key={s} value={s}>
                  {s}×
                </option>
              ))}
            </select>
          </div>
        )}

        <div className="px-4 pb-3">
          <button
            onClick={async () => {
              try {
                const res = await api.get(`/terminal-recordings/${recording.id}/download`, {
                  responseType: "blob" as any,
                });
                const blob = new Blob([res.data], { type: "application/x-ndjson" });
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement("a");
                a.href = url;
                a.download = `terminal-session-${recording.id}.jsonl`;
                a.click();
                window.URL.revokeObjectURL(url);
              } catch {
                setError("Failed to download transcript.");
              }
            }}
            className="text-xs text-brandblue hover:text-white font-medium"
          >
            Download raw transcript (.jsonl)
          </button>
        </div>
      </div>
    </div>
  );
}

export default function TerminalRecordings() {
  const { user } = useAuth();
  const canView = user?.role === "network_admin" || user?.role === "security";
  // Deletion is destructive to what's functionally a compliance artifact
  // (PCI DSS 10.2 / SOC 2 CC6.1 evidence) -- narrower than view access,
  // matching the backend's SECURITY-only delete endpoints.
  const canDelete = user?.role === "security";

  const [recordings, setRecordings] = useState<TerminalSessionRecording[]>([]);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [protocolFilter, setProtocolFilter] = useState("all");
  const [selected, setSelected] = useState<TerminalSessionRecording | null>(null);
  const [checkedIds, setCheckedIds] = useState<Set<string>>(new Set());
  const [deleting, setDeleting] = useState(false);

  const load = () => {
    if (!canView) {
      setLoading(false);
      return;
    }
    setLoading(true);
    api
      .get<TerminalSessionRecording[]>("/terminal-recordings")
      .then((res) => {
        setRecordings(res.data);
        setCheckedIds(new Set());
      })
      .finally(() => setLoading(false));
  };

  useEffect(load, [canView]);

  const protocols = useMemo(
    () => Array.from(new Set(recordings.map((r) => r.protocol).filter((p): p is string => !!p))),
    [recordings]
  );

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return recordings.filter((r) => {
      if (protocolFilter !== "all" && r.protocol !== protocolFilter) return false;
      if (!q) return true;
      return (
        r.actor_email.toLowerCase().includes(q) ||
        (r.device_hostname || "").toLowerCase().includes(q)
      );
    });
  }, [recordings, query, protocolFilter]);

  if (!canView) {
    return (
      <div className="bg-white border border-slate-200 rounded-xl p-8 text-center text-slate-500">
        You don't have access to terminal session recordings. This view is restricted to Security and Network Admin roles.
      </div>
    );
  }

  const toggleChecked = (id: string) => {
    setCheckedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleCheckedAll = () => {
    setCheckedIds((prev) => (prev.size === filtered.length ? new Set() : new Set(filtered.map((r) => r.id))));
  };

  const deleteOne = async (id: string) => {
    if (!window.confirm("Delete this recording? This can't be undone.")) return;
    setDeleting(true);
    try {
      await api.delete(`/terminal-recordings/${id}`);
      if (selected?.id === id) setSelected(null);
      load();
    } finally {
      setDeleting(false);
    }
  };

  const deleteSelected = async () => {
    if (checkedIds.size === 0) return;
    if (!window.confirm(`Delete ${checkedIds.size} selected recording(s)? This can't be undone.`)) return;
    setDeleting(true);
    try {
      await api.post("/terminal-recordings/bulk-delete", { recording_ids: Array.from(checkedIds) });
      load();
    } finally {
      setDeleting(false);
    }
  };

  const deleteAll = async () => {
    if (!window.confirm(`Delete ALL ${recordings.length} terminal recordings on the server? This can't be undone.`))
      return;
    if (!window.confirm("Are you sure? This removes every recorded session, including live evidence for past reviews.")) return;
    setDeleting(true);
    try {
      await api.delete("/terminal-recordings/", { params: { confirm: true } });
      load();
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div>
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-navy">Terminal Session Recordings</h1>
          <p className="text-sm text-slate-500 mt-1">
            Full keystroke/output transcripts of privileged device terminal sessions, for PCI DSS / SOC 2 review.
          </p>
        </div>
      </div>

      <div className="flex flex-wrap gap-2 items-center mt-5 mb-3">
        <input
          className="border border-slate-300 rounded-lg px-3 py-2 text-sm w-full max-w-xs"
          placeholder="Search device or actor…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <select
          className="border border-slate-300 rounded-lg px-3 py-2 text-sm"
          value={protocolFilter}
          onChange={(e) => setProtocolFilter(e.target.value)}
        >
          <option value="all">All protocols</option>
          {protocols.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
        {canDelete && checkedIds.size > 0 && (
          <button
            onClick={deleteSelected}
            disabled={deleting}
            className="text-xs text-red-600 font-semibold hover:text-red-800 border border-red-200 rounded-lg px-3 py-2 disabled:opacity-50"
          >
            Delete {checkedIds.size} selected
          </button>
        )}
        {canDelete && recordings.length > 0 && (
          <button
            onClick={deleteAll}
            disabled={deleting}
            className="text-xs text-red-600 font-semibold hover:text-red-800 border border-red-200 rounded-lg px-3 py-2 disabled:opacity-50"
          >
            Delete all
          </button>
        )}
        <button onClick={load} className="text-xs text-brandblue font-medium hover:text-navy ml-auto">
          ↻ Refresh
        </button>
      </div>

      <div className="bg-white border border-slate-200 rounded-xl overflow-x-auto">
        <table className="w-full text-sm min-w-[820px]">
          <thead className="bg-navy text-white">
            <tr>
              {canDelete && (
                <th className="text-left px-4 py-3 font-semibold w-8">
                  <input
                    type="checkbox"
                    checked={filtered.length > 0 && checkedIds.size === filtered.length}
                    onChange={toggleCheckedAll}
                    aria-label="Select all"
                  />
                </th>
              )}
              <th className="text-left px-4 py-3 font-semibold">Started</th>
              <th className="text-left px-4 py-3 font-semibold">Device</th>
              <th className="text-left px-4 py-3 font-semibold">Actor</th>
              <th className="text-left px-4 py-3 font-semibold">Protocol</th>
              <th className="text-left px-4 py-3 font-semibold">Duration</th>
              <th className="text-left px-4 py-3 font-semibold">Size</th>
              <th className="text-left px-4 py-3 font-semibold">Status</th>
              <th className="text-right px-4 py-3 font-semibold"></th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={canDelete ? 9 : 8} className="text-center text-slate-400 py-8">
                  Loading recordings…
                </td>
              </tr>
            )}
            {!loading && filtered.length === 0 && (
              <tr>
                <td colSpan={canDelete ? 9 : 8} className="text-center text-slate-400 py-8">
                  {recordings.length === 0 ? "No terminal sessions recorded yet." : "No recordings match your search."}
                </td>
              </tr>
            )}
            {filtered.map((r, i) => (
              <tr key={r.id} className={i % 2 ? "bg-slate-50" : "bg-white"}>
                {canDelete && (
                  <td className="px-4 py-3">
                    <input
                      type="checkbox"
                      checked={checkedIds.has(r.id)}
                      onChange={() => toggleChecked(r.id)}
                      aria-label={`Select recording ${r.id}`}
                    />
                  </td>
                )}
                <td className="px-4 py-3 text-slate-500 whitespace-nowrap">{new Date(r.started_at).toLocaleString()}</td>
                <td className="px-4 py-3 font-medium text-navy">{r.device_hostname || "—"}</td>
                <td className="px-4 py-3 text-slate-600">{r.actor_email}</td>
                <td className="px-4 py-3">
                  <span
                    className={`px-2 py-1 rounded-full text-xs font-medium ${
                      PROTOCOL_BADGE[r.protocol || ""] || "bg-slate-100 text-slate-600"
                    }`}
                  >
                    {r.protocol || "unknown"}
                  </span>
                </td>
                <td className="px-4 py-3 text-slate-600">{formatDuration(r.started_at, r.ended_at)}</td>
                <td className="px-4 py-3 text-slate-600">{formatBytes(r.byte_count)}</td>
                <td className="px-4 py-3">
                  {r.in_progress ? (
                    <span className="px-2 py-1 rounded-full text-xs font-medium bg-blue-100 text-blue-700">Live</span>
                  ) : r.redacted ? (
                    <span className="px-2 py-1 rounded-full text-xs font-medium bg-amber-100 text-amber-700">Redacted</span>
                  ) : (
                    <span className="px-2 py-1 rounded-full text-xs font-medium bg-slate-100 text-slate-600">Closed</span>
                  )}
                </td>
                <td className="px-4 py-3 text-right whitespace-nowrap">
                  <button
                    onClick={() => setSelected(r)}
                    className="text-brandblue hover:text-navy text-xs font-semibold"
                  >
                    ▶ Playback
                  </button>
                  {canDelete && (
                    <button
                      onClick={() => deleteOne(r.id)}
                      disabled={deleting}
                      className="text-red-500 hover:text-red-700 text-xs font-semibold ml-3 disabled:opacity-50"
                    >
                      Delete
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!loading && (
        <p className="text-xs text-slate-400 mt-2">
          Showing {filtered.length} of {recordings.length} recordings (most recent 100 from the server).
        </p>
      )}

      {selected && <PlaybackModal recording={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}