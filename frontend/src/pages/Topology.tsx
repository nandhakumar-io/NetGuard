import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, getAccessToken } from "../lib/api";
import {
  TopologyResponse,
  TopologyNode,
  TopologyEdge,
  TopologySnapshotSummary,
  TopologyDiff,
  DataCenterGroup,
  InterfaceCurrentStatus,
  InterfaceStatusHistoryEntry,
  DeviceMetric,
} from "../lib/types";
import Sparkline from "../components/Sparkline";

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
        const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
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

// Canonical role -> tier ordering for the layered layout (core at the top,
// access at the bottom, mirroring how core/distribution/access is always
// drawn on a whiteboard). Any device_role string not in this map -- or no
// role at all -- falls into its own "Unassigned" tier below everything
// else, so devices are never silently dropped just because nobody's
// gotten around to tagging their role yet.
const ROLE_TIERS = ["core", "distribution", "access"];

/** Layered/hierarchical layout: one horizontal row per role tier
 * (core -> distribution -> access -> unassigned), nodes spread evenly
 * left-to-right within their row. Deterministic and non-iterative (unlike
 * the force-directed layout above) -- for troubleshooting, "core is
 * always on top" matters more than minimizing edge crossings. */
function layoutNodesLayered(nodes: TopologyNode[]): LaidOutNode[] {
  if (nodes.length === 0) return [];

  const tiers: string[] = [...ROLE_TIERS, "__unassigned__"];
  const byTier = new Map<string, TopologyNode[]>(tiers.map((t) => [t, []]));
  for (const node of nodes) {
    const role = (node.device_role || "").toLowerCase().trim();
    const tier = ROLE_TIERS.includes(role) ? role : "__unassigned__";
    byTier.get(tier)!.push(node);
  }

  const activeTiers = tiers.filter((t) => (byTier.get(t)?.length ?? 0) > 0);
  const rowHeight = HEIGHT / Math.max(activeTiers.length, 1);

  const out: LaidOutNode[] = [];
  activeTiers.forEach((tier, rowIndex) => {
    const rowNodes = byTier.get(tier)!;
    const y = rowHeight * (rowIndex + 0.5);
    const colWidth = WIDTH / (rowNodes.length + 1);
    rowNodes.forEach((node, colIndex) => {
      out.push({ ...node, x: colWidth * (colIndex + 1), y });
    });
  });
  return out;
}

function roleTierLabel(tier: string): string {
  if (tier === "__unassigned__") return "Unassigned role";
  return tier.charAt(0).toUpperCase() + tier.slice(1);
}

/** Green -> amber -> red for a 0-100 utilization reading, matching the
 * IFACE_UTIL_WARN_PCT/IFACE_UTIL_CRIT_PCT bands SNMP health-scoring uses
 * server-side (app.services.snmp_service): <60 ok, 60-79 warn, 80+ hot --
 * this is the heatmap the Topology page's "color links by utilization"
 * toggle draws from, so a link glows red the moment it crosses the same
 * 80% line that would already be flagging that device's health card.
 * Returns null (caller falls back to the default link color) when
 * there's no reading -- an untagged link is "unknown", not "green". */
function utilizationColor(pct: number | null | undefined): string | null {
  if (pct === null || pct === undefined) return null;
  if (pct >= 80) return "#dc2626";
  if (pct >= 60) return "#d97706";
  return "#16a34a";
}

/** Amber -> red for a node's interface-error badge: any errors at all is
 * worth flagging, IFACE_ERRORS_WARN (100, matching snmp_service's own
 * "warning" finding threshold) is where it goes red-hot. */
