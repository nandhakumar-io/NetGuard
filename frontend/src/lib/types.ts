export type DeviceVendor = "cisco" | "juniper" | "arista" | "linux";
export type DeviceStatus = "online" | "offline" | "degraded" | "unknown";

export type DeviceLifecycleState = "staging" | "production" | "decommissioned";

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
  ssh_credentials_configured?: boolean;
  flagged_unstable?: boolean;
  unstable_since?: string | null;
  supports_snmp?: boolean;
  snmp_version?: "v1" | "v2c" | "v3" | null;
  snmp_port?: number | null;
  snmp_community_ref?: string | null;
  snmp_username?: string | null;
  snmp_auth_credential_ref?: string | null;
  snmp_privacy_credential_ref?: string | null;
  snmp_security_level?: "noAuthNoPriv" | "authNoPriv" | "authPriv" | null;
  snmp_auth_protocol?: "MD5" | "SHA" | "SHA224" | "SHA256" | "SHA384" | "SHA512" | null;
  snmp_priv_protocol?: "DES" | "3DES" | "AES128" | "AES192" | "AES256" | null;
  snmp_credentials_configured?: boolean;
  supports_netconf?: boolean;
  netconf_port?: number | null;
  supports_restconf?: boolean;
  restconf_url?: string | null;
  platform?: string | null;
  model?: string | null;
  serial_number?: string | null;
  os_version?: string | null;
  capabilities?: string | null;
  device_role?: string | null;
  lifecycle_state?: DeviceLifecycleState;
  tags?: string[];
  custom_fields?: Record<string, string>;
  data_center?: string | null;
  rack?: string | null;
  rack_position?: number | null;
  enabled_health_checks?: string[] | null;
  // Derived (not stored) -- see backend eol_service / DeviceRead.from_device.
  eol_matched?: boolean;
  eol_platform_label?: string | null;
  is_eos?: boolean;
  is_eol?: boolean;
  eos_date?: string | null;
  eol_date?: string | null;
  eol_note?: string | null;
}

export interface HealthCheckCatalogEntry {
  name: string;
  description: string;
}

export interface TemplateVariable {
  name: string;
  label?: string | null;
  default?: string | null;
  required?: boolean;
}

