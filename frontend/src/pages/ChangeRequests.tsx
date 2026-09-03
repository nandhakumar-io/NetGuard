import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { BlastRadiusPreview, ChangePriority, ChangeRequest, ChangeStatus, CombinedValidationReport, ConfigTemplate, Device, ImpactSimulationPreview, PendingApprovalItem, TemplateDiffPreviewResponse, ApprovalChain, ThreeWayDiff } from "../lib/types";
import RiskBadge from "../components/RiskBadge";
import ImpactSimulationPanel from "../components/ImpactSimulationPanel";
import ValidationPipelinePanel from "../components/ValidationPipelinePanel";
import EvidencePanel from "../components/EvidencePanel";
import ConfigDiff from "../components/ConfigDiff";
import SideBySideDiff from "../components/SideBySideDiff";
import { useAuth } from "../lib/auth";
import { useSearchParams } from "react-router-dom";
import { downloadCsv, exportButtonClass, toCsv, todayStamp } from "../lib/csv";

const statusStyle: Record<string, string> = {
  draft: "bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300",
  pending_approval: "bg-amber-100 text-amber-700",
  approved: "bg-blue-100 text-blue-700",
  rejected: "bg-red-100 text-red-700",
  validating: "bg-blue-100 text-blue-700",
  deploying: "bg-blue-100 text-blue-700",
  monitoring: "bg-blue-100 text-blue-700",
  success: "bg-green-100 text-green-700",
  failed: "bg-red-100 text-red-700",
  rolled_back: "bg-red-100 text-red-700",
};

const priorityStyle: Record<ChangePriority, string> = {
  low: "bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300",
  medium: "bg-blue-100 text-blue-700",
  high: "bg-amber-100 text-amber-700",
  emergency: "bg-red-100 text-red-700",
};

const DRIFT_DIRECTION_STYLE: Record<ThreeWayDiff["drift_direction"], { badge: string; label: string; panel: string }> = {
  toward_compliance: {
    badge: "bg-green-100 text-green-700",
    label: "✓ Moves toward golden baseline",
    panel: "border-green-200 bg-green-50 dark:bg-green-950/20 dark:border-green-900",
  },
  away_from_compliance: {
    badge: "bg-red-100 text-red-700",
    label: "⚠ Drifts away from golden baseline",
    panel: "border-red-300 bg-red-50 dark:bg-red-950/20 dark:border-red-800",
  },
  unchanged: {
    badge: "bg-slate-100 text-slate-600",
    label: "No change in compliance drift",
    panel: "border-slate-200 bg-slate-50 dark:bg-slate-900 dark:border-slate-700",
  },
  unknown: {
    badge: "bg-slate-100 text-slate-500",
    label: "No golden config set for this device",
    panel: "border-slate-200 bg-slate-50 dark:bg-slate-900 dark:border-slate-700",
  },
};

// Running vs. golden vs. proposed -- not just "what does this change do"
// (the plain two-way diff below already shows that) but "does this
// change move the device toward or away from its approved baseline".
function ThreeWayDiffPanel({ diff, loading }: { diff: ThreeWayDiff | null; loading: boolean }) {
  const [leg, setLeg] = useState<"golden_vs_current" | "golden_vs_proposed">("golden_vs_proposed");

  if (loading && !diff) {
    return (
      <div className="border border-slate-200 dark:border-slate-700 rounded-lg p-3 text-xs bg-slate-50 dark:bg-slate-900">
        <p className="font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 mb-1">
          3-Way Diff (Running vs. Golden vs. Proposed)
        </p>
        <p className="text-slate-400">Comparing against golden config…</p>
      </div>
    );
  }
  if (!diff) return null;

  const style = DRIFT_DIRECTION_STYLE[diff.drift_direction] || DRIFT_DIRECTION_STYLE.unknown;

  return (
    <div className={`border rounded-lg p-3 text-xs ${style.panel}`}>
      <div className="flex items-center justify-between mb-1">
        <p className="font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">
          3-Way Diff (Running vs. Golden vs. Proposed)
        </p>
        <span className={`px-2 py-0.5 rounded-full text-[10px] font-bold ${style.badge}`}>{style.label}</span>
      </div>

      {!diff.golden_available ? (
        <p className="text-slate-500 dark:text-slate-400 mt-1">
          Set a golden config for this device (Devices → device detail → Backups tab) to see whether changes move it
          toward or away from compliance.
        </p>
      ) : (
        <>
          <p className="text-slate-700 dark:text-slate-200 font-mono mt-1">
            Drift from golden: <span className="font-bold">{diff.current_drift_lines}</span> lines today →{" "}
            <span className="font-bold">{diff.proposed_drift_lines}</span> lines after this change
          </p>
          <div className="flex mt-2 rounded-lg border border-slate-200 dark:border-slate-700 overflow-hidden text-[10px] normal-case font-medium w-fit">
            <button
              type="button"
              onClick={() => setLeg("golden_vs_current")}
              className={`px-2 py-0.5 ${leg === "golden_vs_current" ? "bg-navy dark:bg-slate-950 text-white" : "bg-white dark:bg-slate-800 text-slate-500 dark:text-slate-400"}`}
            >
              Golden vs. Running
            </button>
            <button
              type="button"
              onClick={() => setLeg("golden_vs_proposed")}
              className={`px-2 py-0.5 ${leg === "golden_vs_proposed" ? "bg-navy dark:bg-slate-950 text-white" : "bg-white dark:bg-slate-800 text-slate-500 dark:text-slate-400"}`}
            >
              Golden vs. Proposed
            </button>
          </div>
          <div className="mt-2">
            <ConfigDiff diffText={leg === "golden_vs_current" ? diff.golden_vs_current : diff.golden_vs_proposed} />
          </div>
        </>
      )}
    </div>
  );
}

const emptyForm = {
  device_id: "",
  description: "",
  business_justification: "",
  priority: "medium" as ChangePriority,
  proposed_config: "",
  additional_device_ids: [] as string[],
  canary_enabled: false,
  maintenance_window_start: "" as string,
  maintenance_window_end: "" as string,
  // Auto-link (postmortem traceability): set when this form was opened
  // via "Create Change Request" from an Alert Center alert (?alert_id=...
  // on this page's URL, see the useSearchParams effect below).
  alert_id: null as string | null,
};

const STATUS_FILTERS: { value: ChangeStatus | "all"; label: string }[] = [
  { value: "all", label: "All" },
  { value: "pending_approval", label: "Pending" },
  { value: "approved", label: "Approved" },
  { value: "success", label: "Success" },
  { value: "failed", label: "Failed" },
  { value: "rejected", label: "Rejected" },
];

