import { Routes, Route } from "react-router-dom";
import Layout from "./components/Layout";
import ProtectedRoute from "./components/ProtectedRoute";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import ChangeRequests from "./pages/ChangeRequests";
import Deployments from "./pages/Deployments";
import Devices from "./pages/Devices";
import AuditLog from "./pages/AuditLog";
import Security from "./pages/Security";

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
          <Route path="/audit-log" element={<AuditLog />} />
          <Route path="/security" element={<Security />} />
        </Route>
      </Route>
    </Routes>
  );
}
