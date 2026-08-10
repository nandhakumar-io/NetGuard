import { useState } from "react";
import { api } from "../lib/api";

interface BulkDeviceActionResult {
  action: string;
  affected_device_ids: string[];
  failed: Record<string, string>;
  detail: string | null;
  change_request_id: string | null;
}

/** Bulk credential rotation for the Devices multi-select toolbar --
 * sets the same new SSH and/or SNMP secret(s) on every selected device
 * in one call to POST /devices/bulk (rotate_credentials), reusing the
 * same Fernet-encrypted-at-rest storage as the single-device
 * ssh-credentials/snmp-credentials endpoints. Only fields the operator
 * actually fills in are rotated -- leaving a field blank leaves that
 * credential untouched on every device, it's not sent as an empty-string
 * clear (matches the "omit = leave unchanged" convention on the
 * single-device credential endpoints). */
export default function BulkRotateCredentialsModal({
  deviceIds,
  deviceCount,
  onClose,
  onDone,
}: {
  deviceIds: string[];
  deviceCount: number;
  onClose: () => void;
  onDone: (result: BulkDeviceActionResult) => void;
}) {
  const [sshUsername, setSshUsername] = useState("");
  const [sshPassword, setSshPassword] = useState("");
  const [snmpCommunity, setSnmpCommunity] = useState("");
  const [snmpV3Auth, setSnmpV3Auth] = useState("");
  const [snmpV3Priv, setSnmpV3Priv] = useState("");

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmStep, setConfirmStep] = useState(false);

  const hasAnyField =
    sshUsername.trim() || sshPassword || snmpCommunity || snmpV3Auth || snmpV3Priv;

  const fieldsSummary = [
    sshUsername.trim() && "SSH username",
    sshPassword && "SSH password",
    snmpCommunity && "SNMP community",
    snmpV3Auth && "SNMPv3 auth key",
    snmpV3Priv && "SNMPv3 priv key",
  ].filter(Boolean) as string[];

  const submit = async () => {
    if (!hasAnyField || deviceIds.length === 0) return;
    setSubmitting(true);
    setError(null);
    try {
      const params: Record<string, string> = {};
      if (sshUsername.trim()) params.ssh_username = sshUsername.trim();
      if (sshPassword) params.ssh_password = sshPassword;
      if (snmpCommunity) params.snmp_community = snmpCommunity;
      if (snmpV3Auth) params.snmp_v3_auth_key = snmpV3Auth;
      if (snmpV3Priv) params.snmp_v3_priv_key = snmpV3Priv;

      const res = await api.post<BulkDeviceActionResult>("/devices/bulk", {
        device_ids: deviceIds,
        action: "rotate_credentials",
        params,
      });
      onDone(res.data);
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to rotate credentials.");
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-navy dark:bg-slate-950/40 flex items-center justify-center z-50 px-4">
      <div className="bg-white dark:bg-slate-800 rounded-xl shadow-xl max-w-md w-full p-5">
        <div className="flex items-start justify-between">
          <div>
            <h3 className="font-semibold text-navy dark:text-white">
              Rotate Credentials — {deviceCount} device{deviceCount === 1 ? "" : "s"}
            </h3>
            <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">
              Sets the same new secret(s) on every selected device. Leave a field blank to leave
              that credential untouched. Values are encrypted at rest and never shown again.
            </p>
          </div>
          <button
            onClick={onClose}
            className="text-slate-400 dark:text-slate-500 hover:text-slate-600 dark:text-slate-300 text-lg leading-none"
          >
            ✕
          </button>
        </div>

        {!confirmStep ? (
          <div className="mt-4 space-y-4">
            <div className="space-y-2">
              <p className="text-[11px] font-bold uppercase tracking-wide text-slate-400">SSH</p>
              <input
                type="text"
                autoComplete="off"
                className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm"
                placeholder="Username (leave blank to keep current)"
                value={sshUsername}
                onChange={(e) => setSshUsername(e.target.value)}
              />
              <input
                type="password"
                autoComplete="new-password"
                className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm"
                placeholder="New password (leave blank to keep current)"
                value={sshPassword}
                onChange={(e) => setSshPassword(e.target.value)}
              />
            </div>

            <div className="space-y-2">
              <p className="text-[11px] font-bold uppercase tracking-wide text-slate-400">SNMP</p>
              <input
                type="password"
                autoComplete="off"
                className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm"
                placeholder="v1/v2c community string"
                value={snmpCommunity}
                onChange={(e) => setSnmpCommunity(e.target.value)}
              />
              <input
                type="password"
                autoComplete="off"
                className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm"
                placeholder="v3 auth passphrase"
                value={snmpV3Auth}
                onChange={(e) => setSnmpV3Auth(e.target.value)}
              />
              <input
                type="password"
                autoComplete="off"
                className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm"
                placeholder="v3 privacy passphrase"
                value={snmpV3Priv}
                onChange={(e) => setSnmpV3Priv(e.target.value)}
              />
            </div>

            {error && <p className="text-riskcrit text-xs">{error}</p>}

            <div className="flex gap-2 justify-end pt-1">
              <button
                onClick={onClose}
                className="px-3 py-2 text-xs font-semibold text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200"
              >
                Cancel
              </button>
              <button
                onClick={() => setConfirmStep(true)}
                disabled={!hasAnyField}
                className="px-4 py-2 text-xs font-semibold text-white bg-brandblue rounded-lg hover:bg-navy dark:bg-slate-950 disabled:opacity-50"
              >
                Continue
              </button>
            </div>
          </div>
        ) : (
          <div className="mt-4 space-y-4">
            <div className="text-xs bg-amber-50 dark:bg-amber-950/40 border border-amber-200 dark:border-amber-800 text-amber-800 dark:text-amber-300 rounded-lg px-3 py-2.5">
              <p className="font-semibold">
                This will overwrite {fieldsSummary.join(", ")} on {deviceCount} device
                {deviceCount === 1 ? "" : "s"}.
              </p>
              <p className="mt-1">This cannot be undone. Confirm the values above are correct.</p>
            </div>

            {error && <p className="text-riskcrit text-xs">{error}</p>}

            <div className="flex gap-2 justify-end pt-1">
              <button
                onClick={() => setConfirmStep(false)}
                disabled={submitting}
                className="px-3 py-2 text-xs font-semibold text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200 disabled:opacity-50"
              >
                Back
              </button>
              <button
                onClick={submit}
                disabled={submitting}
                className="px-4 py-2 text-xs font-semibold text-white bg-riskcrit rounded-lg hover:bg-red-700 disabled:opacity-50"
              >
                {submitting ? "Rotating…" : `Rotate on ${deviceCount} device${deviceCount === 1 ? "" : "s"}`}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}