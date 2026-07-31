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

export interface DashboardSummary {
  devices_online: number;
  devices_total: number;
  active_deployments: number;
  failed_deployments: number;
  rollbacks: number;
  pending_change_requests: number;
}