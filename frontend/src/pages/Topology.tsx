import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { TopologyResponse, TopologyNode, TopologyEdge } from "../lib/types";

const statusColor: Record<string, string> = {
  online: "#16a34a", // risklow
  offline: "#94a3b8", // slate-400
  degraded: "#d97706", // riskmed
  unknown: "#cbd5e1", // slate-300
};

const vendorLabel: Record<string, string> = {
  cisco: "Cisco",
  juniper: "Juniper",
  arista: "Arista",
  linux: "Linux",
};

interface LaidOutNode extends TopologyNode {
  x: number;
  y: number;
}

const WIDTH = 1000;
const HEIGHT = 620;
const ITERATIONS = 400;

/** Tiny dependency-free force-directed layout (Fruchterman-Reingold-ish):
 * every node repels every other node, edges pull their endpoints together,
 * everything is nudged toward the canvas center so isolated components
 * don't drift off-screen. Runs once per graph load, synchronously, before
 * first render -- fleet sizes here are small enough (tens, not thousands
 * of devices) that this is cheap and avoids pulling in a graph-layout
 * library for one page. */
function layoutNodes(nodes: TopologyNode[], edges: TopologyEdge[]): LaidOutNode[] {
  const n = nodes.length;
  if (n === 0) return [];

  const positions = new Map<string, { x: number; y: number; vx: number; vy: number }>();
  const cx = WIDTH / 2;
  const cy = HEIGHT / 2;
  const radius = Math.min(WIDTH, HEIGHT) / 2.6;

  nodes.forEach((node, i) => {
    const angle = (2 * Math.PI * i) / n;
    positions.set(node.id, {
      x: cx + radius * Math.cos(angle),
      y: cy + radius * Math.sin(angle),
      vx: 0,
      vy: 0,
    });
  });

  const k = Math.sqrt((WIDTH * HEIGHT) / Math.max(n, 1)); // ideal spring/repulsion distance
  const nodeIds = nodes.map((n) => n.id);

  for (let iter = 0; iter < ITERATIONS; iter++) {
    const temp = Math.max(1, 30 * (1 - iter / ITERATIONS)); // cooling factor

    // Repulsion between every pair
    for (let i = 0; i < nodeIds.length; i++) {
      const a = positions.get(nodeIds[i])!;
      for (let j = i + 1; j < nodeIds.length; j++) {
        const b = positions.get(nodeIds[j])!;
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
        const force = (k * k) / dist;
        dx = (dx / dist) * force;
        dy = (dy / dist) * force;
        a.vx += dx;
        a.vy += dy;
        b.vx -= dx;
        b.vy -= dy;
      }
    }

    // Attraction along edges
    for (const edge of edges) {
      const a = positions.get(edge.source);
      const b = positions.get(edge.target);
      if (!a || !b) continue;
      let dx = a.x - b.x;
      let dy = a.y - b.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const force = (dist * dist) / k;
      dx = (dx / dist) * force;
      dy = (dy / dist) * force;
      a.vx -= dx;
      a.vy -= dy;
      b.vx += dx;
      b.vy += dy;
    }

    // Mild pull toward center so disconnected nodes/components don't drift off-canvas
    for (const id of nodeIds) {
      const p = positions.get(id)!;
      p.vx += (cx - p.x) * 0.01;
      p.vy += (cy - p.y) * 0.01;
    }

    // Apply, capped by the cooling temperature
    for (const id of nodeIds) {
      const p = positions.get(id)!;
      const disp = Math.sqrt(p.vx * p.vx + p.vy * p.vy) || 0.01;
      const capped = Math.min(disp, temp);
      p.x += (p.vx / disp) * capped;
      p.y += (p.vy / disp) * capped;
      p.x = Math.max(40, Math.min(WIDTH - 40, p.x));
      p.y = Math.max(40, Math.min(HEIGHT - 40, p.y));
      p.vx = 0;
      p.vy = 0;
    }
  }

  return nodes.map((node) => {
    const p = positions.get(node.id)!;
    return { ...node, x: p.x, y: p.y };
  });
}

