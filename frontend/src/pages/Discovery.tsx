import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";

interface DiscoveryScan {
  id: string;
  cidr: string;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  error: string | null;
  total_hosts: number;
  responsive_hosts: number;
  new_hosts: number;
  started_by: string | null;
  started_at: string;
  completed_at: string | null;
}

interface DiscoveredHost {
  id: string;
  scan_id: string;
  ip_address: string;
  hostname: string | null;
  mac_address: string | null;
  open_ports: string | null;
  snmp_sys_name: string | null;
  snmp_sys_descr: string | null;
  vendor_guess: string | null;
  response_time_ms: number | null;
  matched_device_id: string | null;
  ipam_status: "unmanaged" | "expected" | "rogue" | "assigned";
  ipam_reservation_note: string | null;
  imported: boolean;
  imported_device_id: string | null;
  ignored: boolean;
  discovered_at: string;
}

interface CredentialSuggestion {
  vendor: string;
  sample_size: number;
  total_vendor_devices: number;
  ssh_credential_ref: string | null;
  ssh_username: string | null;
  snmp_community_ref: string | null;
  snmp_username: string | null;
  snmp_version: string | null;
  snmp_security_level: string | null;
}

const IPAM_BADGE: Record<string, string> = {
  rogue: "bg-red-100 text-red-700",
  expected: "bg-sky-100 text-sky-700",
  assigned: "bg-slate-100 text-slate-500",
  unmanaged: "",
};

const IPAM_LABEL: Record<string, string> = {
  rogue: "rogue — unexpected on managed subnet",
  expected: "expected — reserved in IPAM",
  assigned: "already tracked",
  unmanaged: "",
};

const STATUS_BADGE: Record<string, string> = {
  pending: "bg-slate-100 text-slate-700",
  running: "bg-amber-100 text-amber-700",
  completed: "bg-emerald-100 text-emerald-700",
  failed: "bg-red-100 text-red-700",
  cancelled: "bg-slate-100 text-slate-500",
};

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit",
  });
}

// Scans left running/pending get re-polled on this interval so the list
// picks up completion without a manual refresh -- same idea as the
// Backups/Deployments pages' status polling, just scoped to only the
// scans that are actually still in flight.
const POLL_INTERVAL_MS = 3000;