export default function ChangeRequests() {
  const { user } = useAuth();
  const canApprove = user?.role === "network_admin";
  const [requests, setRequests] = useState<ChangeRequest[]>([]);
  const [digestDownloading, setDigestDownloading] = useState(false);
  const [devices, setDevices] = useState<Device[]>([]);
  const [selected, setSelected] = useState<ChangeRequest | null>(null);

  // Multi-stage approval chain (Peer Review -> Manager Sign-off -> Admin
  // Approval) for the currently selected change request, if it has one.
  const [approvalChain, setApprovalChain] = useState<ApprovalChain | null>(null);
  const [approvalChainLoading, setApprovalChainLoading] = useState(false);
  const [chainActing, setChainActing] = useState(false);
  const [chainActionError, setChainActionError] = useState<string | null>(null);
  const [chainNotes, setChainNotes] = useState("");
  const [form, setForm] = useState(emptyForm);
  const [loading, setLoading] = useState(false);
  const [initialLoading, setInitialLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [acting, setActing] = useState(false);
  const [statusFilter, setStatusFilter] = useState<ChangeStatus | "all">("all");
  const [showForm, setShowForm] = useState(false);
  // Peer-review diff toggle on the detail panel: unified (compact, good
  // for scanning line-level changes) vs side-by-side (easier to compare
  // whole blocks old-vs-new before approving).
  const [diffView, setDiffView] = useState<"unified" | "side-by-side">("unified");
  // Approval workflow visibility: the pending-approval queue with SLA
  // timers (GET /change-requests/pending-approvals), shown in its own tab
  // rather than the plain list.
  const [queueTab, setQueueTab] = useState<"all" | "queue">("all");
  const [pendingQueue, setPendingQueue] = useState<PendingApprovalItem[]>([]);
  const [queueLoading, setQueueLoading] = useState(false);

  const [searchParams, setSearchParams] = useSearchParams();

  // Deep link from Alert Center's "Create Change Request" action
  // (?alert_id=...&device_id=...): opens the form pre-filled and
  // pre-linked to that alert, so the resulting CR auto-links back to the
  // incident for postmortem (see triggering_alert_id).
  useEffect(() => {
    const alertId = searchParams.get("alert_id");
    if (!alertId) return;
    const deviceId = searchParams.get("device_id") || "";
    const category = searchParams.get("category") || "";
    setForm((prev) => ({
      ...prev,
      alert_id: alertId,
      device_id: deviceId,
      description: category ? `Remediate: ${category}` : prev.description,
    }));
    setShowForm(true);
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.delete("alert_id");
      next.delete("device_id");
      next.delete("category");
      return next;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadQueue = () => {
    setQueueLoading(true);
    api
      .get<PendingApprovalItem[]>("/change-requests/pending-approvals")
      .then((res) => setPendingQueue(res.data))
      .finally(() => setQueueLoading(false));
  };

  useEffect(() => {
    if (queueTab !== "queue") return;
    loadQueue();
    const interval = setInterval(loadQueue, 30000);
    return () => clearInterval(interval);
  }, [queueTab]);

  // Config template picker (Jinja2 provisioning templates -- see
  // /templates page and app/services/template_service.py). Filtered by
  // the selected primary device's device_role so the operator only sees
  // templates that actually apply, instead of the full library.
  // Blast-radius preview (device_id + additional_device_ids -> "touches N
  // devices, M core, K more depend on them via topology") -- refetched
  // whenever the target device selection changes, so a risky-looking
  // fan-out gets flagged before the change is even submitted.
  const [blastRadius, setBlastRadius] = useState<BlastRadiusPreview | null>(null);
  const [blastRadiusLoading, setBlastRadiusLoading] = useState(false);

  useEffect(() => {
    if (!form.device_id) {
      setBlastRadius(null);
      return;
    }
    let cancelled = false;
    setBlastRadiusLoading(true);
    const timer = setTimeout(() => {
      api
        .get("/change-requests/blast-radius", {
          params: {
            device_id: form.device_id,
            additional_device_ids: form.additional_device_ids.length ? form.additional_device_ids.join(",") : undefined,
          },
        })
        .then((res) => {
          if (!cancelled) setBlastRadius(res.data);
        })
        .catch(() => {
          if (!cancelled) setBlastRadius(null);
        })
        .finally(() => {
          if (!cancelled) setBlastRadiusLoading(false);
        });
    }, 300);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [form.device_id, form.additional_device_ids.join(",")]);

  // Pre-deployment impact simulation ("what-if" dry run): unlike the
  // blast-radius count above, this actually parses the proposed config
  // for interface shutdowns / lockout ACLs, removes the matching links
  // from the live topology graph, and re-checks reachability -- so it
  // needs proposed_config too, not just the device selection.
  const [impactSim, setImpactSim] = useState<ImpactSimulationPreview | null>(null);
  const [impactSimLoading, setImpactSimLoading] = useState(false);

  useEffect(() => {
    if (!form.device_id || !form.proposed_config.trim()) {
      setImpactSim(null);
      return;
    }
    let cancelled = false;
    setImpactSimLoading(true);
    const timer = setTimeout(() => {
      api
        .post("/change-requests/simulate-impact", {
          device_id: form.device_id,
          proposed_config: form.proposed_config,
        })
        .then((res) => {
          if (!cancelled) setImpactSim(res.data);
        })
        .catch(() => {
          if (!cancelled) setImpactSim(null);
        })
        .finally(() => {
          if (!cancelled) setImpactSimLoading(false);
        });
    }, 500);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [form.device_id, form.proposed_config]);

  // Same dry run, re-run against an already-submitted CR's stored
  // current/proposed config when it's opened in the detail panel -- lets
  // a reviewer check routing impact against the topology as it stands
  // right now, not just what it looked like at submission time.
  const [selectedImpactSim, setSelectedImpactSim] = useState<ImpactSimulationPreview | null>(null);
  const [selectedImpactSimLoading, setSelectedImpactSimLoading] = useState(false);

  useEffect(() => {
    if (!selected) {
      setSelectedImpactSim(null);
      return;
    }
    let cancelled = false;
    setSelectedImpactSimLoading(true);
    api
      .get<ImpactSimulationPreview>(`/change-requests/${selected.id}/simulate-impact`)
      .then((res) => {
        if (!cancelled) setSelectedImpactSim(res.data);
      })
      .catch(() => {
        if (!cancelled) setSelectedImpactSim(null);
      })
      .finally(() => {
        if (!cancelled) setSelectedImpactSimLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selected?.id]);

  // Three-way diff (running vs. golden vs. proposed) -- lets a reviewer
  // see whether this change moves the device toward or away from its
  // approved baseline, not just what the change itself does.
  const [selectedThreeWayDiff, setSelectedThreeWayDiff] = useState<ThreeWayDiff | null>(null);
  const [selectedThreeWayDiffLoading, setSelectedThreeWayDiffLoading] = useState(false);

  useEffect(() => {
    if (!selected) {
      setSelectedThreeWayDiff(null);
      return;
    }
    let cancelled = false;
    setSelectedThreeWayDiffLoading(true);
    api
      .get<ThreeWayDiff>(`/change-requests/${selected.id}/three-way-diff`)
      .then((res) => {
        if (!cancelled) setSelectedThreeWayDiff(res.data);
      })
      .catch(() => {
        if (!cancelled) setSelectedThreeWayDiff(null);
      })
      .finally(() => {
        if (!cancelled) setSelectedThreeWayDiffLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selected?.id]);

  // OPA + Batfish validation pipeline (Section 15) for the CR detail
  // panel. Loads the last-computed result (GET .../validation, 404 if
  // never run) and exposes a manual re-run (POST .../validate) so a
  // reviewer can get a fresh read before acting on approval -- see
  // ValidationPipelinePanel for rendering.
  const [selectedValidation, setSelectedValidation] = useState<CombinedValidationReport | null>(null);
  const [selectedValidationLoading, setSelectedValidationLoading] = useState(false);
  const [validationRerunning, setValidationRerunning] = useState(false);

  useEffect(() => {
    if (!selected) {
      setSelectedValidation(null);
      return;
    }
    let cancelled = false;
    setSelectedValidationLoading(true);
    api
      .get<CombinedValidationReport>(`/change-requests/${selected.id}/validation`)
      .then((res) => {
        if (!cancelled) setSelectedValidation(res.data);
      })
      .catch(() => {
        if (!cancelled) setSelectedValidation(null);
      })
      .finally(() => {
        if (!cancelled) setSelectedValidationLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selected?.id]);

  const rerunValidation = async () => {
    if (!selected) return;
    setValidationRerunning(true);
    try {
      const res = await api.post<CombinedValidationReport>(`/change-requests/${selected.id}/validate`);
      setSelectedValidation(res.data);
    } catch {
      // Leave the last-known result in place; the button stays available to retry.
    } finally {
      setValidationRerunning(false);
    }
  };

  const [availableTemplates, setAvailableTemplates] = useState<ConfigTemplate[]>([]);
  const [selectedTemplateId, setSelectedTemplateId] = useState("");
  const [templateValues, setTemplateValues] = useState<Record<string, string>>({});
  const [templateRendering, setTemplateRendering] = useState(false);
  const [templateError, setTemplateError] = useState<string | null>(null);
  const [templateDiffPreview, setTemplateDiffPreview] = useState<TemplateDiffPreviewResponse | null>(null);
  const [templateDiffLoading, setTemplateDiffLoading] = useState(false);
  const [templateDiffError, setTemplateDiffError] = useState<string | null>(null);

  const load = () => {
    Promise.all([api.get<ChangeRequest[]>("/change-requests"), api.get<Device[]>("/devices")])
      .then(([reqRes, devRes]) => {
        setRequests(reqRes.data);
        setDevices(devRes.data);
        setSelected((prev) => (prev ? reqRes.data.find((r) => r.id === prev.id) || null : null));
      })
      .finally(() => setInitialLoading(false));
  };

  useEffect(load, []);

  // Reload the applicable template list whenever the form is open and
  // the primary device (-> device_role) changes -- a template tied to
  // "access" role shouldn't show up when a "core" device is selected.
  useEffect(() => {
    if (!showForm) return;
    const device = devices.find((d) => d.id === form.device_id);
    const params: Record<string, string> = {};
    if (device?.device_role) params.device_role = device.device_role;
    if (device?.vendor) params.vendor = device.vendor;
    api
      .get<ConfigTemplate[]>("/config-templates", { params })
      .then((res) => setAvailableTemplates(res.data))
      .catch(() => setAvailableTemplates([]));
    setSelectedTemplateId("");
    setTemplateValues({});
    setTemplateError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showForm, form.device_id]);

  const selectedTemplate = availableTemplates.find((t) => t.id === selectedTemplateId) || null;

  const selectTemplate = (id: string) => {
    setSelectedTemplateId(id);
    setTemplateError(null);
    setTemplateDiffPreview(null);
    setTemplateDiffError(null);
    const t = availableTemplates.find((tpl) => tpl.id === id);
    const initial: Record<string, string> = {};
    t?.variables.forEach((v) => {
      initial[v.name] = v.default || "";
    });
    setTemplateValues(initial);
  };

  const applyTemplate = async () => {
    if (!selectedTemplate) return;
    setTemplateRendering(true);
    setTemplateError(null);
    try {
      const res = await api.post<{ rendered_config: string }>(
        `/config-templates/${selectedTemplate.id}/render`,
        { variables: templateValues }
      );
      setForm((f) => ({ ...f, proposed_config: res.data.rendered_config }));
    } catch (err: any) {
      setTemplateError(err?.response?.data?.detail || "Failed to render template.");
    } finally {
      setTemplateRendering(false);
    }
  };

  // Rendered-config diff preview (not raw template text): renders the
  // template with the current variable values, then diffs that rendered
  // output against the target device's golden config (falling back to a
  // live read if none is set) -- so a reviewer sees the actual delta the
  // template produces on this device, not just {{ placeholders }} in
  // isolation.
  const previewTemplateDiff = async () => {
    if (!selectedTemplate || !form.device_id) return;
    setTemplateDiffLoading(true);
    setTemplateDiffError(null);
    try {
      const res = await api.post<TemplateDiffPreviewResponse>(
        `/config-templates/${selectedTemplate.id}/diff-preview`,
        { device_id: form.device_id, variables: templateValues, compare_against: "golden" }
      );
      setTemplateDiffPreview(res.data);
    } catch (err: any) {
      // No golden config set for this device yet -- fall back to a live read.
      if (err?.response?.status === 404) {
        try {
          const res = await api.post<TemplateDiffPreviewResponse>(
            `/config-templates/${selectedTemplate.id}/diff-preview`,
            { device_id: form.device_id, variables: templateValues, compare_against: "live" }
          );
          setTemplateDiffPreview(res.data);
          return;
        } catch (err2: any) {
          setTemplateDiffError(err2?.response?.data?.detail || "Failed to preview template diff.");
          return;
        }
      }
      setTemplateDiffError(err?.response?.data?.detail || "Failed to preview template diff.");
    } finally {
      setTemplateDiffLoading(false);
    }
  };

  // Keep the detail panel fresh while a pipeline is running.
  useEffect(() => {
    if (!selected || !["approved", "validating", "deploying", "monitoring"].includes(selected.status)) return;
    const interval = setInterval(load, 4000);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.id, selected?.status]);

  // Load (and, while pending, keep refreshing) the approval chain for
  // whichever change request is selected -- only PENDING_APPROVAL requests
  // can have an in-progress chain, so no need to poll once it's resolved.
  const loadApprovalChain = (id: string) => {
    setApprovalChainLoading(true);
    api
      .get<ApprovalChain>(`/change-requests/${id}/approval-chain`)
      .then((res) => setApprovalChain(res.data))
      .catch(() => setApprovalChain(null))
      .finally(() => setApprovalChainLoading(false));
  };

  useEffect(() => {
    setChainNotes("");
    setChainActionError(null);
    if (!selected) {
      setApprovalChain(null);
      return;
    }
    loadApprovalChain(selected.id);
    if (selected.status !== "pending_approval") return;
    const interval = setInterval(() => loadApprovalChain(selected.id), 5000);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.id, selected?.status]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      await api.post("/change-requests", {
        ...form,
        maintenance_window_start: form.maintenance_window_start
          ? new Date(form.maintenance_window_start).toISOString()
          : null,
        maintenance_window_end: form.maintenance_window_end
          ? new Date(form.maintenance_window_end).toISOString()
          : null,
      });
      setForm(emptyForm);
      setShowForm(false);
      load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to submit change request.");
    } finally {
      setLoading(false);
    }
  };

  const act = async (id: string, action: "approve" | "reject" | "rescore") => {
    setActing(true);
    setActionError(null);
    try {
      await api.post(`/change-requests/${id}/${action}`);
      load();
      if (queueTab === "queue") loadQueue();
    } catch (err: any) {
      setActionError(err?.response?.data?.detail || `Failed to ${action} the request.`);
    } finally {
      setActing(false);
    }
  };

  // Peer Review / Manager Sign-off stage action. Rejecting requires a
  // reason (segregation-of-duties audit trail) even though the API
  // itself only requires notes on rejection informally -- enforced here.
  const actOnChain = async (id: string, approve: boolean) => {
    if (!approve && !chainNotes.trim()) {
      setChainActionError("A reason is required when rejecting a stage.");
      return;
    }
    setChainActing(true);
    setChainActionError(null);
    try {
      await api.post(`/change-requests/${id}/approval-chain/act`, { approve, notes: chainNotes || undefined });
      setChainNotes("");
      loadApprovalChain(id);
      load();
      if (queueTab === "queue") loadQueue();
    } catch (err: any) {
      setChainActionError(err?.response?.data?.detail || "Failed to record your decision on this stage.");
    } finally {
      setChainActing(false);
    }
  };

  const hostnameFor = (deviceId: string) => devices.find((d) => d.id === deviceId)?.hostname || deviceId.slice(0, 8);

  // Distinct device_roles present among devices, for the "add all devices
  // with this role" bulk-select shortcut below (ties into per-device-role
  // compliance baselines on the Drift page: e.g. roll a change out to
  // every "access" switch at once).
  const rolesAvailable = useMemo(
    () => Array.from(new Set(devices.map((d) => d.device_role).filter((r): r is string => !!r))).sort(),
    [devices]
  );

  const toggleAdditionalDevice = (id: string) => {
    setForm((f) => ({
      ...f,
      additional_device_ids: f.additional_device_ids.includes(id)
        ? f.additional_device_ids.filter((x) => x !== id)
        : [...f.additional_device_ids, id],
    }));
  };

  const addAllWithRole = (role: string) => {
    if (!role) return;
    const matchIds = devices.filter((d) => d.device_role === role && d.id !== form.device_id).map((d) => d.id);
    setForm((f) => ({
      ...f,
      additional_device_ids: Array.from(new Set([...f.additional_device_ids, ...matchIds])),
    }));
  };

  const filtered = useMemo(
    () => (statusFilter === "all" ? requests : requests.filter((r) => r.status === statusFilter)),
    [requests, statusFilter]
  );

  const pendingCount = requests.filter((r) => r.status === "pending_approval").length;

  const exportCsv = () => {
    downloadCsv(
      `netguard-change-requests-${todayStamp()}.csv`,
      toCsv(filtered, [
        { header: "Device", value: (r) => hostnameFor(r.device_id) },
        { header: "Status", value: (r) => r.status },
        { header: "Priority", value: (r) => r.priority },
        { header: "Description", value: (r) => r.description },
        { header: "Risk Classification", value: (r) => r.risk_classification || "" },
        { header: "Risk Score", value: (r) => r.risk_score ?? "" },
        { header: "Submitted By", value: (r) => r.submitted_by_name || r.submitted_by },
        { header: "Approved By", value: (r) => r.approved_by_name || r.approved_by || "" },
        { header: "Approved At", value: (r) => (r.approved_at ? new Date(r.approved_at).toISOString() : "") },
        { header: "Dual Approval", value: (r) => (r.requires_dual_approval ? "Yes" : "No") },
        { header: "Is Rollback", value: (r) => (r.is_rollback === "true" ? "Yes" : "No") },
        { header: "Created At", value: (r) => new Date(r.created_at).toISOString() },
      ])
    );
  };

  const downloadWeeklyDigest = async () => {
    setDigestDownloading(true);
    try {
      const res = await api.get("/reports/change-request-digest", { params: { days: 7 }, responseType: "blob" as any });
      const blob = new Blob([res.data], { type: "text/csv" });
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `netguard-change-request-digest-${todayStamp()}.csv`;
      a.click();
      window.URL.revokeObjectURL(url);
    } catch (err: any) {
      alert(err?.response?.data?.detail || "Failed to download digest");
    } finally {
      setDigestDownloading(false);
    }
  };

  return (
    <div>
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-navy dark:text-white">Change Requests</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            Submit configuration changes for AI risk analysis, validation, and approval.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={exportCsv} disabled={filtered.length === 0} className={`${exportButtonClass} disabled:opacity-40`}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><path d="M7 10l5 5 5-5" /><path d="M12 15V3" />
            </svg>
            Export CSV
          </button>
          <button
            onClick={downloadWeeklyDigest}
            disabled={digestDownloading}
            title="Server-computed weekly digest: volume, status breakdown, risk, approval time, and the full org-wide pending queue — not just what's on screen"
            className={`${exportButtonClass} disabled:opacity-40`}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <rect x="3" y="4" width="18" height="16" rx="2" /><path d="M3 9h18" /><path d="M8 3v3M16 3v3" />
            </svg>
            {digestDownloading ? "Preparing…" : "Weekly Digest"}
          </button>
          <button
            onClick={() => setShowForm((s) => !s)}
            disabled={!devices.length}
            className="bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-navy dark:bg-slate-950 transition-colors disabled:opacity-50"
          >
            {showForm ? "Cancel" : "+ New Change Request"}
          </button>
        </div>
      </div>

      {!devices.length && !initialLoading && (
        <p className="text-xs text-slate-400 dark:text-slate-500 mt-3">Add a device first under the Devices page.</p>
      )}

      {showForm && (
        <form onSubmit={submit} className="mt-5 bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-5 space-y-3">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <select
              className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm"
              value={form.device_id}
              onChange={(e) => setForm({ ...form, device_id: e.target.value })}
              required
            >
              <option value="">Select device…</option>
              {devices.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.hostname} ({d.ip_address})
                </option>
              ))}
            </select>
            <input
              className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm"
              placeholder="Change description"
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
              required
            />
            <select
              className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm"
              value={form.priority}
              onChange={(e) => setForm({ ...form, priority: e.target.value as ChangePriority })}
            >
              <option value="low">Low priority</option>
              <option value="medium">Medium priority</option>
              <option value="high">High priority</option>
              <option value="emergency">Emergency</option>
            </select>
          </div>

          {/* Bulk / multi-device deploy (SRS 6.6) -- the same
              proposed_config below also gets sent to every device checked
              here, alongside the primary device selected above. Backend
              already fully supports this (ChangeRequestCreate.
              additional_device_ids + canary_enabled / canary_gate_task);
              this panel is what was missing to actually reach it. */}
          {devices.length > 1 && (
            <div className="border border-slate-200 dark:border-slate-700 rounded-lg p-3 bg-slate-50 dark:bg-slate-900">
              <div className="flex items-center justify-between flex-wrap gap-2 mb-2">
                <p className="text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">
                  Also deploy to ({form.additional_device_ids.length} selected)
                </p>
                {rolesAvailable.length > 0 && (
                  <select
                    className="border border-slate-300 dark:border-slate-600 rounded-lg px-2 py-1 text-xs bg-white dark:bg-slate-800"
                    value=""
                    onChange={(e) => addAllWithRole(e.target.value)}
                  >
                    <option value="">+ Add all devices with role…</option>
                    {rolesAvailable.map((role) => (
                      <option key={role} value={role}>
                        {role}
                      </option>
                    ))}
                  </select>
                )}
              </div>
              <div className="flex flex-wrap gap-2 max-h-32 overflow-y-auto">
                {devices
                  .filter((d) => d.id !== form.device_id)
                  .map((d) => (
                    <label
                      key={d.id}
                      className={`flex items-center gap-1.5 text-xs border rounded-full px-2.5 py-1 cursor-pointer ${
                        form.additional_device_ids.includes(d.id)
                          ? "bg-blue-50 border-brandblue text-brandblue dark:bg-blue-950/40"
                          : "bg-white dark:bg-slate-800 border-slate-200 dark:border-slate-700 text-slate-600 dark:text-slate-300"
                      }`}
                    >
                      <input
                        type="checkbox"
                        className="hidden"
                        checked={form.additional_device_ids.includes(d.id)}
                        onChange={() => toggleAdditionalDevice(d.id)}
                      />
                      {d.hostname}
                      {d.device_role && <span className="opacity-60">· {d.device_role}</span>}
                    </label>
                  ))}
              </div>
              {form.additional_device_ids.length > 0 && (
                <label className="flex items-center gap-2 mt-3 text-xs text-slate-600 dark:text-slate-300 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={form.canary_enabled}
                    onChange={(e) => setForm({ ...form, canary_enabled: e.target.checked })}
                  />
                  <span>
                    <span className="font-semibold">Staged rollout (canary)</span> — deploy to the primary device
                    first, wait for its health check to pass, then roll out to the rest. If the canary fails, the
                    remaining {form.additional_device_ids.length} device{form.additional_device_ids.length === 1 ? "" : "s"} are skipped automatically.
                  </span>
                </label>
              )}
            </div>
          )}

          {/* Blast-radius preview (re-deployment safety): shows how many
              devices this change touches directly and how many more
              depend on them via topology, before submission. */}
          {form.device_id && (
            <div
              className={`border rounded-lg p-3 text-xs ${
                blastRadius && blastRadius.dependent_count > 0
                  ? "border-amber-300 bg-amber-50 dark:bg-amber-950/20 dark:border-amber-800"
                  : "border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900"
              }`}
            >
              <p className="font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 mb-1">
                Blast Radius Preview
              </p>
              {blastRadiusLoading && !blastRadius ? (
                <p className="text-slate-400">Checking topology…</p>
              ) : blastRadius ? (
                <p className="text-slate-700 dark:text-slate-200">
                  This change touches <span className="font-semibold">{blastRadius.touched_count}</span> device
                  {blastRadius.touched_count === 1 ? "" : "s"}
                  {blastRadius.touched_core_count > 0 && (
                    <>
                      {" "}
                      (<span className="font-semibold text-amber-700 dark:text-amber-400">{blastRadius.touched_core_count} core</span>)
                    </>
                  )}
                  {blastRadius.dependent_count > 0 ? (
                    <>
                      {" — "}
                      <span className="font-semibold">{blastRadius.dependent_count} more device{blastRadius.dependent_count === 1 ? "" : "s"}</span>{" "}
                      depend on {blastRadius.touched_count === 1 ? "it" : "them"} via topology. Review carefully before
                      submitting.
                    </>
                  ) : (
                    <> and nothing else depends on {blastRadius.touched_count === 1 ? "it" : "them"} via known topology.</>
                  )}
                  {blastRadius.unknown_device_ids.length > 0 && (
                    <span className="block mt-1 text-slate-400">
                      ({blastRadius.unknown_device_ids.length} selected device{blastRadius.unknown_device_ids.length === 1 ? "" : "s"} not found in
                      inventory yet)
                    </span>
                  )}
                </p>
              ) : (
                <p className="text-slate-400">Blast radius unavailable.</p>
              )}
            </div>
          )}

          {form.device_id && form.proposed_config.trim() && (
            <ImpactSimulationPanel sim={impactSim} loading={impactSimLoading} />
          )}

          <div className="border border-slate-200 dark:border-slate-700 rounded-lg p-3 bg-slate-50 dark:bg-slate-900">
            <p className="font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 text-xs mb-1">
              Scheduled Deployment Window (optional)
            </p>
            <p className="text-xs text-slate-500 dark:text-slate-400 mb-2">
              Leave blank to deploy immediately on approval. Set a start time to auto-deploy at that exact time —
              approving just queues the change, deployment fires on schedule. An end time is a hard cutoff: approving
              (or the window elapsing) after it blocks deployment rather than firing late.
            </p>
            <div className="flex gap-2">
              <label className="flex-1 text-xs text-slate-600 dark:text-slate-300">
                Deploy at
                <input
                  type="datetime-local"
                  className="mt-1 w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-slate-800"
                  value={form.maintenance_window_start}
                  onChange={(e) => setForm({ ...form, maintenance_window_start: e.target.value })}
                />
              </label>
              <label className="flex-1 text-xs text-slate-600 dark:text-slate-300">
                Window closes (optional)
                <input
                  type="datetime-local"
                  className="mt-1 w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-slate-800"
                  value={form.maintenance_window_end}
                  onChange={(e) => setForm({ ...form, maintenance_window_end: e.target.value })}
                />
              </label>
            </div>
          </div>

          <input
            className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm"
            placeholder="Business justification (optional)"
            value={form.business_justification}
            onChange={(e) => setForm({ ...form, business_justification: e.target.value })}
          />
          {availableTemplates.length > 0 && (
            <div className="border border-slate-200 dark:border-slate-700 rounded-lg p-3 bg-slate-50 dark:bg-slate-900">
              <p className="text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 mb-2">
                Use a Template
              </p>
              <select
                className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-slate-800"
                value={selectedTemplateId}
                onChange={(e) => selectTemplate(e.target.value)}
              >
                <option value="">Write config from scratch…</option>
                {availableTemplates.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                    {t.device_role ? ` (${t.device_role})` : ""}
                  </option>
                ))}
              </select>
              {selectedTemplate && (
                <div className="mt-3 flex flex-col gap-2">
                  {selectedTemplate.variables.length > 0 && (
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                      {selectedTemplate.variables.map((v) => (
                        <div key={v.name}>
                          <label className="block text-[11px] font-semibold text-slate-500 dark:text-slate-400 uppercase mb-1">
                            {v.label || v.name}
                            {v.required && <span className="text-riskcrit ml-1">*</span>}
                          </label>
                          <input
                            className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-2.5 py-1.5 text-sm bg-white dark:bg-slate-800"
                            placeholder={v.default || v.name}
                            value={templateValues[v.name] ?? ""}
                            onChange={(e) => setTemplateValues((p) => ({ ...p, [v.name]: e.target.value }))}
                          />
                        </div>
                      ))}
                    </div>
                  )}
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={applyTemplate}
                      disabled={templateRendering}
                      className="self-start bg-brandblue text-white rounded-lg px-4 py-1.5 text-xs font-bold uppercase tracking-wider hover:bg-navy disabled:opacity-50"
                    >
                      {templateRendering ? "Rendering…" : "Fill Proposed Config"}
                    </button>
                    <button
                      type="button"
                      onClick={previewTemplateDiff}
                      disabled={templateDiffLoading || !form.device_id}
                      title={!form.device_id ? "Select a target device first" : "Preview the rendered diff against this device"}
                      className="self-start border border-brandblue text-brandblue rounded-lg px-4 py-1.5 text-xs font-bold uppercase tracking-wider hover:bg-blue-50 dark:hover:bg-blue-950/30 disabled:opacity-50"
                    >
                      {templateDiffLoading ? "Rendering diff…" : "Preview Rendered Diff"}
                    </button>
                  </div>
                  {templateError && <p className="text-riskcrit text-xs">{templateError}</p>}
                  {templateDiffError && <p className="text-riskcrit text-xs">{templateDiffError}</p>}
                  {templateDiffPreview && (
                    <div className="border border-slate-200 dark:border-slate-700 rounded-lg overflow-hidden">
                      <div className="px-3 py-2 bg-slate-100 dark:bg-slate-800 text-xs font-semibold text-slate-600 dark:text-slate-300 flex items-center justify-between">
                        <span>Rendered config vs. {templateDiffPreview.base_label}</span>
                        {templateDiffPreview.identical && (
                          <span className="text-green-700 bg-green-100 rounded-full px-2 py-0.5 text-[10px] font-bold">
                            No changes
                          </span>
                        )}
                      </div>
                      {templateDiffPreview.change_summary.length > 0 && (
                        <ul className="px-3 py-2 text-xs text-slate-600 dark:text-slate-300 list-disc list-inside border-b border-slate-100 dark:border-slate-700">
                          {templateDiffPreview.change_summary.map((line, i) => (
                            <li key={i}>{line}</li>
                          ))}
                        </ul>
                      )}
                      <div className="max-h-64 overflow-y-auto">
                        <ConfigDiff diffText={templateDiffPreview.diff} />
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
          <textarea
            className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm font-mono"
            rows={5}
            placeholder={"Proposed configuration, e.g.\ninterface Gi0/1\n ip address 10.2.2.1 255.255.255.0"}
            value={form.proposed_config}
            onChange={(e) => setForm({ ...form, proposed_config: e.target.value })}
            required
          />
          <button
            type="submit"
            disabled={loading || !devices.length}
            className="bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-navy dark:bg-slate-950 transition-colors disabled:opacity-50"
          >
            {loading ? "Analyzing…" : "Submit Change Request"}
          </button>
          {error && <p className="text-riskcrit text-sm">{error}</p>}
        </form>
      )}

      <div className="flex gap-2 mt-6">
        <button
          onClick={() => setQueueTab("all")}
          className={`px-3 py-1.5 rounded-lg text-xs font-semibold border transition-colors ${
            queueTab === "all"
              ? "bg-navy dark:bg-slate-950 text-white border-navy dark:border-slate-600"
              : "bg-white dark:bg-slate-800 text-slate-500 dark:text-slate-400 border-slate-200 dark:border-slate-700"
          }`}
        >
          All Requests
        </button>
        <button
          onClick={() => setQueueTab("queue")}
          className={`px-3 py-1.5 rounded-lg text-xs font-semibold border transition-colors ${
            queueTab === "queue"
              ? "bg-navy dark:bg-slate-950 text-white border-navy dark:border-slate-600"
              : "bg-white dark:bg-slate-800 text-slate-500 dark:text-slate-400 border-slate-200 dark:border-slate-700"
          }`}
        >
          Approval Queue
          {pendingCount > 0 && (
            <span className="ml-1.5 bg-amber-500 text-white rounded-full px-1.5 text-[10px]">{pendingCount}</span>
          )}
        </button>
      </div>

      {queueTab === "queue" && (
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl overflow-x-auto mt-3 mb-3">
          <table className="w-full text-sm min-w-[720px]">
            <thead className="bg-navy dark:bg-slate-950 text-white">
              <tr>
                <th className="text-left px-4 py-3 font-semibold">Device</th>
                <th className="text-left px-4 py-3 font-semibold">Priority</th>
                <th className="text-left px-4 py-3 font-semibold">Submitted By</th>
                <th className="text-left px-4 py-3 font-semibold">SLA</th>
                <th className="text-left px-4 py-3 font-semibold">Status</th>
              </tr>
            </thead>
            <tbody>
              {queueLoading && (
                <tr>
                  <td colSpan={5} className="text-center text-slate-400 dark:text-slate-500 py-8">
                    Loading…
                  </td>
                </tr>
              )}
              {!queueLoading && pendingQueue.length === 0 && (
                <tr>
                  <td colSpan={5} className="text-center text-slate-400 dark:text-slate-500 py-8 italic">
                    Nothing pending approval. 🎉
                  </td>
                </tr>
              )}
              {pendingQueue.map((item) => {
                const cr = item.change_request;
                const remainingHours = item.sla_hours - item.elapsed_hours;
                return (
                  <tr
                    key={cr.id}
                    onClick={() => setSelected(cr)}
                    className={`cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-700/50 border-t border-slate-100 dark:border-slate-700 ${
                      selected?.id === cr.id ? "bg-blue-50 dark:bg-slate-700" : ""
                    }`}
                  >
                    <td className="px-4 py-3">{hostnameFor(cr.device_id)}</td>
                    <td className="px-4 py-3">
                      <span className={`px-2 py-1 rounded-full text-xs font-semibold capitalize ${priorityStyle[cr.priority]}`}>
                        {cr.priority}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-500 dark:text-slate-400">
                      {cr.submitted_by_name || "—"}
                      {item.is_first_approval_needed && (
                        <span className="ml-1.5 px-1.5 py-0.5 rounded text-[10px] font-semibold bg-amber-100 text-amber-700">
                          needs 1st approval
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      {item.is_overdue ? (
                        <span className="px-2 py-1 rounded-full text-xs font-semibold bg-red-100 text-riskcrit">
                          Overdue by {Math.abs(remainingHours).toFixed(1)}h
                        </span>
                      ) : (
                        <span className="px-2 py-1 rounded-full text-xs font-semibold bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300">
                          {remainingHours.toFixed(1)}h left ({item.sla_hours}h SLA)
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <span className={`px-2 py-1 rounded-full text-xs font-semibold capitalize ${statusStyle[cr.status] || ""}`}>
                        {cr.status.replace(/_/g, " ")}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <div className="flex flex-wrap gap-2 mt-3 mb-3">
        {STATUS_FILTERS.map((f) => (
          <button
            key={f.value}
            onClick={() => setStatusFilter(f.value)}
            className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-colors ${
              statusFilter === f.value
                ? "bg-navy dark:bg-slate-950 text-white border-navy dark:border-slate-600"
                : "bg-white dark:bg-slate-800 text-slate-500 dark:text-slate-400 border-slate-200 dark:border-slate-700 hover:border-slate-300 dark:border-slate-600"
            }`}
          >
            {f.label}
            {f.value === "pending_approval" && pendingCount > 0 && (
              <span className="ml-1.5 bg-amber-500 text-white rounded-full px-1.5 text-[10px]">{pendingCount}</span>
            )}
          </button>
        ))}
      </div>

      <div className={queueTab === "queue" ? "grid grid-cols-1 gap-6" : "grid grid-cols-1 lg:grid-cols-2 gap-6"}>
        {queueTab === "all" && (
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl overflow-x-auto self-start">
          <table className="w-full text-sm min-w-[560px]">
            <thead className="bg-navy dark:bg-slate-950 text-white">
              <tr>
                <th className="text-left px-4 py-3 font-semibold">Device</th>
                <th className="text-left px-4 py-3 font-semibold">Priority</th>
                <th className="text-left px-4 py-3 font-semibold">Risk</th>
                <th className="text-left px-4 py-3 font-semibold">Status</th>
              </tr>
            </thead>
            <tbody>
              {initialLoading && (
                <tr>
                  <td colSpan={4} className="text-center text-slate-400 dark:text-slate-500 py-8">
                    Loading…
                  </td>
                </tr>
              )}
              {!initialLoading && filtered.length === 0 && (
                <tr>
                  <td colSpan={4} className="text-center text-slate-400 dark:text-slate-500 py-8">
                    {requests.length === 0 ? "No change requests yet." : "No requests match this filter."}
                  </td>
                </tr>
              )}
              {filtered.map((r, i) => (
                <tr
                  key={r.id}
                  onClick={() => setSelected(r)}
                  className={`cursor-pointer hover:bg-blue-50 ${i % 2 ? "bg-slate-50 dark:bg-slate-900" : "bg-white dark:bg-slate-800"} ${
                    selected?.id === r.id ? "ring-2 ring-inset ring-brandblue" : ""
                  }`}
                >
                  <td className="px-4 py-3 font-medium text-navy dark:text-white">
                    {hostnameFor(r.device_id)}
                    {(r.target_device_count || 1) > 1 && (
                      <span
                        className="ml-1.5 px-1.5 py-0.5 rounded text-[10px] font-semibold bg-slate-100 text-slate-600 align-middle"
                        title={r.canary_enabled ? "Staged (canary) rollout" : "Bulk deploy"}
                      >
                        +{(r.target_device_count || 1) - 1} {r.canary_enabled ? "🐤" : ""}
                      </span>
                    )}
                    {r.is_rollback === "true" && (
                      <span className="ml-1.5 px-1.5 py-0.5 rounded text-[10px] font-semibold bg-purple-100 text-purple-700 align-middle">
                        ↺ rollback
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-1 rounded-full text-xs font-semibold capitalize ${priorityStyle[r.priority]}`}>
                      {r.priority}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    {r.risk_score != null && (
                      <span className="inline-flex items-center gap-1.5">
                        <RiskBadge score={r.risk_score} />
                        {r.risk_engine_backend === "llm" && r.risk_llm_applied && (
                          <span title="AI-reviewed" className="text-xs">✨</span>
                        )}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-1 rounded-full text-xs font-semibold capitalize ${statusStyle[r.status] || ""}`}>
                      {r.status.replace(/_/g, " ")}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        )}

        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-5">
          {!selected ? (
            <p className="text-sm text-slate-400 dark:text-slate-500 italic">Select a change request to view details.</p>
          ) : (
            <div className="space-y-4">
              <div className="flex items-start justify-between gap-2">
                <div>
                  <h3 className="font-semibold text-navy dark:text-white flex items-center gap-2">
                    {selected.description}
                    {selected.is_rollback === "true" && (
                      <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-purple-100 text-purple-700">
                        ↺ rollback
                      </span>
                    )}
                  </h3>
                  <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">
                    Device: {hostnameFor(selected.device_id)} · Submitted{" "}
                    {new Date(selected.created_at).toLocaleString()}
                    {selected.submitted_by_name && <> by {selected.submitted_by_name}</>}
                  </p>
                  {/* Approval workflow visibility: who approved this and when. */}
                  {selected.approved_by_name && selected.approved_at && (
                    <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">
                      Approved by {selected.approved_by_name} · {new Date(selected.approved_at).toLocaleString()}
                    </p>
                  )}
                  {selected.first_approved_by_name && !selected.approved_by_name && (
                    <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">
                      1st approval by {selected.first_approved_by_name} — awaiting 2nd
                    </p>
                  )}
                  {/* Scheduled deployment window: shows when a change is queued
                      to auto-deploy at a future time rather than immediately
                      on approval (see maintenance_window_start/end handling
                      in POST /change-requests/{id}/approve). */}
                  {selected.maintenance_window_start && (
                    <p className="text-xs mt-1">
                      {selected.status === "approved" && new Date(selected.maintenance_window_start) > new Date() ? (
                        <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-indigo-100 text-indigo-700">
                          ⏱ scheduled to deploy {new Date(selected.maintenance_window_start).toLocaleString()}
                        </span>
                      ) : (
                        <span className="text-slate-500 dark:text-slate-400">
                          Deployment window: {new Date(selected.maintenance_window_start).toLocaleString()}
                          {selected.maintenance_window_end && (
                            <> – {new Date(selected.maintenance_window_end).toLocaleString()}</>
                          )}
                        </span>
                      )}
                    </p>
                  )}
                  {/* Alert -> CR auto-link, for postmortem review. */}
                  {selected.triggering_alert && (
                    <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">
                      Raised from alert:{" "}
                      <span className="font-medium text-navy dark:text-white">{selected.triggering_alert.category}</span>{" "}
                      ({selected.triggering_alert.severity}, {new Date(selected.triggering_alert.created_at).toLocaleString()})
                    </p>
                  )}
                  {(selected.target_device_count || 1) > 1 && (
                    <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">
                      Also targets: {(selected.additional_device_ids || []).map(hostnameFor).join(", ")}
                      {selected.canary_enabled && (
                        <span className="ml-1.5 px-1.5 py-0.5 rounded text-[10px] font-semibold bg-amber-100 text-amber-700">
                          🐤 staged rollout
                        </span>
                      )}
                    </p>
                  )}
                  {selected.business_justification && (
                    <p className="text-xs text-slate-500 dark:text-slate-400 mt-1 italic">"{selected.business_justification}"</p>
                  )}
                </div>
                <span className={`shrink-0 px-2 py-1 rounded-full text-xs font-semibold capitalize ${priorityStyle[selected.priority]}`}>
                  {selected.priority}
                </span>
              </div>
              {selected.risk_score != null && (
                <div>
                  <div className="flex items-center justify-between mb-1">
                    <p className="text-xs font-semibold text-slate-500 dark:text-slate-400 uppercase">AI Risk Analysis</p>
                    {selected.risk_engine_backend === "llm" && (
                      selected.risk_llm_applied ? (
                        <span
                          className="inline-flex items-center gap-1 bg-purple-50 border border-purple-200 text-purple-700 text-[10px] font-bold uppercase tracking-wide px-2 py-0.5 rounded-full"
                          title="An LLM pass ran and its findings were merged into this score, in addition to the rule engine."
                        >
                          ✨ AI-Reviewed
                        </span>
                      ) : (
                        <span
                          className="inline-flex items-center gap-1 bg-slate-100 dark:bg-slate-700 border border-slate-200 dark:border-slate-700 text-slate-500 dark:text-slate-400 text-[10px] font-bold uppercase tracking-wide px-2 py-0.5 rounded-full"
                          title={selected.risk_llm_error || "LLM backend selected but the model pass did not run; this score is rule-engine only."}
                        >
                          Rule-Engine Only
                        </span>
                      )
                    )}
                  </div>
                  <RiskBadge score={selected.risk_score} />
                  {selected.risk_findings && (
                    <ul className="text-xs text-slate-600 dark:text-slate-300 mt-2 list-disc list-inside space-y-0.5">
                      {selected.risk_findings.split("; ").map((f, i) => (
                        <li key={i}>{f}</li>
                      ))}
                    </ul>
                  )}
                  {selected.config_source && selected.config_source !== "live" && (
                    <p className="text-[11px] text-riskmed bg-amber-50 border border-amber-200 rounded-lg px-2.5 py-1.5 mt-2">
                      This score was computed against a {selected.config_source === "snapshot" ? "stale snapshot" : "missing"}{" "}
                      config, not a fresh live read from the device.
                    </p>
                  )}
                  {selected.risk_llm_error && (
                    <p className="text-[11px] text-slate-500 dark:text-slate-400 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-lg px-2.5 py-1.5 mt-2">
                      LLM pass didn't run: {selected.risk_llm_error}
                    </p>
                  )}
                  {["draft", "pending_approval"].includes(selected.status) &&
                    (selected.config_source !== "live" || (selected.risk_engine_backend === "llm" && !selected.risk_llm_applied)) && (
                      <button
                        onClick={() => act(selected.id, "rescore")}
                        disabled={acting}
                        className="mt-2 text-[11px] font-bold uppercase tracking-wide text-brandblue border border-blue-200 bg-blue-50 hover:bg-blue-100 px-2.5 py-1 rounded-lg shadow-sm disabled:opacity-50"
                      >
                        {acting ? "Retrying…" : "↻ Retry / Re-score"}
                      </button>
                    )}
                </div>
              )}
              <ValidationPipelinePanel
                report={selectedValidation}
                loading={selectedValidationLoading}
                onRerun={rerunValidation}
                rerunning={validationRerunning}
              />
              <ImpactSimulationPanel sim={selectedImpactSim} loading={selectedImpactSimLoading} />
              <ThreeWayDiffPanel diff={selectedThreeWayDiff} loading={selectedThreeWayDiffLoading} />
              <EvidencePanel changeRequestId={selected.id} />
              <div className="space-y-3">
                {selected.config_diff_summary && (
                  <div>
                    <p className="text-xs font-semibold text-slate-500 dark:text-slate-400 uppercase mb-1">Change Summary</p>
                    <ul className="text-xs text-slate-600 dark:text-slate-300 list-disc list-inside space-y-0.5">
                      {selected.config_diff_summary.split("\n").filter(Boolean).map((line, i) => (
                        <li key={i}>{line}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {selected.config_diff_cli && (
                  <div>
                    <p className="text-xs font-semibold text-slate-500 dark:text-slate-400 uppercase mb-1">CLI Commands</p>
                    <pre className="bg-slate-900 text-xs rounded-lg p-4 overflow-x-auto leading-relaxed">
                      {selected.config_diff_cli.split("\n").map((line, i) => {
                        let cls = "text-slate-300";
                        if (line.startsWith("interface ") || line.startsWith("router ")) cls = "text-accent font-semibold block";
                        else if (line.trimStart().startsWith("no ")) cls = "text-riskcrit bg-red-950/40 block";
                        else if (line.startsWith("  ")) cls = "text-risklow bg-green-950/40 block";
                        return (
                          <span key={i} className={cls}>
                            {line || " "}
                            {"\n"}
                          </span>
                        );
                      })}
                    </pre>
                  </div>
                )}
                <details className="group" open={!selected.config_diff_cli}>
                  <summary className="text-xs font-semibold text-slate-500 dark:text-slate-400 uppercase mb-1 cursor-pointer hover:text-brandblue select-none flex items-center justify-between">
                    <span>{selected.config_diff_cli ? "Raw Configuration Diff ▸" : "Configuration Diff"}</span>
                    <span className="inline-flex rounded-lg border border-slate-200 dark:border-slate-700 overflow-hidden text-[10px] normal-case font-medium">
                      <button
                        type="button"
                        onClick={(e) => {
                          e.preventDefault();
                          setDiffView("unified");
                        }}
                        className={`px-2 py-0.5 ${diffView === "unified" ? "bg-navy dark:bg-slate-950 text-white" : "bg-white dark:bg-slate-800 text-slate-500 dark:text-slate-400"}`}
                      >
                        Unified
                      </button>
                      <button
                        type="button"
                        onClick={(e) => {
                          e.preventDefault();
                          setDiffView("side-by-side");
                        }}
                        className={`px-2 py-0.5 ${diffView === "side-by-side" ? "bg-navy dark:bg-slate-950 text-white" : "bg-white dark:bg-slate-800 text-slate-500 dark:text-slate-400"}`}
                      >
                        Side-by-side
                      </button>
                    </span>
                  </summary>
                  <div className="mt-1">
                    {diffView === "unified" ? (
                      <ConfigDiff diffText={selected.config_diff} />
                    ) : (
                      <SideBySideDiff currentConfig={selected.current_config} proposedConfig={selected.proposed_config} />
                    )}
                  </div>
                </details>
              </div>
              {approvalChain && approvalChain.stages.length > 0 && (
                <div className="rounded-lg border border-slate-200 dark:border-slate-700 p-3">
                  <h4 className="text-xs font-bold text-slate-600 dark:text-slate-300 uppercase tracking-wide mb-2">
                    Approval Chain (Segregation of Duties)
                  </h4>
                  <ol className="space-y-2">
                    {approvalChain.stages.map((stage) => {
                      const label =
                        stage.stage_type === "peer_review"
                          ? "Peer Review"
                          : stage.stage_type === "manager_signoff"
                          ? "Manager Sign-off"
                          : "Admin Approval";
                      const badge =
                        stage.status === "approved"
                          ? "bg-green-100 text-green-700"
                          : stage.status === "rejected"
                          ? "bg-red-100 text-red-700"
                          : "bg-slate-100 dark:bg-slate-700 text-slate-500 dark:text-slate-400";
                      const eligibleRoles = stage.required_role.split("/");
                      const isMyPendingStage =
                        stage.status === "pending" &&
                        approvalChain.current_stage_sequence === stage.sequence &&
                        !!user?.role &&
                        eligibleRoles.includes(user.role) &&
                        user?.id !== selected.submitted_by;

                      return (
                        <li key={stage.id} className="flex items-start gap-3">
                          <span className={`text-[10px] font-bold uppercase tracking-wide rounded-full px-2 py-0.5 mt-0.5 ${badge}`}>
                            {stage.sequence}. {label}
                          </span>
                          <div className="flex-1 min-w-0">
                            <p className="text-xs text-slate-500 dark:text-slate-400">
                              {stage.status === "pending"
                                ? `Awaiting ${stage.required_role.replace(/_/g, " ").replace(/\//g, " or ")}`
                                : `${stage.status === "approved" ? "Approved" : "Rejected"} by ${
                                    stage.acted_by_name ?? "unknown"
                                  }${stage.acted_at ? " · " + new Date(stage.acted_at).toLocaleString() : ""}`}
                              {stage.notes && <span className="italic"> — "{stage.notes}"</span>}
                            </p>
                            {isMyPendingStage && (
                              <div className="mt-1.5 flex items-center gap-2">
                                <input
                                  className="flex-1 border border-slate-300 dark:border-slate-600 rounded-md px-2 py-1 text-xs focus:ring-2 focus:ring-brandblue outline-none"
                                  placeholder="Notes (required to reject)"
                                  value={chainNotes}
                                  onChange={(e) => setChainNotes(e.target.value)}
                                />
                                <button
                                  onClick={() => actOnChain(selected.id, true)}
                                  disabled={chainActing}
                                  className="bg-risklow text-white rounded-md px-3 py-1 text-xs font-semibold hover:opacity-90 disabled:opacity-50"
                                >
                                  {label === "Peer Review" ? "Approve Review" : "Sign Off"}
                                </button>
                                <button
                                  onClick={() => actOnChain(selected.id, false)}
                                  disabled={chainActing}
                                  className="bg-riskcrit text-white rounded-md px-3 py-1 text-xs font-semibold hover:opacity-90 disabled:opacity-50"
                                >
                                  Reject
                                </button>
                              </div>
                            )}
                          </div>
                        </li>
                      );
                    })}
                  </ol>
                  {chainActionError && <p className="text-riskcrit text-xs mt-2">{chainActionError}</p>}
                  {approvalChain.current_stage_sequence !== null &&
                    approvalChain.stages[approvalChain.current_stage_sequence! - 1]?.stage_type !== "admin_approval" &&
                    !approvalChain.stages.some((s) => s.status === "rejected") && (
                      <p className="text-[11px] text-slate-400 mt-2 italic">
                        Network Administrator approval below is only available once every earlier stage is approved.
                      </p>
                    )}
                </div>
              )}
              {selected.requires_dual_approval && selected.status === "pending_approval" && (
                <div className="rounded-lg bg-amber-50 border border-amber-200 text-amber-800 text-xs px-3 py-2">
                  {selected.first_approved_by
                    ? `${selected.dual_approval_reason ?? "Critical Risk"}: first approval recorded. A second, different Network Administrator must approve to deploy.`
                    : `${selected.dual_approval_reason ?? "Critical Risk"}: this change requires approval from two different Network Administrators before deployment.`}
                </div>
              )}
              {actionError && <p className="text-riskcrit text-xs">{actionError}</p>}
              {selected.status === "pending_approval" &&
                (() => {
                  const chainBlocking =
                    !!approvalChain &&
                    approvalChain.stages.length > 0 &&
                    !approvalChain.fully_approved &&
                    !approvalChain.stages.some((s) => s.status === "rejected");
                  return canApprove ? (
                    <div className="flex gap-2 pt-2">
                      <button
                        onClick={() => act(selected.id, "approve")}
                        disabled={acting || chainBlocking}
                        title={chainBlocking ? "Earlier approval chain stages must be cleared first." : undefined}
                        className="bg-risklow text-white rounded-lg px-4 py-2 text-sm font-semibold hover:opacity-90 disabled:opacity-50"
                      >
                        {acting
                          ? "Working…"
                          : chainBlocking
                          ? "Waiting on Approval Chain"
                          : selected.requires_dual_approval
                          ? selected.first_approved_by
                            ? "Give 2nd Approval & Deploy"
                            : "Give 1st Approval"
                          : "Approve & Deploy"}
                      </button>
                      <button
                        onClick={() => act(selected.id, "reject")}
                        disabled={acting}
                        className="bg-riskcrit text-white rounded-lg px-4 py-2 text-sm font-semibold hover:opacity-90 disabled:opacity-50"
                      >
                        Reject
                      </button>
                    </div>
                  ) : (
                    <p className="text-xs text-slate-400 dark:text-slate-500 italic pt-2">
                      Only a Network Administrator can approve or reject this request.
                    </p>
                  );
                })()}
              {["approved", "validating", "deploying", "monitoring", "success", "failed", "rolled_back"].includes(
                selected.status
              ) && (
                <p className="text-xs text-slate-500 dark:text-slate-400 pt-1">
                  See the <span className="font-medium text-navy dark:text-white">Deployments</span> page for pipeline details
                  (snapshot, health checks, rollback status).
                  {["approved", "validating", "deploying", "monitoring"].includes(selected.status) &&
                    " This panel auto-refreshes while the pipeline runs."}
                </p>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}