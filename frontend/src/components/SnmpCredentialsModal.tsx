import { useState } from "react";
import { api } from "../lib/api";
import { Device } from "../lib/types";

interface SnmpTestResult {
  success: boolean;
  message: string;
  sys_descr?: string | null;
  sys_uptime_seconds?: number | null;
}

function formatUptime(seconds?: number | null): string {
  if (seconds == null) return "";
  const days = Math.floor(seconds / 86400);
  const hrs = Math.floor((seconds % 86400) / 3600);
  const mins = Math.floor((seconds % 3600) / 60);
  if (days > 0) return `${days}d ${hrs}h uptime`;
  if (hrs > 0) return `${hrs}h ${mins}m uptime`;
  return `${mins}m uptime`;
}

export default function SnmpCredentialsModal({
  device,
  onClose,
  onSaved,
}: {
  device: Device;
  onClose: () => void;
  onSaved: (updated: Device) => void;
}) {
  const isV3 = device.snmp_version === "v3";

  const [community, setCommunity] = useState("");
  const [authKey, setAuthKey] = useState("");
  const [privKey, setPrivKey] = useState("");

  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [testResult, setTestResult] = useState<SnmpTestResult | null>(null);

  const save = async () => {
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const body: Record<string, string> = {};
      if (isV3) {
        if (authKey) body.v3_auth_key = authKey;
        if (privKey) body.v3_priv_key = privKey;
      } else if (community) {
        body.community = community;
      }
      const res = await api.post<Device>(`/devices/${device.id}/snmp-credentials`, body);
      setSaved(true);
      onSaved(res.data);
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to save SNMP credentials.");
    } finally {
      setSaving(false);
    }
  };

  const test = async () => {
    setTesting(true);
    setError(null);
    setTestResult(null);
    try {
      const res = await api.post<SnmpTestResult>(`/devices/${device.id}/snmp-credentials/test`);
      setTestResult(res.data);
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to test SNMP connection.");
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-navy/40 flex items-center justify-center z-50 px-4">
      <div className="bg-white rounded-xl shadow-xl max-w-md w-full p-5">
        <div className="flex items-start justify-between">
          <div>
            <h3 className="font-semibold text-navy">SNMP Credentials — {device.hostname}</h3>
            <p className="text-xs text-slate-500 mt-1">
              {isV3
                ? `SNMPv3 (${device.snmp_security_level || "noAuthNoPriv"}) for user "${device.snmp_username || "—"}"`
                : `SNMP ${device.snmp_version?.toUpperCase() || "v2c"} community string`}
              . Stored encrypted; never shown again after saving.
            </p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600 text-lg leading-none">
            ✕
          </button>
        </div>

        {!device.snmp_version && (
          <p className="text-xs text-riskcrit mt-4">
            This device has no SNMP version configured yet — set one under Edit Device first.
          </p>
        )}

        {device.snmp_version && (
          <div className="mt-4 space-y-3">
            {!isV3 ? (
              <div>
                <label className="block text-xs font-medium text-slate-600 mb-1">Community string</label>
                <input
                  type="password"
                  autoComplete="new-password"
                  className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
                  placeholder={device.snmp_credentials_configured ? "•••••••• (leave blank to keep current)" : "e.g. public"}
                  value={community}
                  onChange={(e) => setCommunity(e.target.value)}
                />
              </div>
            ) : (
              <>
                {(device.snmp_security_level === "authNoPriv" || device.snmp_security_level === "authPriv") && (
                  <div>
                    <label className="block text-xs font-medium text-slate-600 mb-1">
                      Auth passphrase ({device.snmp_auth_protocol || "SHA"})
                    </label>
                    <input
                      type="password"
                      autoComplete="new-password"
                      className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
                      placeholder={device.snmp_credentials_configured ? "•••••••• (leave blank to keep current)" : ""}
                      value={authKey}
                      onChange={(e) => setAuthKey(e.target.value)}
                    />
                  </div>
                )}
                {device.snmp_security_level === "authPriv" && (
                  <div>
                    <label className="block text-xs font-medium text-slate-600 mb-1">
                      Privacy passphrase ({device.snmp_priv_protocol || "AES128"})
                    </label>
                    <input
                      type="password"
                      autoComplete="new-password"
                      className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
                      placeholder={device.snmp_credentials_configured ? "•••••••• (leave blank to keep current)" : ""}
                      value={privKey}
                      onChange={(e) => setPrivKey(e.target.value)}
                    />
                  </div>
                )}
                {(!device.snmp_security_level || device.snmp_security_level === "noAuthNoPriv") && (
                  <p className="text-xs text-slate-400 italic">
                    Security level is noAuthNoPriv — no auth/privacy passphrase needed, just the username
                    (already set on the device record).
                  </p>
                )}
              </>
            )}

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
                {testResult.sys_descr && <p className="mt-1 text-slate-500 truncate">{testResult.sys_descr}</p>}
                {testResult.sys_uptime_seconds != null && (
                  <p className="text-slate-400">{formatUptime(testResult.sys_uptime_seconds)}</p>
                )}
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
                disabled={saving || (!isV3 && !community) || (isV3 && !authKey && !privKey)}
                className="px-4 py-2 text-xs font-semibold text-white bg-brandblue rounded-lg hover:bg-navy disabled:opacity-50"
              >
                {saving ? "Saving…" : "Save Credentials"}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}