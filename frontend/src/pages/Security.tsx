import { useState } from "react";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";

/** Settings page for enrolling in / disabling TOTP-based MFA (FR-1). */
export default function Security() {
  const { user, refreshMe } = useAuth();
  const [otpauthUri, setOtpauthUri] = useState<string | null>(null);
  const [secret, setSecret] = useState<string | null>(null);
  const [enableCode, setEnableCode] = useState("");
  const [disablePassword, setDisablePassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  const copySecret = async () => {
    if (!secret) return;
    try {
      await navigator.clipboard.writeText(secret);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard unavailable — the visible text is still selectable */
    }
  };

  const qrSrc = otpauthUri
    ? `https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=${encodeURIComponent(otpauthUri)}`
    : null;

  const startSetup = async () => {
    setError(null);
    setMessage(null);
    setBusy(true);
    try {
      const res = await api.post("/auth/mfa/setup");
      setSecret(res.data.secret);
      setOtpauthUri(res.data.otpauth_uri);
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Could not start MFA setup.");
    } finally {
      setBusy(false);
    }
  };

  const confirmEnable = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setMessage(null);
    setBusy(true);
    try {
      await api.post("/auth/mfa/enable", { code: enableCode });
      setOtpauthUri(null);
      setSecret(null);
      setEnableCode("");
      setMessage("Two-factor authentication is now enabled on your account.");
      await refreshMe();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Invalid code. Please try again.");
    } finally {
      setBusy(false);
    }
  };

  const disable = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setMessage(null);
    setBusy(true);
    try {
      await api.post("/auth/mfa/disable", { password: disablePassword });
      setDisablePassword("");
      setMessage("Two-factor authentication has been disabled.");
      await refreshMe();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Incorrect password.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="max-w-xl">
      <h1 className="text-xl font-bold text-navy mb-1">Security</h1>
      <p className="text-sm text-slate-500 mb-6">Manage two-factor authentication for your account.</p>

      {error && <p className="text-riskcrit text-sm mb-4">{error}</p>}
      {message && <p className="text-sm mb-4 text-green-700">{message}</p>}

      <div className="bg-white border border-slate-200 rounded-xl p-6">
        <div className="flex items-center justify-between mb-4">
          <div>
            <p className="text-sm font-semibold text-navy">Authenticator app (TOTP)</p>
            <p className="text-xs text-slate-500 mt-0.5">
              Status: <span className={user?.mfa_enabled ? "text-green-700 font-medium" : "text-slate-500"}>
                {user?.mfa_enabled ? "Enabled" : "Disabled"}
              </span>
            </p>
          </div>
          {!user?.mfa_enabled && !otpauthUri && (
            <button
              onClick={startSetup}
              disabled={busy}
              className="bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-navy transition-colors disabled:opacity-50"
            >
              Enable MFA
            </button>
          )}
        </div>

        {otpauthUri && (
          <form onSubmit={confirmEnable} className="border-t border-slate-100 pt-4 space-y-3">
            <p className="text-xs text-slate-500">
              Scan this QR code with your authenticator app (Google Authenticator, Authy, 1Password, etc), then enter
              the 6-digit code it generates to confirm.
            </p>
            {qrSrc && <img src={qrSrc} alt="MFA QR code" className="mx-auto" width={200} height={200} />}
            {secret && (
              <p className="text-[11px] text-slate-400 text-center break-all">
                Can't scan? Enter this key manually: <span className="font-mono">{secret}</span>
                <button
                  type="button"
                  onClick={copySecret}
                  className="ml-2 text-brandblue font-medium hover:text-navy"
                >
                  {copied ? "Copied!" : "Copy"}
                </button>
              </p>
            )}
            <input
              className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm tracking-widest text-center"
              inputMode="numeric"
              maxLength={6}
              placeholder="000000"
              value={enableCode}
              onChange={(e) => setEnableCode(e.target.value.replace(/\D/g, ""))}
              required
            />
            <button
              type="submit"
              disabled={busy || enableCode.length < 6}
              className="w-full bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-navy transition-colors disabled:opacity-50"
            >
              Confirm & Enable
            </button>
          </form>
        )}

        {user?.mfa_enabled && (
          <form onSubmit={disable} className="border-t border-slate-100 pt-4 space-y-3">
            <p className="text-xs text-slate-500">Enter your password to disable two-factor authentication.</p>
            <input
              className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
              type="password"
              placeholder="Password"
              value={disablePassword}
              onChange={(e) => setDisablePassword(e.target.value)}
              required
            />
            <button
              type="submit"
              disabled={busy}
              className="w-full bg-white border border-riskcrit text-riskcrit rounded-lg px-4 py-2 text-sm font-semibold hover:bg-red-50 transition-colors disabled:opacity-50"
            >
              Disable MFA
            </button>
          </form>
        )}
      </div>
    </div>
  );
}