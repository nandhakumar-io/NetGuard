import { Suspense, lazy } from "react";
import { Routes, Route } from "react-router-dom";
import Layout from "./components/Layout";
import ProtectedRoute from "./components/ProtectedRoute";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";

// Everything but Login/Dashboard is code-split at the route level: each page
// only downloads when the person actually navigates to it, instead of all
// 25 pages landing in one ~1.5MB bundle up front. Biggest win is on the
// heaviest pages (Devices, Topology, ChangeRequests, Lab) which most
// sessions never touch.
const ChangeRequests = lazy(() => import("./pages/ChangeRequests"));
const Deployments = lazy(() => import("./pages/Deployments"));
const Jobs = lazy(() => import("./pages/Jobs"));
const Devices = lazy(() => import("./pages/Devices"));
const DeviceDetail = lazy(() => import("./pages/DeviceDetail"));
const Groups = lazy(() => import("./pages/Groups"));
const IPAMPage = lazy(() => import("./pages/IPAM"));
const AuditLog = lazy(() => import("./pages/AuditLog"));
const TerminalRecordings = lazy(() => import("./pages/TerminalRecordings"));
const AlertRunbooks = lazy(() => import("./pages/AlertRunbooks"));
const Security = lazy(() => import("./pages/Security"));
const DriftPage = lazy(() => import("./pages/Drift"));
const AlertCenter = lazy(() => import("./pages/AlertCenter"));
const DeviceConfiguration = lazy(() => import("./pages/DeviceConfiguration"));
const Lab = lazy(() => import("./pages/Lab"));
const Topology = lazy(() => import("./pages/Topology"));
const ConfigSearchPage = lazy(() => import("./pages/ConfigSearch"));
const TemplatesPage = lazy(() => import("./pages/Templates"));
const SyslogViewer = lazy(() => import("./pages/SyslogViewer"));
const TrafficAnalysis = lazy(() => import("./pages/TrafficAnalysis"));
const PathTracePage = lazy(() => import("./pages/PathTrace"));
const MaintenanceWindowsPage = lazy(() => import("./pages/MaintenanceWindows"));
const FirmwareUpgradesPage = lazy(() => import("./pages/FirmwareUpgrades"));
const Incidents = lazy(() => import("./pages/Incidents"));
const RbacAudit = lazy(() => import("./pages/RbacAudit"));
const JitAccess = lazy(() => import("./pages/JitAccess"));
const Insights = lazy(() => import("./pages/Insights"));
const IntegrationsPage = lazy(() => import("./pages/Integrations"));
const MobileNOC = lazy(() => import("./pages/MobileNOC"));
const Users = lazy(() => import("./pages/Users"));
const Backups = lazy(() => import("./pages/Backups"));
const Discovery = lazy(() => import("./pages/Discovery"));
const WallBoard = lazy(() => import("./pages/WallBoard"));
const OnCallSchedules = lazy(() => import("./pages/OnCallSchedules"));
const EscalationPolicies = lazy(() => import("./pages/EscalationPolicies"));
const AuditorExport = lazy(() => import("./pages/AuditorExport"));

function RouteFallback() {
  return (
    <div className="flex items-center justify-center py-24">
      <div className="w-6 h-6 border-2 border-slate-200 dark:border-slate-700 border-t-brandblue rounded-full animate-spin" />
    </div>
  );
}

function withSuspense(el: JSX.Element) {
  return <Suspense fallback={<RouteFallback />}>{el}</Suspense>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<ProtectedRoute />}>
        {/* Deliberately outside <Layout> -- no sidebar/topbar chrome, just
            the stripped-down alert list an on-call engineer wants on a
            phone. See pages/MobileNOC.tsx. */}
        <Route path="/noc" element={withSuspense(<MobileNOC />)} />
        {/* Same rationale as /noc above -- a wall-mounted monitor wants
            fullscreen kiosk chrome, not the sidebar/topbar. See
            pages/WallBoard.tsx. */}
        <Route path="/wallboard" element={withSuspense(<WallBoard />)} />
        <Route element={<Layout />}>
          <Route path="/" element={<Dashboard />} />
          <Route path="/change-requests" element={withSuspense(<ChangeRequests />)} />
          <Route path="/deployments" element={withSuspense(<Deployments />)} />
          <Route path="/jobs" element={withSuspense(<Jobs />)} />
          <Route path="/devices" element={withSuspense(<Devices />)} />
          <Route path="/devices/config" element={withSuspense(<DeviceConfiguration />)} />
          <Route path="/devices/:deviceId" element={withSuspense(<DeviceDetail />)} />
          <Route path="/groups" element={withSuspense(<Groups />)} />
          <Route path="/ipam" element={withSuspense(<IPAMPage />)} />
          <Route path="/config-search" element={withSuspense(<ConfigSearchPage />)} />
          <Route path="/templates" element={withSuspense(<TemplatesPage />)} />
          <Route path="/topology" element={withSuspense(<Topology />)} />
          <Route path="/path-trace" element={withSuspense(<PathTracePage />)} />
          <Route path="/syslog" element={withSuspense(<SyslogViewer />)} />
          <Route path="/traffic-analysis" element={withSuspense(<TrafficAnalysis />)} />
          <Route path="/drift" element={withSuspense(<DriftPage />)} />
          <Route path="/alerts" element={withSuspense(<AlertCenter />)} />
          <Route path="/alert-runbooks" element={withSuspense(<AlertRunbooks />)} />
          <Route path="/on-call-schedules" element={withSuspense(<OnCallSchedules />)} />
          <Route path="/escalation-policies" element={withSuspense(<EscalationPolicies />)} />
          <Route path="/maintenance-windows" element={withSuspense(<MaintenanceWindowsPage />)} />
          <Route path="/firmware-upgrades" element={withSuspense(<FirmwareUpgradesPage />)} />
          <Route path="/incidents" element={withSuspense(<Incidents />)} />
          <Route path="/insights" element={withSuspense(<Insights />)} />
          <Route path="/rbac-audit" element={withSuspense(<RbacAudit />)} />
          <Route path="/jit-access" element={withSuspense(<JitAccess />)} />
          <Route path="/lab" element={withSuspense(<Lab />)} />
          <Route path="/audit-log" element={withSuspense(<AuditLog />)} />
          <Route path="/auditor-export" element={withSuspense(<AuditorExport />)} />
          <Route path="/terminal-recordings" element={withSuspense(<TerminalRecordings />)} />
          <Route path="/security" element={withSuspense(<Security />)} />
          <Route path="/integrations" element={withSuspense(<IntegrationsPage />)} />
          <Route path="/users" element={withSuspense(<Users />)} />
          <Route path="/backups" element={withSuspense(<Backups />)} />
          <Route path="/discovery" element={withSuspense(<Discovery />)} />
        </Route>
      </Route>
    </Routes>
  );
}