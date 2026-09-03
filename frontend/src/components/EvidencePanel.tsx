/**
 * EvidencePanel — Hyperledger Fabric evidence lifecycle panel for the
 * Change Request detail view (Sections 22/23/24).
 *
 * Loads every evidence record anchored against the selected CR and lets an
 * operator verify individual records against the ledger hash, surfacing
 * tamper-detection warnings on any mismatch.
 *
 * API calls:
 *   GET  /api/v1/change-requests/{id}/evidence  → list
 *   POST /api/v1/evidence/{id}/verify            → re-compute + compare
 *
 * Field names exactly match the backend `_serialize` output in evidence.py.
 */
import { useState } from "react";
import { api } from "../lib/api";

// ---------------------------------------------------------------------------
// Types — must match evidence.py::_serialize exactly
// ---------------------------------------------------------------------------

/** Values from AnchorStatus enum (uppercase, matching the backend Enum) */
type AnchorStatus =
  | "PENDING"
  | "ANCHORING"
  | "ANCHORED"
  | "FAILED"
  | "VERIFIED"
  | "MISMATCH";

export interface EvidenceRecord {
  evidence_id: string;
  evidence_type: string;
  change_request_id: string | null;
  device_id: string | null;
  deployment_id: string | null;
  evidence_hash: string | null;
  configuration_hash: string | null;
  hash_algorithm: string | null;
  canonicalization_version: number | null;
  previous_evidence_id: string | null;
  previous_evidence_hash: string | null;
  fabric_channel: string | null;
  fabric_chaincode: string | null;
  /** backend column: fabric_transaction_id */
  fabric_transaction_id: string | null;
  fabric_block_number: number | null;
  anchor_status: AnchorStatus;
  /** backend column: anchor_attempts */
  anchor_attempts: number;
  anchor_error: string | null;
  policy_version: string | null;
  batfish_version: string | null;
  validation_engine_version: string | null;
  application_version: string | null;
  actor_subject: string | null;
  created_at: string | null;
  anchored_at: string | null;
  verified_at: string | null;
  evidence_body: Record<string, unknown> | null;
}

interface VerifyResult {
  verified: boolean;
  evidence_id: string;
  calculated_hash: string | null;
  ledger_hash: string | null;
  transaction_id: string | null;
  block_number: number | null;
  status: AnchorStatus;
}

// ---------------------------------------------------------------------------
// Display helpers
// ---------------------------------------------------------------------------

const TYPE_LABELS: Record<string, string> = {
  change_request_created: "CR Created",
  change_validation: "Validation",
  opa_decision: "OPA Decision",
  batfish_validation: "Batfish",
  change_approved: "Approved",
  change_rejected: "Rejected",
  deployment_started: "Deploy Started",
  deployment_completed: "Deployed",
  deployment_failed: "Deploy Failed",
  post_deployment_verification: "Post-Deploy Verify",
  rollback_started: "Rollback Started",
  rollback_completed: "Rolled Back",
  rollback_failed: "Rollback Failed",
  configuration_baseline: "Config Baseline",
  configuration_drift: "Config Drift",
  policy_version: "Policy Version",
};

const STATUS_STYLE: Record<AnchorStatus, { badge: string; label: string }> = {
  PENDING:   { badge: "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300",   label: "Pending" },
  ANCHORING: { badge: "bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300",       label: "Anchoring…" },
  ANCHORED:  { badge: "bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300",   label: "Anchored" },
  FAILED:    { badge: "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300",           label: "Failed" },
  VERIFIED:  { badge: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300", label: "Verified ✓" },
  MISMATCH:  { badge: "bg-rose-100 text-rose-700 dark:bg-rose-900/40 dark:text-rose-300",       label: "⚠ Mismatch" },
};

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" });
}

function truncate(s: string | null | undefined, n = 24): string {
  if (!s) return "—";
  return s.length > n ? s.slice(0, n) + "…" : s;
}

// ---------------------------------------------------------------------------
// Verify button + result inline display
// ---------------------------------------------------------------------------