export default function Discovery() {
  const [scans, setScans] = useState<DiscoveryScan[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  const [cidr, setCidr] = useState("");
  const [community, setCommunity] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  const [selectedScan, setSelectedScan] = useState<DiscoveryScan | null>(null);
  const [hosts, setHosts] = useState<DiscoveredHost[]>([]);
  const [hostsLoading, setHostsLoading] = useState(false);
  const [actioningHostId, setActioningHostId] = useState<string | null>(null);
  const [importTarget, setImportTarget] = useState<DiscoveredHost | null>(null);
  const [importHostname, setImportHostname] = useState("");
  const [importDeviceType, setImportDeviceType] = useState("");
  const [importSnmpVersion, setImportSnmpVersion] = useState("");
  const [importPollInterval, setImportPollInterval] = useState("300");
  const [suggestion, setSuggestion] = useState<CredentialSuggestion | null>(null);
  const [suggestionLoading, setSuggestionLoading] = useState(false);

  const [rescanTargetId, setRescanTargetId] = useState<string | null>(null);
  const [rescanCommunity, setRescanCommunity] = useState("");
  const [rescanningHostId, setRescanningHostId] = useState<string | null>(null);
  const [rescanError, setRescanError] = useState<string | null>(null);

  const pollRef = useRef<number | null>(null);

  const loadScans = async () => {
    try {
      const res = await api.get<DiscoveryScan[]>("/discovery/scans");
      setScans(res.data);
      setError(null);
      return res.data;
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to load discovery scans");
      return [];
    } finally {
      setLoading(false);
    }
  };

  const loadHosts = async (scanId: string) => {
    setHostsLoading(true);
    try {
      const res = await api.get<DiscoveredHost[]>(`/discovery/scans/${scanId}/hosts`);
      setHosts(res.data);
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to load discovered hosts");
    } finally {
      setHostsLoading(false);
    }
  };

  useEffect(() => {
    loadScans();
  }, []);

  // Poll while any scan is pending/running; also refreshes the open
  // results panel so newly-found hosts stream in without a manual click.
  useEffect(() => {
    const hasInFlight = scans.some((s) => s.status === "pending" || s.status === "running");
    if (!hasInFlight) return;

    pollRef.current = window.setInterval(async () => {
      const updated = await loadScans();
      if (selectedScan) {
        const stillTracked = updated.find((s) => s.id === selectedScan.id);
        if (stillTracked) {
          setSelectedScan(stillTracked);
          loadHosts(stillTracked.id);
        }
      }
    }, POLL_INTERVAL_MS);

    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scans.map((s) => s.status).join(","), selectedScan?.id]);

  const openScan = (scan: DiscoveryScan) => {
    setSelectedScan(scan);
    loadHosts(scan.id);
  };

  const startScan = async () => {
    setFormError(null);
    if (!cidr.trim()) {
      setFormError("Enter a CIDR range, e.g. 10.0.4.0/24");
      return;
    }
    setStarting(true);
    try {
      const res = await api.post<DiscoveryScan>("/discovery/scans", {
        cidr: cidr.trim(),
        snmp_community: community.trim() || undefined,
      });
      setCidr("");
      setCommunity("");
      await loadScans();
      openScan(res.data);
    } catch (err: any) {
      setFormError(err?.response?.data?.detail || "Failed to start scan");
    } finally {
      setStarting(false);
    }
  };

  const [cancellingScanId, setCancellingScanId] = useState<string | null>(null);

  const cancelScan = async (scan: DiscoveryScan) => {
    if (!confirm(`Stop the scan of ${scan.cidr}? Hosts already found will be kept.`)) return;
    setCancellingScanId(scan.id);
    try {
      const res = await api.post<DiscoveryScan>(`/discovery/scans/${scan.id}/cancel`);
      setScans((prev) => prev.map((s) => (s.id === res.data.id ? res.data : s)));
      if (selectedScan?.id === scan.id) setSelectedScan(res.data);
    } catch (err: any) {
      alert(err?.response?.data?.detail || "Failed to cancel scan");
    } finally {
      setCancellingScanId(null);
    }
  };

  const [retryingScanId, setRetryingScanId] = useState<string | null>(null);

  const retryScan = async (scan: DiscoveryScan) => {
    setRetryingScanId(scan.id);
    try {
      const res = await api.post<DiscoveryScan>("/discovery/scans", {
        cidr: scan.cidr,
        snmp_community: community.trim() || undefined,
      });
      await loadScans();
      openScan(res.data);
    } catch (err: any) {
      alert(err?.response?.data?.detail || "Failed to retry scan");
    } finally {
      setRetryingScanId(null);
    }
  };

  const deleteScan = async (scan: DiscoveryScan) => {
    if (!confirm(`Delete scan of ${scan.cidr} and its results?`)) return;
    try {
      await api.delete(`/discovery/scans/${scan.id}`);
      if (selectedScan?.id === scan.id) {
        setSelectedScan(null);
        setHosts([]);
      }
      await loadScans();
    } catch (err: any) {
      alert(err?.response?.data?.detail || "Failed to delete scan");
    }
  };

  const ignoreHost = async (host: DiscoveredHost) => {
    setActioningHostId(host.id);
    try {
      await api.post(`/discovery/hosts/${host.id}/ignore`);
      if (selectedScan) await loadHosts(selectedScan.id);
    } catch (err: any) {
      alert(err?.response?.data?.detail || "Failed to ignore host");
    } finally {
      setActioningHostId(null);
    }
  };

  const reserveHost = async (host: DiscoveredHost) => {
    // "This is fine, I know about it" -- creates an IPAM IPReservation
    // for the host's address in place, without making it a managed
    // Device. Complements Import (which does create a Device) and
    // Ignore (which doesn't touch IPAM at all).
    const note = window.prompt(
      `Reserve ${host.ip_address} in IPAM as expected? Optional note (e.g. ticket/owner):`,
      ""
    );
    if (note === null) return; // cancelled
    setActioningHostId(host.id);
    try {
      await api.post(`/discovery/hosts/${host.id}/reserve`, { note: note.trim() || undefined });
      if (selectedScan) await loadHosts(selectedScan.id);
    } catch (err: any) {
      alert(err?.response?.data?.detail || "Failed to reserve this address in IPAM");
    } finally {
      setActioningHostId(null);
    }
  };

  const openRescan = (host: DiscoveredHost) => {
    setRescanTargetId(host.id);
    setRescanCommunity("");
    setRescanError(null);
  };

  const confirmRescan = async () => {
    if (!rescanTargetId) return;
    if (!rescanCommunity.trim()) {
      setRescanError("Enter an SNMP community string");
      return;
    }
    setRescanningHostId(rescanTargetId);
    setRescanError(null);
    try {
      await api.post(`/discovery/hosts/${rescanTargetId}/rescan`, { snmp_community: rescanCommunity.trim() });
      setRescanTargetId(null);
      if (selectedScan) await loadHosts(selectedScan.id);
    } catch (err: any) {
      setRescanError(err?.response?.data?.detail || "Rescan failed");
    } finally {
      setRescanningHostId(null);
    }
  };

  const openImport = async (host: DiscoveredHost) => {
    setImportTarget(host);
    setImportHostname(host.hostname?.replace(/\.$/, "") || host.snmp_sys_name || "");
    // Best-effort device_type guess from sysDescr keywords -- purely a
    // starting point the operator can overwrite, same "never invent
    // data we don't have" posture as the backend's own vendor guessing;
    // left blank if nothing in sysDescr hints at a type.
    const descr = (host.snmp_sys_descr || "").toLowerCase();
    setImportDeviceType(
      descr.includes("switch") ? "switch" : descr.includes("router") ? "router" : descr.includes("firewall") ? "firewall" : ""
    );
    setImportPollInterval("300");
    setSuggestion(null);
    // Best-effort credential-profile suggestion for this host's guessed
    // vendor -- a 404/null response just means "nothing to pre-fill",
    // never blocks the import form from opening.
    setSuggestionLoading(true);
    try {
      const res = await api.get<CredentialSuggestion | null>(`/discovery/hosts/${host.id}/suggested-credentials`);
      setSuggestion(res.data || null);
      setImportSnmpVersion(res.data?.snmp_version || "");
    } catch {
      setSuggestion(null);
      setImportSnmpVersion("");
    } finally {
      setSuggestionLoading(false);
    }
  };

  const confirmImport = async () => {
    if (!importTarget) return;
    if (!importHostname.trim()) {
      alert("Enter a hostname for the new device");
      return;
    }
    setActioningHostId(importTarget.id);
    try {
      await api.post(`/discovery/hosts/${importTarget.id}/import`, {
        hostname: importHostname.trim(),
        vendor: importTarget.vendor_guess?.toLowerCase(),
        device_type: importDeviceType || undefined,
        // Credential *pointers* only (ref names / usernames / SNMP
        // dialect) pre-filled from the suggestion above -- no secret
        // material is ever sent here. The operator still sets the real
        // password/community after import, same as any other device.
        ssh_credential_ref: suggestion?.ssh_credential_ref || undefined,
        ssh_username: suggestion?.ssh_username || undefined,
        snmp_community_ref: suggestion?.snmp_community_ref || undefined,
        snmp_username: suggestion?.snmp_username || undefined,
        // Editable field, defaulted from the suggestion when opening the
        // modal -- what actually turns SNMP polling on for the new
        // device, so it doesn't sit unmonitored until a second trip to
        // the device page.
        snmp_version: importSnmpVersion || undefined,
        snmp_poll_interval_seconds: importPollInterval.trim() ? Number(importPollInterval) : undefined,
      });
      setImportTarget(null);
      setSuggestion(null);
      if (selectedScan) await loadHosts(selectedScan.id);
    } catch (err: any) {
      alert(err?.response?.data?.detail || "Failed to import device");
    } finally {
      setActioningHostId(null);
    }
  };

  return (
    <div className="p-6 space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Network Discovery</h1>
        <p className="text-sm text-slate-500 mt-1">
          Sweep a subnet for live hosts not yet in inventory, then import the ones you want to manage.
        </p>
      </div>

      <div className="rounded-lg border border-slate-200 bg-white p-4 space-y-3">
        <h2 className="font-medium">Start a scan</h2>
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <label className="block text-xs text-slate-500 mb-1">CIDR range</label>
            <input
              className="border rounded px-3 py-1.5 text-sm w-56"
              placeholder="10.0.4.0/24"
              value={cidr}
              onChange={(e) => setCidr(e.target.value)}
              disabled={starting}
            />
          </div>
          <div>
            <label className="block text-xs text-slate-500 mb-1">SNMP community (optional)</label>
            <input
              className="border rounded px-3 py-1.5 text-sm w-56"
              placeholder="public"
              value={community}
              onChange={(e) => setCommunity(e.target.value)}
              disabled={starting}
            />
          </div>
          <button
            className="px-4 py-1.5 rounded bg-slate-900 text-white text-sm disabled:opacity-50"
            onClick={startScan}
            disabled={starting}
          >
            {starting ? "Starting…" : "Start scan"}
          </button>
        </div>
        {formError && <p className="text-sm text-red-600">{formError}</p>}
        <p className="text-xs text-slate-400">
          Up to a /22 (1,024 hosts) per scan. Probes common management ports (22, 23, 80, 443, 161, 3389) —
          no ICMP required. Providing an SNMP community lets NetGuard read a device's sysName/sysDescr for
          identification; leave it blank to just find live IPs.
        </p>
      </div>

      <div className="rounded-lg border border-slate-200 bg-white">
        <div className="px-4 py-3 border-b border-slate-100">
          <h2 className="font-medium">Scan history</h2>
        </div>
        {loading ? (
          <div className="p-4 text-sm text-slate-500">Loading…</div>
        ) : error ? (
          <div className="p-4 text-sm text-red-600">{error}</div>
        ) : scans.length === 0 ? (
          <div className="p-4 text-sm text-slate-500">No scans yet — start one above.</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-xs text-slate-500 border-b border-slate-100">
              <tr>
                <th className="text-left px-4 py-2 font-medium">CIDR</th>
                <th className="text-left px-4 py-2 font-medium">Status</th>
                <th className="text-left px-4 py-2 font-medium">Hosts</th>
                <th className="text-left px-4 py-2 font-medium">Responsive</th>
                <th className="text-left px-4 py-2 font-medium">New</th>
                <th className="text-left px-4 py-2 font-medium">Started</th>
                <th className="text-left px-4 py-2 font-medium">By</th>
                <th className="px-4 py-2" />
              </tr>
            </thead>
            <tbody>
              {scans.map((scan) => (
                <tr
                  key={scan.id}
                  className={`border-b border-slate-50 cursor-pointer hover:bg-slate-50 ${
                    selectedScan?.id === scan.id ? "bg-slate-50" : ""
                  }`}
                  onClick={() => openScan(scan)}
                >
                  <td className="px-4 py-2 font-mono">{scan.cidr}</td>
                  <td className="px-4 py-2">
                    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${STATUS_BADGE[scan.status]}`}>
                      {scan.status === "failed" && (
                        <svg viewBox="0 0 20 20" fill="currentColor" className="w-3 h-3">
                          <path fillRule="evenodd" d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495ZM10 6a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z" clipRule="evenodd" />
                        </svg>
                      )}
                      {scan.status === "failed" ? "error" : scan.status}
                    </span>
                    {scan.status === "failed" && scan.error && (
                      <span className="ml-2 text-xs text-red-500" title={scan.error}>
                        {scan.error.slice(0, 60)}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-2">{scan.total_hosts}</td>
                  <td className="px-4 py-2">{scan.responsive_hosts}</td>
                  <td className="px-4 py-2">
                    {scan.new_hosts > 0 ? (
                      <span className="text-emerald-700 font-medium">{scan.new_hosts}</span>
                    ) : (
                      "0"
                    )}
                  </td>
                  <td className="px-4 py-2 text-slate-500">{fmtDate(scan.started_at)}</td>
                  <td className="px-4 py-2 text-slate-500">{scan.started_by || "—"}</td>
                  <td className="px-4 py-2 text-right space-x-3 whitespace-nowrap">
                    {(scan.status === "pending" || scan.status === "running") && (
                      <button
                        className="text-xs text-amber-600 hover:underline disabled:opacity-50"
                        disabled={cancellingScanId === scan.id}
                        onClick={(e) => {
                          e.stopPropagation();
                          cancelScan(scan);
                        }}
                      >
                        {cancellingScanId === scan.id ? "Stopping…" : "Stop"}
                      </button>
                    )}
                    {scan.status === "failed" && (
                      <button
                        className="text-xs text-navy dark:text-white font-medium hover:underline disabled:opacity-50"
                        disabled={retryingScanId === scan.id}
                        onClick={(e) => {
                          e.stopPropagation();
                          retryScan(scan);
                        }}
                      >
                        {retryingScanId === scan.id ? "Retrying…" : "Retry"}
                      </button>
                    )}
                    <button
                      className="text-xs text-red-500 hover:underline disabled:opacity-50"
                      disabled={scan.status === "running"}
                      title={scan.status === "running" ? "Stop the scan before deleting it" : undefined}
                      onClick={(e) => {
                        e.stopPropagation();
                        deleteScan(scan);
                      }}
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {selectedScan && (
        <div className="rounded-lg border border-slate-200 bg-white">
          <div className="px-4 py-3 border-b border-slate-100 flex items-center justify-between">
            <h2 className="font-medium">
              Results — <span className="font-mono text-sm">{selectedScan.cidr}</span>
            </h2>
            <div className="flex items-center gap-2">
              <span className={`px-2 py-0.5 rounded text-xs font-medium ${STATUS_BADGE[selectedScan.status]}`}>
                {selectedScan.status === "failed" ? "error" : selectedScan.status}
              </span>
              {(selectedScan.status === "pending" || selectedScan.status === "running") && (
                <button
                  className="text-xs text-amber-600 hover:underline disabled:opacity-50"
                  disabled={cancellingScanId === selectedScan.id}
                  onClick={() => cancelScan(selectedScan)}
                >
                  {cancellingScanId === selectedScan.id ? "Stopping…" : "Stop scan"}
                </button>
              )}
              {selectedScan.status === "failed" && (
                <button
                  className="text-xs px-2 py-1 rounded bg-navy text-white dark:bg-white dark:text-navy font-medium hover:opacity-90 disabled:opacity-50"
                  disabled={retryingScanId === selectedScan.id}
                  onClick={() => retryScan(selectedScan)}
                >
                  {retryingScanId === selectedScan.id ? "Retrying…" : "Retry scan"}
                </button>
              )}
            </div>
          </div>
          {selectedScan.status === "failed" && (
            <div className="mx-4 mt-4 rounded-lg border border-red-300 bg-red-50 dark:border-red-500/40 dark:bg-red-950/30 px-4 py-3 flex items-start gap-3">
              <svg viewBox="0 0 20 20" fill="currentColor" className="w-5 h-5 text-red-500 shrink-0 mt-0.5">
                <path fillRule="evenodd" d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495ZM10 6a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z" clipRule="evenodd" />
              </svg>
              <div className="min-w-0">
                <div className="text-sm font-semibold text-red-700 dark:text-red-300">Scan failed to complete</div>
                <div className="text-xs text-red-600 dark:text-red-400 mt-0.5 break-words">
                  {selectedScan.error || "The scan worker did not report a result before the timeout. This usually means the worker queue is backed up or the worker process is not running."}
                </div>
                <div className="text-xs text-red-500/80 dark:text-red-400/70 mt-1">
                  Started {fmtDate(selectedScan.started_at)}{selectedScan.completed_at ? ` · failed ${fmtDate(selectedScan.completed_at)}` : ""}
                </div>
              </div>
            </div>
          )}
          {hostsLoading && hosts.length === 0 ? (
            <div className="p-4 text-sm text-slate-500">Loading…</div>
          ) : hosts.length === 0 ? (
            <div className="p-4 text-sm text-slate-500">
              {selectedScan.status === "running" || selectedScan.status === "pending"
                ? "Scan in progress — results will appear as hosts respond."
                : selectedScan.status === "failed"
                ? "No results — the scan did not complete. Retry above."
                : "No responsive hosts found in this range."}
            </div>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-xs text-slate-500 border-b border-slate-100">
                <tr>
                  <th className="text-left px-4 py-2 font-medium">IP</th>
                  <th className="text-left px-4 py-2 font-medium">Hostname</th>
                  <th className="text-left px-4 py-2 font-medium">Identification</th>
                  <th className="text-left px-4 py-2 font-medium">Open port</th>
                  <th className="text-left px-4 py-2 font-medium">RTT</th>
                  <th className="text-left px-4 py-2 font-medium">Inventory</th>
                  <th className="px-4 py-2" />
                </tr>
              </thead>
              <tbody>
                {hosts.map((host) => (
                  <tr key={host.id} className="border-b border-slate-50">
                    <td className="px-4 py-2 font-mono">{host.ip_address}</td>
                    <td className="px-4 py-2">{host.hostname || "—"}</td>
                    <td className="px-4 py-2">
                      {host.snmp_sys_name || host.vendor_guess ? (
                        <span title={host.snmp_sys_descr || undefined}>
                          {[host.vendor_guess, host.snmp_sys_name].filter(Boolean).join(" · ")}
                        </span>
                      ) : (
                        <div className="flex items-center gap-1.5">
                          <span
                            className="text-slate-400"
                            title="No SNMP sysName/sysDescr on file for this host -- either no SNMP community was supplied for this scan, or the host didn't respond to SNMP."
                          >
                            unidentified
                          </span>
                          {!host.imported && (
                            <button
                              className="text-xs text-slate-900 underline decoration-dotted hover:decoration-solid disabled:opacity-50"
                              disabled={rescanningHostId === host.id}
                              onClick={() => openRescan(host)}
                            >
                              {rescanningHostId === host.id ? "Rescanning…" : "Rescan with SNMP"}
                            </button>
                          )}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-2 font-mono text-xs">{host.open_ports || "—"}</td>
                    <td className="px-4 py-2 text-slate-500">
                      {host.response_time_ms != null ? `${host.response_time_ms} ms` : "—"}
                    </td>
                    <td className="px-4 py-2">
                      {host.matched_device_id ? (
                        <span className="text-slate-500">already tracked</span>
                      ) : host.imported ? (
                        <span className="text-emerald-700">imported</span>
                      ) : host.ignored ? (
                        <span className="text-slate-400">ignored</span>
                      ) : (
                        <div className="flex flex-col gap-0.5">
                          <span className="text-amber-700">new</span>
                          {(host.ipam_status === "rogue" || host.ipam_status === "expected") && (
                            <span
                              className={`inline-block w-fit px-1.5 py-0.5 rounded text-[11px] ${IPAM_BADGE[host.ipam_status]}`}
                              title={host.ipam_status === "expected" ? host.ipam_reservation_note || undefined : undefined}
                            >
                              {IPAM_LABEL[host.ipam_status]}
                            </span>
                          )}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-2 text-right space-x-2 whitespace-nowrap">
                      {!host.matched_device_id && !host.imported && !host.ignored && (
                        <>
                          <button
                            className="text-xs text-slate-900 font-medium hover:underline disabled:opacity-50"
                            disabled={actioningHostId === host.id}
                            onClick={() => openImport(host)}
                          >
                            Import
                          </button>
                          {host.ipam_status === "rogue" && (
                            <button
                              className="text-xs text-sky-700 hover:underline disabled:opacity-50"
                              disabled={actioningHostId === host.id}
                              onClick={() => reserveHost(host)}
                              title="Acknowledge this as expected and hold its IP in IPAM, without adding it as a managed device"
                            >
                              Reserve
                            </button>
                          )}
                          <button
                            className="text-xs text-slate-400 hover:underline disabled:opacity-50"
                            disabled={actioningHostId === host.id}
                            onClick={() => ignoreHost(host)}
                          >
                            Ignore
                          </button>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {importTarget && (
        <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-5 w-[26rem] space-y-3">
            <h3 className="font-medium">Import {importTarget.ip_address} as a device</h3>
            <div>
              <label className="block text-xs text-slate-500 mb-1">Hostname</label>
              <input
                className="border rounded px-3 py-1.5 text-sm w-full"
                value={importHostname}
                onChange={(e) => setImportHostname(e.target.value)}
                autoFocus
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs text-slate-500 mb-1">Device type</label>
                <input
                  className="border rounded px-3 py-1.5 text-sm w-full"
                  placeholder="switch, router, firewall…"
                  value={importDeviceType}
                  onChange={(e) => setImportDeviceType(e.target.value)}
                />
              </div>
              <div>
                <label className="block text-xs text-slate-500 mb-1">SNMP version</label>
                <select
                  className="border rounded px-3 py-1.5 text-sm w-full bg-white"
                  value={importSnmpVersion}
                  onChange={(e) => setImportSnmpVersion(e.target.value)}
                >
                  <option value="">Not set</option>
                  <option value="v1">v1</option>
                  <option value="v2c">v2c</option>
                  <option value="v3">v3</option>
                </select>
              </div>
            </div>
            <div>
              <label className="block text-xs text-slate-500 mb-1">SNMP poll interval (seconds)</label>
              <input
                type="number"
                min={30}
                step={30}
                className="border rounded px-3 py-1.5 text-sm w-32"
                value={importPollInterval}
                onChange={(e) => setImportPollInterval(e.target.value)}
              />
              <p className="text-[11px] text-slate-400 mt-1">
                Sets up polling on import — leave blank to import unmonitored and configure it later on the device page.
              </p>
            </div>
            {importTarget.vendor_guess && (
              <p className="text-xs text-slate-500">
                Vendor detected as <span className="font-medium">{importTarget.vendor_guess}</span>.
              </p>
            )}
            {(importTarget.ipam_status === "rogue" || importTarget.ipam_status === "expected") && (
              <p className={`text-xs rounded px-2 py-1 ${IPAM_BADGE[importTarget.ipam_status]}`}>
                {IPAM_LABEL[importTarget.ipam_status]}
                {importTarget.ipam_status === "expected" && importTarget.ipam_reservation_note && (
                  <> — {importTarget.ipam_reservation_note}</>
                )}
              </p>
            )}
            {suggestionLoading ? (
              <p className="text-xs text-slate-400">Checking for a matching credential profile…</p>
            ) : (
              suggestion && (
                <p className="text-xs text-slate-500 bg-slate-50 rounded px-2 py-1">
                  Matches the credential profile used by {suggestion.sample_size} other{" "}
                  {suggestion.vendor} device{suggestion.sample_size === 1 ? "" : "s"}
                  {suggestion.ssh_credential_ref && (
                    <> — SSH ref <span className="font-mono">{suggestion.ssh_credential_ref}</span></>
                  )}
                  {suggestion.snmp_community_ref && (
                    <>, SNMP ref <span className="font-mono">{suggestion.snmp_community_ref}</span></>
                  )}
                  {" "}will be pre-filled on this device (secrets still need to be set after import).
                </p>
              )
            )}
            <div className="flex justify-end gap-2 pt-2">
              <button
                className="px-3 py-1.5 rounded text-sm text-slate-600"
                onClick={() => {
                  setImportTarget(null);
                  setSuggestion(null);
                }}
              >
                Cancel
              </button>
              <button
                className="px-3 py-1.5 rounded bg-slate-900 text-white text-sm disabled:opacity-50"
                disabled={actioningHostId === importTarget.id}
                onClick={confirmImport}
              >
                {actioningHostId === importTarget.id ? "Importing…" : "Add device"}
              </button>
            </div>
          </div>
        </div>
      )}

      {rescanTargetId && (
        <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-5 w-96 space-y-3">
            <h3 className="font-medium">Rescan with SNMP</h3>
            <p className="text-xs text-slate-500">
              Re-probes just this host with a community string to pick up its sysName/sysDescr — doesn't
              re-run the whole range.
            </p>
            <div>
              <label className="block text-xs text-slate-500 mb-1">SNMP community</label>
              <input
                className="border rounded px-3 py-1.5 text-sm w-full"
                placeholder="public"
                value={rescanCommunity}
                onChange={(e) => setRescanCommunity(e.target.value)}
                autoFocus
              />
            </div>
            {rescanError && <p className="text-sm text-red-600">{rescanError}</p>}
            <div className="flex justify-end gap-2 pt-2">
              <button
                className="px-3 py-1.5 rounded text-sm text-slate-600"
                onClick={() => setRescanTargetId(null)}
              >
                Cancel
              </button>
              <button
                className="px-3 py-1.5 rounded bg-slate-900 text-white text-sm disabled:opacity-50"
                disabled={rescanningHostId === rescanTargetId}
                onClick={confirmRescan}
              >
                {rescanningHostId === rescanTargetId ? "Rescanning…" : "Rescan"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}