export type DeviceVendor = "cisco" | "juniper" | "arista" | "linux";
export type DeviceStatus = "online" | "offline" | "degraded" | "unknown";

export interface Device {
  id: string;
  hostname: string;
  ip_address: string;
  vendor: DeviceVendor;
  site?: string | null;
  device_type?: string | null;
  status: DeviceStatus;
  ssh_username?: string | null;
  ssh_credential_ref?: string | null;
}

export type ChangePriority = "low" | "medium" | "high" | "emergency";
export type ChangeStatus =
  | "draft"
  | "pending_approval"
  | "approved"
  | "rejected"
  | "validating"
  | "deploying"
  | "monitoring"
  | "success"
  | "failed"
  | "rolled_back";

export interface ChangeRequest {
  id: string;
  device_id: string;
  submitted_by: string;
  approved_by?: string | null;
  priority: ChangePriority;
  description: string;
  business_justification?: string | null;
  current_config?: string | null;
  proposed_config: string;
  config_diff?: string | null;
  risk_score?: number | null;
  risk_findings?: string | null;
  status: ChangeStatus;
  is_rollback: "true" | "false";
  rollback_snapshot_id?: string | null;
  created_at: string;
}

export interface Snapshot {
  id: string;
  device_id: string;
  change_request_id?: string | null;
  version: string;
  checksum: string;
  created_at: string;
}

export interface AuditLogEntry {
  id: string;
  time: string;
  user: string;
  action: string;
  device?: string | null;
  result: string;
}

// --- Configuration Management ---

export interface RunningConfig {
  device_id: string;
  hostname: string;
  protocol: string;
  config: string;
  retrieved_at: string;
}

export interface StartupConfig {
  device_id: string;
  hostname: string;
  config: string | null;
  source: "snapshot" | "unavailable";
  snapshot_id?: string | null;
  retrieved_at: string;
}

export interface BackupHistoryEntry {
  id: string;
  device_id: string;
  change_request_id?: string | null;
  version: string;
  checksum: string;
  has_startup_config: boolean;
  created_at: string;
}

export interface BackupConfigResponse {
  snapshot: BackupHistoryEntry;
  protocol: string;
  message: string;
}

export interface RestoreConfigResponse {
  device_id: string;
  hostname: string;
  restored_from_snapshot_id: string;
  post_restore_snapshot_id?: string | null;
  protocol: string;
  success: boolean;
  message: string;
}

export interface CompareConfigResponse {
  device_id: string;
  base_label: string;
  target_label: string;
  identical: boolean;
  diff: string;
}

// --- Configuration Drift Detection ---

export type DriftBaseline = "golden_config" | "previous_backup";
export type DriftSeverity = "low" | "medium" | "high" | "critical";
export type DriftStatus = "open" | "approved" | "rolled_back" | "dismissed";

export interface Drift {
  id: string;
  device_id: string;
  baseline: DriftBaseline;
  added_lines: number;
  removed_lines: number;
  modified_lines: number;
  risk_score: number;
  compliance_score: number;
  severity: DriftSeverity;
  ai_summary?: string | null;
  status: DriftStatus;
  detected_at: string;
}

export interface DriftDetail extends Drift {
  diff_text: string;
}

export interface RollbackRecommendation {
  recommended: boolean;
  reason: string;
}

export interface DriftScanResponse {
  drift: DriftDetail;
  baseline_label: string;
  findings: string[];
  rollback_recommendation: RollbackRecommendation;
}

export interface DriftFleetSummary {
  total_open_drifts: number;
  devices_drifted: number;
  average_compliance_score: number;
  by_severity: Record<DriftSeverity, number>;
  rollback_recommended_count: number;
}

export interface DashboardSummary {
  devices_online: number;
  devices_total: number;
  active_deployments: number;
  failed_deployments: number;
  rollbacks: number;
  pending_change_requests: number;
  critical_alerts: number;
  warning_alerts: number;
}

// --- Alert System ---

export type AlertSeverity = "critical" | "warning" | "info";
export type AlertSourceType = "snmp_trap" | "health_poll" | "drift" | "protocol_failure";

export interface Alert {
  id: string;
  device_id: string | null;
  severity: AlertSeverity;
  source: AlertSourceType;
  category: string;
  message: string;
  acknowledged: boolean;
  acknowledged_by: string | null;
  resolved: boolean;
  resolved_at: string | null;
  resolved_by: string | null;
  created_at: string;
}

export interface AlertSummary {
  critical: number;
  warning: number;
  info: number;
  active_total: number;
  resolved: number;
}

export interface HealthCheck {
  category: string;
  check_name: string;
  passed: boolean;
  detail: string | null;
  checked_at: string;
}

export interface DeploymentLog {
  id: string;
  step: string;
  level: "INFO" | "WARN" | "ERROR";
  message: string;
  timestamp: string;
}

export interface DeploymentRecord {
  id: string;
  change_request_id: string;
  device_id: string;
  snapshot_id: string | null;
  protocol: string;
  status: "queued" | "in_progress" | "succeeded" | "failed" | "rolled_back";
  error_message: string | null;
  created_at: string;
  health_checks: HealthCheck[];
}