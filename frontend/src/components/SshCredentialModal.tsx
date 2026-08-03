import { useState } from "react";
import { api } from "../lib/api";
import { Device } from "../lib/types";

interface SshTestResult {
  success: boolean;
  message: string;
  protocol?: string | null;
}

export default function SshCredentialsModal({
  device,
  onClose,
  onSaved,
}: {
  device: Device;
  onClose: () => void;
  onSaved: (updated: Device) => void;
}) {
  const [username, setUsername] = useState(device.ssh_username || "");
  const [password, setPassword] = useState("");

  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [testResult, setTestResult] = useState<SshTestResult | null>(null);

  const save = async () => {
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const body: Record<string, string> = {};
      if (username !== (device.ssh_username || "")) body.username = username;
      if (password) body.password = password;
      const res = await api.post<Device>(`/devices/${device.id}/ssh-credentials`, body);
      setSaved(true);
      setPassword("");
      onSaved(res.data);
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to save SSH credentials.");
    } finally {
      setSaving(false);
    }
  };

  const test = async () => {
    setTesting(true);
    setError(null);
    setTestResult(null);
    try {
      const res = await api.post<SshTestResult>(`/devices/${device.id}/ssh-credentials/test`);
      setTestResult(res.data);
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to test SSH connection.");
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-navy dark:bg-slate-950/40 flex items-center justify-center z-50 px-4">
      <div className="bg-white dark:bg-slate-800 rounded-xl shadow-xl max-w-md w-full p-5">
        <div className="flex items-start justify-between">
          <div>
            <h3 className="font-semibold text-navy dark:text-white">SSH Credentials — {device.hostname}</h3>
            <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">
              Used for NETCONF/RESTCONF/SSH config reads, backups, deployments and rollbacks.
              Password is stored encrypted; never shown again after saving.
            </p>
          </div>
          <button onClick={onClose} className="text-slate-400 dark:text-slate-500 hover:text-slate-600 dark:text-slate-300 text-lg leading-none">
            ✕
          </button>
        </div>

        <div className="mt-4 space-y-3">
          <div>
            <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">Username</label>
            <input
              type="text"
              autoComplete="off"
              className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm"
              placeholder="e.g. admin"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">Password</label>
            <input
              type="password"
              autoComplete="new-password"
              className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm"
              placeholder={device.ssh_credentials_configured ? "•••••••• (leave blank to keep current)" : ""}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>

          {error && <p className="text-riskcrit text-xs">{error}</p>}
          {saved && !error && <p className="text-risklow text-xs">Credentials saved.</p>}

          {testResult && (
            <div
              className={`text-xs rounded-lg px-3 py-2 border ${
                testResult.success ? "bg-green-50 border-green-200 text-risklow" : "bg-red-50 border-red-200 text-riskcrit"
              }`}
            >
              <p className="font-medium">{testResult.success ? "✓ Connection succeeded" : "✕ Connection failed"}</p>
              <p className="mt-0.5">{testResult.message}</p>
              {testResult.protocol && <p className="mt-1 text-slate-500 dark:text-slate-400">via {testResult.protocol}</p>}
            </div>
          )}

          <div className="flex gap-2 justify-end pt-2">
            <button
              onClick={test}
              disabled={testing}
              title="Tests the credentials currently on file for this device — save first if you just changed them."
              className="px-3 py-2 text-xs font-semibold text-brandblue border border-blue-200 bg-blue-50 rounded-lg hover:bg-blue-100 disabled:opacity-50"
            >
              {testing ? "Testing…" : "Test Connection"}
            </button>
            <button
              onClick={save}
              disabled={saving || (!password && username === (device.ssh_username || ""))}
              className="px-4 py-2 text-xs font-semibold text-white bg-brandblue rounded-lg hover:bg-navy dark:bg-slate-950 disabled:opacity-50"
            >
              {saving ? "Saving…" : "Save Credentials"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}