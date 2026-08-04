import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { TopologyResponse, TopologyNode, TopologyEdge } from "../lib/types";

const STATUS_COLOR: Record<string, string> = {
  online: "#16a34a",
  offline: "#94a3b8",
  degraded: "#d97706",
  unknown: "#cbd5e1",
};

// Mirrors app.models.device_metric.HealthColor (green/yellow/red/gray) --
// gray specifically means "device answered SNMP but resolved none of the
// health OIDs" (see that enum's docstring), distinct from "never polled"
// (null), which falls back to the plain unselected-node ring below.
const HEALTH_COLOR: Record<string, string> = {
  green: "#16a34a",
  yellow: "#d97706",
  red: "#dc2626",
  gray: "#94a3b8",
};

const VENDOR_META: Record<string, { label: string; accent: string }> = {
  cisco: { label: "Cisco", accent: "#049fd9" },
  juniper: { label: "Juniper", accent: "#84b135" },
  arista: { label: "Arista", accent: "#f5871f" },
  linux: { label: "Linux", accent: "#f7b93e" },
};

interface LaidOutNode extends TopologyNode {
  x: number;
  y: number;
}

type Selection = { kind: "node"; node: LaidOutNode } | { kind: "edge"; edge: TopologyEdge } | null;

const WIDTH = 1100;
const HEIGHT = 680;
const ITERATIONS = 420;
const MIN_SCALE = 0.4;
const MAX_SCALE = 2.5;

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

  const k = Math.sqrt((WIDTH * HEIGHT) / Math.max(n, 1));
  const nodeIds = nodes.map((nn) => nn.id);

  for (let iter = 0; iter < ITERATIONS; iter++) {
    const temp = Math.max(1, 30 * (1 - iter / ITERATIONS));

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

    for (const id of nodeIds) {
      const p = positions.get(id)!;
      p.vx += (cx - p.x) * 0.01;
      p.vy += (cy - p.y) * 0.01;
    }

    for (const id of nodeIds) {
      const p = positions.get(id)!;
      const disp = Math.sqrt(p.vx * p.vx + p.vy * p.vy) || 0.01;
      const capped = Math.min(disp, temp);
      p.x += (p.vx / disp) * capped;
      p.y += (p.vy / disp) * capped;
      p.x = Math.max(60, Math.min(WIDTH - 60, p.x));
      p.y = Math.max(60, Math.min(HEIGHT - 60, p.y));
      p.vx = 0;
      p.vy = 0;
    }
  }

  return nodes.map((node) => {
    const p = positions.get(node.id)!;
    return { ...node, x: p.x, y: p.y };
  });
}

/** Point on the line from a->b, `offset` px in from a -- used to place the
 * IP-address chip near each device rather than floating in open space,
 * the way professional topology tools (SolarWinds/PRTG/NetBrain) label
 * links: the address belongs visually to the interface it sits on. */
function pointAlong(ax: number, ay: number, bx: number, by: number, offset: number) {
  const dx = bx - ax;
  const dy = by - ay;
  const len = Math.sqrt(dx * dx + dy * dy) || 1;
  return { x: ax + (dx / len) * offset, y: ay + (dy / len) * offset };
}

function DeviceGlyph({ vendor, size = 13 }: { vendor: string; size?: number }) {
  // Small router/switch glyph -- three "ports" under a chassis bar -- kept
  // as plain SVG paths (no icon library dependency) so it renders
  // identically everywhere and scales cleanly at any zoom level.
  const w = size * 1.8;
  const h = size * 1.15;
  return (
    <g transform={`translate(${-w / 2}, ${-h / 2})`}>
      <rect x={0} y={0} width={w} height={h} rx={2.5} fill="#0f1b33" stroke="#1e293b" strokeWidth={0.5} />
      <rect x={1.5} y={1.5} width={w - 3} height={h * 0.32} rx={1} fill={VENDOR_META[vendor]?.accent || "#64748b"} />
      {[0, 1, 2].map((i) => (
        <rect
          key={i}
          x={w * 0.15 + i * (w * 0.28)}
          y={h * 0.62}
          width={w * 0.16}
          height={h * 0.22}
          rx={0.6}
          fill="#38bdf8"
          opacity={0.85}
        />
      ))}
    </g>
  );
}

