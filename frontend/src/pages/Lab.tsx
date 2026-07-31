import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import {
  GNS3BootstrapRequest,
  GNS3BootstrapResponse,
  GNS3Node,
  GNS3Project,
  GNS3Status,
  GNS3SyncResponse,
} from "../lib/types";

export default function Lab() {
  const { user } = useAuth();
  const canManage = user?.role === "network_admin";

  const [status, setStatus] = useState<GNS3Status | null>(null);
  const [statusLoading, setStatusLoading] = useState(true);

  const [projects, setProjects] = useState<GNS3Project[]>([]);
  const [projectsLoading, setProjectsLoading] = useState(false);
  const [projectsError, setProjectsError] = useState<string | null>(null);

  const [selectedProject, setSelectedProject] = useState<GNS3Project | null>(null);
  const [nodes, setNodes] = useState<GNS3Node[]>([]);
  const [nodesLoading, setNodesLoading] = useState(false);
  const [nodesError, setNodesError] = useState<string | null>(null);

  const [actionError, setActionError] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<string | null>(null);

  const [syncResult, setSyncResult] = useState<GNS3SyncResponse | null>(null);
  const [syncing, setSyncing] = useState(false);

  const [bootstrapNode, setBootstrapNode] = useState<GNS3Node | null>(null);
  const [bootstrapForm, setBootstrapForm] = useState<GNS3BootstrapRequest>({
    mgmt_interface: "GigabitEthernet0/0",
    mgmt_ip: "",
    mgmt_subnet_mask: "255.255.255.0",
    ssh_username: "admin",
    ssh_password: "",
    create_device: true,
    site: "GNS3 Lab",
  });
  const [bootstrapping, setBootstrapping] = useState(false);
  const [bootstrapResult, setBootstrapResult] = useState<GNS3BootstrapResponse | null>(null);
  const [bootstrapError, setBootstrapError] = useState<string | null>(null);

  const loadStatus = () => {
    setStatusLoading(true);
    api
      .get<GNS3Status>("/gns3/status")
      .then((res) => setStatus(res.data))
      .finally(() => setStatusLoading(false));
  };

  useEffect(loadStatus, []);

  const loadProjects = () => {
    setProjectsLoading(true);
    setProjectsError(null);
    api
      .get<GNS3Project[]>("/gns3/projects")
      .then((res) => setProjects(res.data))
      .catch((err) => setProjectsError(err?.response?.data?.detail || "Failed to load GNS3 projects."))
      .finally(() => setProjectsLoading(false));
  };

  useEffect(() => {
    if (status?.enabled && status?.reachable) loadProjects();
  }, [status?.enabled, status?.reachable]);

  const loadNodes = (projectId: string) => {
    setNodesLoading(true);
    setNodesError(null);
    api
      .get<GNS3Node[]>(`/gns3/projects/${projectId}/nodes`)
      .then((res) => setNodes(res.data))
      .catch((err) => setNodesError(err?.response?.data?.detail || "Failed to load nodes."))
      .finally(() => setNodesLoading(false));
  };

  const openProject = async (project: GNS3Project) => {
    setActionError(null);
    setSyncResult(null);
    setPendingAction(`open:${project.project_id}`);
    try {
      const res = await api.post<GNS3Project>(`/gns3/projects/${project.project_id}/open`);
      setSelectedProject(res.data);
      loadNodes(project.project_id);
    } catch (err: any) {
      setActionError(err?.response?.data?.detail || "Failed to open project.");
    } finally {
      setPendingAction(null);
    }
  };

  const doNodeAction = async (node: GNS3Node, action: "start" | "stop") => {
    if (!selectedProject) return;
    setActionError(null);
    setPendingAction(`${action}:${node.node_id}`);
    try {
      await api.post(`/gns3/projects/${selectedProject.project_id}/nodes/${node.node_id}/${action}`);
      loadNodes(selectedProject.project_id);
    } catch (err: any) {
      setActionError(err?.response?.data?.detail || `Failed to ${action} node.`);
    } finally {
      setPendingAction(null);
    }
  };

  const syncProject = async () => {
    if (!selectedProject) return;
    setSyncing(true);
    setActionError(null);
    try {
      const res = await api.post<GNS3SyncResponse>(`/gns3/projects/${selectedProject.project_id}/sync`, {});
      setSyncResult(res.data);
      loadNodes(selectedProject.project_id);
    } catch (err: any) {
      setActionError(err?.response?.data?.detail || "Failed to sync project.");
    } finally {
      setSyncing(false);
    }
  };

  const openBootstrap = (node: GNS3Node) => {
    setBootstrapNode(node);
    setBootstrapResult(null);
    setBootstrapError(null);
    setBootstrapForm((f) => ({ ...f, hostname: node.name }));
  };

  const submitBootstrap = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedProject || !bootstrapNode) return;
    setBootstrapping(true);
    setBootstrapError(null);
    setBootstrapResult(null);
    try {
      const res = await api.post<GNS3BootstrapResponse>(
        `/gns3/projects/${selectedProject.project_id}/nodes/${bootstrapNode.node_id}/bootstrap`,
        bootstrapForm
      );
      setBootstrapResult(res.data);
      if (res.data.success) loadNodes(selectedProject.project_id);
    } catch (err: any) {
      setBootstrapError(err?.response?.data?.detail || "Bootstrap failed.");
    } finally {
      setBootstrapping(false);
    }
  };

  if (statusLoading) {
    return <p className="text-sm text-slate-400 italic">Loading…</p>;
  }

  if (!status?.enabled) {
    return (
      <div>
        <h1 className="text-2xl font-bold text-navy">GNS3 Lab</h1>
        <div className="mt-6 bg-white border border-slate-200 rounded-xl p-6">
          <p className="text-sm text-slate-600">
            GNS3 integration is disabled. Set <code className="bg-slate-100 px-1 rounded">GNS3_ENABLED=true</code>{" "}
            (and the <code className="bg-slate-100 px-1 rounded">GNS3_BASE_URL</code> of your controller) in the
            backend environment to use the lab.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-navy">GNS3 Lab</h1>
          <p className="text-sm text-slate-500 mt-1">
            Validate change requests end-to-end against real virtual routers/switches in a GNS3 topology instead of
            production hardware.
          </p>
        </div>
        <div
          className={`shrink-0 px-3 py-1.5 rounded-full text-xs font-semibold ${
            status.reachable ? "bg-risklow/10 text-risklow" : "bg-riskcrit/10 text-riskcrit"
          }`}
        >
          {status.reachable ? `Connected · ${status.version || "GNS3"}` : "Unreachable"}
        </div>
      </div>

      {!status.reachable && (
        <div className="mt-6 bg-red-50 border border-red-200 rounded-xl p-4 text-sm text-riskcrit">
          {status.detail || `Could not reach the GNS3 controller at ${status.controller_url}.`}
        </div>
      )}

      {status.reachable && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 mt-6">
          <div className="bg-white border border-slate-200 rounded-xl overflow-hidden self-start lg:col-span-1">
            <div className="px-4 py-3 border-b border-slate-100 flex items-center justify-between">
              <h2 className="font-semibold text-navy text-sm">Projects</h2>
              <button
                onClick={loadProjects}
                className="text-xs text-brandblue font-semibold hover:underline"
                disabled={projectsLoading}
              >
                Refresh
              </button>
            </div>
            {projectsError && <p className="text-riskcrit text-xs px-4 py-2">{projectsError}</p>}
            <ul className="divide-y divide-slate-100">
              {projectsLoading && <li className="px-4 py-6 text-center text-slate-400 text-sm">Loading…</li>}
              {!projectsLoading && projects.length === 0 && !projectsError && (
                <li className="px-4 py-6 text-center text-slate-400 text-sm">No GNS3 projects found.</li>
              )}
              {projects.map((p) => (
                <li
                  key={p.project_id}
                  onClick={() => canManage && openProject(p)}
                  className={`px-4 py-3 text-sm ${canManage ? "cursor-pointer hover:bg-blue-50" : ""} ${
                    selectedProject?.project_id === p.project_id ? "bg-blue-50 ring-2 ring-inset ring-brandblue" : ""
                  }`}
                >
                  <p className="font-medium text-navy">{p.name}</p>
                  <p className="text-xs text-slate-500 mt-0.5 capitalize">
                    {p.status || "unknown"}
                    {pendingAction === `open:${p.project_id}` && " · opening…"}
                  </p>
                </li>
              ))}
            </ul>
            {!canManage && (
              <p className="text-xs text-slate-400 italic px-4 py-3 border-t border-slate-100">
                Only a Network Administrator can open projects or manage lab nodes.
              </p>
            )}
          </div>

          <div className="bg-white border border-slate-200 rounded-xl p-5 lg:col-span-2">
            {!selectedProject ? (
              <p className="text-sm text-slate-400 italic">Select a project to view and manage its nodes.</p>
            ) : (
              <div className="space-y-4">
                <div className="flex items-center justify-between gap-2 flex-wrap">
                  <h3 className="font-semibold text-navy">{selectedProject.name}</h3>
                  {canManage && (
                    <button
                      onClick={syncProject}
                      disabled={syncing}
                      className="bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-navy transition-colors disabled:opacity-50"
                    >
                      {syncing ? "Syncing…" : "Sync into Device Inventory"}
                    </button>
                  )}
                </div>

                {actionError && <p className="text-riskcrit text-xs">{actionError}</p>}
                {syncResult && (
                  <div className="rounded-lg bg-green-50 text-risklow text-xs p-3">
                    Synced: {syncResult.created} created, {syncResult.updated} updated, {syncResult.skipped} skipped.
                  </div>
                )}
                {nodesError && <p className="text-riskcrit text-xs">{nodesError}</p>}

                <div className="overflow-hidden rounded-xl border border-slate-200">
                  <table className="w-full text-sm">
                    <thead className="bg-navy text-white">
                      <tr>
                        <th className="text-left px-4 py-3 font-semibold">Node</th>
                        <th className="text-left px-4 py-3 font-semibold">Status</th>
                        <th className="text-left px-4 py-3 font-semibold">Vendor</th>
                        <th className="text-left px-4 py-3 font-semibold">Inventory</th>
                        <th className="text-left px-4 py-3 font-semibold">Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {nodesLoading && (
                        <tr>
                          <td colSpan={5} className="text-center text-slate-400 py-8">
                            Loading…
                          </td>
                        </tr>
                      )}
                      {!nodesLoading && nodes.length === 0 && (
                        <tr>
                          <td colSpan={5} className="text-center text-slate-400 py-8">
                            No nodes in this project.
                          </td>
                        </tr>
                      )}
                      {nodes.map((n, i) => (
                        <tr key={n.node_id} className={i % 2 ? "bg-slate-50" : "bg-white"}>
                          <td className="px-4 py-3">
                            <p className="font-medium text-navy">{n.name}</p>
                            <p className="text-xs text-slate-500">{n.node_type}</p>
                          </td>
                          <td className="px-4 py-3 capitalize">{n.status || "unknown"}</td>
                          <td className="px-4 py-3 capitalize">{n.vendor_guess}</td>
                          <td className="px-4 py-3">
                            {n.bootstrapped ? (
                              <span className="px-2 py-1 rounded-full text-xs font-semibold bg-risklow/10 text-risklow">
                                Bootstrapped · {n.management_ip}
                              </span>
                            ) : n.synced ? (
                              <span className="px-2 py-1 rounded-full text-xs font-semibold bg-amber-100 text-amber-700">
                                Synced, not bootstrapped
                              </span>
                            ) : (
                              <span className="px-2 py-1 rounded-full text-xs font-semibold bg-slate-100 text-slate-500">
                                Not synced
                              </span>
                            )}
                          </td>
                          <td className="px-4 py-3">
                            {canManage ? (
                              <div className="flex flex-wrap gap-1.5">
                                <button
                                  onClick={() => doNodeAction(n, "start")}
                                  disabled={pendingAction === `start:${n.node_id}`}
                                  className="text-xs font-semibold text-brandblue hover:underline disabled:opacity-50"
                                >
                                  Start
                                </button>
                                <button
                                  onClick={() => doNodeAction(n, "stop")}
                                  disabled={pendingAction === `stop:${n.node_id}`}
                                  className="text-xs font-semibold text-slate-500 hover:underline disabled:opacity-50"
                                >
                                  Stop
                                </button>
                                {!n.bootstrapped && n.console_type === "telnet" && (
                                  <button
                                    onClick={() => openBootstrap(n)}
                                    className="text-xs font-semibold text-riskcrit hover:underline"
                                  >
                                    Bootstrap
                                  </button>
                                )}
                              </div>
                            ) : (
                              <span className="text-xs text-slate-400 italic">View only</span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {bootstrapNode && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center p-4 z-50">
          <div className="bg-white rounded-xl p-6 max-w-md w-full space-y-4">
            <div className="flex items-start justify-between">
              <h3 className="font-semibold text-navy">Bootstrap “{bootstrapNode.name}”</h3>
              <button
                onClick={() => setBootstrapNode(null)}
                className="text-slate-400 hover:text-slate-600 text-sm"
              >
                ✕
              </button>
            </div>
            <p className="text-xs text-slate-500">
              Pushes a management IP and SSH access over the node's console, then adds it to Device Inventory.
            </p>
            <form onSubmit={submitBootstrap} className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-semibold text-slate-500 uppercase mb-1">Interface</label>
                  <input
                    className="border border-slate-300 rounded-lg px-3 py-2 text-sm w-full"
                    value={bootstrapForm.mgmt_interface}
                    onChange={(e) => setBootstrapForm((f) => ({ ...f, mgmt_interface: e.target.value }))}
                  />
                </div>
                <div>
                  <label className="block text-xs font-semibold text-slate-500 uppercase mb-1">Subnet Mask</label>
                  <input
                    className="border border-slate-300 rounded-lg px-3 py-2 text-sm w-full"
                    value={bootstrapForm.mgmt_subnet_mask}
                    onChange={(e) => setBootstrapForm((f) => ({ ...f, mgmt_subnet_mask: e.target.value }))}
                  />
                </div>
              </div>
              <div>
                <label className="block text-xs font-semibold text-slate-500 uppercase mb-1">Management IP</label>
                <input
                  required
                  className="border border-slate-300 rounded-lg px-3 py-2 text-sm w-full"
                  value={bootstrapForm.mgmt_ip}
                  onChange={(e) => setBootstrapForm((f) => ({ ...f, mgmt_ip: e.target.value }))}
                  placeholder="10.10.0.11"
                />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-semibold text-slate-500 uppercase mb-1">SSH Username</label>
                  <input
                    className="border border-slate-300 rounded-lg px-3 py-2 text-sm w-full"
                    value={bootstrapForm.ssh_username}
                    onChange={(e) => setBootstrapForm((f) => ({ ...f, ssh_username: e.target.value }))}
                  />
                </div>
                <div>
                  <label className="block text-xs font-semibold text-slate-500 uppercase mb-1">SSH Password</label>
                  <input
                    required
                    type="password"
                    className="border border-slate-300 rounded-lg px-3 py-2 text-sm w-full"
                    value={bootstrapForm.ssh_password}
                    onChange={(e) => setBootstrapForm((f) => ({ ...f, ssh_password: e.target.value }))}
                  />
                </div>
              </div>
              <div>
                <label className="block text-xs font-semibold text-slate-500 uppercase mb-1">
                  Enable Password (optional)
                </label>
                <input
                  type="password"
                  className="border border-slate-300 rounded-lg px-3 py-2 text-sm w-full"
                  value={bootstrapForm.enable_password || ""}
                  onChange={(e) => setBootstrapForm((f) => ({ ...f, enable_password: e.target.value }))}
                />
              </div>

              {bootstrapError && <p className="text-riskcrit text-xs">{bootstrapError}</p>}
              {bootstrapResult && (
                <div
                  className={`rounded-lg p-3 text-xs ${
                    bootstrapResult.success ? "bg-green-50 text-risklow" : "bg-red-50 text-riskcrit"
                  }`}
                >
                  {bootstrapResult.message}
                </div>
              )}

              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => setBootstrapNode(null)}
                  className="bg-slate-200 text-slate-700 rounded-lg px-4 py-2 text-sm font-semibold hover:bg-slate-300 transition-colors"
                >
                  Close
                </button>
                <button
                  type="submit"
                  disabled={bootstrapping}
                  className="bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-navy transition-colors disabled:opacity-50"
                >
                  {bootstrapping ? "Bootstrapping…" : "Run Bootstrap"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}