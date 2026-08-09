import { Routes, Route } from "react-router-dom";
import Layout from "./components/Layout";
import ProtectedRoute from "./components/ProtectedRoute";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import ChangeRequests from "./pages/ChangeRequests";
import Deployments from "./pages/Deployments";
import Devices from "./pages/Devices";
import Groups from "./pages/Groups";
import AuditLog from "./pages/AuditLog";
import Security from "./pages/Security";
import DriftPage from "./pages/Drift";
import AlertCenter from "./pages/AlertCenter";
import DeviceConfiguration from "./pages/DeviceConfiguration";
import Lab from "./pages/Lab";
import Topology from "./pages/Topology";
import ConfigSearchPage from "./pages/ConfigSearch";
import TemplatesPage from "./pages/Templates";
import SyslogViewer from "./pages/SyslogViewer";
import TrafficAnalysis from "./pages/TrafficAnalysis";
import PathTracePage from "./pages/PathTrace";
import MaintenanceWindowsPage from "./pages/MaintenanceWindows";
import FirmwareUpgradesPage from "./pages/FirmwareUpgrades";
import Incidents from "./pages/Incidents";
import RbacAudit from "./pages/RbacAudit";
import Insights from "./pages/Insights";

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<ProtectedRoute />}>
        <Route element={<Layout />}>
          <Route path="/" element={<Dashboard />} />
          <Route path="/change-requests" element={<ChangeRequests />} />
          <Route path="/deployments" element={<Deployments />} />
          <Route path="/devices" element={<Devices />} />
          <Route path="/devices/config" element={<DeviceConfiguration />} />
          <Route path="/groups" element={<Groups />} />
          <Route path="/config-search" element={<ConfigSearchPage />} />
          <Route path="/templates" element={<TemplatesPage />} />
          <Route path="/topology" element={<Topology />} />
          <Route path="/path-trace" element={<PathTracePage />} />
          <Route path="/syslog" element={<SyslogViewer />} />
          <Route path="/traffic-analysis" element={<TrafficAnalysis />} />
          <Route path="/drift" element={<DriftPage />} />
          <Route path="/alerts" element={<AlertCenter />} />
          <Route path="/maintenance-windows" element={<MaintenanceWindowsPage />} />
          <Route path="/firmware-upgrades" element={<FirmwareUpgradesPage />} />
          <Route path="/incidents" element={<Incidents />} />
          <Route path="/insights" element={<Insights />} />
          <Route path="/rbac-audit" element={<RbacAudit />} />
          <Route path="/lab" element={<Lab />} />
          <Route path="/audit-log" element={<AuditLog />} />
          <Route path="/security" element={<Security />} />
        </Route>
      </Route>
    </Routes>
  );
}