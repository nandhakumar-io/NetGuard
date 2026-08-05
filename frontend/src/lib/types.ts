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
  recent_backups: { id: string; version: string; created_at: string; hostname: string }[];
  recent_protocol_operations: { id: string; protocol: string; operation: string; success: boolean; created_at: string; operator: string; device_hostname: string }[];
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
  last_seen_at?: string | null;
  occurrence_count?: number;
  root_cause_alert_id: string | null;
  suppressed: boolean;
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
}

export interface TopologyEdge {
  source: string;
  target: string;
  subnet: string | null;
  source_ip: string | null;
  target_ip: string | null;
  link_source: "lldp" | "cdp" | "subnet";
  local_port: string | null;
  neighbor_port: string | null;
}

export interface TopologyResponse {
  nodes: TopologyNode[];
  edges: TopologyEdge[];
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

// --- Topology snapshots / diffing -----------------------------------------

export interface TopologySnapshotSummary {
  id: string;
  node_count: number;
  edge_count: number;
  captured_at: string | null;
}

export interface TopologyDiffNode {
  id: string;
  hostname: string;
  ip_address: string;
}

export interface TopologyDiffEdge {
  source: string;
  target: string;
  link_source: string;
}

export interface TopologyDiff {
  older_snapshot_id: string;
  newer_snapshot_id: string;
  older_captured_at: string | null;
  newer_captured_at: string | null;
  added_nodes: TopologyDiffNode[];
  removed_nodes: TopologyDiffNode[];
  added_edges: TopologyDiffEdge[];
  removed_edges: TopologyDiffEdge[];
  unchanged_node_count: number;
  unchanged_edge_count: number;
}