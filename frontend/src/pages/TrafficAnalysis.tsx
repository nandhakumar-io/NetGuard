import { useCallback, useEffect, useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../lib/api";
import {
  BandwidthPoint,
  FlowExporter,
  ProtocolShare,
  TopConversation,
  TopTalker,
  TrafficSummary,
} from "../lib/types";

const WINDOW_OPTIONS = [
  { label: "15 min", minutes: 15 },
  { label: "1 hour", minutes: 60 },
  { label: "6 hours", minutes: 360 },
  { label: "24 hours", minutes: 1440 },
  { label: "7 days", minutes: 10080 },
];

function formatBytes(n: number): string {
  if (n >= 1e12) return `${(n / 1e12).toFixed(2)} TB`;
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} GB`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)} MB`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)} KB`;
  return `${n} B`;
}

function formatBps(bytesPerSec: number): string {
  const bits = bytesPerSec * 8;
  if (bits >= 1e9) return `${(bits / 1e9).toFixed(2)} Gbps`;
  if (bits >= 1e6) return `${(bits / 1e6).toFixed(2)} Mbps`;
  if (bits >= 1e3) return `${(bits / 1e3).toFixed(1)} Kbps`;
  return `${bits.toFixed(0)} bps`;
}

const PROTOCOL_COLORS: Record<string, string> = {
  TCP: "bg-brandblue",
  UDP: "bg-emerald-500",
  ICMP: "bg-amber-500",
  GRE: "bg-purple-500",
  ESP: "bg-pink-500",
};

export default function TrafficAnalysis() {
  const [minutes, setMinutes] = useState(60);
  const [summary, setSummary] = useState<TrafficSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchSummary = useCallback(() => {
    api
      .get<TrafficSummary>(`/flows/summary?minutes=${minutes}`)
      .then((res) => {
        setSummary(res.data);
        setError(null);
        setLoading(false);
      })
      .catch(() => {
        setError(
          "Could not reach the NetFlow/IPFIX/sFlow collector. Make sure the backend is running and at least one device is exporting flows."
        );
        setLoading(false);
      });
  }, [minutes]);

  useEffect(() => {
    fetchSummary();
    const interval = setInterval(fetchSummary, 15000);
    return () => clearInterval(interval);
  }, [fetchSummary]);

  const talkers: TopTalker[] = summary?.top_talkers ?? [];
  const conversations: TopConversation[] = summary?.top_conversations ?? [];
  const protocols: ProtocolShare[] = summary?.protocol_breakdown ?? [];
  const bandwidth: BandwidthPoint[] = summary?.bandwidth_timeseries ?? [];
  const exporters: FlowExporter[] = summary?.exporters ?? [];
  const maxTalkerBytes = Math.max(...talkers.map((t) => t.bytes), 1);

  return (
    <div className="pb-16 max-w-7xl mx-auto flex flex-col gap-6 pt-2">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold text-navy dark:text-white">Traffic Analysis</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1 font-medium">
            NetFlow v5/v9, IPFIX, and sFlow -- top talkers, top conversations, protocol mix, and bandwidth over
            time.
          </p>
        </div>
        <div className="flex gap-1 bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg p-1 shadow-sm">
          {WINDOW_OPTIONS.map((opt) => (
            <button
              key={opt.minutes}
              onClick={() => setMinutes(opt.minutes)}
              className={`px-3 py-1.5 text-xs font-bold rounded-md transition-colors ${
                minutes === opt.minutes
                  ? "bg-brandblue text-white"
                  : "text-slate-500 dark:text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-700"
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 text-riskcrit text-sm font-semibold rounded-lg px-4 py-3 shadow-sm">
          {error}
        </div>
      )}

      {!loading && !error && exporters.length === 0 && (
        <div className="bg-amber-50 border border-amber-200 text-amber-800 text-sm font-semibold rounded-lg px-4 py-3 shadow-sm">
          No flow exporters seen in this window yet. Point a device's NetFlow/IPFIX export at UDP 2055, or sFlow
          at UDP 6343, on this host.
        </div>
      )}

      {/* Bandwidth over time */}
      <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-5 shadow-sm">
        <p className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wide mb-3">
          Bandwidth over time
        </p>
        <div className="h-56">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={bandwidth}>
              <defs>
                <linearGradient id="bwFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#2563eb" stopOpacity={0.35} />
                  <stop offset="95%" stopColor="#2563eb" stopOpacity={0.02} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
              <XAxis
                dataKey="timestamp"
                tickFormatter={(v) => new Date(v).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                tick={{ fontSize: 11 }}
                minTickGap={40}
              />
              <YAxis tickFormatter={(v) => formatBps(v)} tick={{ fontSize: 11 }} width={70} />
              <Tooltip
                formatter={(v: number) => formatBps(v)}
                labelFormatter={(v) => new Date(v).toLocaleString()}
              />
              <Area type="monotone" dataKey="bytes_per_sec" stroke="#2563eb" fill="url(#bwFill)" strokeWidth={2} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Top talkers */}
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-5 shadow-sm">
          <p className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wide mb-3">
            Top talkers
          </p>
          {talkers.length === 0 ? (
            <p className="text-sm text-slate-400 py-6 text-center">No flow data in this window.</p>
          ) : (
            <div className="flex flex-col gap-2">
              {talkers.map((t) => (
                <div key={t.ip_address} className="flex items-center gap-3">
                  <span className="font-mono text-xs w-32 shrink-0 text-navy dark:text-white">{t.ip_address}</span>
                  <div className="flex-1 h-4 bg-slate-100 dark:bg-slate-700 rounded overflow-hidden">
                    <div
                      className="h-full bg-brandblue rounded"
                      style={{ width: `${Math.max((t.bytes / maxTalkerBytes) * 100, 3)}%` }}
                    />
                  </div>
                  <span className="text-xs font-bold text-slate-500 dark:text-slate-400 w-20 text-right">
                    {formatBytes(t.bytes)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Protocol breakdown */}
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-5 shadow-sm">
          <p className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wide mb-3">
            Protocol breakdown
          </p>
          {protocols.length === 0 ? (
            <p className="text-sm text-slate-400 py-6 text-center">No flow data in this window.</p>
          ) : (
            <>
              <div className="flex h-4 w-full rounded overflow-hidden mb-4">
                {protocols.map((p) => (
                  <div
                    key={p.protocol}
                    title={`${p.protocol}: ${p.pct}%`}
                    className={`${PROTOCOL_COLORS[p.protocol] ?? "bg-slate-400"}`}
                    style={{ width: `${p.pct}%` }}
                  />
                ))}
              </div>
              <div className="flex flex-col gap-2">
                {protocols.map((p) => (
                  <div key={p.protocol} className="flex items-center justify-between text-sm">
                    <span className="flex items-center gap-2 font-semibold text-navy dark:text-white">
                      <span className={`inline-block w-2.5 h-2.5 rounded-sm ${PROTOCOL_COLORS[p.protocol] ?? "bg-slate-400"}`} />
                      {p.protocol}
                    </span>
                    <span className="text-slate-500 dark:text-slate-400">
                      {formatBytes(p.bytes)} <span className="text-slate-400">({p.pct}%)</span>
                    </span>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </div>

      {/* Top conversations */}
      <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl shadow-sm overflow-x-auto">
        <p className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wide p-5 pb-3">
          Top conversations
        </p>
        <table className="w-full text-sm min-w-[600px]">
          <thead>
            <tr className="text-left text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wide border-y border-slate-100 dark:border-slate-700">
              <th className="px-5 py-2">Source</th>
              <th className="px-5 py-2">Destination</th>
              <th className="px-5 py-2">Protocol</th>
              <th className="px-5 py-2 text-right">Packets</th>
              <th className="px-5 py-2 text-right">Bytes</th>
            </tr>
          </thead>
          <tbody>
            {conversations.length === 0 ? (
              <tr>
                <td colSpan={5} className="text-center text-slate-400 py-6">
                  No flow data in this window.
                </td>
              </tr>
            ) : (
              conversations.map((c, i) => (
                <tr key={i} className="border-b border-slate-50 dark:border-slate-700/50 last:border-0">
                  <td className="px-5 py-2 font-mono text-navy dark:text-white">{c.src_ip}</td>
                  <td className="px-5 py-2 font-mono text-navy dark:text-white">{c.dst_ip}</td>
                  <td className="px-5 py-2 text-slate-500 dark:text-slate-400">{c.protocol}</td>
                  <td className="px-5 py-2 text-right text-slate-500 dark:text-slate-400">{c.packets.toLocaleString()}</td>
                  <td className="px-5 py-2 text-right font-bold text-navy dark:text-white">{formatBytes(c.bytes)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Exporters */}
      <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl shadow-sm overflow-x-auto">
        <p className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wide p-5 pb-3">
          Flow exporters
        </p>
        <table className="w-full text-sm min-w-[520px]">
          <thead>
            <tr className="text-left text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wide border-y border-slate-100 dark:border-slate-700">
              <th className="px-5 py-2">Exporter</th>
              <th className="px-5 py-2">Device</th>
              <th className="px-5 py-2">Format</th>
              <th className="px-5 py-2">Last seen</th>
              <th className="px-5 py-2 text-right">Flows</th>
            </tr>
          </thead>
          <tbody>
            {exporters.length === 0 ? (
              <tr>
                <td colSpan={5} className="text-center text-slate-400 py-6">
                  No exporters active in this window.
                </td>
              </tr>
            ) : (
              exporters.map((e) => (
                <tr key={e.exporter_ip} className="border-b border-slate-50 dark:border-slate-700/50 last:border-0">
                  <td className="px-5 py-2 font-mono text-navy dark:text-white">{e.exporter_ip}</td>
                  <td className="px-5 py-2 text-slate-500 dark:text-slate-400">{e.hostname ?? "Unknown"}</td>
                  <td className="px-5 py-2 text-slate-500 dark:text-slate-400 uppercase">{e.flow_version}</td>
                  <td className="px-5 py-2 text-slate-500 dark:text-slate-400">
                    {e.last_seen ? new Date(e.last_seen).toLocaleTimeString() : "—"}
                  </td>
                  <td className="px-5 py-2 text-right font-bold text-navy dark:text-white">
                    {e.flow_count.toLocaleString()}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}