function interfaceErrorColor(count: number): string {
  return count >= 100 ? "#dc2626" : "#d97706";
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
  const [nodeSearch, setNodeSearch] = useState<string>("");
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  // Custom hover tooltip for links -- the browser's native <title> tooltip
  // is slow to appear and easy to miss when several links are drawn close
  // together, so a link hover also shows a small styled popup near the
  // cursor with the same info the click-to-select detail panel shows,
  // without requiring a click. Screen-space (clientX/clientY relative to
  // the diagram container), not SVG-space, since it's a plain absolutely-
  // positioned div overlay, not part of the SVG.
  const [hoveredEdgeTip, setHoveredEdgeTip] = useState<{ edge: TopologyEdge; x: number; y: number } | null>(null);
  const diagramContainerRef = useRef<HTMLDivElement>(null);
  const [selection, setSelection] = useState<Selection>(null);
  // Blast-radius highlighting: fetched on demand for the selected node
  // (see the "Blast Radius" panel in the node detail sidebar). Reuses the
  // same GET /change-requests/blast-radius endpoint the New Change
  // Request form's preview calls, so "what would deploying to this
  // device touch" is answered identically in both places.
  const [blastRadiusFor, setBlastRadiusFor] = useState<string | null>(null);
  const [blastRadiusLoading, setBlastRadiusLoading] = useState(false);
  const [blastRadiusError, setBlastRadiusError] = useState<string | null>(null);
  const [blastRadiusResult, setBlastRadiusResult] = useState<{
    touched_count: number;
    touched_core_count: number;
    dependent_count: number;
    dependent_device_ids: string[];
  } | null>(null);
  const [showIpLabels, setShowIpLabels] = useState(true);
  // Live NOC wall-display feed (GET /topology/ws) -- pushes the graph the
  // instant a device status changes, moves rack/DC, or gets added/removed,
  // instead of requiring a manual refresh. Defaults on; a user actively
  // dragging/inspecting nodes can pause it so the layout doesn't jump
  // under their cursor mid-interaction.
  const [liveMode, setLiveMode] = useState(true);
  const [liveStatus, setLiveStatus] = useState<"connecting" | "live" | "offline">("connecting");
  const [lastLiveUpdate, setLastLiveUpdate] = useState<Date | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  // --- view mode: force-directed graph vs. data center / rack grouping ---
  const [viewMode, setViewMode] = useState<"graph" | "layered" | "groups">("graph");
  const [colorByUtilization, setColorByUtilization] = useState(false);
  // Badges each node with its interface_error_rate reading (errors seen
  // since the previous SNMP poll) directly on the map -- the node-level
  // counterpart to colorByUtilization's link heatmap. Off by default so a
  // healthy fleet's map isn't cluttered with "0" badges.
  const [showErrorBadges, setShowErrorBadges] = useState(false);
  // Alert overlay: independently toggleable layer that pulses links red
  // where either endpoint has an active (unresolved, non-suppressed)
  // alert, and rings the device itself -- so "what's currently on fire"
  // reads directly off the map without cross-referencing the Alerts page.
  // Defaults on since it's the highest-signal overlay for troubleshooting.
  // Off by default -- this overlay paints a link red the moment *either*
  // endpoint device has *any* active warning/critical alert, regardless
  // of that link's own state (a device's unrelated CPU/disk alert used
  // to turn every link touching it red, even idle/healthy ones). That's
  // useful as an opt-in "what's near an incident" view, but shouldn't be
  // the default lens a healthy link is seen through -- links should read
  // their own status/utilization color by default; alertOverlay is now
  // an explicit toggle for layering incidents on top of that.
  const [alertOverlay, setAlertOverlay] = useState(false);

  // --- Path highlight: click two devices, trace the path between them ---
  // Reuses the existing /path-trace endpoint (same one that powers the
  // standalone Path Trace page) rather than re-deriving a route client
  // side, so results honor whatever hop source (real traceroute vs.
  // topology-graph BFS fallback) that service picks.
  const [pathMode, setPathMode] = useState(false);
  const [pathSourceId, setPathSourceId] = useState<string | null>(null);
  const [pathTargetId, setPathTargetId] = useState<string | null>(null);
  const [pathTraceLoading, setPathTraceLoading] = useState(false);
  const [pathTraceError, setPathTraceError] = useState<string | null>(null);
  const [pathTraceResult, setPathTraceResult] = useState<{ hops: { device_id: string | null; hostname: string | null; ip_address: string | null; status: string }[]; reached_target: boolean; hop_source: string } | null>(null);

  const togglePathMode = () => {
    setPathMode((v) => !v);
    setPathSourceId(null);
    setPathTargetId(null);
    setPathTraceResult(null);
    setPathTraceError(null);
  };

  const runPathTrace = (sourceId: string, targetId: string) => {
    setPathTraceLoading(true);
    setPathTraceError(null);
    setPathTraceResult(null);
    api
      .post("/path-trace", { source_device_id: sourceId, target_device_id: targetId })
      .then((res) => setPathTraceResult(res.data))
      .catch((err) => setPathTraceError(err?.response?.data?.detail || "Path trace failed."))
      .finally(() => setPathTraceLoading(false));
  };

  const handlePathClick = (nodeId: string) => {
    if (!pathSourceId) {
      setPathSourceId(nodeId);
      return;
    }
    if (!pathTargetId && nodeId !== pathSourceId) {
      setPathTargetId(nodeId);
      runPathTrace(pathSourceId, nodeId);
    }
  };

  // Device ids the traced path actually passed through (in order), used
  // to highlight both the nodes and the edges between consecutive hops.
  const pathDeviceIds = useMemo(() => {
    if (!pathTraceResult) return [];
    return pathTraceResult.hops.map((h) => h.device_id).filter((id): id is string => Boolean(id));
  }, [pathTraceResult]);

  const pathEdgeKeySet = useMemo(() => {
    const s = new Set<string>();
    for (let i = 0; i < pathDeviceIds.length - 1; i++) {
      s.add(`${pathDeviceIds[i]}|${pathDeviceIds[i + 1]}`);
      s.add(`${pathDeviceIds[i + 1]}|${pathDeviceIds[i]}`);
    }
    return s;
  }, [pathDeviceIds]);
  const [groups, setGroups] = useState<DataCenterGroup[] | null>(null);
  const [groupsLoading, setGroupsLoading] = useState(false);
  const [groupsError, setGroupsError] = useState<string | null>(null);

  const loadGroups = () => {
    setGroupsLoading(true);
    setGroupsError(null);
    api
      .get<DataCenterGroup[]>("/devices/groups/summary")
      .then((res) => setGroups(res.data))
      .catch(() => setGroupsError("Failed to load device groups."))
      .finally(() => setGroupsLoading(false));
  };

  useEffect(() => {
    // Refetch every time the groups view is switched into, not just the
    // first time -- previously this only loaded once per page visit
    // (guarded on `groups === null`), so the datacenter/rack view kept
    // showing whatever was true at first click even after devices moved
    // racks, were added/removed, or changed status. Also re-fires on a
    // live topology push while the groups view is the active tab, so it
    // doesn't need a manual switch-away-and-back to catch up.
    if (viewMode === "groups") {
      loadGroups();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewMode, lastLiveUpdate]);

  // --- selected device's per-interface (port) status + short metrics
  // history -- fetched lazily whenever a node is selected, powers the
  // "Interfaces" and "Recent metrics" tabs in the detail panel. ---
  const [detailTab, setDetailTab] = useState<"overview" | "interfaces" | "metrics">("overview");
  const [ifaces, setIfaces] = useState<InterfaceCurrentStatus[] | null>(null);
  const [ifacesLoading, setIfacesLoading] = useState(false);
  const [ifaceHistory, setIfaceHistory] = useState<InterfaceStatusHistoryEntry[] | null>(null);
  const [ifaceHistoryFor, setIfaceHistoryFor] = useState<string | null>(null);
  const [metricHistory, setMetricHistory] = useState<DeviceMetric[] | null>(null);
  const [metricHistoryLoading, setMetricHistoryLoading] = useState(false);

  useEffect(() => {
    setDetailTab("overview");
    setIfaces(null);
    setIfaceHistory(null);
    setIfaceHistoryFor(null);
    setMetricHistory(null);
    if (selection?.kind !== "node") return;
    const deviceId = selection.node.id;

    setIfacesLoading(true);
    api
      .get<InterfaceCurrentStatus[]>(`/devices/${deviceId}/interfaces`)
      .then((res) => setIfaces(res.data))
      .catch(() => setIfaces([]))
      .finally(() => setIfacesLoading(false));

    setMetricHistoryLoading(true);
    api
      .get<DeviceMetric[]>(`/devices/${deviceId}/metrics/history?hours=24&limit=200`)
      .then((res) => setMetricHistory(res.data))
      .catch(() => setMetricHistory([]))
      .finally(() => setMetricHistoryLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selection?.kind === "node" ? selection.node.id : null]);

  const loadIfaceHistory = (deviceId: string, ifIndex: string) => {
    setIfaceHistoryFor(ifIndex);
    api
      .get<InterfaceStatusHistoryEntry[]>(`/devices/${deviceId}/interfaces/history?if_index=${encodeURIComponent(ifIndex)}&limit=50`)
      .then((res) => setIfaceHistory(res.data))
      .catch(() => setIfaceHistory([]));
  };

  // --- history / diffing ------------------------------------------------
  const [showDiff, setShowDiff] = useState(false);
  const [snapshots, setSnapshots] = useState<TopologySnapshotSummary[]>([]);
  const [diff, setDiff] = useState<TopologyDiff | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);
  const [diffError, setDiffError] = useState<string | null>(null);
  const [olderId, setOlderId] = useState<string>("");
  const [newerId, setNewerId] = useState<string>("");

  const loadSnapshots = () => {
    api
      .get<TopologySnapshotSummary[]>("/topology/snapshots")
      .then((res) => setSnapshots(res.data))
      .catch(() => {});
  };

  const loadDiff = (params?: { older_id?: string; newer_id?: string }) => {
    setDiffLoading(true);
    setDiffError(null);
    const qs = new URLSearchParams();
    if (params?.older_id) qs.set("older_id", params.older_id);
    if (params?.newer_id) qs.set("newer_id", params.newer_id);
    api
      .get<TopologyDiff>(`/topology/diff?${qs.toString()}`)
      .then((res) => {
        setDiff(res.data);
        setOlderId(res.data.older_snapshot_id);
        setNewerId(res.data.newer_snapshot_id);
      })
      .catch((err) =>
        setDiffError(
          err?.response?.data?.detail ||
            "Not enough snapshot history yet -- a snapshot is captured automatically once a day."
        )
      )
      .finally(() => setDiffLoading(false));
  };

  const captureSnapshotNow = () => {
    api
      .post<TopologySnapshotSummary>("/topology/snapshots")
      .then(() => {
        loadSnapshots();
      })
      .catch(() => {});
  };

  const openDiffPanel = () => {
    setShowDiff(true);
    loadSnapshots();
    loadDiff();
  };

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

  // Live push feed. Reconnects with backoff on drop (network blip, API
  // restart) rather than giving up silently -- a wall display left
  // running overnight should recover on its own. REST fetch above still
  // owns first paint (and remains the fallback if websockets are blocked
  // by a proxy); this effect only takes over afterward.
  useEffect(() => {
    if (!liveMode) {
      setLiveStatus("offline");
      return;
    }

    let cancelled = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;

    const connect = () => {
      if (cancelled) return;
      setLiveStatus("connecting");
      const httpBase = api.defaults.baseURL || window.location.origin;
      const wsUrl =
        httpBase.replace(/^http/, "ws").replace(/\/$/, "") +
        "/topology/ws?token=" +
        encodeURIComponent(getAccessToken() || "");
      const ws = new WebSocket(wsUrl);
      socket = ws;

      ws.onopen = () => {
        attempt = 0;
        setLiveStatus("live");
      };
      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === "topology_snapshot" && msg.data) {
            setGraph(msg.data);
            setError(null);
            setLastLiveUpdate(new Date());
          }
        } catch {
          // ignore malformed frame
        }
      };
      ws.onclose = () => {
        if (cancelled) return;
        setLiveStatus("offline");
        attempt += 1;
        const delay = Math.min(15000, 1000 * 2 ** attempt);
        reconnectTimer = setTimeout(connect, delay);
      };
      ws.onerror = () => {
        ws.close();
      };
    };

    connect();

    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, [liveMode]);

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

  // "Link" (edge) count collapses every physical cable between the same
  // device pair into one line on the map -- this is the actual count of
  // real cables (LLDP/CDP/GNS3 members), shown alongside the link count
  // in the legend so a trunk/bundle doesn't quietly undercount how many
  // physical connections are really there.
  const totalMemberLinks = useMemo(
    () => filteredEdges.reduce((sum, e) => sum + (e.members && e.members.length > 0 ? e.members.length : 1), 0),
    [filteredEdges]
  );

  const laidOut = useMemo(
    () => (viewMode === "layered" ? layoutNodesLayered(filteredNodes) : layoutNodes(filteredNodes, filteredEdges)),
    [filteredNodes, filteredEdges, viewMode]
  );
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

  // Hovering a device highlights it and every neighbor it has a link to
  // (not just the links themselves) -- the single most useful thing for
  // "why can't A reach B" troubleshooting: sweep the mouse across the map
  // and immediately see each device's blast radius of direct neighbors.
  const hoveredNeighborIds = useMemo(() => {
    if (!hoveredId) return null;
    const s = new Set<string>([hoveredId]);
    filteredEdges.forEach((e) => {
      if (e.source === hoveredId) s.add(e.target);
      if (e.target === hoveredId) s.add(e.source);
    });
    return s;
  }, [hoveredId, filteredEdges]);

  const nodeAlertSeverity = (nodeId: string): string | null | undefined => nodeById.get(nodeId)?.active_alert_severity;

  const edgeHasActiveAlert = (e: TopologyEdge) => {
    const a = nodeAlertSeverity(e.source);
    const b = nodeAlertSeverity(e.target);
    return a === "critical" || a === "warning" || b === "critical" || b === "warning";
  };

  const selectedNodeId = selection?.kind === "node" ? selection.node.id : null;
  const selectedEdgeKey = selection?.kind === "edge" ? edgeKey(selection.edge) : null;

  // Clear any blast-radius result whenever the selection changes so a
  // stale highlight from a previously-inspected device doesn't linger.
  useEffect(() => {
    setBlastRadiusFor(null);
    setBlastRadiusResult(null);
    setBlastRadiusError(null);
  }, [selectedNodeId]);

  const fetchBlastRadius = (nodeId: string) => {
    setBlastRadiusLoading(true);
    setBlastRadiusError(null);
    api
      .get("/change-requests/blast-radius", { params: { device_id: nodeId } })
      .then((res) => {
        setBlastRadiusResult(res.data);
        setBlastRadiusFor(nodeId);
      })
      .catch((err) => {
        setBlastRadiusError(err?.response?.data?.detail || "Failed to compute blast radius.");
        setBlastRadiusResult(null);
      })
      .finally(() => setBlastRadiusLoading(false));
  };

  // Node ids to highlight on the graph while a blast-radius result is
  // showing: the touched device itself plus everything topology says
  // depends on it. null when there's nothing to highlight (falls back to
  // normal search-based dimming below).
  const blastRadiusIds = useMemo(() => {
    if (!blastRadiusResult || !blastRadiusFor) return null;
    return new Set<string>([blastRadiusFor, ...blastRadiusResult.dependent_device_ids]);
  }, [blastRadiusResult, blastRadiusFor]);

  const linkCountFor = (nodeId: string) => graph?.edges.filter((e) => e.source === nodeId || e.target === nodeId).length ?? 0;

  // --- search / find-a-device -------------------------------------------
  // Enterprise topology tools (SolarWinds, NetBrain, LibreNMS) all offer a
  // quick "find" box that dims everything else and jumps the view to the
  // match -- indispensable once a fleet grows past a screenful of nodes.
  const matchedNodeIds = useMemo(() => {
    const q = nodeSearch.trim().toLowerCase();
    if (!q) return null;
    const s = new Set<string>();
    laidOut.forEach((n) => {
      if (
        n.hostname?.toLowerCase().includes(q) ||
        n.ip_address?.toLowerCase().includes(q) ||
        (n.site || "").toLowerCase().includes(q) ||
        (n.vendor || "").toLowerCase().includes(q)
      ) {
        s.add(n.id);
      }
    });
    return s;
  }, [nodeSearch, laidOut]);

  const focusOnSearch = () => {
    if (!matchedNodeIds || matchedNodeIds.size === 0) return;
    const first = laidOut.find((n) => matchedNodeIds.has(n.id));
    if (!first) return;
    setView({ x: first.x - WIDTH / 2, y: first.y - HEIGHT / 2, scale: 1.4 });
    setSelection({ kind: "node", node: first });
  };

  // --- export the current view as a PNG ----------------------------------
  // No server round-trip -- serialize the live SVG (already has every
  // color as an inline attribute, no external stylesheet needed), rasterize
  // it on an offscreen canvas, and hand back a PNG download. Useful for
  // pasting into a change-request ticket or an incident postmortem.
  const [exporting, setExporting] = useState(false);
  const exportPng = () => {
    const svgEl = svgRef.current;
    if (!svgEl) return;
    setExporting(true);
    try {
      const clone = svgEl.cloneNode(true) as SVGSVGElement;
      clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
      clone.setAttribute("width", String(WIDTH));
      clone.setAttribute("height", String(HEIGHT));
      const bg = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      bg.setAttribute("x", "0");
      bg.setAttribute("y", "0");
      bg.setAttribute("width", String(WIDTH));
      bg.setAttribute("height", String(HEIGHT));
      bg.setAttribute("fill", "#ffffff");
      clone.insertBefore(bg, clone.firstChild);
      const svgString = new XMLSerializer().serializeToString(clone);
      const svgBlob = new Blob([svgString], { type: "image/svg+xml;charset=utf-8" });
      const url = URL.createObjectURL(svgBlob);
      const img = new Image();
      img.onload = () => {
        const scale = 2; // 2x for crisp export
        const canvas = document.createElement("canvas");
        canvas.width = WIDTH * scale;
        canvas.height = HEIGHT * scale;
        const ctx = canvas.getContext("2d");
        if (ctx) {
          ctx.scale(scale, scale);
          ctx.drawImage(img, 0, 0, WIDTH, HEIGHT);
        }
        URL.revokeObjectURL(url);
        canvas.toBlob((blob) => {
          if (!blob) {
            setExporting(false);
            return;
          }
          const dlUrl = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = dlUrl;
          a.download = `topology-${new Date().toISOString().slice(0, 10)}.png`;
          a.click();
          URL.revokeObjectURL(dlUrl);
          setExporting(false);
        }, "image/png");
      };
      img.onerror = () => setExporting(false);
      img.src = url;
    } catch {
      setExporting(false);
    }
  };

  return (
    <div className="pb-16 flex flex-col gap-6 md:p-2">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-navy">Network Topology</h1>
          <p className="text-sm text-slate-500 mt-1 max-w-2xl">
            Devices and the links between them — green badges and solid lines are confirmed physical links (LLDP/CDP neighbor discovery
            via SNMP, or imported GNS3 lab wiring). Gray labels and dashed lines are logical/inferred connections based on interfaces sharing the same
            subnet. Click a device or a link for details. Scroll to zoom, drag to pan.
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <div className="flex items-center rounded-lg border border-slate-200 bg-white shadow-sm overflow-hidden text-xs font-bold">
            <button
              onClick={() => setViewMode("graph")}
              className={`px-3 py-2 transition-colors ${viewMode === "graph" ? "bg-brandblue text-white" : "text-slate-600 hover:bg-slate-50"}`}
            >
              Graph
            </button>
            <button
              onClick={() => setViewMode("layered")}
              title="Rows by device role: core on top, then distribution, then access"
              className={`px-3 py-2 transition-colors border-l border-slate-200 ${viewMode === "layered" ? "bg-brandblue text-white" : "text-slate-600 hover:bg-slate-50"}`}
            >
              Layered
            </button>
            <button
              onClick={() => setViewMode("groups")}
              className={`px-3 py-2 transition-colors border-l border-slate-200 ${viewMode === "groups" ? "bg-brandblue text-white" : "text-slate-600 hover:bg-slate-50"}`}
            >
              Data Center / Rack
            </button>
          </div>

          <button
            onClick={() => setLiveMode((v) => !v)}
            title={
              liveMode
                ? `Pause live updates${lastLiveUpdate ? ` (last update ${lastLiveUpdate.toLocaleTimeString()})` : ""}`
                : "Resume live updates"
            }
            className={`flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-bold border transition-colors ${
              liveMode
                ? liveStatus === "live"
                  ? "bg-green-50 border-green-300 text-green-700"
                  : "bg-amber-50 border-amber-300 text-amber-700"
                : "bg-slate-100 border-slate-300 text-slate-500"
            }`}
          >
            <span
              className={`w-2 h-2 rounded-full ${
                liveMode
                  ? liveStatus === "live"
                    ? "bg-green-500 animate-pulse"
                    : "bg-amber-500 animate-pulse"
                  : "bg-slate-400"
              }`}
            />
            {liveMode ? (liveStatus === "live" ? "Live" : liveStatus === "connecting" ? "Connecting…" : "Reconnecting…") : "Paused"}
          </button>

          <div className="relative">
            <input
              value={nodeSearch}
              onChange={(e) => setNodeSearch(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") focusOnSearch();
              }}
              placeholder="Find device, IP, or site…"
              className="border border-slate-300 rounded-lg pl-3 pr-16 py-2 text-sm bg-white shadow-sm w-48 focus:ring-2 focus:ring-brandblue focus:border-transparent outline-none"
            />
            {nodeSearch && (
              <div className="absolute right-1 top-1/2 -translate-y-1/2 flex items-center gap-1">
                <span className="text-[10px] text-slate-400 font-mono">{matchedNodeIds?.size ?? 0}</span>
                <button
                  onClick={focusOnSearch}
                  disabled={!matchedNodeIds || matchedNodeIds.size === 0}
                  title="Jump to first match"
                  className="text-[10px] font-bold text-brandblue hover:text-navy disabled:text-slate-300 px-1"
                >
                  ➔
                </button>
                <button
                  onClick={() => setNodeSearch("")}
                  title="Clear search"
                  className="text-[10px] font-bold text-slate-400 hover:text-slate-600 px-1"
                >
                  ✕
                </button>
              </div>
            )}
          </div>
          <button
            onClick={exportPng}
            disabled={exporting || viewMode !== "graph"}
            title="Export current graph view as PNG"
            className="flex items-center gap-1.5 text-xs font-bold rounded-lg px-2.5 py-2 shadow-sm border bg-white text-slate-600 border-slate-200 hover:bg-slate-50 disabled:opacity-40"
          >
            {exporting ? "Exporting…" : "⭳ Export PNG"}
          </button>
          <button
            onClick={() => (showDiff ? setShowDiff(false) : openDiffPanel())}
            className={`flex items-center gap-1.5 text-xs font-bold rounded-lg px-2.5 py-2 shadow-sm border transition-colors ${
              showDiff
                ? "bg-brandblue text-white border-brandblue"
                : "bg-white text-slate-600 border-slate-200 hover:bg-slate-50"
            }`}
          >
            What changed?
          </button>
          <label className="flex items-center gap-1.5 text-xs text-slate-500 bg-white border border-slate-200 rounded-lg px-2.5 py-2 shadow-sm">
            <input type="checkbox" checked={showIpLabels} onChange={(e) => setShowIpLabels(e.target.checked)} />
            Show interface IPs
          </label>
          <label
            className="flex items-center gap-1.5 text-xs text-slate-500 bg-white border border-slate-200 rounded-lg px-2.5 py-2 shadow-sm"
            title="Colors each link by the busier endpoint's SNMP interface utilization (green <60%, amber 60-79%, red 80%+). Links with no recent poll on either end keep the default gray."
          >
            <input
              type="checkbox"
              checked={colorByUtilization}
              onChange={(e) => setColorByUtilization(e.target.checked)}
            />
            Color links by utilization
          </label>
          <label
            className="flex items-center gap-1.5 text-xs text-slate-500 bg-white border border-slate-200 rounded-lg px-2.5 py-2 shadow-sm"
            title="Badges each node with its interface error count since the last SNMP poll (amber, red at 100+). Devices with zero errors or no recent poll show no badge."
          >
            <input
              type="checkbox"
              checked={showErrorBadges}
              onChange={(e) => setShowErrorBadges(e.target.checked)}
            />
            Show interface errors
          </label>
          <label
            className="flex items-center gap-1.5 text-xs text-slate-500 bg-white border border-slate-200 rounded-lg px-2.5 py-2 shadow-sm"
            title="Pulses links red and rings devices that have an active, unresolved alert -- an independent layer on top of whatever view/overlay is already showing."
          >
            <input type="checkbox" checked={alertOverlay} onChange={(e) => setAlertOverlay(e.target.checked)} />
            Alert overlay
          </label>
          <button
            onClick={togglePathMode}
            title="Click two devices on the graph to trace the path between them"
            className={`flex items-center gap-1.5 text-xs font-bold rounded-lg px-2.5 py-2 shadow-sm border transition-colors ${
              pathMode ? "bg-brandblue text-white border-brandblue" : "bg-white text-slate-600 border-slate-200 hover:bg-slate-50"
            }`}
          >
            {pathMode ? "Cancel path trace" : "Trace path"}
          </button>
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

      {showDiff && (
        <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm flex flex-col gap-4">
          <div className="flex items-center justify-between flex-wrap gap-3">
            <div>
              <p className="text-sm font-bold text-navy">What changed in the graph</p>
              <p className="text-xs text-slate-500 mt-0.5">
                Compares two saved topology snapshots. A new snapshot is captured automatically once a day.
              </p>
            </div>
            <button
              onClick={captureSnapshotNow}
              className="text-xs font-bold rounded-lg px-3 py-2 border border-slate-200 bg-white text-slate-600 hover:bg-slate-50 shadow-sm"
            >
              Capture snapshot now
            </button>
          </div>

          {snapshots.length >= 2 && (
            <div className="flex items-center gap-2 flex-wrap text-xs">
              <span className="font-bold text-slate-500">From</span>
              <select
                value={olderId}
                onChange={(e) => setOlderId(e.target.value)}
                className="border border-slate-300 rounded-lg px-2 py-1.5 bg-white"
              >
                {snapshots.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.captured_at ? new Date(s.captured_at).toLocaleString() : s.id} ({s.node_count}n/{s.edge_count}e)
                  </option>
                ))}
              </select>
              <span className="font-bold text-slate-500">To</span>
              <select
                value={newerId}
                onChange={(e) => setNewerId(e.target.value)}
                className="border border-slate-300 rounded-lg px-2 py-1.5 bg-white"
              >
                {snapshots.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.captured_at ? new Date(s.captured_at).toLocaleString() : s.id} ({s.node_count}n/{s.edge_count}e)
                  </option>
                ))}
              </select>
              <button
                onClick={() => loadDiff({ older_id: olderId, newer_id: newerId })}
                className="text-xs font-bold rounded-lg px-3 py-1.5 bg-brandblue text-white shadow-sm"
              >
                Compare
              </button>
            </div>
          )}

          {diffLoading && <p className="text-sm text-slate-400">Loading diff…</p>}
          {diffError && !diffLoading && (
            <p className="text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
              {diffError}
            </p>
          )}

          {diff && !diffLoading && !diffError && (
            <div>
              <p className="text-xs text-slate-500 mb-3">
                {diff.older_captured_at ? new Date(diff.older_captured_at).toLocaleString() : "—"} →{" "}
                {diff.newer_captured_at ? new Date(diff.newer_captured_at).toLocaleString() : "—"} ·{" "}
                {diff.unchanged_node_count} unchanged device{diff.unchanged_node_count === 1 ? "" : "s"},{" "}
                {diff.unchanged_edge_count} unchanged link{diff.unchanged_edge_count === 1 ? "" : "s"}
              </p>

              {diff.added_nodes.length === 0 &&
              diff.removed_nodes.length === 0 &&
              diff.added_edges.length === 0 &&
              diff.removed_edges.length === 0 ? (
                <p className="text-sm text-slate-400">No changes between these snapshots.</p>
              ) : (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
                  {diff.added_nodes.length > 0 && (
                    <div>
                      <p className="text-xs font-bold text-emerald-600 uppercase tracking-wide mb-1">
                        + Devices added ({diff.added_nodes.length})
                      </p>
                      <ul className="flex flex-col gap-1">
                        {diff.added_nodes.map((n) => (
                          <li key={n.id} className="text-slate-700">
                            {n.hostname} <span className="text-slate-400 font-mono text-xs">{n.ip_address}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {diff.removed_nodes.length > 0 && (
                    <div>
                      <p className="text-xs font-bold text-riskcrit uppercase tracking-wide mb-1">
                        − Devices removed ({diff.removed_nodes.length})
                      </p>
                      <ul className="flex flex-col gap-1">
                        {diff.removed_nodes.map((n) => (
                          <li key={n.id} className="text-slate-700">
                            {n.hostname} <span className="text-slate-400 font-mono text-xs">{n.ip_address}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {diff.added_edges.length > 0 && (
                    <div>
                      <p className="text-xs font-bold text-emerald-600 uppercase tracking-wide mb-1">
                        + Links added ({diff.added_edges.length})
                      </p>
                      <ul className="flex flex-col gap-1">
                        {diff.added_edges.map((e, i) => (
                          <li key={i} className="text-slate-700 font-mono text-xs">
                            {e.source} ↔ {e.target}{" "}
                            <span className="text-slate-400 font-sans">({e.link_source})</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {diff.removed_edges.length > 0 && (
                    <div>
                      <p className="text-xs font-bold text-riskcrit uppercase tracking-wide mb-1">
                        − Links removed ({diff.removed_edges.length})
                      </p>
                      <ul className="flex flex-col gap-1">
                        {diff.removed_edges.map((e, i) => (
                          <li key={i} className="text-slate-700 font-mono text-xs">
                            {e.source} ↔ {e.target}{" "}
                            <span className="text-slate-400 font-sans">({e.link_source})</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {pathMode && (
        <div className="bg-white border border-slate-200 rounded-xl px-4 py-3 shadow-sm flex items-center gap-3 flex-wrap text-xs">
          {!pathSourceId && <span className="text-slate-500">Click a device on the graph to start the trace.</span>}
          {pathSourceId && !pathTargetId && (
            <span className="text-slate-500">
              From <span className="font-bold text-navy">{nodeById.get(pathSourceId)?.hostname || pathSourceId}</span> — now click the destination device.
            </span>
          )}
          {pathTraceLoading && <span className="text-slate-400">Tracing…</span>}
          {pathTraceError && <span className="text-riskcrit font-bold">{pathTraceError}</span>}
          {pathTraceResult && (
            <span className="text-slate-600">
              <span className="font-bold text-navy">
                {pathTraceResult.reached_target ? "Path found" : "Path incomplete"}
              </span>{" "}
              ({pathTraceResult.hop_source === "topology_graph" ? "topology graph" : "traceroute"}, {pathTraceResult.hops.length} hop
              {pathTraceResult.hops.length === 1 ? "" : "s"}):{" "}
              {pathTraceResult.hops.map((h, i) => (
                <span key={i}>
                  {i > 0 && " → "}
                  <span className={h.status === "ok" ? "text-slate-700" : "text-riskwarn font-bold"}>
                    {h.hostname || h.ip_address || "?"}
                  </span>
                </span>
              ))}
            </span>
          )}
          {(pathSourceId || pathTargetId) && (
            <button
              onClick={() => {
                setPathSourceId(null);
                setPathTargetId(null);
                setPathTraceResult(null);
                setPathTraceError(null);
              }}
              className="ml-auto text-[10px] font-bold text-slate-400 hover:text-navy"
            >
              Start over
            </button>
          )}
        </div>
      )}

      {loading && <p className="text-sm text-slate-400">Loading topology...</p>}
      {error && <p className="text-sm text-riskcrit">{error}</p>}

      {!loading && !error && graph && graph.nodes.length === 0 && (
        <div className="bg-white border border-slate-200 rounded-xl p-10 text-center text-slate-400">
          No devices in inventory yet.
        </div>
      )}

      {!loading && !error && graph && graph.nodes.length > 0 && (
        <div className="grid grid-cols-1 xl:grid-cols-4 gap-6">
          <div ref={diagramContainerRef} className="xl:col-span-3 bg-white border border-slate-200 rounded-xl shadow-sm overflow-hidden relative">
          {viewMode === "graph" || viewMode === "layered" ? (
          <>
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
              onMouseLeave={() => { setHoveredId(null); setHoveredEdgeTip(null); }}
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
                {/* Layered view: row labels + divider lines for each device_role tier */}
                {viewMode === "layered" && (
                  <g>
                    {(() => {
                      const tiersPresent = [...ROLE_TIERS, "__unassigned__"].filter((t) =>
                        laidOut.some((n) => (ROLE_TIERS.includes((n.device_role || "").toLowerCase().trim()) ? (n.device_role || "").toLowerCase().trim() : "__unassigned__") === t)
                      );
                      const rowHeight = HEIGHT / Math.max(tiersPresent.length, 1);
                      return tiersPresent.map((tier, i) => (
                        <g key={tier}>
                          {i > 0 && (
                            <line x1={0} y1={rowHeight * i} x2={WIDTH} y2={rowHeight * i} stroke="#e2e8f0" strokeWidth={1} strokeDasharray="4 4" />
                          )}
                          <text x={12} y={rowHeight * i + 16} fontSize={10} fontWeight={700} fill="#94a3b8" className="pointer-events-none select-none">
                            {roleTierLabel(tier).toUpperCase()}
                          </text>
                        </g>
                      ));
                    })()}
                  </g>
                )}

                {/* Links */}
                <g>
                  {filteredEdges.map((e) => {
                    const a = nodeById.get(e.source);
                    const b = nodeById.get(e.target);
                    if (!a || !b) return null;
                    const key = edgeKey(e);
                    const onTracedPath = pathEdgeKeySet.has(`${e.source}|${e.target}`);
                    const active = highlightedEdgeKeys?.has(key) || selectedEdgeKey === key || onTracedPath;
                    const dimForSearch = blastRadiusIds
                      ? !blastRadiusIds.has(e.source) && !blastRadiusIds.has(e.target)
                      : matchedNodeIds
                      ? !matchedNodeIds.has(e.source) && !matchedNodeIds.has(e.target)
                      : false;
                    const dimForPath = pathTraceResult ? !onTracedPath : false;
                    const dimForHover = hoveredNeighborIds
                      ? !(hoveredNeighborIds.has(e.source) && hoveredNeighborIds.has(e.target))
                      : false;
                    const utilColor = colorByUtilization ? utilizationColor(e.utilization_pct) : null;
                    const edgeAlerting = alertOverlay && edgeHasActiveAlert(e);
                    const isStaleLink = e.link_source !== "subnet" && e.stale;
                    const srcLabelPos = pointAlong(a.x, a.y, b.x, b.y, 30);
                    const tgtLabelPos = pointAlong(b.x, b.y, a.x, a.y, 30);
                    const midX = (a.x + b.x) / 2;
                    const midY = (a.y + b.y) / 2;
                    const hasIps = Boolean(e.source_ip && e.target_ip);
                    // Real physical members (one row per confirmed LLDP/CDP/GNS3
                    // cable) fan out as parallel lines rather than collapsing
                    // to one -- a 4-cable LACP bundle should look like 4
                    // cables, same as an enterprise NMS (Auvik/SolarWinds)
                    // would draw it, each independently colored by its own
                    // up/down state rather than the whole bundle sharing one
                    // color. subnet/mgmt_subnet-inferred edges have no
                    // per-port data at all, so they still render as the
                    // single dashed/solid inference line they always did.
                    const members = e.members && e.members.length > 0 ? e.members : null;
                    const downMemberCount = members ? members.filter((m) => m.status === "down").length : 0;
                    const dx = b.x - a.x;
                    const dy = b.y - a.y;
                    const segLen = Math.hypot(dx, dy) || 1;
                    const nx = -dy / segLen;
                    const ny = dx / segLen;
                    const FAN_SPACING = 4.5;
                    const memberOffsets = members
                      ? members.map((_, i) => (i - (members.length - 1) / 2) * FAN_SPACING)
                      : [0];
                    // If every physical member agrees on switchport mode,
                    // fold it into the on-map label (e.g. "LLDP · trunk")
                    // so trunk vs access is visible without hovering or
                    // clicking -- previously the map only ever showed the
                    // discovery protocol, never whether the link carries
                    // tagged VLAN traffic or a single access VLAN.
                    const memberModes = members ? new Set(members.map((m) => m.port_mode).filter(Boolean)) : null;
                    const uniformMode = memberModes && memberModes.size === 1 ? [...memberModes][0] : null;
                    const linkLabel =
                      e.link_source === "subnet"
                        ? e.subnet || ""
                        : `${e.link_source.toUpperCase()}${members && members.length > 1 ? ` ×${members.length}` : ""}${
                            uniformMode ? ` · ${uniformMode}` : ""
                          }${downMemberCount > 0 ? ` (${downMemberCount} down)` : ""}${isStaleLink ? " ⚠ stale" : ""}`; // "LLDP" / "CDP" -- confirmed neighbor, no subnet to show
                    return (
                      <g
                        key={key}
                        className="cursor-pointer"
                        opacity={dimForSearch || dimForPath || dimForHover ? 0.12 : 1}
                        onClick={() => setSelection({ kind: "edge", edge: e })}
                        onMouseMove={(evt) => {
                          const rect = diagramContainerRef.current?.getBoundingClientRect();
                          if (!rect) return;
                          setHoveredEdgeTip({ edge: e, x: evt.clientX - rect.left, y: evt.clientY - rect.top });
                        }}
                        onMouseLeave={() => setHoveredEdgeTip((cur) => (cur?.edge === e ? null : cur))}
                      >
                        {/* fat invisible hit-area so thin lines are easy to click */}
                        <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke="transparent" strokeWidth={14} />
                        {members ? (
                          members.map((member, i) => {
                            const offset = memberOffsets[i];
                            const mx1 = a.x + nx * offset;
                            const my1 = a.y + ny * offset;
                            const mx2 = b.x + nx * offset;
                            const my2 = b.y + ny * offset;
                            // Per-member color: down members are visibly
                            // grayed-out/dashed rather than omitted or
                            // looking identical to a live member -- that's
                            // the whole point of drawing every real cable.
                            // Alert/active/traced-path overrides still win
                            // for the whole bundle so an alerting link
                            // still reads as one incident.
                            const memberColor = onTracedPath
                              ? "#7c3aed"
                              : edgeAlerting
                              ? "#dc2626"
                              : active
                              ? "#2563eb"
                              : member.status === "down"
                              ? "#cbd5e1"
                              : e.is_uplink
                              ? "#0f766e"
                              : member.status === "up"
                              ? utilColor || "#16a34a"
                              : utilColor || "#94a3b8";
                            return (
                              <line
                                key={`${key}-m${i}`}
                                x1={mx1}
                                y1={my1}
                                x2={mx2}
                                y2={my2}
                                stroke={memberColor}
                                strokeWidth={active ? 2.5 : e.is_uplink ? 2.75 : 1.6}
                                strokeDasharray={
                                  member.status === "down" ? "3 3" : isStaleLink ? "5 3" : undefined
                                }
                                strokeLinecap="round"
                              >
                                <title>
                                  {`${e.link_source.toUpperCase()} ${member.local_port || "?"} \u2194 ${
                                    member.neighbor_port || "?"
                                  } \u2014 ${member.status}${member.stale ? " (stale)" : ""}${
                                    member.port_mode
                                      ? ` \u2014 ${member.port_mode}${member.vlan ? ` (vlan ${member.vlan})` : ""}`
                                      : ""
                                  }`}
                                </title>
                              </line>
                            );
                          })
                        ) : (
                          <line
                            x1={a.x}
                            y1={a.y}
                            x2={b.x}
                            y2={b.y}
                            stroke={onTracedPath ? "#7c3aed" : edgeAlerting ? "#dc2626" : active ? "#2563eb" : e.is_uplink ? "#0f766e" : utilColor || "#94a3b8"}
                            strokeWidth={active ? 2.75 : e.is_uplink ? 3 : 1.5}
                            strokeDasharray={isStaleLink ? "5 3" : undefined}
                          />
                        )}
                        {/* Alert overlay: a pulsing red line laid on top so an actively
                            alerting link reads as "live incident", not just a static color. */}
                        {edgeAlerting && (
                          <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke="#dc2626" strokeWidth={4}>
                            <animate attributeName="opacity" values="0.55;0.05;0.55" dur="1.6s" repeatCount="indefinite" />
                          </line>
                        )}
                        {/* subnet / link-source label at midpoint */}
                        <rect
                          x={midX - linkLabel.length * 3.1 - 4}
                          y={midY - 15}
                          width={linkLabel.length * 6.2 + 8}
                          height={13}
                          rx={3}
                          fill={
                            isStaleLink ? "#fef3c7" : e.link_source !== "subnet" ? "#dcfce7" : active ? "#dbeafe" : "#f1f5f9"
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
                          fill={isStaleLink ? "#92400e" : e.link_source !== "subnet" ? "#166534" : active ? "#1e3a8a" : "#64748b"}
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
                    const isSearchMatch = blastRadiusIds
                      ? blastRadiusIds.has(node.id)
                      : matchedNodeIds
                      ? matchedNodeIds.has(node.id)
                      : true;
                    const isBlastDependent = blastRadiusIds ? blastRadiusIds.has(node.id) && node.id !== blastRadiusFor : false;
                    const isBlastTouched = blastRadiusFor === node.id;
                    // Neighbor highlight: with a device hovered, everything that isn't
                    // it or one of its direct neighbors fades out, and the hovered
                    // device's neighbors get a blue ring so the "who's connected to
                    // this" answer is visible at a glance, not just via edge color.
                    const isHoveredNeighbor = hoveredNeighborIds ? hoveredNeighborIds.has(node.id) : false;
                    const dimForHoverNode = hoveredNeighborIds ? !isHoveredNeighbor : false;
                    const isAlerting = alertOverlay && (node.active_alert_severity === "critical" || node.active_alert_severity === "warning");
                    return (
                      <g
                        key={node.id}
                        transform={`translate(${node.x}, ${node.y})`}
                        className="cursor-pointer"
                        opacity={!isSearchMatch ? 0.18 : dimForHoverNode ? 0.25 : 1}
                        onMouseEnter={() => setHoveredId(node.id)}
                        onClick={() => (pathMode ? handlePathClick(node.id) : setSelection({ kind: "node", node }))}
                      >
                        {pathMode && (pathSourceId === node.id || pathTargetId === node.id || pathDeviceIds.includes(node.id)) && (
                          <circle r={26} fill="none" stroke="#7c3aed" strokeWidth={2.5} strokeDasharray="4 3" />
                        )}
                        {isHoveredNeighbor && hoveredId !== node.id && (
                          <circle r={22} fill="none" stroke="#2563eb" strokeWidth={2.5} />
                        )}
                        {isAlerting && (
                          <circle r={24} fill="none" stroke={node.active_alert_severity === "critical" ? "#dc2626" : "#d97706"} strokeWidth={2.5}>
                            <animate attributeName="opacity" values="1;0.25;1" dur="1.6s" repeatCount="indefinite" />
                          </circle>
                        )}
                        {matchedNodeIds && !blastRadiusIds && isSearchMatch && (
                          <circle r={24} fill="none" stroke="#2563eb" strokeWidth={2} strokeDasharray="2 2">
                            <animateTransform
                              attributeName="transform"
                              type="rotate"
                              from="0 0 0"
                              to="360 0 0"
                              dur="6s"
                              repeatCount="indefinite"
                            />
                          </circle>
                        )}
                        {isBlastTouched && (
                          <circle r={26} fill="none" stroke="#d97706" strokeWidth={2.5} strokeDasharray="4 2" />
                        )}
                        {isBlastDependent && (
                          <circle r={24} fill="none" stroke="#7c3aed" strokeWidth={2} strokeDasharray="2 2">
                            <animateTransform
                              attributeName="transform"
                              type="rotate"
                              from="0 0 0"
                              to="360 0 0"
                              dur="6s"
                              repeatCount="indefinite"
                            />
                          </circle>
                        )}
                        {node.flagged_unstable && (
                          <circle r={22} fill="none" stroke="#dc2626" strokeWidth={1.75} strokeDasharray="3 2" />
                        )}
                        {node.is_uplink && (
                          <circle r={20} fill="none" stroke="#0f766e" strokeWidth={2.25} />
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
                        {showErrorBadges && !!node.interface_error_rate && node.interface_error_rate > 0 && (
                          <g transform="translate(-12, -12)">
                            <circle
                              r={8}
                              fill={interfaceErrorColor(node.interface_error_rate)}
                              stroke="white"
                              strokeWidth={1.5}
                            />
                            <text
                              textAnchor="middle"
                              dominantBaseline="central"
                              fontSize={8.5}
                              fontWeight={800}
                              fill="white"
                              className="pointer-events-none select-none"
                            >
                              {node.interface_error_rate > 99 ? "99+" : node.interface_error_rate}
                            </text>
                            <title>{`${node.interface_error_rate} interface error(s) since last poll`}</title>
                          </g>
                        )}
                        {node.is_spof && (
                          <g transform="translate(-12, 12)">
                            <path
                              d="M0,-9 L8,7 L-8,7 Z"
                              fill="#d97706"
                              stroke="white"
                              strokeWidth={1.25}
                              strokeLinejoin="round"
                            />
                            <text
                              y={4.5}
                              textAnchor="middle"
                              fontSize={9}
                              fontWeight={800}
                              fill="white"
                              className="pointer-events-none select-none"
                            >
                              !
                            </text>
                            <title>Single point of failure — no redundant path; removing this device would split the topology.</title>
                          </g>
                        )}
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

            {hoveredEdgeTip && (() => {
              const e = hoveredEdgeTip.edge;
              const src = graph.nodes.find((n) => n.id === e.source);
              const tgt = graph.nodes.find((n) => n.id === e.target);
              // Summarize member port modes: if every member agrees, show
              // one mode chip; if they differ (mixed trunk/access on one
              // bundle), say "mixed" rather than picking one arbitrarily.
              const modes = new Set((e.members || []).map((m) => m.port_mode).filter(Boolean));
              const modeSummary = modes.size === 1 ? [...modes][0] : modes.size > 1 ? "mixed" : null;
              // Keep the tooltip on-screen near the cursor without spilling
              // past the right/bottom edge of the diagram panel.
              const left = Math.min(hoveredEdgeTip.x + 14, 1000);
              const top = Math.max(hoveredEdgeTip.y - 10, 8);
              return (
                <div
                  className="pointer-events-none absolute z-20 bg-navy text-white text-xs rounded-lg shadow-lg px-3 py-2 max-w-[260px]"
                  style={{ left, top }}
                >
                  <div className="font-bold flex items-center gap-1.5 flex-wrap">
                    <span>{src?.hostname || "?"}</span>
                    <span className="text-slate-400">↔</span>
                    <span>{tgt?.hostname || "?"}</span>
                  </div>
                  <div className="text-slate-300 font-mono mt-0.5">
                    {e.local_port || "?"} ↔ {e.neighbor_port || "?"}
                  </div>
                  <div className="flex items-center gap-1.5 mt-1 flex-wrap">
                    <span
                      className={`px-1.5 py-0.5 rounded-full text-[10px] font-bold uppercase ${
                        e.link_source === "subnet" ? "bg-slate-600" : "bg-emerald-600"
                      }`}
                    >
                      {e.link_source === "subnet" ? "inferred" : `${e.link_source.toUpperCase()} confirmed`}
                    </span>
                    {modeSummary && (
                      <span
                        className={`px-1.5 py-0.5 rounded-full text-[10px] font-bold uppercase ${
                          modeSummary === "trunk"
                            ? "bg-purple-600"
                            : modeSummary === "mixed"
                            ? "bg-amber-600"
                            : "bg-sky-600"
                        }`}
                      >
                        {modeSummary}
                      </span>
                    )}
                    {e.stale && <span className="px-1.5 py-0.5 rounded-full text-[10px] font-bold uppercase bg-amber-600">stale</span>}
                  </div>
                  {e.members && e.members.length > 1 && (
                    <div className="text-slate-400 mt-1">{e.members.length} physical cables — click for details</div>
                  )}
                  {(!e.members || e.members.length <= 1) && (
                    <div className="text-slate-400 mt-1">Click for full details</div>
                  )}
                </div>
              );
            })()}

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
              <span className="flex items-center gap-1.5">
                <span className="w-3.5 h-3.5 rounded-full inline-block border-2" style={{ borderColor: "#0f766e" }} />
                Uplink
              </span>
              <span className="flex items-center gap-1.5">
                <span
                  className="inline-block"
                  style={{
                    width: 0,
                    height: 0,
                    borderLeft: "5px solid transparent",
                    borderRight: "5px solid transparent",
                    borderBottom: "8px solid #d97706",
                  }}
                />
                Single point of failure
              </span>
              <span className="flex items-center gap-1.5">
                <span
                  className="inline-block w-4 h-0 border-t-2 border-dashed"
                  style={{ borderColor: "#cbd5e1" }}
                />
                Down/inactive cable
              </span>
              <span className="flex items-center gap-1.5 ml-auto text-slate-400">
                {filteredNodes.length} device{filteredNodes.length === 1 ? "" : "s"} · {filteredEdges.length} link
                {filteredEdges.length === 1 ? "" : "s"}
                {totalMemberLinks !== filteredEdges.length ? ` (${totalMemberLinks} physical cables)` : ""}
              </span>
            </div>
          </>
          ) : (
            <div className="p-5 min-h-[680px]">
              {groupsLoading && <p className="text-sm text-slate-400">Loading groups…</p>}
              {groupsError && <p className="text-sm text-riskcrit">{groupsError}</p>}
              {!groupsLoading && !groupsError && groups && groups.length === 0 && (
                <p className="text-sm text-slate-400">No devices in inventory yet.</p>
              )}
              {!groupsLoading && !groupsError && groups && groups.length > 0 && (
                <div className="flex flex-col gap-6">
                  {groups.map((dc) => (
                    <div key={dc.name} className="border border-slate-200 rounded-xl overflow-hidden">
                      <div className="flex items-center justify-between px-4 py-2.5 bg-slate-50 border-b border-slate-200">
                        <div className="flex items-center gap-2">
                          <span className="text-sm">🏢</span>
                          <h3 className="text-sm font-bold text-navy">{dc.name}</h3>
                        </div>
                        <span className="text-[11px] font-bold text-slate-400 uppercase tracking-wide">
                          {dc.device_count} device{dc.device_count === 1 ? "" : "s"} ·{" "}
                          {dc.blocks.reduce((n, b) => n + b.racks.length, 0)} rack
                          {dc.blocks.reduce((n, b) => n + b.racks.length, 0) === 1 ? "" : "s"}
                        </span>
                      </div>
                      <div className="p-4 flex flex-col gap-4">
                        {dc.blocks.map((block) => (
                          <div key={block.name}>
                            {/* Block is an optional middle tier (building/pod) between
                                data center and rack -- most orgs never set it, so every
                                device lands in a single "Unassigned" block and this
                                header is skipped to avoid noise. */}
                            {(block.name !== "Unassigned" || dc.blocks.length > 1) && (
                              <div className="flex items-center gap-1.5 mb-2">
                                <span className="text-xs">🏗️</span>
                                <span className="text-xs font-bold text-slate-500">{block.name}</span>
                              </div>
                            )}
                            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
                              {block.racks.map((rack) => (
                                <div key={rack.name} className="border border-slate-200 rounded-lg overflow-hidden bg-slate-50/40">
                                  <div className="flex items-center gap-1.5 px-3 py-1.5 bg-white border-b border-slate-200">
                                    <span className="text-xs">🗄️</span>
                                    <span className="text-xs font-bold text-slate-600">{rack.name}</span>
                                    <span className="text-[10px] text-slate-400 ml-auto">{rack.devices.length}</span>
                                  </div>
                                  <ul className="p-2 flex flex-col gap-1">
                                    {rack.devices.map((dv) => {
                                      const fullNode = graph.nodes.find((n) => n.id === dv.id);
                                      return (
                                        <li
                                          key={dv.id}
                                          onClick={() =>
                                            fullNode &&
                                            setSelection({ kind: "node", node: { ...fullNode, x: 0, y: 0 } as LaidOutNode })
                                          }
                                          className={`flex items-center gap-2 text-xs rounded-md px-2 py-1.5 cursor-pointer border transition-colors ${
                                            selectedNodeId === dv.id
                                              ? "border-brandblue bg-blue-50"
                                              : "border-transparent hover:bg-white hover:border-slate-200"
                                          }`}
                                        >
                                          <span
                                            className="w-2 h-2 rounded-full shrink-0"
                                            style={{ backgroundColor: STATUS_COLOR[dv.status] || STATUS_COLOR.unknown }}
                                          />
                                          <span className="font-semibold text-slate-700 truncate">{dv.hostname}</span>
                                          {dv.device_type && (
                                            <span className="text-[10px] text-slate-400 ml-auto shrink-0">{dv.device_type}</span>
                                          )}
                                        </li>
                                      );
                                    })}
                                  </ul>
                                </div>
                              ))}
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
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

                <div className="flex items-center rounded-lg border border-slate-200 bg-slate-50 p-0.5 text-[11px] font-bold">
                  {(["overview", "interfaces", "metrics"] as const).map((tab) => (
                    <button
                      key={tab}
                      onClick={() => setDetailTab(tab)}
                      className={`flex-1 rounded-md px-2 py-1.5 capitalize transition-colors ${
                        detailTab === tab ? "bg-white text-brandblue shadow-sm" : "text-slate-500 hover:text-slate-700"
                      }`}
                    >
                      {tab}
                      {tab === "interfaces" && ifaces && ifaces.some((i) => i.status === "down") && (
                        <span className="ml-1 inline-block w-1.5 h-1.5 rounded-full bg-riskcrit align-middle" />
                      )}
                    </button>
                  ))}
                </div>

                {detailTab === "overview" && (
                <>
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
                    <dt className="text-slate-500">Data center</dt>
                    <dd className="text-slate-700">{selection.node.data_center || "Unassigned"}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-500">Rack</dt>
                    <dd className="text-slate-700">{selection.node.rack || "Unassigned"}</dd>
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
                    <dt className="text-slate-500">Interface errors</dt>
                    <dd
                      className="font-mono font-bold"
                      style={{
                        color:
                          selection.node.interface_error_rate != null && selection.node.interface_error_rate > 0
                            ? interfaceErrorColor(selection.node.interface_error_rate)
                            : "#334155",
                      }}
                    >
                      {selection.node.interface_error_rate != null
                        ? `${selection.node.interface_error_rate} since last poll`
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
                {selection.node.is_spof && (
                  <p className="text-[11px] bg-amber-50 border border-amber-200 text-amber-800 rounded-lg px-2.5 py-1.5">
                    Single point of failure — no redundant path in the discovered topology. Removing this device would split other devices off from each other, not just itself.
                  </p>
                )}
                {selection.node.is_uplink && (
                  <p className="text-[11px] bg-teal-50 border border-teal-200 text-teal-800 rounded-lg px-2.5 py-1.5">
                    Flagged as a WAN/uplink device.
                  </p>
                )}

                {/* Blast-radius preview: how many devices would be
                    affected if a change to this device goes wrong,
                    computed by walking the topology graph outward. */}
                <div className="border border-slate-200 rounded-lg p-2.5 bg-slate-50">
                  <div className="flex items-center justify-between">
                    <h4 className="text-[11px] font-bold uppercase text-slate-500 tracking-wide">Blast Radius</h4>
                    {blastRadiusFor === selection.node.id ? (
                      <button
                        onClick={() => {
                          setBlastRadiusFor(null);
                          setBlastRadiusResult(null);
                        }}
                        className="text-[10px] text-slate-400 hover:text-slate-600"
                      >
                        Clear
                      </button>
                    ) : (
                      <button
                        onClick={() => fetchBlastRadius(selection.node.id)}
                        disabled={blastRadiusLoading}
                        className="text-[10px] font-semibold text-brandblue hover:underline disabled:opacity-50"
                      >
                        {blastRadiusLoading ? "Computing…" : "Compute"}
                      </button>
                    )}
                  </div>
                  {blastRadiusError && <p className="text-[11px] text-riskcrit mt-1">{blastRadiusError}</p>}
                  {blastRadiusFor === selection.node.id && blastRadiusResult && (
                    <p className="text-[11px] text-slate-600 mt-1.5">
                      {blastRadiusResult.dependent_count > 0 ? (
                        <>
                          <span className="font-semibold text-purple-700">{blastRadiusResult.dependent_count}</span> device
                          {blastRadiusResult.dependent_count === 1 ? "" : "s"} depend on this one via topology — highlighted on
                          the graph.
                        </>
                      ) : (
                        <>No other devices depend on this one via known topology.</>
                      )}
                    </p>
                  )}
                  {!blastRadiusFor && (
                    <p className="text-[11px] text-slate-400 mt-1">
                      See how many devices would be affected if a change here goes wrong.
                    </p>
                  )}
                </div>
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
                </>
                )}

                {detailTab === "interfaces" && (
                  <div className="flex flex-col gap-3">
                    {ifacesLoading && <p className="text-xs text-slate-400">Loading interfaces…</p>}
                    {!ifacesLoading && ifaces && ifaces.length === 0 && (
                      <p className="text-xs text-slate-400 italic">
                        No interface data yet — this device hasn't completed an SNMP poll with interface details.
                      </p>
                    )}
                    {!ifacesLoading && ifaces && ifaces.length > 0 && (
                      <ul className="space-y-1.5 max-h-64 overflow-y-auto pr-1">
                        {ifaces.map((iface) => (
                          <li
                            key={iface.if_index}
                            onClick={() => loadIfaceHistory(selection.node.id, iface.if_index)}
                            className={`text-xs rounded-lg px-2.5 py-1.5 border cursor-pointer flex items-center justify-between gap-2 ${
                              iface.status === "down"
                                ? "bg-red-50 border-red-200"
                                : "bg-slate-50 border-slate-200 hover:border-blue-300"
                            } ${ifaceHistoryFor === iface.if_index ? "ring-2 ring-brandblue/40" : ""}`}
                          >
                            <span className="flex items-center gap-1.5 min-w-0">
                              <span
                                className={`w-2 h-2 rounded-full shrink-0 ${
                                  iface.status === "down" ? "bg-riskcrit" : "bg-emerald-500"
                                }`}
                              />
                              <span className="font-semibold text-slate-700 truncate">{iface.if_descr}</span>
                            </span>
                            <span className={`font-bold uppercase text-[10px] shrink-0 ${iface.status === "down" ? "text-riskcrit" : "text-emerald-600"}`}>
                              {iface.status}
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}

                    {ifaceHistoryFor && (
                      <div>
                        <h4 className="text-[11px] font-bold uppercase text-slate-500 tracking-wide mb-1.5">
                          History — {ifaces?.find((i) => i.if_index === ifaceHistoryFor)?.if_descr || ifaceHistoryFor}
                        </h4>
                        {ifaceHistory === null && <p className="text-xs text-slate-400">Loading…</p>}
                        {ifaceHistory && ifaceHistory.length === 0 && (
                          <p className="text-xs text-slate-400 italic">No transitions recorded yet.</p>
                        )}
                        {ifaceHistory && ifaceHistory.length > 0 && (
                          <ul className="space-y-1 max-h-48 overflow-y-auto pr-1">
                            {ifaceHistory.map((h) => (
                              <li key={h.id} className="text-[11px] flex items-center justify-between border-b border-slate-100 py-1">
                                <span className={h.status === "down" ? "text-riskcrit font-bold" : "text-emerald-600 font-bold"}>
                                  {h.previous_status ? `${h.previous_status} → ${h.status}` : h.status}
                                </span>
                                <span className="text-slate-400 font-mono">
                                  {h.changed_at ? new Date(h.changed_at).toLocaleString() : "—"}
                                </span>
                              </li>
                            ))}
                          </ul>
                        )}
                      </div>
                    )}
                  </div>
                )}

                {detailTab === "metrics" && (
                  <div className="flex flex-col gap-3">
                    {metricHistoryLoading && <p className="text-xs text-slate-400">Loading metrics history…</p>}
                    {!metricHistoryLoading && metricHistory && metricHistory.length === 0 && (
                      <p className="text-xs text-slate-400 italic">
                        No metrics recorded yet — this device isn't SNMP-polled, or hasn't completed a poll.
                      </p>
                    )}
                    {!metricHistoryLoading && metricHistory && metricHistory.length > 0 && (
                      <>
                        {([
                          ["CPU", "cpu_utilization_pct", "#2563eb"],
                          ["Memory", "memory_utilization_pct", "#7c3aed"],
                          ["Interface util.", "interface_utilization_pct", "#0891b2"],
                        ] as const).map(([label, field, color]) => {
                          const values = metricHistory.map((m) => (m as any)[field]).filter((v: number | null) => v !== null) as number[];
                          const latest = values[values.length - 1];
                          return (
                            <div key={field} className="flex items-center justify-between gap-3 bg-slate-50 border border-slate-200 rounded-lg px-3 py-2">
                              <div>
                                <p className="text-[10px] font-bold uppercase text-slate-400 tracking-wide">{label}</p>
                                <p className="text-sm font-bold text-slate-700">{latest !== undefined ? `${latest}%` : "—"}</p>
                              </div>
                              <Sparkline values={values} color={color} />
                            </div>
                          );
                        })}
                        <p className="text-[10px] text-slate-400">Last 24h · {metricHistory.length} poll{metricHistory.length === 1 ? "" : "s"}</p>
                        <Link
                          to={`/devices?device=${selection.node.id}`}
                          className="text-xs font-bold text-brandblue uppercase tracking-wide hover:underline mt-1"
                        >
                          Full health history →
                        </Link>
                      </>
                    )}
                  </div>
                )}
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

                    {e.members && e.members.length > 0 && (
                      <div>
                        <p className="text-[10px] font-bold uppercase text-slate-400 tracking-wide mb-1.5">
                          Physical cables ({e.members.length}
                          {e.members.filter((m) => m.status === "down").length > 0
                            ? ` · ${e.members.filter((m) => m.status === "down").length} down`
                            : ""}
                          )
                        </p>
                        <ul className="space-y-1 max-h-40 overflow-y-auto pr-1">
                          {e.members.map((m, i) => (
                            <li
                              key={i}
                              className={`text-[11px] rounded-md px-2 py-1.5 border flex items-center justify-between gap-2 ${
                                m.status === "down"
                                  ? "bg-slate-50 border-slate-200 text-slate-400"
                                  : m.status === "up"
                                  ? "bg-emerald-50 border-emerald-200 text-slate-700"
                                  : "bg-slate-50 border-slate-200 text-slate-600"
                              }`}
                            >
                              <span className="flex flex-col min-w-0">
                                <span
                                  className={`font-mono truncate ${m.status === "down" ? "line-through decoration-slate-300" : ""}`}
                                >
                                  {m.local_port || "?"} ↔ {m.neighbor_port || "?"}
                                </span>
                                {m.port_mode && (
                                  <span className="flex items-center gap-1 mt-0.5">
                                    <span
                                      className={`px-1 py-0 rounded text-[9px] font-bold uppercase tracking-wide ${
                                        m.port_mode === "trunk"
                                          ? "bg-purple-100 text-purple-700"
                                          : "bg-sky-100 text-sky-700"
                                      }`}
                                    >
                                      {m.port_mode}
                                    </span>
                                    {m.vlan && (
                                      <span className="font-mono text-slate-400 text-[10px]">
                                        {m.port_mode === "trunk" ? "native " : ""}vlan {m.vlan}
                                        {m.port_mode === "trunk" && m.trunk_vlans && m.trunk_vlans.length > 1
                                          ? ` (${m.trunk_vlans.length} tagged)`
                                          : ""}
                                      </span>
                                    )}
                                  </span>
                                )}
                              </span>
                              <span className="flex items-center gap-1.5 shrink-0">
                                {m.utilization_pct !== null && m.utilization_pct !== undefined && (
                                  <span className="font-mono text-slate-400">{m.utilization_pct}%</span>
                                )}
                                {m.stale && <span className="text-amber-600">⚠</span>}
                                <span
                                  className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                                    m.status === "down" ? "bg-slate-300" : m.status === "up" ? "bg-emerald-500" : "bg-slate-300"
                                  }`}
                                />
                              </span>
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}

                    <dl className="text-xs space-y-1.5">
                      <div className="flex justify-between">
                        <dt className="text-slate-500">{e.link_source === "subnet" ? "Subnet" : "Discovered via"}</dt>
                        <dd className="font-mono text-slate-700">{e.link_source === "subnet" ? e.subnet : e.link_source.toUpperCase()}</dd>
                        {e.utilization_pct !== null && e.utilization_pct !== undefined && (
                          <>
                            <dt className="text-slate-500">Utilization</dt>
                            <dd className="font-mono font-bold" style={{ color: utilizationColor(e.utilization_pct) || "#334155" }}>
                              {e.utilization_pct}% (busier endpoint)
                            </dd>
                          </>
                        )}
                        {e.link_source !== "subnet" && e.last_confirmed_at && (
                          <>
                            <dt className="text-slate-500">Last confirmed</dt>
                            <dd className={`font-mono ${e.stale ? "text-amber-600 font-bold" : "text-slate-700"}`}>
                              {new Date(e.last_confirmed_at).toLocaleString()}
                              {e.stale ? " ⚠ stale" : ""}
                            </dd>
                          </>
                        )}
                      </div>
                    </dl>
                    <p className="text-[11px] text-slate-400">
                      {e.link_source === "subnet"
                        ? "Inferred because both devices have an interface configured into this subnet in their latest config snapshot."
                        : `Confirmed by ${e.link_source.toUpperCase()} neighbor discovery — this is a real reported adjacency, not a guess.`}
                      {e.link_source !== "subnet" && e.stale && (
                        <span className="block text-amber-600 font-semibold mt-1">
                          This link hasn't been reconfirmed by a fresh discovery run in over a week — the device may have rebooted or been recabled since. Re-run SNMP Discovery to refresh it.
                        </span>
                      )}
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