import { useState } from "react";
import { api } from "../lib/api";
import { Device } from "../lib/types";

interface SnmpTestResult {
  success: boolean;
  message: string;
  sys_descr?: string | null;
  sys_uptime_seconds?: number | null;
}

interface ConnectionDiagnosticStep {
  name: string;
  success: boolean;
  detail: string;
}

interface ConnectionTestAndFixResult {
  steps: ConnectionDiagnosticStep[];
  overall_success: boolean;
  status_before: string;
  status_after: string;
  fix_applied: boolean;
  fix_detail?: string | null;
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
  onDeviceUpdated,
}: {
  device: Device;
  onClose: () => void;
  onSaved: (updated: Device) => void;
  /** Same as onSaved but doesn't close the modal -- used when finishing
   * the version-setup step still needs to hand off into the credential
   * step below, in the same modal session. */
  onDeviceUpdated: (updated: Device) => void;
}) {
  // Local copy of the device so switching SNMP version / finishing setup
  // updates what this modal renders immediately, without waiting for the
  // parent to re-render (and without the modal closing in between).
  const [localDevice, setLocalDevice] = useState<Device>(device);
  const isV3 = localDevice.snmp_version === "v3";

  const [community, setCommunity] = useState("");
  const [authKey, setAuthKey] = useState("");
  const [privKey, setPrivKey] = useState("");

  // Version/protocol setup -- shown automatically the first time SNMP is
  // configured for a device, and also reachable afterwards via "Change
  // SNMP version" so an admin can move a device from v2c to v3 (or just
  // change the port/security level) without deleting and re-adding it.
  const [changingVersion, setChangingVersion] = useState(!localDevice.snmp_version);
  const [setupVersion, setSetupVersion] = useState<"v1" | "v2c" | "v3">(
    (localDevice.snmp_version as "v1" | "v2c" | "v3") || "v2c"
  );
  const [setupPort, setSetupPort] = useState(String(localDevice.snmp_port || 161));
  const [setupSecurityLevel, setSetupSecurityLevel] = useState<"noAuthNoPriv" | "authNoPriv" | "authPriv">(
    (localDevice.snmp_security_level as "noAuthNoPriv" | "authNoPriv" | "authPriv") || "authPriv"
  );
  const [setupAuthProtocol, setSetupAuthProtocol] = useState<string>(localDevice.snmp_auth_protocol || "SHA");
  const [setupPrivProtocol, setSetupPrivProtocol] = useState<string>(localDevice.snmp_priv_protocol || "AES128");
  const [setupUsername, setSetupUsername] = useState(localDevice.snmp_username || "");
  const [settingUp, setSettingUp] = useState(false);
  const [setupError, setSetupError] = useState<string | null>(null);

  const startChangeVersion = () => {
    // Re-seed the form from whatever's currently configured, not stale
    // defaults from when the modal first opened.
    setSetupVersion((localDevice.snmp_version as "v1" | "v2c" | "v3") || "v2c");
    setSetupPort(String(localDevice.snmp_port || 161));
    setSetupSecurityLevel((localDevice.snmp_security_level as "noAuthNoPriv" | "authNoPriv" | "authPriv") || "authPriv");
    setSetupAuthProtocol(localDevice.snmp_auth_protocol || "SHA");
    setSetupPrivProtocol(localDevice.snmp_priv_protocol || "AES128");
    setSetupUsername(localDevice.snmp_username || "");
    setSetupError(null);
    setChangingVersion(true);
  };

  const setupSnmp = async () => {
    if (setupVersion === "v3" && !setupUsername.trim()) {
      setSetupError("SNMPv3 requires a username.");
      return;
    }
    setSettingUp(true);
    setSetupError(null);
    try {
      const body: Record<string, unknown> = {
        supports_snmp: true,
        snmp_version: setupVersion,
        snmp_port: setupPort ? Number(setupPort) : 161,
      };
      if (setupVersion === "v3") {
        body.snmp_username = setupUsername.trim();
        body.snmp_security_level = setupSecurityLevel;
        if (setupSecurityLevel !== "noAuthNoPriv") {
          body.snmp_auth_protocol = setupAuthProtocol;
        }
        if (setupSecurityLevel === "authPriv") {
          body.snmp_priv_protocol = setupPrivProtocol;
        }
      } else {
        // Switching away from v3 back to v1/v2c -- clear the now-irrelevant
        // v3 USM fields so a stale security level doesn't linger on the
        // device record and confuse the next person who opens this modal.
        body.snmp_username = null;
        body.snmp_security_level = null;
      }
      const res = await api.patch<Device>(`/devices/${device.id}`, body);
      setLocalDevice(res.data);
      onDeviceUpdated(res.data);
      // Version/protocol is set -- now every version (v1/v2c community,
      // or v3 auth/priv passphrases) needs actual secret material before
      // polling can work, so always continue straight into the
      // credentials step rather than closing here.
      setChangingVersion(false);
      setCommunity("");
      setAuthKey("");
      setPrivKey("");
    } catch (err: any) {
      setSetupError(err?.response?.data?.detail || "Failed to enable SNMP for this device.");
    } finally {
      setSettingUp(false);
    }
  };

  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [testResult, setTestResult] = useState<SnmpTestResult | null>(null);

  // "Test & Fix" -- diagnoses the "SNMP works but device still shows
  // offline" mismatch (see backend POST /connection/test-and-fix
  // docstring: the independent ping sweep doesn't know about SNMP at
  // all) and immediately re-syncs Device.status instead of waiting for
  // the next reachability sweep.
  const [fixing, setFixing] = useState(false);
  const [fixResult, setFixResult] = useState<ConnectionTestAndFixResult | null>(null);

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
      setLocalDevice(res.data);
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

  const testAndFix = async () => {
    setFixing(true);
    setError(null);
    setFixResult(null);
    try {
      const res = await api.post<ConnectionTestAndFixResult>(`/devices/${device.id}/connection/test-and-fix`);
      setFixResult(res.data);
      if (res.data.fix_applied) {
        setLocalDevice((prev) => ({ ...prev, status: res.data.status_after } as Device));
        onDeviceUpdated({ ...localDevice, status: res.data.status_after } as Device);
      }
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to run connection diagnostics.");
    } finally {
      setFixing(false);
    }
  };

  // v3 needs at least the auth key whenever the security level requires
  // auth (authNoPriv/authPriv), and the priv key too for authPriv --
  // "no creds entered yet" is only an acceptable Save state for
  // noAuthNoPriv, where there's genuinely nothing secret to store.
  const v3NeedsAuth = isV3 && (localDevice.snmp_security_level === "authNoPriv" || localDevice.snmp_security_level === "authPriv");
  const v3NeedsPriv = isV3 && localDevice.snmp_security_level === "authPriv";
  const saveDisabled =
    saving ||
    (!isV3 && !community) ||
    (isV3 && !localDevice.snmp_credentials_configured && ((v3NeedsAuth && !authKey) || (v3NeedsPriv && !privKey)));

  return (
    <div className="fixed inset-0 bg-navy dark:bg-slate-950/40 flex items-center justify-center z-50 px-4">
      <div className="bg-white dark:bg-slate-800 rounded-xl shadow-xl max-w-md w-full p-5">
        <div className="flex items-start justify-between">
          <div>
            <h3 className="font-semibold text-navy dark:text-white">SNMP Credentials — {localDevice.hostname}</h3>
            <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">
              {localDevice.snmp_version
                ? isV3
                  ? `SNMPv3 (${localDevice.snmp_security_level || "noAuthNoPriv"}) for user "${localDevice.snmp_username || "—"}"`
                  : `SNMP ${localDevice.snmp_version?.toUpperCase() || "v2c"} community string`
                : "SNMP isn't configured for this device yet."}
              {localDevice.snmp_version && ". Stored encrypted; never shown again after saving."}
            </p>
          </div>
          <button onClick={onClose} className="text-slate-400 dark:text-slate-500 hover:text-slate-600 dark:text-slate-300 text-lg leading-none">
            ✕
          </button>
        </div>

        {changingVersion && (
          <div className="mt-4 space-y-3">
            <p className="text-xs text-slate-500 dark:text-slate-400">
              {localDevice.snmp_version
                ? "Changing the version, port, or security level doesn't touch any secret already on file, but a mismatched security level (e.g. switching from noAuthNoPriv to authPriv) needs a fresh passphrase entered below before polling will work."
                : "Set the protocol version first, then save credentials."}
            </p>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">Version</label>
                <select
                  value={setupVersion}
                  onChange={(e) => setSetupVersion(e.target.value as "v1" | "v2c" | "v3")}
                  className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-2.5 py-2 text-sm bg-white dark:bg-slate-800"
                >
                  <option value="v2c">v2c</option>
                  <option value="v1">v1</option>
                  <option value="v3">v3</option>
                </select>
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">Port</label>
                <input
                  type="number"
                  className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-2.5 py-2 text-sm"
                  value={setupPort}
                  onChange={(e) => setSetupPort(e.target.value)}
                />
              </div>
            </div>
            {setupVersion === "v3" && (
              <>
                <div>
                  <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">Username</label>
                  <input
                    className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm"
                    value={setupUsername}
                    onChange={(e) => setSetupUsername(e.target.value)}
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">Security level</label>
                  <select
                    value={setupSecurityLevel}
                    onChange={(e) => setSetupSecurityLevel(e.target.value as "noAuthNoPriv" | "authNoPriv" | "authPriv")}
                    className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-2.5 py-2 text-sm bg-white dark:bg-slate-800"
                  >
                    <option value="authPriv">authPriv (auth + encryption)</option>
                    <option value="authNoPriv">authNoPriv (auth only)</option>
                    <option value="noAuthNoPriv">noAuthNoPriv (none)</option>
                  </select>
                </div>
                {setupSecurityLevel !== "noAuthNoPriv" && (
                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">Auth Protocol</label>
                      <select
                        value={setupAuthProtocol}
                        onChange={(e) => setSetupAuthProtocol(e.target.value)}
                        className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-2.5 py-2 text-sm bg-white dark:bg-slate-800"
                      >
                        {["MD5", "SHA", "SHA224", "SHA256", "SHA384", "SHA512"].map((p) => (
                          <option key={p} value={p}>{p}</option>
                        ))}
                      </select>
                    </div>
                    {setupSecurityLevel === "authPriv" && (
                      <div>
                        <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">Privacy Protocol</label>
                        <select
                          value={setupPrivProtocol}
                          onChange={(e) => setSetupPrivProtocol(e.target.value)}
                          className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-2.5 py-2 text-sm bg-white dark:bg-slate-800"
                        >
                          {["DES", "3DES", "AES128", "AES192", "AES256"].map((p) => (
                            <option key={p} value={p}>{p}</option>
                          ))}
                        </select>
                      </div>
                    )}
                  </div>
                )}
                <p className="text-[11px] text-slate-400 dark:text-slate-500 italic">
                  You'll be asked for the {setupSecurityLevel === "authPriv" ? "auth and privacy passphrases" : setupSecurityLevel === "authNoPriv" ? "auth passphrase" : "credentials"} next, since SNMPv3 needs {setupSecurityLevel === "noAuthNoPriv" ? "just the username" : "more than just a username"} to actually poll a device.
                </p>
              </>
            )}
            {setupError && <p className="text-riskcrit text-xs">{setupError}</p>}
            <div className="flex justify-end gap-2 pt-1">
              {localDevice.snmp_version && (
                <button
                  onClick={() => setChangingVersion(false)}
                  disabled={settingUp}
                  className="px-3 py-2 text-xs font-semibold text-slate-600 dark:text-slate-300 border border-slate-300 dark:border-slate-600 rounded-lg hover:bg-slate-50 dark:hover:bg-slate-700 disabled:opacity-50"
                >
                  Cancel
                </button>
              )}
              <button
                onClick={setupSnmp}
                disabled={settingUp}
                className="px-4 py-2 text-xs font-semibold text-white bg-brandblue rounded-lg hover:bg-navy dark:bg-slate-950 disabled:opacity-50"
              >
                {settingUp ? "Saving…" : localDevice.snmp_version ? "Save Version & Continue" : "Enable SNMP & Continue"}
              </button>
            </div>
          </div>
        )}

        {!changingVersion && localDevice.snmp_version && (
          <div className="mt-4 space-y-3">
            <div className="flex justify-end -mt-1">
              <button
                onClick={startChangeVersion}
                className="text-[11px] font-semibold text-brandblue hover:underline"
              >
                Change SNMP version
              </button>
            </div>

            {!isV3 ? (
              <div>
                <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">Community string</label>
                <input
                  type="password"
                  autoComplete="new-password"
                  className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm"
                  placeholder={localDevice.snmp_credentials_configured ? "•••••••• (leave blank to keep current)" : "e.g. public"}
                  value={community}
                  onChange={(e) => setCommunity(e.target.value)}
                />
              </div>
            ) : (
              <>
                {(localDevice.snmp_security_level === "authNoPriv" || localDevice.snmp_security_level === "authPriv") && (
                  <div>
                    <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">
                      Auth passphrase ({localDevice.snmp_auth_protocol || "SHA"})
                    </label>
                    <input
                      type="password"
                      autoComplete="new-password"
                      className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm"
                      placeholder={localDevice.snmp_credentials_configured ? "•••••••• (leave blank to keep current)" : "required"}
                      value={authKey}
                      onChange={(e) => setAuthKey(e.target.value)}
                    />
                  </div>
                )}
                {localDevice.snmp_security_level === "authPriv" && (
                  <div>
                    <label className="block text-xs font-medium text-slate-600 dark:text-slate-300 mb-1">
                      Privacy passphrase ({localDevice.snmp_priv_protocol || "AES128"})
                    </label>
                    <input
                      type="password"
                      autoComplete="new-password"
                      className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm"
                      placeholder={localDevice.snmp_credentials_configured ? "•••••••• (leave blank to keep current)" : "required"}
                      value={privKey}
                      onChange={(e) => setPrivKey(e.target.value)}
                    />
                  </div>
                )}
                {(!localDevice.snmp_security_level || localDevice.snmp_security_level === "noAuthNoPriv") && (
                  <p className="text-xs text-slate-400 dark:text-slate-500 italic">
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
                {testResult.sys_descr && <p className="mt-1 text-slate-500 dark:text-slate-400 truncate">{testResult.sys_descr}</p>}
                {testResult.sys_uptime_seconds != null && (
                  <p className="text-slate-400 dark:text-slate-500">{formatUptime(testResult.sys_uptime_seconds)}</p>
                )}
              </div>
            )}

            {fixResult && (
              <div
                className={`text-xs rounded-lg px-3 py-2 border ${
                  fixResult.overall_success ? "bg-green-50 border-green-200 text-risklow" : "bg-red-50 border-red-200 text-riskcrit"
                }`}
              >
                <p className="font-medium">
                  {fixResult.fix_applied
                    ? `✓ Fixed — status corrected from ${fixResult.status_before} to ${fixResult.status_after}`
                    : fixResult.overall_success
                    ? `Status is already accurate (${fixResult.status_after}) — no fix needed.`
                    : `✕ Still unreachable (${fixResult.status_after})`}
                </p>
                <ul className="mt-1.5 space-y-0.5">
                  {fixResult.steps.map((s) => (
                    <li key={s.name} className="flex items-start gap-1.5">
                      <span className={s.success ? "text-risklow" : "text-riskcrit"}>{s.success ? "✓" : "✕"}</span>
                      <span>
                        <span className="font-medium">{s.name}:</span> {s.detail}
                      </span>
                    </li>
                  ))}
                </ul>
                {fixResult.fix_detail && (
                  <p className="mt-1.5 text-slate-500 dark:text-slate-400 italic">{fixResult.fix_detail}</p>
                )}
              </div>
            )}

            <div className="flex gap-2 justify-end pt-2">
              <button
                onClick={testAndFix}
                disabled={fixing}
                title="Runs a full connection diagnostic (SNMP + TCP/ICMP reachability) and immediately corrects the device's status if it's stuck disagreeing with a working SNMP connection."
                className="px-3 py-2 text-xs font-semibold text-amber-700 border border-amber-200 bg-amber-50 rounded-lg hover:bg-amber-100 disabled:opacity-50"
              >
                {fixing ? "Diagnosing…" : "Test & Fix"}
              </button>
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
                disabled={saveDisabled}
                className="px-4 py-2 text-xs font-semibold text-white bg-brandblue rounded-lg hover:bg-navy dark:bg-slate-950 disabled:opacity-50"
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