export default function Topology() {
  const [graph, setGraph] = useState<TopologyResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [siteFilter, setSiteFilter] = useState<string>("all");
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [selected, setSelected] = useState<TopologyNode | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    api
      .get<TopologyResponse>("/topology")
      .then((res) => setGraph(res.data))
      .catch((err) => setError(err?.response?.data?.detail || "Failed to load topology."))
      .finally(() => setLoading(false));
  }, []);

  const sites = useMemo(() => {
    if (!graph) return [];
    return Array.from(new Set(graph.nodes.map((n) => n.site || "Unassigned"))).sort();
  }, [graph]);

  const filteredNodes = useMemo(() => {
    if (!graph) return [];
    if (siteFilter === "all") return graph.nodes;
    return graph.nodes.filter((n) => (n.site || "Unassigned") === siteFilter);
  }, [graph, siteFilter]);

  const filteredNodeIds = useMemo(() => new Set(filteredNodes.map((n) => n.id)), [filteredNodes]);

  const filteredEdges = useMemo(() => {
    if (!graph) return [];
    return graph.edges.filter((e) => filteredNodeIds.has(e.source) && filteredNodeIds.has(e.target));
  }, [graph, filteredNodeIds]);

  const laidOut = useMemo(() => layoutNodes(filteredNodes, filteredEdges), [filteredNodes, filteredEdges]);
  const nodeById = useMemo(() => new Map(laidOut.map((n) => [n.id, n])), [laidOut]);

  const connectedIds = useMemo(() => {
    const s = new Set<string>();
    filteredEdges.forEach((e) => {
      s.add(e.source);
      s.add(e.target);
    });
    return s;
  }, [filteredEdges]);

  const highlightedEdgeKeys = useMemo(() => {
    if (!hoveredId) return null;
    const keys = new Set<string>();
    filteredEdges.forEach((e) => {
      if (e.source === hoveredId || e.target === hoveredId) keys.add(`${e.source}-${e.target}-${e.subnet}`);
    });
    return keys;
  }, [hoveredId, filteredEdges]);

  return (
    <div className="pb-16 flex flex-col gap-6 md:p-2">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-navy">Network Topology</h1>
          <p className="text-sm text-slate-500 mt-1 max-w-2xl">
            Devices and the links between them, inferred from interfaces sharing the same subnet across each
            device's latest config snapshot. Devices with no snapshot on file yet still appear, just without links.
          </p>
        </div>
        {sites.length > 1 && (
          <select
            value={siteFilter}
            onChange={(e) => setSiteFilter(e.target.value)}
            className="border border-slate-300 rounded-lg px-3 py-2 text-sm bg-white shadow-sm"
          >
            <option value="all">All sites</option>
            {sites.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        )}
      </div>

      {loading && <p className="text-sm text-slate-400">Loading topology...</p>}
      {error && <p className="text-sm text-riskcrit">{error}</p>}

      {!loading && !error && graph && graph.nodes.length === 0 && (
        <div className="bg-white border border-slate-200 rounded-xl p-10 text-center text-slate-400">
          No devices in inventory yet.
        </div>
      )}

      {!loading && !error && graph && graph.nodes.length > 0 && (
        <div className="grid grid-cols-1 xl:grid-cols-4 gap-6">
          <div className="xl:col-span-3 bg-white border border-slate-200 rounded-xl shadow-sm overflow-hidden">
            <svg
              ref={svgRef}
              viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
              className="w-full h-[620px]"
              onMouseLeave={() => setHoveredId(null)}
            >
              <g>
                {filteredEdges.map((e) => {
                  const a = nodeById.get(e.source);
                  const b = nodeById.get(e.target);
                  if (!a || !b) return null;
                  const key = `${e.source}-${e.target}-${e.subnet}`;
                  const active = highlightedEdgeKeys?.has(key);
                  return (
                    <g key={key}>
                      <line
                        x1={a.x}
                        y1={a.y}
                        x2={b.x}
                        y2={b.y}
                        stroke={active ? "#2563eb" : "#cbd5e1"}
                        strokeWidth={active ? 2.5 : 1.5}
                      />
                      <text
                        x={(a.x + b.x) / 2}
                        y={(a.y + b.y) / 2 - 4}
                        textAnchor="middle"
                        className="pointer-events-none select-none"
                        fontSize={10}
                        fill={active ? "#1e3a8a" : "#94a3b8"}
                        fontFamily="monospace"
                      >
                        {e.subnet}
                      </text>
                    </g>
                  );
                })}
              </g>
              <g>
                {laidOut.map((node) => {
                  const isIsolated = !connectedIds.has(node.id);
                  return (
                    <g
                      key={node.id}
                      transform={`translate(${node.x}, ${node.y})`}
                      className="cursor-pointer"
                      onMouseEnter={() => setHoveredId(node.id)}
                      onClick={() => setSelected(node)}
                    >
                      {node.flagged_unstable && (
                        <circle r={17} fill="none" stroke="#dc2626" strokeWidth={2} strokeDasharray="3 2" />
                      )}
                      <circle
                        r={13}
                        fill={statusColor[node.status] || statusColor.unknown}
                        stroke={isIsolated ? "#e2e8f0" : "#1e293b"}
                        strokeWidth={selected?.id === node.id ? 3 : 1.5}
                        opacity={isIsolated ? 0.55 : 1}
                      />
                      <text
                        y={28}
                        textAnchor="middle"
                        fontSize={11}
                        fontWeight={600}
                        fill="#1e293b"
                        className="pointer-events-none select-none"
                      >
                        {node.hostname}
                      </text>
                    </g>
                  );
                })}
              </g>
            </svg>
            <div className="flex flex-wrap gap-4 items-center px-5 py-3 border-t border-slate-100 text-xs text-slate-500">
              {Object.entries(statusColor).map(([status, color]) => (
                <span key={status} className="flex items-center gap-1.5">
                  <span className="w-2.5 h-2.5 rounded-full inline-block" style={{ backgroundColor: color }} />
                  <span className="capitalize">{status}</span>
                </span>
              ))}
              <span className="flex items-center gap-1.5">
                <span className="w-3 h-3 rounded-full inline-block border-2 border-dashed border-riskcrit" />
                Flagged unstable
              </span>
              <span className="flex items-center gap-1.5">
                <span className="w-2.5 h-2.5 rounded-full inline-block bg-slate-300 opacity-50" />
                No inferred links
              </span>
            </div>
          </div>

          <div className="bg-white border border-slate-200 rounded-xl shadow-sm p-5 h-fit">
            {selected ? (
              <div className="flex flex-col gap-3">
                <div className="flex items-center justify-between">
                  <h3 className="font-bold text-navy">{selected.hostname}</h3>
                  <button onClick={() => setSelected(null)} className="text-slate-400 hover:text-slate-600 text-sm">
                    ✕
                  </button>
                </div>
                <dl className="text-xs space-y-1.5">
                  <div className="flex justify-between">
                    <dt className="text-slate-500">Management IP</dt>
                    <dd className="font-mono text-slate-700">{selected.ip_address}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-500">Vendor</dt>
                    <dd className="text-slate-700">{vendorLabel[selected.vendor] || selected.vendor}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-500">Site</dt>
                    <dd className="text-slate-700">{selected.site || "—"}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-500">Status</dt>
                    <dd className="capitalize text-slate-700">{selected.status}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-500">Config on file</dt>
                    <dd className="text-slate-700">{selected.has_config_on_file ? "Yes" : "No"}</dd>
                  </div>
                </dl>
                {selected.flagged_unstable && (
                  <p className="text-[11px] bg-red-50 border border-red-200 text-riskcrit rounded-lg px-2.5 py-1.5">
                    Flagged unstable — automated deploys blocked pending manual review.
                  </p>
                )}
                <div>
                  <h4 className="text-[11px] font-bold uppercase text-slate-500 tracking-wide mb-1.5">
                    Inferred links ({graph.edges.filter((e) => e.source === selected.id || e.target === selected.id).length})
                  </h4>
                  <ul className="space-y-1">
                    {graph.edges
                      .filter((e) => e.source === selected.id || e.target === selected.id)
                      .map((e) => {
                        const otherId = e.source === selected.id ? e.target : e.source;
                        const other = graph.nodes.find((n) => n.id === otherId);
                        return (
                          <li
                            key={`${e.source}-${e.target}-${e.subnet}`}
                            className="text-xs bg-slate-50 border border-slate-200 rounded-lg px-2.5 py-1.5"
                          >
                            <span className="font-semibold text-slate-700">{other?.hostname || "unknown"}</span>
                            <span className="text-slate-400"> via </span>
                            <span className="font-mono text-slate-500">{e.subnet}</span>
                          </li>
                        );
                      })}
                    {graph.edges.filter((e) => e.source === selected.id || e.target === selected.id).length === 0 && (
                      <li className="text-xs text-slate-400 italic">No shared-subnet links found.</li>
                    )}
                  </ul>
                </div>
                <Link
                  to="/devices"
                  className="text-xs font-bold text-brandblue uppercase tracking-wide hover:underline mt-1"
                >
                  View in Device Inventory →
                </Link>
              </div>
            ) : (
              <div className="text-sm text-slate-400 flex flex-col items-center justify-center py-10 text-center gap-2">
                <div className="text-2xl">🗺️</div>
                <p>Click a device to see its details and inferred links.</p>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}