function VerifyButton({
  evidenceId,
  onResult,
}: {
  evidenceId: string;
  onResult: (res: VerifyResult) => void;
}) {
  const [busy, setBusy] = useState(false);

  const run = async () => {
    setBusy(true);
    try {
      const res = await api.post<VerifyResult>(`/evidence/${evidenceId}/verify`);
      onResult(res.data);
    } catch {
      onResult({
        verified: false,
        evidence_id: evidenceId,
        calculated_hash: null,
        ledger_hash: null,
        transaction_id: null,
        block_number: null,
        status: "FAILED",
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      id={`verify-evidence-${evidenceId}`}
      type="button"
      onClick={run}
      disabled={busy}
      className="text-[10px] font-bold uppercase tracking-wide text-brandblue border border-blue-200 bg-blue-50 hover:bg-blue-100 dark:bg-blue-950/40 dark:border-blue-800 dark:text-blue-400 px-2 py-0.5 rounded disabled:opacity-50 transition-colors"
    >
      {busy ? "Checking…" : "Verify"}
    </button>
  );
}

function VerifyResultBlock({ result }: { result: VerifyResult }) {
  if (result.verified) {
    return (
      <div className="mt-1 flex items-center gap-1.5 text-[10px] text-emerald-700 dark:text-emerald-400">
        <span className="font-bold">✓ VERIFIED</span>
        {result.calculated_hash && (
          <span className="font-mono opacity-60">{truncate(result.calculated_hash, 28)}</span>
        )}
      </div>
    );
  }
  return (
    <div className="mt-1 rounded bg-red-50 dark:bg-red-950/40 border border-red-300 dark:border-red-800 p-1.5 text-[10px]">
      <p className="font-bold text-red-700 dark:text-red-400 uppercase tracking-wide">⚠ Evidence Integrity Failure</p>
      <div className="mt-0.5 space-y-0.5 font-mono text-red-600 dark:text-red-300 break-all">
        {result.ledger_hash && <p>Ledger:     {result.ledger_hash}</p>}
        <p>Calculated: {result.calculated_hash ?? "—"}</p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Single evidence row
// ---------------------------------------------------------------------------

function EvidenceRow({ record }: { record: EvidenceRecord }) {
  const [verifyResult, setVerifyResult] = useState<VerifyResult | null>(null);
  const ss = STATUS_STYLE[record.anchor_status] ?? STATUS_STYLE.PENDING;
  const label = TYPE_LABELS[record.evidence_type.toLowerCase()] ?? record.evidence_type;
  const canVerify = record.anchor_status === "ANCHORED" || record.anchor_status === "VERIFIED" || record.anchor_status === "MISMATCH";

  return (
    <div className="rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-2.5 text-xs space-y-1">
      {/* Header */}
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2 min-w-0">
          <span className={`shrink-0 text-[10px] font-bold uppercase tracking-wide rounded-full px-2 py-0.5 ${ss.badge}`}>
            {ss.label}
          </span>
          <span className="font-semibold text-slate-700 dark:text-slate-200 truncate">{label}</span>
          <span className="font-mono text-slate-400 dark:text-slate-500 text-[10px] truncate max-w-[120px]" title={record.evidence_id}>
            {record.evidence_id}
          </span>
        </div>
        {canVerify && <VerifyButton evidenceId={record.evidence_id} onResult={setVerifyResult} />}
      </div>

      {/* Metadata */}
      <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-[11px] text-slate-500 dark:text-slate-400">
        <span>Created: {fmtDate(record.created_at)}</span>
        <span>Anchored: {fmtDate(record.anchored_at)}</span>
        {record.fabric_transaction_id && (
          <span
            className="font-mono col-span-2 truncate"
            title={record.fabric_transaction_id}
          >
            TX: {truncate(record.fabric_transaction_id, 40)}
          </span>
        )}
        {record.fabric_block_number !== null && record.fabric_block_number !== undefined && (
          <span>Block #{record.fabric_block_number}</span>
        )}
        {record.anchor_attempts > 0 && (
          <span className={record.anchor_attempts > 1 ? "text-amber-600 dark:text-amber-400" : ""}>
            {record.anchor_attempts === 1 ? "1 attempt" : `${record.anchor_attempts} attempts`}
          </span>
        )}
        {record.anchor_error && (
          <span
            className="col-span-2 text-red-500 dark:text-red-400 italic truncate"
            title={record.anchor_error}
          >
            Error: {record.anchor_error}
          </span>
        )}
      </div>

      {/* Verify result */}
      {verifyResult && <VerifyResultBlock result={verifyResult} />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Panel wrapper
// ---------------------------------------------------------------------------

interface EvidencePanelProps {
  changeRequestId: string;
}

export default function EvidencePanel({ changeRequestId }: EvidencePanelProps) {
  const [records, setRecords] = useState<EvidenceRecord[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    setError(null);
    api
      .get<EvidenceRecord[]>(`/change-requests/${changeRequestId}/evidence`)
      .then((res) => setRecords(res.data))
      .catch((err) => {
        if (err?.response?.status === 404) {
          setRecords([]);
        } else {
          setError(err?.response?.data?.detail ?? "Failed to load evidence records.");
        }
      })
      .finally(() => setLoading(false));
  };

  const toggle = () => {
    const open = !expanded;
    setExpanded(open);
    if (open && records === null) load();
  };

  const anchored = (records ?? []).filter(
    (r) => r.anchor_status === "ANCHORED" || r.anchor_status === "VERIFIED",
  ).length;
  const total = (records ?? []).length;

  return (
    <div className="border border-slate-200 dark:border-slate-700 rounded-lg overflow-hidden">
      {/* Toggle header */}
      <button
        id="evidence-panel-toggle"
        type="button"
        onClick={toggle}
        className="w-full flex items-center justify-between px-3 py-2 bg-slate-50 dark:bg-slate-900 hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors text-left"
      >
        <div className="flex items-center gap-2">
          {/* Fabric hex icon */}
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-brandblue dark:text-blue-400 shrink-0">
            <path d="M12 2l9 4.9v10.2L12 22l-9-4.9V6.9L12 2z" />
          </svg>
          <span className="text-xs font-bold uppercase tracking-wider text-slate-600 dark:text-slate-300">
            Fabric Evidence
          </span>
          {records !== null && (
            <span
              className={`text-[10px] font-bold rounded-full px-1.5 py-0.5 ${
                total === 0
                  ? "bg-slate-100 dark:bg-slate-700 text-slate-500"
                  : anchored === total
                  ? "bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300"
                  : "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"
              }`}
            >
              {anchored}/{total} anchored
            </span>
          )}
        </div>
        <span className="text-slate-400 text-[10px]">{expanded ? "▲" : "▼"}</span>
      </button>

      {/* Expanded body */}
      {expanded && (
        <div className="p-2.5 space-y-2 bg-white dark:bg-slate-900">
          {loading && (
            <p className="text-xs text-slate-400 py-2 text-center">Loading evidence…</p>
          )}
          {error && (
            <div className="flex items-center justify-between gap-2">
              <p className="text-xs text-red-500 dark:text-red-400">{error}</p>
              <button
                type="button"
                onClick={load}
                className="text-[10px] text-brandblue hover:underline shrink-0"
              >
                Retry
              </button>
            </div>
          )}
          {!loading && !error && records !== null && records.length === 0 && (
            <p className="text-xs text-slate-400 dark:text-slate-500 py-2 text-center italic">
              No evidence records yet. Evidence is generated automatically when a change is
              validated, approved, or deployed.
            </p>
          )}
          {!loading && (records ?? []).map((rec) => (
            <EvidenceRow key={rec.evidence_id} record={rec} />
          ))}
          {!loading && records !== null && records.length > 0 && (
            <button
              type="button"
              onClick={load}
              className="w-full text-[10px] text-slate-400 hover:text-brandblue py-1 transition-colors"
            >
              ↻ Refresh
            </button>
          )}
        </div>
      )}
    </div>
  );
}