export interface ConfigTemplate {
  id: string;
  name: string;
  description?: string | null;
  device_role?: string | null;
  vendor?: string | null;
  body: string;
  variables: TemplateVariable[];
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface TemplateDiffPreviewResponse {
  rendered_config: string;
  base_label: string;
  identical: boolean;
  diff: string;
  cli_diff: string[];
  change_summary: string[];
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

export interface TriggeringAlertSummary {
  id: string;
  severity: string;
  category: string;
  message: string;
  created_at: string;
}

export interface ChangeRequest {
  id: string;
  device_id: string;
  submitted_by: string;
  approved_by?: string | null;
  priority: ChangePriority;
  description: string;
  business_justification?: string | null;
  maintenance_window_start?: string | null;
  maintenance_window_end?: string | null;
  current_config?: string | null;
  proposed_config: string;
  config_diff?: string | null;
  risk_score?: number | null;
  risk_findings?: string | null;
  risk_classification?: string | null;
  config_source?: string | null;
  risk_engine_backend?: string | null;
  risk_llm_applied?: boolean;
  risk_llm_error?: string | null;
  requires_dual_approval?: boolean;
  dual_approval_reason?: string | null;
  first_approved_by?: string | null;
  status: ChangeStatus;
  is_rollback: "true" | "false";
  rollback_snapshot_id?: string | null;
  canary_enabled?: boolean;
  additional_device_ids?: string[];
  target_device_count?: number;
  config_diff_cli?: string | null;
  config_diff_summary?: string | null;
  created_at: string;
  // Approval workflow visibility (who approved, when).
  approved_at?: string | null;
  submitted_by_name?: string | null;
  approved_by_name?: string | null;
  first_approved_by_name?: string | null;
  // Alert -> CR auto-link (postmortem traceability).
  triggering_alert_id?: string | null;
  triggering_alert?: TriggeringAlertSummary | null;
}

export interface PendingApprovalItem {
  change_request: ChangeRequest;
  sla_hours: number;
  elapsed_hours: number;
  due_at: string;
  is_overdue: boolean;
  is_first_approval_needed: boolean;
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
  config_pretty?: string | null;
  is_xml?: boolean;
  retrieved_at: string;
}

export interface StartupConfig {
  device_id: string;
  hostname: string;
  config: string | null;
  config_pretty?: string | null;
  is_xml?: boolean;
  source: "snapshot" | "unavailable";
  snapshot_id?: string | null;
  retrieved_at: string;
}

export interface GoldenConfig {
  device_id: string;
  config: string;
  config_pretty?: string | null;
  is_xml?: boolean;
  checksum: string;
  set_by: string;
  created_at: string;
  updated_at: string;
}

export interface ArpEntry {
  if_index: string;
  ip_address: string;
  mac_address: string;
}

export interface RouteEntry {
  destination: string;
  mask: string | null;
  next_hop: string;
  if_index: string | null;
}

export interface LldpNeighbor {
  local_port_index: string;
  neighbor_name: string | null;
  neighbor_port: string | null;
}

export interface CdpNeighbor {
  local_if_index: string;
  neighbor_id: string | null;
  neighbor_port: string | null;
  neighbor_platform: string | null;
}

export interface InventoryItem {
  index: string;
  name: string | null;
  description: string | null;
  model: string | null;
  serial_number: string | null;
}

export interface DeviceDiscoveryResult {
  device_id: string;
  hostname: string | null;
  reported_hostname: string | null;
  arp_table: ArpEntry[];
  routing_table: RouteEntry[];
  lldp_neighbors: LldpNeighbor[];
  cdp_neighbors: CdpNeighbor[];
  inventory: InventoryItem[];
  retrieved_at: string;
}

export interface InterfaceStatus {
  name: string;
  description?: string | null;
  admin_status?: string | null;
  oper_status?: string | null;
  ip_addresses: string[];
  mtu?: number | null;
  speed?: string | null;
  mac_address?: string | null;
}

export interface InterfacesResponse {
  device_id: string;
  hostname: string;
  protocol: string;
  interfaces: InterfaceStatus[];
  retrieved_at: string;
  error?: string | null;
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

export type DriftBaseline = "golden_config" | "previous_backup" | "role_baseline";
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
  cli_diff?: string | null;
}

export interface ComplianceBaselineSummary {
  device_role: string;
  checksum: string;
  description?: string | null;
  set_by: string;
  device_count: number;
  updated_at: string;
}

export interface ComplianceBaselineDetail {
  device_role: string;
  config: string;
  config_pretty?: string | null;
  is_xml: boolean;
  checksum: string;
  description?: string | null;
  set_by: string;
  device_count: number;
  created_at: string;
  updated_at: string;
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

export interface WeeklyGoldenDriftEntry extends Drift {
  hostname: string;
}

export interface WeeklyGoldenDriftReport {
  since: string;
  days: number;
  devices: WeeklyGoldenDriftEntry[];
}

// --- Drift trending / flapping ---

export interface DriftTrendPoint {
  bucket_start: string;
  total: number;
  critical: number;
  high: number;
  distinct_devices: number;
}

export interface DriftTrendResponse {
  days: number;
  bucket_days: number;
  points: DriftTrendPoint[];
}

export interface FlappingDeviceEntry {
  device_id: string;
  hostname: string;
  event_count: number;
  last_detected_at: string;
  max_severity: DriftSeverity;
}

export interface FlappingDevicesResponse {
  days: number;
  min_events: number;
  devices: FlappingDeviceEntry[];
}

// --- Snapshot Retention Policy ---

export interface RetentionPolicy {
  retention_days: number;
  min_snapshots_per_device: number;
  sweep_hour_utc: number;
  description: string;
}

export interface DeviceRetentionStatus {
  device_id: string;
  total_snapshots: number;
  protected_snapshots: number;
  eligible_for_purge: number;
  oldest_snapshot_at?: string | null;
  newest_snapshot_at?: string | null;
}

export interface RetentionPolicyResponse {
  policy: RetentionPolicy;
  device?: DeviceRetentionStatus | null;
}

// --- Rollback Preview ---

export interface RollbackPreviewResponse {
  device_id: string;
  snapshot_id: string;
  target_version: string;
  current_source: "live" | "last_snapshot" | "unavailable";
  diff: string;
  identical: boolean;
  added_lines: number;
  removed_lines: number;
  warning?: string | null;
  blocked: boolean;
  blocked_reason?: string | null;
}

// --- Notification Center ---

export type NotificationEventType =
  | "deployment_succeeded"
  | "deployment_failed"
  | "rollback_triggered"
  | "drift_high"
  | "drift_critical"
  | "generic";

export interface AppNotification {
  id: string;
  event_type: NotificationEventType;
  severity: "info" | "warning" | "critical";
  title: string;
  message: string;
  device_hostname?: string | null;
  change_request_id?: string | null;
  deployment_id?: string | null;
  read: boolean;
  created_at: string;
}

export interface NotificationSummary {
  unread_count: number;
  total: number;
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
  open_drifts: number;
  flagged_unstable_count: number;
  eos_device_count: number;
  flagged_unstable_devices: { id: string; hostname: string; ip_address: string; unstable_since: string | null }[];
  global_health_score: number;
  deployment_success_rate: number;
  top_cpu_devices: { hostname: string; ip_address: string; cpu: number; cpu_history: number[] }[];
  top_memory_devices: { hostname: string; ip_address: string; memory: number; memory_history: number[] }[];
  top_bandwidth_devices: { hostname: string; ip_address: string; bandwidth: number; bandwidth_history: number[] }[];
  uplinks: {
    hostname: string;
    ip_address: string;
    role: string | null;
    status: string;
    utilization_pct: number;
    throughput_bps: number | null;
    link_speed_bps: number | null;
    errors: number | null;
    history: number[];
  }[];
  fleet_health_history: {
    timestamp: string | null;
    avg_cpu: number | null;
    avg_memory: number | null;
    avg_bandwidth: number | null;
  }[];
  down_ports: { hostname: string; interface: string; down_since: string | null }[];
  recent_reboots: { hostname: string; ip_address: string; uptime_seconds: number; polled_at: string | null }[];
  offline_devices: { id: string; hostname: string; ip_address: string; status: string; last_seen: string | null; last_error: string | null }[];
  top_error_devices: { hostname: string; ip_address: string; interface_errors: number }[];
  flapping_interfaces: { hostname: string; interface: string; flap_count: number; last_change: string | null }[];
  recent_backups: { id: string; version: string; created_at: string; hostname: string }[];
  recent_protocol_operations: { id: string; protocol: string; operation: string; success: boolean; created_at: string; operator: string; device_hostname: string }[];
}

// --- Dashboard widget customization ---

export interface DashboardWidgetInfo {
  id: string;
  title: string;
  data_source: string;
  default_visible: boolean;
}

export interface DashboardLayoutEntry {
  id: string;
  visible: boolean;
}

export interface DashboardPreferenceResponse {
  layout: DashboardLayoutEntry[];
  available_widgets: DashboardWidgetInfo[];
}

// --- Alert System ---

export type AlertSeverity = "critical" | "warning" | "info";
export type AlertSourceType = "snmp_trap" | "health_poll" | "drift" | "protocol_failure" | "syslog";

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
  last_seen_at?: string | null;
  occurrence_count?: number;
  root_cause_alert_id: string | null;
  suppressed: boolean;
  suppressed_by_window_id?: string | null;
  muted_until?: string | null;
  escalated?: boolean;
  escalated_at?: string | null;
  last_escalated_at?: string | null;
  escalation_count?: number;
  escalation_policy_id?: string | null;
  created_at: string;
}

export interface AlertSummary {
  critical: number;
  warning: number;
  info: number;
  active_total: number;
  resolved: number;
}

// --- Alert correlation grouping ---

export interface CorrelatedAlertGroup {
  root_cause_alert: Alert;
  suppressed_alerts: Alert[];
  total_alert_count: number;
}

// --- Escalation Policies ---

export type EscalationSeverityScope = "critical" | "warning" | "all";
export type EscalationChannel = "email" | "webhook" | "slack" | "teams";

export interface EscalationPolicy {
  id: string;
  name: string;
  description?: string | null;
  severity_scope: EscalationSeverityScope;
  unack_minutes: number;
  repeat_minutes?: number | null;
  secondary_contacts?: string | null;
  channel: EscalationChannel;
  webhook_url?: string | null;
  enabled: boolean;
  created_by?: string | null;
  created_at: string;
  updated_at?: string | null;
}

export interface EscalatedAlertEntry {
  id: string;
  device_id: string | null;
  severity: AlertSeverity;
  category: string;
  message: string;
  acknowledged: boolean;
  escalated_at?: string | null;
  last_escalated_at?: string | null;
  escalation_count: number;
  escalation_policy_id?: string | null;
  escalation_policy_name?: string | null;
  created_at: string;
}

// --- Maintenance Windows ---

export type MaintenanceScope = "device" | "site" | "fleet";

export interface MaintenanceWindow {
  id: string;
  name: string;
  reason: string | null;
  scope: MaintenanceScope;
  device_id: string | null;
  site: string | null;
  starts_at: string;
  ends_at: string;
  cancelled: boolean;
  cancelled_at: string | null;
  cancelled_by: string | null;
  created_by: string;
  created_at: string;
  is_active: boolean;
}

// --- Firmware / OS Upgrade Orchestration ---

export type FirmwareUpgradeStatus =
  | "pending"
  | "scheduled"
  | "downloading"
  | "installing"
  | "rebooting"
  | "verifying"
  | "completed"
  | "failed"
  | "rolled_back"
  | "cancelled";

export interface FirmwareUpgrade {
  id: string;
  batch_id: string | null;
  device_id: string;
  from_version: string | null;
  target_version: string;
  image_filename: string;
  image_sha256: string | null;
  status: FirmwareUpgradeStatus;
  current_step_detail: string | null;
  error_message: string | null;
  maintenance_window_id: string | null;
  scheduled_at: string | null;
  pre_upgrade_snapshot_id: string | null;
  reboot_wait_seconds: number;
  attempts: number;
  initiated_by: string;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
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
  target_device_count: number;
  health_checks: HealthCheck[];
}

export interface ProtocolOperationRecord {
  id: string;
  protocol: "netconf" | "restconf" | "snmp";
  operation: string;
  operator: string;
  success: boolean;
  error_message: string | null;
  http_status: number | null;
  execution_time_ms: number | null;
  created_at: string;
}

// --- GNS3 Lab Integration ---
export interface GNS3Status {
  enabled: boolean;
  reachable: boolean;
  version?: string | null;
  controller_url?: string | null;
  detail?: string | null;
}

export interface GNS3Project {
  project_id: string;
  name: string;
  status?: string | null;
  filename?: string | null;
}

export interface GNS3Node {
  node_id: string;
  name: string;
  node_type?: string | null;
  status?: string | null;
  console_host?: string | null;
  console_port?: number | null;
  console_type?: string | null;
  vendor_guess: string;
  synced: boolean;
  device_id?: string | null;
  bootstrapped: boolean;
  management_ip?: string | null;
}

export interface GNS3BootstrapRequest {
  mgmt_interface?: string;
  mgmt_ip: string;
  mgmt_subnet_mask?: string;
  ssh_username?: string;
  ssh_password: string;
  enable_password?: string | null;
  hostname?: string | null;
  ssh_credential_ref?: string | null;
  create_device?: boolean;
  site?: string | null;
}

export interface GNS3BootstrapResponse {
  success: boolean;
  output: string;
  error?: string | null;
  device_id?: string | null;
  hostname?: string | null;
  management_ip?: string | null;
  message: string;
}

export interface GNS3SyncResponse {
  project_id: string;
  created: number;
  updated: number;
  skipped: number;
  results: Array<{
    node_id: string;
    name: string;
    action: string;
    device_id?: string | null;
    detail?: string | null;
  }>;
}

export interface TopologyNode {
  id: string;
  hostname: string;
  ip_address: string;
  vendor: string;
  site?: string | null;
  device_type?: string | null;
  status: DeviceStatus;
  flagged_unstable: boolean;
  has_config_on_file: boolean;
  // Latest SNMP health reading for this device (null if never polled /
  // not SNMP-monitored) -- lets the topology map color nodes by live
  // health instead of just online/offline/degraded status.
  health_color: HealthColor | null;
  health_score: number | null;
  data_center?: string | null;
  rack?: string | null;
  // "core" | "distribution" | "access" | ... (free-text, org-defined) --
  // powers the Topology page's optional layered layout. null if never set.
  device_role?: string | null;
  // Interface errors seen on this device's most recent SNMP poll (delta
  // since the prior poll), for the Topology map's error-rate badge. null
  // if the device has never been polled.
  interface_error_rate?: number | null;
}

export interface TopologyEdge {
  source: string;
  target: string;
  subnet: string | null;
  source_ip: string | null;
  target_ip: string | null;
  link_source: "lldp" | "cdp" | "gns3" | "subnet";
  local_port: string | null;
  neighbor_port: string | null;
  // Best-effort link utilization, 0-100, for the "color links by
  // utilization" toggle -- null if neither endpoint has a recent SNMP
  // poll. See backend TopologyEdge.utilization_pct's docstring: this is
  // the higher of the two endpoints' whole-device interface utilization
  // readings, not a true per-port figure.
  utilization_pct: number | null;
}

export interface TopologyResponse {
  nodes: TopologyNode[];
  edges: TopologyEdge[];
}

// --- Global search (Cmd+K command palette) ---

export interface GlobalSearchResultItem {
  id: string;
  title: string;
  subtitle: string | null;
  url: string;
}

export interface GlobalSearchResponse {
  query: string;
  devices: GlobalSearchResultItem[];
  groups: GlobalSearchResultItem[];
  alerts: GlobalSearchResultItem[];
  change_requests: GlobalSearchResultItem[];
  templates: GlobalSearchResultItem[];
  incidents: GlobalSearchResultItem[];
  configs: GlobalSearchResultItem[];
}

// --- Alert snooze/mute (app.models.alert_snooze.AlertSnooze) ---

export interface AlertSnooze {
  id: string;
  device_id: string | null;
  device_hostname: string | null;
  category: string | null;
  reason: string | null;
  expires_at: string;
  created_by: string;
  created_at: string | null;
}

export interface DeviceGroupRule {
  field: "hostname" | "tag" | "site" | "device_type" | "device_role";
  pattern: string;
}

export interface DeviceGroup {
  id: string;
  name: string;
  description: string | null;
  group_type: string;
  parent_group_id: string | null;
  is_dynamic: boolean;
  membership_rules: DeviceGroupRule[];
  created_at: string | null;
  updated_at: string | null;
  device_count: number;
  child_group_count: number;
}

export interface DeviceGroupRuleMatch {
  device_id: string;
  hostname: string;
  matched_rule: DeviceGroupRule;
  already_member: boolean;
}

export interface DeviceGroupRulePreview {
  matches: DeviceGroupRuleMatch[];
}

export interface DeviceGroupRuleApplyResult {
  assigned_device_ids: string[];
  already_member_device_ids: string[];
}

export interface GroupHealthRollup {
  group_id: string;
  group_name: string;
  include_descendants: boolean;
  device_count: number;
  unmonitored_count: number;
  green_count: number;
  yellow_count: number;
  red_count: number;
  gray_count: number;
  average_health_score: number | null;
  worst_health_score: number | null;
  worst_device_hostname: string | null;
}

export interface DeviceCsvImportError {
  row: number;
  hostname: string | null;
  error: string;
}

export interface DeviceCsvImportResult {
  created: string[];
  updated: string[];
  errors: DeviceCsvImportError[];
  total_rows: number;
}

export type BulkDeviceAction =
  | "move_group"
  | "assign_tags"
  | "set_lifecycle_state"
  | "apply_config_template"
  | "add_maintenance_window";

export interface BulkDeviceActionRequest {
  device_ids: string[];
  action: BulkDeviceAction;
  params: Record<string, unknown>;
}

export interface BulkDeviceActionResult {
  action: BulkDeviceAction;
  affected_device_ids: string[];
  failed: Record<string, string>;
  detail: string | null;
  change_request_id: string | null;
}

export interface RackGroup {
  name: string;
  devices: Array<{
    id: string;
    hostname: string;
    status: DeviceStatus;
    device_type?: string | null;
    rack_position?: number | null;
  }>;
}

export interface DataCenterGroup {
  name: string;
  device_count: number;
  racks: RackGroup[];
}

// --- Interface (port) status: current + history (NOC dashboard, device panel) ---

export type InterfaceOperStatus = "up" | "down";

export interface InterfaceCurrentStatus {
  if_index: string;
  if_descr: string;
  status: InterfaceOperStatus;
  changed_at: string | null;
  seconds_in_status: number | null;
}

export interface InterfaceStatusHistoryEntry {
  id: string;
  device_id: string;
  if_index: string;
  if_descr: string;
  status: InterfaceOperStatus;
  previous_status: InterfaceOperStatus | null;
  changed_at: string | null;
}

// --- SNMP Health / Metrics (per-device Health & Interfaces tabs) ---

export type HealthColor = "green" | "yellow" | "red" | "gray";

export interface DeviceMetric {
  id: string;
  device_id: string;
  cpu_utilization_pct: number | null;
  memory_utilization_pct: number | null;
  interface_utilization_pct: number | null;
  interface_errors: number | null;
  temperature_celsius: number | null;
  fan_status: string | null;
  power_supply_status: string | null;
  uptime_seconds: number | null;
  health_score: number | null;
  health_color: HealthColor | null;
  polled_at: string;
}

export interface MetricFreshness {
  cpu: string | null;
  memory: string | null;
  interface: string | null;
  temperature: string | null;
  fan: string | null;
  power: string | null;
}

export interface DeviceHealthSummary {
  device_id: string;
  hostname: string;
  health_score: number | null;
  health_color: string;
  reachable: boolean;
  latest_metric: DeviceMetric | null;
  metric_freshness: MetricFreshness | null;
  stale_metrics: string[];
}

export interface FleetHealthSummary {
  devices_monitored: number;
  green: number;
  yellow: number;
  red: number;
  unknown: number;
  average_health_score: number | null;
  devices_with_stale_metrics: number;
}

export interface DeviceAvailability {
  device_id: string;
  hostname: string;
  availability_pct: number;
}

export interface FleetAvailabilitySummary {
  window_hours: number;
  devices_in_rollup: number;
  fleet_availability_pct: number | null;
  fleet_availability_label: string;
  worst_devices: DeviceAvailability[];
}

export interface UnstableDevice {
  device_id: string;
  hostname: string;
  reachability_flaps: number;
  interface_flaps: number;
  drift_events: number;
  instability_score: number;
}

// --- Syslog Collection & Correlation ---

export type SyslogSeverity = "EMERGENCY" | "ALERT" | "CRITICAL" | "ERROR" | "WARNING" | "NOTICE" | "INFORMATIONAL" | "DEBUG";

export interface SyslogMessage {
  id: string;
  device_id: string | null;
  device_hostname: string | null;
  source_ip: string;
  facility: number | null;
  severity: SyslogSeverity;
  reported_hostname: string | null;
  tag: string | null;
  message: string;
  device_reported_at: string | null;
  received_at: string;
  correlated_category: string | null;
  correlated_alert_id: string | null;
}

export interface SyslogVolumePoint {
  hour: string;
  count: number;
}

export interface SyslogSummary {
  total: number;
  correlated: number;
  by_severity: Record<string, number>;
  volume_by_hour: SyslogVolumePoint[];
}

// --- Path/Route Tracing (NetPath-style) ---

export type HopStatus = "ok" | "degraded" | "timeout" | "unknown";
export type PathTraceStatus = "complete" | "partial" | "failed";

export interface PathHop {
  id: string;
  hop_index: number;
  ip_address: string | null;
  hostname: string | null;
  device_id: string | null;
  rtt_ms: number | null;
  packet_loss_pct: number | null;
  status: HopStatus;
}

export interface PathTrace {
  id: string;
  source_device_id: string | null;
  source_hostname: string | null;
  source_ip: string;
  target_device_id: string | null;
  target_hostname: string | null;
  target_input: string;
  target_resolved_ip: string | null;
  hop_source: "traceroute" | "topology";
  status: PathTraceStatus;
  total_hops: number;
  reached_target: boolean;
  requested_by: string | null;
  created_at: string;
  hops: PathHop[];
}

// --- Traffic Analysis (NetFlow / IPFIX / sFlow) ---------------------------

export interface TopTalker {
  ip_address: string;
  bytes: number;
  packets: number;
}

export interface TopConversation {
  src_ip: string;
  dst_ip: string;
  protocol: string;
  bytes: number;
  packets: number;
}

export interface ProtocolShare {
  protocol: string;
  bytes: number;
  pct: number;
}

export interface BandwidthPoint {
  timestamp: string;
  bytes_per_sec: number;
}

export interface FlowExporter {
  exporter_ip: string;
  flow_version: string;
  hostname: string | null;
  last_seen: string | null;
  flow_count: number;
}

export interface TrafficSummary {
  window_minutes: number;
  top_talkers: TopTalker[];
  top_conversations: TopConversation[];
  protocol_breakdown: ProtocolShare[];
  bandwidth_timeseries: BandwidthPoint[];
  exporters: FlowExporter[];
}

// --- Customizable Alert Rules ---

export interface AlertRule {
  id: string;
  name: string;
  description: string | null;
  metric: string;
  operator: string;
  threshold: number;
  severity: string;
  scope_vendor: string | null;
  scope_site: string | null;
  scope_device_role: string | null;
  cooldown_seconds: number;
  enabled: boolean;
  created_by: string | null;
  created_at: string;
  updated_at: string | null;
}

// --- Webhook Endpoints ---

export type WebhookType = "generic" | "slack" | "teams" | "telegram";

export interface WebhookEndpoint {
  id: string;
  name: string;
  url: string;
  webhook_type: WebhookType;
  secret: string | null;
  events: string[] | null;
  telegram_chat_id: string | null;
  enabled: boolean;
  created_by: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface WebhookTestResult {
  success: boolean;
  message: string;
  status_code: number | null;
}

// --- Topology Snapshots & Diff ---

export interface TopologySnapshotSummary {
  id: string;
  node_count: number;
  edge_count: number;
  captured_at: string;
}

export interface NodeDiff {
  id: string;
  hostname: string;
  ip_address: string;
}

export interface EdgeDiff {
  source: string;
  target: string;
  link_source: string;
  subnet?: string;
  source_ip?: string;
  target_ip?: string;
}

export interface TopologyDiff {
  older_snapshot_id: string;
  newer_snapshot_id: string;
  older_captured_at: string;
  newer_captured_at: string;
  unchanged_node_count: number;
  unchanged_edge_count: number;
  added_nodes: NodeDiff[];
  removed_nodes: NodeDiff[];
  added_edges: EdgeDiff[];
  removed_edges: EdgeDiff[];
}
export interface BlastRadiusPreview {
  touched_count: number;
  touched_core_count: number;
  touched_roles: Record<string, number>;
  touched_device_ids: string[];
  dependent_count: number;
  dependent_device_ids: string[];
  unknown_device_ids: string[];
}