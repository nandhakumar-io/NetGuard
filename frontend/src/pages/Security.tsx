import { useEffect, useState } from "react";
import { api, REFRESH_TOKEN_KEY } from "../lib/api";
import { useAuth } from "../lib/auth";

interface SessionInfo {
  id: string;
  created_at: string;
  expires_at: string;
  current: boolean;
}

interface RotationTableResult {
  table: string;
  column: string;
  rotated: number;
  skipped_null: number;
  failed: number;
  failed_ids: string[];
}

interface RotationResult {
  active_key_count: number;
  total_rotated: number;
  total_failed: number;
  tables: RotationTableResult[];
}

/** Settings page for enrolling in / disabling TOTP-based MFA (FR-1), and
 *  for viewing/revoking active login sessions (refresh tokens) across
 *  devices -- backs GET/DELETE /auth/sessions. */
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

  const [sessions, setSessions] = useState<SessionInfo[] | null>(null);
  const [sessionsError, setSessionsError] = useState<string | null>(null);
  const [revokingId, setRevokingId] = useState<string | null>(null);

  const canRotateSecrets = user?.role === "network_admin" || user?.role === "security";
  const [activeKeyCount, setActiveKeyCount] = useState<number | null>(null);
  const [rotating, setRotating] = useState(false);
  const [rotationResult, setRotationResult] = useState<RotationResult | null>(null);
  const [rotationError, setRotationError] = useState<string | null>(null);

  const loadRotationStatus = async () => {
    try {
      const res = await api.get<{ active_key_count: number }>("/security/secrets/rotation-status");
      setActiveKeyCount(res.data.active_key_count);
    } catch {
      /* non-critical -- rotation panel still usable without this hint */
    }
  };

  useEffect(() => {
    if (canRotateSecrets) loadRotationStatus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canRotateSecrets]);

  const rotateSecrets = async () => {
    const ok = window.confirm(
      "This re-encrypts every stored SSH password, SSH private key, SNMP credential, and stored " +
        "device config under the current encryption key. It runs immediately and cannot be undone. Continue?"
    );
    if (!ok) return;
    setRotating(true);
    setRotationError(null);
    setRotationResult(null);
    try {
      const res = await api.post<RotationResult>("/security/secrets/rotate");
      setRotationResult(res.data);
      loadRotationStatus();
    } catch (err: any) {
      setRotationError(err?.response?.data?.detail || "Secret rotation failed. Nothing was changed.");
    } finally {
      setRotating(false);
    }
  };

  const loadSessions = async () => {
    setSessionsError(null);
    try {
      const currentRefreshToken = localStorage.getItem(REFRESH_TOKEN_KEY);
      const res = await api.get<SessionInfo[]>("/auth/sessions", {
        params: currentRefreshToken ? { current_refresh_token: currentRefreshToken } : {},
      });
      // Current session first, then newest-first (API already orders
      // newest-first; current just gets pinned to the top).
      const sorted = [...res.data].sort((a, b) => Number(b.current) - Number(a.current));
      setSessions(sorted);
    } catch (err: any) {
      setSessionsError(err?.response?.data?.detail || "Could not load active sessions.");
    }
  };

  useEffect(() => {
    loadSessions();
  }, []);

  const revokeSession = async (id: string, isCurrent: boolean) => {
    if (isCurrent) {
      const ok = window.confirm(
        "This is your current session. Revoking it will sign you out immediately. Continue?"
      );
      if (!ok) return;
    }
    setRevokingId(id);
    setSessionsError(null);
    try {
      await api.delete(`/auth/sessions/${id}`);
      if (isCurrent) {
        // Own session was just killed server-side -- refresh will fail
        // from here on, so send the user back to Login rather than
        // leaving a dead session in place.
        localStorage.removeItem(REFRESH_TOKEN_KEY);
        window.location.href = "/login";
        return;
      }
      setSessions((prev) => (prev ? prev.filter((s) => s.id !== id) : prev));
    } catch (err: any) {
      setSessionsError(err?.response?.data?.detail || "Could not revoke that session.");
    } finally {
      setRevokingId(null);
    }
  };

  const formatDate = (iso: string) =>
    new Date(iso).toLocaleString(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    });

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

      <h2 className="text-lg font-bold text-navy mt-8 mb-1">Active Sessions</h2>
      <p className="text-sm text-slate-500 mb-4">
        Everywhere you're currently signed in. Revoke any session you don't recognize.
      </p>

      <div className="bg-white border border-slate-200 rounded-xl p-6">
        {sessionsError && <p className="text-riskcrit text-sm mb-3">{sessionsError}</p>}

        {sessions === null && !sessionsError && (
          <p className="text-sm text-slate-500">Loading sessions...</p>
        )}

        {sessions !== null && sessions.length === 0 && (
          <p className="text-sm text-slate-500">No active sessions found.</p>
        )}

        {sessions !== null && sessions.length > 0 && (
          <ul className="divide-y divide-slate-100">
            {sessions.map((s) => (
              <li key={s.id} className="flex items-center justify-between py-3 first:pt-0 last:pb-0">
                <div>
                  <p className="text-sm font-medium text-navy flex items-center gap-2">
                    Session {s.id.slice(0, 8)}
                    {s.current && (
                      <span className="text-[10px] uppercase tracking-wide font-semibold text-green-700 bg-green-50 border border-green-200 rounded-full px-2 py-0.5">
                        This device
                      </span>
                    )}
                  </p>
                  <p className="text-xs text-slate-500 mt-0.5">
                    Signed in {formatDate(s.created_at)} &middot; expires {formatDate(s.expires_at)}
                  </p>
                </div>
                <button
                  onClick={() => revokeSession(s.id, s.current)}
                  disabled={revokingId === s.id}
                  className="text-xs font-semibold text-riskcrit border border-riskcrit rounded-lg px-3 py-1.5 hover:bg-red-50 transition-colors disabled:opacity-50"
                >
                  {revokingId === s.id ? "Revoking..." : s.current ? "Sign out" : "Revoke"}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {canRotateSecrets && (
        <>
          <h2 className="text-lg font-bold text-navy mt-8 mb-1">Secrets Rotation</h2>
          <p className="text-sm text-slate-500 mb-4">
            Re-encrypts every stored SSH password, SSH private key, SNMP credential, and stored device
            config under the current primary encryption key. Add a new key ahead of the old one in
            config first, then run this to migrate existing rows to it.
          </p>

          <div className="bg-white border border-slate-200 rounded-xl p-6">
            {activeKeyCount !== null && (
              <p className="text-xs text-slate-500 mb-4">
                {activeKeyCount} encryption key{activeKeyCount === 1 ? "" : "s"} currently configured
                {activeKeyCount > 1 && (
                  <span className="text-amber-600"> — an old key is still active as a fallback</span>
                )}
                .
              </p>
            )}

            {rotationError && <p className="text-riskcrit text-sm mb-3">{rotationError}</p>}

            <button
              onClick={rotateSecrets}
              disabled={rotating}
              className="bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-navy transition-colors disabled:opacity-50"
            >
              {rotating ? "Rotating secrets..." : "Rotate all secrets now"}
            </button>

            {rotationResult && (
              <div className="mt-4 border-t border-slate-100 pt-4">
                <p className="text-sm font-medium text-navy">
                  Rotated {rotationResult.total_rotated} secret{rotationResult.total_rotated === 1 ? "" : "s"}
                  {rotationResult.total_failed > 0 && (
                    <span className="text-riskcrit"> — {rotationResult.total_failed} failed</span>
                  )}
                  .
                </p>
                <ul className="mt-2 space-y-1">
                  {rotationResult.tables.map((t) => (
                    <li key={`${t.table}.${t.column}`} className="text-xs text-slate-500">
                      <span className="font-mono">
                        {t.table}.{t.column}
                      </span>
                      : {t.rotated} rotated, {t.skipped_null} empty
                      {t.failed > 0 && (
                        <span className="text-riskcrit"> , {t.failed} failed (ids: {t.failed_ids.join(", ")})</span>
                      )}
                    </li>
                  ))}
                </ul>
                {rotationResult.total_failed > 0 && (
                  <p className="text-xs text-riskcrit mt-2">
                    Failed rows were left untouched (still on the old key) and need manual review before
                    that key can be retired from config.
                  </p>
                )}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}