export default function Topology() {
  const [graph, setGraph] = useState<TopologyResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [siteFilter, setSiteFilter] = useState<string>("all");
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [selection, setSelection] = useState<Selection>(null);
  const [showIpLabels, setShowIpLabels] = useState(true);
  const svgRef = useRef<SVGSVGElement>(null);

  // --- pan / zoom -----------------------------------------------------
  const [view, setView] = useState({ x: 0, y: 0, scale: 1 });
  const dragState = useRef<{ startX: number; startY: number; viewX: number; viewY: number } | null>(null);

  const onWheel = (e: React.WheelEvent) => {
    e.preventDefault();
    const delta = -e.deltaY * 0.0015;
    setView((v) => {
      const nextScale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, v.scale * (1 + delta)));
      return { ...v, scale: nextScale };
    });
  };
  const onPointerDown = (e: React.PointerEvent) => {
    dragState.current = { startX: e.clientX, startY: e.clientY, viewX: view.x, viewY: view.y };
  };
  const onPointerMove = (e: React.PointerEvent) => {
    if (!dragState.current) return;
    const dx = (e.clientX - dragState.current.startX) / view.scale;
    const dy = (e.clientY - dragState.current.startY) / view.scale;
    setView((v) => ({ ...v, x: dragState.current!.viewX - dx, y: dragState.current!.viewY - dy }));
  };
  const onPointerUp = () => {
    dragState.current = null;
  };
  const resetView = () => setView({ x: 0, y: 0, scale: 1 });
  const zoomBy = (factor: number) =>
    setView((v) => ({ ...v, scale: Math.min(MAX_SCALE, Math.max(MIN_SCALE, v.scale * factor)) }));

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

  const edgeKey = (e: TopologyEdge) => `${e.source}-${e.target}-${e.subnet}`;

  const highlightedEdgeKeys = useMemo(() => {
    if (!hoveredId) return null;
    const keys = new Set<string>();
    filteredEdges.forEach((e) => {
      if (e.source === hoveredId || e.target === hoveredId) keys.add(edgeKey(e));
    });
    return keys;
  }, [hoveredId, filteredEdges]);

  const selectedNodeId = selection?.kind === "node" ? selection.node.id : null;
  const selectedEdgeKey = selection?.kind === "edge" ? edgeKey(selection.edge) : null;

  const linkCountFor = (nodeId: string) => graph?.edges.filter((e) => e.source === nodeId || e.target === nodeId).length ?? 0;

  return (
    <div className="pb-16 flex flex-col gap-6 md:p-2">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-navy">Network Topology</h1>
          <p className="text-sm text-slate-500 mt-1 max-w-2xl">
            Devices and the links between them — green badges are confirmed links (LLDP/CDP neighbor discovery
            via SNMP, or imported GNS3 lab wiring), gray labels are inferred from interfaces sharing the same
            subnet. Click a device or a link for details. Scroll to zoom, drag to pan. Run Discovery on a
            device to add confirmed links here.
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <label className="flex items-center gap-1.5 text-xs text-slate-500 bg-white border border-slate-200 rounded-lg px-2.5 py-2 shadow-sm">
            <input type="checkbox" checked={showIpLabels} onChange={(e) => setShowIpLabels(e.target.checked)} />
            Show interface IPs
          </label>
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
          <div className="xl:col-span-3 bg-white border border-slate-200 rounded-xl shadow-sm overflow-hidden relative">
            {/* Zoom controls -- top-right overlay, like any real network-diagram tool */}
            <div className="absolute top-3 right-3 z-10 flex flex-col gap-1 bg-white/95 backdrop-blur border border-slate-200 rounded-lg shadow-sm overflow-hidden">
              <button
                onClick={() => zoomBy(1.25)}
                className="w-8 h-8 flex items-center justify-center text-slate-600 hover:bg-slate-50 text-base font-bold border-b border-slate-100"
                title="Zoom in"
              >
                +
              </button>
              <button
                onClick={() => zoomBy(0.8)}
                className="w-8 h-8 flex items-center justify-center text-slate-600 hover:bg-slate-50 text-base font-bold border-b border-slate-100"
                title="Zoom out"
              >
                −
              </button>
              <button
                onClick={resetView}
                className="w-8 h-8 flex items-center justify-center text-slate-500 hover:bg-slate-50 text-[10px] font-bold"
                title="Reset view"
              >
                ⟲
              </button>
            </div>

            <svg
              ref={svgRef}
              viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
              className="w-full h-[680px] bg-[radial-gradient(circle_at_1px_1px,#e2e8f0_1px,transparent_0)] [background-size:22px_22px] cursor-grab active:cursor-grabbing"
              onMouseLeave={() => setHoveredId(null)}
              onWheel={onWheel}
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={onPointerUp}
              onPointerLeave={onPointerUp}
            >
              <g
                transform={`scale(${view.scale}) translate(${-view.x}, ${-view.y})`}
                style={{ transformOrigin: `${WIDTH / 2}px ${HEIGHT / 2}px` }}
              >
                {/* Links */}
                <g>
                  {filteredEdges.map((e) => {
                    const a = nodeById.get(e.source);
                    const b = nodeById.get(e.target);
                    if (!a || !b) return null;
                    const key = edgeKey(e);
                    const active = highlightedEdgeKeys?.has(key) || selectedEdgeKey === key;
                    const srcLabelPos = pointAlong(a.x, a.y, b.x, b.y, 30);
                    const tgtLabelPos = pointAlong(b.x, b.y, a.x, a.y, 30);
                    const midX = (a.x + b.x) / 2;
                    const midY = (a.y + b.y) / 2;
                    const hasIps = Boolean(e.source_ip && e.target_ip);
                    const linkLabel =
                      e.link_source === "subnet"
                        ? e.subnet || ""
                        : e.link_source.toUpperCase(); // "LLDP" / "CDP" -- confirmed neighbor, no subnet to show
                    return (
                      <g key={key} className="cursor-pointer" onClick={() => setSelection({ kind: "edge", edge: e })}>
                        {/* fat invisible hit-area so thin lines are easy to click */}
                        <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke="transparent" strokeWidth={14} />
                        <line
                          x1={a.x}
                          y1={a.y}
                          x2={b.x}
                          y2={b.y}
                          stroke={active ? "#2563eb" : "#94a3b8"}
                          strokeWidth={active ? 2.75 : 1.5}
                          strokeDasharray={active ? undefined : undefined}
                        />
                        {/* subnet / link-source label at midpoint */}
                        <rect
                          x={midX - linkLabel.length * 3.1 - 4}
                          y={midY - 15}
                          width={linkLabel.length * 6.2 + 8}
                          height={13}
                          rx={3}
                          fill={
                            e.link_source !== "subnet" ? "#dcfce7" : active ? "#dbeafe" : "#f1f5f9"
                          }
                          opacity={0.95}
                        />
                        <text
                          x={midX}
                          y={midY - 5.5}
                          textAnchor="middle"
                          className="pointer-events-none select-none"
                          fontSize={9.5}
                          fontWeight={600}
                          fill={e.link_source !== "subnet" ? "#166534" : active ? "#1e3a8a" : "#64748b"}
                          fontFamily="ui-monospace, monospace"
                        >
                          {linkLabel}
                        </text>

                        {/* per-endpoint interface IP chips -- only meaningful for subnet-inferred edges */}
                        {showIpLabels && hasIps && (
                          <>
                            <g transform={`translate(${srcLabelPos.x}, ${srcLabelPos.y})`}>
                              <rect
                                x={-String(e.source_ip).length * 2.9 - 3}
                                y={-7}
                                width={String(e.source_ip).length * 5.8 + 6}
                                height={12}
                                rx={2.5}
                                fill="#0f1b33"
                                opacity={active ? 0.95 : 0.75}
                              />
                              <text
                                textAnchor="middle"
                                y={2}
                                fontSize={8.5}
                                fill="#7dd3fc"
                                fontFamily="ui-monospace, monospace"
                                className="pointer-events-none select-none"
                              >
                                {e.source_ip}
                              </text>
                            </g>
                            <g transform={`translate(${tgtLabelPos.x}, ${tgtLabelPos.y})`}>
                              <rect
                                x={-String(e.target_ip).length * 2.9 - 3}
                                y={-7}
                                width={String(e.target_ip).length * 5.8 + 6}
                                height={12}
                                rx={2.5}
                                fill="#0f1b33"
                                opacity={active ? 0.95 : 0.75}
                              />
                              <text
                                textAnchor="middle"
                                y={2}
                                fontSize={8.5}
                                fill="#7dd3fc"
                                fontFamily="ui-monospace, monospace"
                                className="pointer-events-none select-none"
                              >
                                {e.target_ip}
                              </text>
                            </g>
                          </>
                        )}
                      </g>
                    );
                  })}
                </g>

                {/* Nodes */}
                <g>
                  {laidOut.map((node) => {
                    const isIsolated = !connectedIds.has(node.id);
                    const isSelected = selectedNodeId === node.id;
                    const vendorMeta = VENDOR_META[node.vendor] || { label: node.vendor, accent: "#64748b" };
                    return (
                      <g
                        key={node.id}
                        transform={`translate(${node.x}, ${node.y})`}
                        className="cursor-pointer"
                        onMouseEnter={() => setHoveredId(node.id)}
                        onClick={() => setSelection({ kind: "node", node })}
                      >
                        {node.flagged_unstable && (
                          <circle r={22} fill="none" stroke="#dc2626" strokeWidth={1.75} strokeDasharray="3 2" />
                        )}
                        {node.status === "online" && (
                          <circle r={19} fill="none" stroke={STATUS_COLOR.online} strokeWidth={1} opacity={0.35}>
                            <animate attributeName="r" values="15;22;15" dur="2.4s" repeatCount="indefinite" />
                            <animate attributeName="opacity" values="0.45;0;0.45" dur="2.4s" repeatCount="indefinite" />
                          </circle>
                        )}
                        <circle
                          r={17}
                          fill="white"
                          stroke={isSelected ? "#2563eb" : node.health_color ? HEALTH_COLOR[node.health_color] : "#e2e8f0"}
                          strokeWidth={isSelected ? 2.5 : node.health_color ? 2.5 : 1}
                          opacity={isIsolated ? 0.6 : 1}
                        />
                        <DeviceGlyph vendor={node.vendor} size={13} />
                        <circle
                          cx={12}
                          cy={-12}
                          r={4}
                          fill={STATUS_COLOR[node.status] || STATUS_COLOR.unknown}
                          stroke="white"
                          strokeWidth={1.5}
                        />
                        <text
                          y={32}
                          textAnchor="middle"
                          fontSize={11}
                          fontWeight={700}
                          fill="#1e293b"
                          className="pointer-events-none select-none"
                        >
                          {node.hostname}
                        </text>
                        <text
                          y={44}
                          textAnchor="middle"
                          fontSize={9}
                          fill="#94a3b8"
                          fontFamily="ui-monospace, monospace"
                          className="pointer-events-none select-none"
                        >
                          {node.ip_address}
                        </text>
                        <text
                          x={-24}
                          y={4}
                          textAnchor="end"
                          fontSize={8}
                          fontWeight={700}
                          fill={vendorMeta.accent}
                          className="pointer-events-none select-none uppercase tracking-wide"
                        >
                          {vendorMeta.label}
                        </text>
                      </g>
                    );
                  })}
                </g>
              </g>
            </svg>

            <div className="flex flex-wrap gap-4 items-center px-5 py-3 border-t border-slate-100 text-xs text-slate-500">
              <span className="font-bold text-slate-400 uppercase tracking-wide text-[10px]">Status</span>
              {Object.entries(STATUS_COLOR).map(([status, color]) => (
                <span key={status} className="flex items-center gap-1.5">
                  <span className="w-2.5 h-2.5 rounded-full inline-block" style={{ backgroundColor: color }} />
                  <span className="capitalize">{status}</span>
                </span>
              ))}
              <span className="w-px h-3 bg-slate-200 mx-1" />
              <span className="font-bold text-slate-400 uppercase tracking-wide text-[10px]">Health ring</span>
              {Object.entries(HEALTH_COLOR).map(([health, color]) => (
                <span key={health} className="flex items-center gap-1.5">
                  <span className="w-2.5 h-2.5 rounded-full inline-block border-2" style={{ borderColor: color, backgroundColor: "white" }} />
                  <span className="capitalize">{health}</span>
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
              <span className="flex items-center gap-1.5 ml-auto text-slate-400">
                {filteredNodes.length} device{filteredNodes.length === 1 ? "" : "s"} · {filteredEdges.length} link
                {filteredEdges.length === 1 ? "" : "s"}
              </span>
            </div>
          </div>

          <div className="bg-white border border-slate-200 rounded-xl shadow-sm p-5 h-fit sticky top-4">
            {selection?.kind === "node" && (
              <div className="flex flex-col gap-3">
                <div className="flex items-center justify-between">
                  <h3 className="font-bold text-navy">{selection.node.hostname}</h3>
                  <button onClick={() => setSelection(null)} className="text-slate-400 hover:text-slate-600 text-sm">
                    ✕
                  </button>
                </div>
                <dl className="text-xs space-y-1.5">
                  <div className="flex justify-between">
                    <dt className="text-slate-500">Management IP</dt>
                    <dd className="font-mono text-slate-700">{selection.node.ip_address}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-500">Vendor</dt>
                    <dd className="text-slate-700">{VENDOR_META[selection.node.vendor]?.label || selection.node.vendor}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-500">Site</dt>
                    <dd className="text-slate-700">{selection.node.site || "—"}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-500">Status</dt>
                    <dd className="capitalize text-slate-700">{selection.node.status}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-500">Health</dt>
                    <dd className="capitalize text-slate-700">
                      {selection.node.health_score !== null
                        ? `${selection.node.health_score}/100 (${selection.node.health_color})`
                        : "Not SNMP-polled"}
                    </dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-500">Config on file</dt>
                    <dd className="text-slate-700">{selection.node.has_config_on_file ? "Yes" : "No"}</dd>
                  </div>
                </dl>
                {selection.node.flagged_unstable && (
                  <p className="text-[11px] bg-red-50 border border-red-200 text-riskcrit rounded-lg px-2.5 py-1.5">
                    Flagged unstable — automated deploys blocked pending manual review.
                  </p>
                )}
                <div>
                  <h4 className="text-[11px] font-bold uppercase text-slate-500 tracking-wide mb-1.5">
                    Links ({linkCountFor(selection.node.id)})
                  </h4>
                  <ul className="space-y-1.5">
                    {graph.edges
                      .filter((e) => e.source === selection.node.id || e.target === selection.node.id)
                      .map((e) => {
                        const isSource = e.source === selection.node.id;
                        const otherId = isSource ? e.target : e.source;
                        const other = graph.nodes.find((n) => n.id === otherId);
                        const localIp = isSource ? e.source_ip : e.target_ip;
                        const remoteIp = isSource ? e.target_ip : e.source_ip;
                        return (
                          <li
                            key={`${e.source}-${e.target}-${e.subnet}`}
                            className="text-xs bg-slate-50 border border-slate-200 rounded-lg px-2.5 py-1.5 cursor-pointer hover:border-blue-300"
                            onClick={() => setSelection({ kind: "edge", edge: e })}
                          >
                            <div className="flex items-center justify-between">
                              <span className="font-semibold text-slate-700">{other?.hostname || "unknown"}</span>
                              <span
                                className={`font-mono text-[10px] ${
                                  e.link_source !== "subnet" ? "text-green-700 font-bold" : "text-slate-400"
                                }`}
                              >
                                {e.link_source !== "subnet" ? e.link_source.toUpperCase() : e.subnet}
                              </span>
                            </div>
                            {(localIp || remoteIp) && (
                              <div className="flex items-center gap-1.5 mt-1 font-mono text-[10px] text-slate-500">
                                <span className="bg-white border border-slate-200 rounded px-1.5 py-0.5">{localIp || "—"}</span>
                                <span className="text-slate-300">↔</span>
                                <span className="bg-white border border-slate-200 rounded px-1.5 py-0.5">{remoteIp || "—"}</span>
                              </div>
                            )}
                          </li>
                        );
                      })}
                    {linkCountFor(selection.node.id) === 0 && (
                      <li className="text-xs text-slate-400 italic">No links found (no shared subnet, confirmed LLDP/CDP neighbor, or GNS3 wiring).</li>
                    )}
                  </ul>
                </div>
                <Link to="/devices" className="text-xs font-bold text-brandblue uppercase tracking-wide hover:underline mt-1">
                  View in Device Inventory →
                </Link>
              </div>
            )}

            {selection?.kind === "edge" &&
              (() => {
                const e = selection.edge;
                const src = graph.nodes.find((n) => n.id === e.source);
                const tgt = graph.nodes.find((n) => n.id === e.target);
                return (
                  <div className="flex flex-col gap-3">
                    <div className="flex items-center justify-between">
                      <h3 className="font-bold text-navy">Link Detail</h3>
                      <button onClick={() => setSelection(null)} className="text-slate-400 hover:text-slate-600 text-sm">
                        ✕
                      </button>
                    </div>
                    <div className="bg-slate-50 border border-slate-200 rounded-lg p-3 space-y-2.5">
                      <div>
                        <p className="text-[10px] font-bold uppercase text-slate-400 tracking-wide">Endpoint A</p>
                        <p className="text-sm font-semibold text-slate-700">{src?.hostname || "unknown"}</p>
                        {e.source_ip && <p className="text-xs font-mono text-brandblue">{e.source_ip}</p>}
                        {e.local_port && <p className="text-[10px] font-mono text-slate-400">port {e.local_port}</p>}
                      </div>
                      <div className="flex items-center gap-2 text-slate-300">
                        <div className="flex-1 border-t border-dashed border-slate-300" />
                        <span className="text-[10px]">{e.link_source === "subnet" ? "shared subnet" : `${e.link_source.toUpperCase()} confirmed`}</span>
                        <div className="flex-1 border-t border-dashed border-slate-300" />
                      </div>
                      <div>
                        <p className="text-[10px] font-bold uppercase text-slate-400 tracking-wide">Endpoint B</p>
                        <p className="text-sm font-semibold text-slate-700">{tgt?.hostname || "unknown"}</p>
                        {e.target_ip && <p className="text-xs font-mono text-brandblue">{e.target_ip}</p>}
                        {e.neighbor_port && <p className="text-[10px] font-mono text-slate-400">port {e.neighbor_port}</p>}
                      </div>
                    </div>
                    <dl className="text-xs space-y-1.5">
                      <div className="flex justify-between">
                        <dt className="text-slate-500">{e.link_source === "subnet" ? "Subnet" : "Discovered via"}</dt>
                        <dd className="font-mono text-slate-700">{e.link_source === "subnet" ? e.subnet : e.link_source.toUpperCase()}</dd>
                      </div>
                    </dl>
                    <p className="text-[11px] text-slate-400">
                      {e.link_source === "subnet"
                        ? "Inferred because both devices have an interface configured into this subnet in their latest config snapshot."
                        : `Confirmed by ${e.link_source.toUpperCase()} neighbor discovery — this is a real reported adjacency, not a guess.`}
                    </p>
                    <div className="flex gap-3">
                      {src && (
                        <button
                          onClick={() => setSelection({ kind: "node", node: nodeById.get(src.id) || (src as LaidOutNode) })}
                          className="text-xs font-bold text-brandblue uppercase tracking-wide hover:underline"
                        >
                          {src.hostname} →
                        </button>
                      )}
                      {tgt && (
                        <button
                          onClick={() => setSelection({ kind: "node", node: nodeById.get(tgt.id) || (tgt as LaidOutNode) })}
                          className="text-xs font-bold text-brandblue uppercase tracking-wide hover:underline"
                        >
                          {tgt.hostname} →
                        </button>
                      )}
                    </div>
                  </div>
                );
              })()}

            {!selection && (
              <div className="text-sm text-slate-400 flex flex-col items-center justify-center py-10 text-center gap-2">
                <div className="text-2xl">🗺️</div>
                <p>Click a device or a link to see its details.</p>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}