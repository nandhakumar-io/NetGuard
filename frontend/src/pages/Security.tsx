import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useConfirm } from "../lib/confirm";

interface SessionInfo {
  id: string;
  created_at: string;
  expires_at: string;
  current: boolean;
  device: string | null;
  ip_address: string | null;
  location: string | null;
  user_id: string | null;
  user_email: string | null;
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
  const confirm = useConfirm();
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

  // Security PIN (step-up auth for terminal access + critical actions).
  // Separate error/message/busy state from MFA above so an error in one
  // card never clobbers or clears the other's.
  const [pinError, setPinError] = useState<string | null>(null);
  const [pinMessage, setPinMessage] = useState<string | null>(null);
  const [pinBusy, setPinBusy] = useState(false);
  const [showPinSetup, setShowPinSetup] = useState(false);
  const [pinPassword, setPinPassword] = useState("");
  const [newPin, setNewPin] = useState("");
  const [newPinConfirm, setNewPinConfirm] = useState("");
  const [disablePinPassword, setDisablePinPassword] = useState("");
  const [showDisablePin, setShowDisablePin] = useState(false);

  // Admins can additionally see and revoke *every* user's active
  // sessions, not just their own -- separate state/endpoint
  // (GET/DELETE /auth/sessions/all) so the default "my sessions" view
  // (and its "sign out of all other sessions" action, which only ever
  // means "my other sessions") stays unaffected by the toggle.
  const isAdmin = user?.role === "network_admin";
  const [showAllSessions, setShowAllSessions] = useState(false);
  const [allSessions, setAllSessions] = useState<SessionInfo[] | null>(null);
  const [allSessionsError, setAllSessionsError] = useState<string | null>(null);
  const [revokingAllSessionId, setRevokingAllSessionId] = useState<string | null>(null);

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
    const ok = await confirm(
      "This re-encrypts every stored SSH password, SSH private key, SNMP credential, and stored " +
        "device config under the current encryption key. It runs immediately and cannot be undone. Continue?",
      { confirmLabel: "Rotate secrets" }
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
      // No params needed -- the backend identifies the "current" session
      // from the httpOnly refresh cookie it already receives with this
      // request (see /auth/sessions).
      const res = await api.get<SessionInfo[]>("/auth/sessions");
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

  const loadAllSessions = async () => {
    setAllSessionsError(null);
    try {
      const res = await api.get<SessionInfo[]>("/auth/sessions/all");
      const sorted = [...res.data].sort((a, b) => Number(b.current) - Number(a.current));
      setAllSessions(sorted);
    } catch (err: any) {
      setAllSessionsError(err?.response?.data?.detail || "Could not load sessions.");
    }
  };

  useEffect(() => {
    if (isAdmin && showAllSessions && allSessions === null) loadAllSessions();
  }, [isAdmin, showAllSessions]);

  const revokeAnySession = async (id: string, isCurrent: boolean) => {
    if (isCurrent) {
      const ok = await confirm(
        "This is your current session. Revoking it will sign you out immediately. Continue?",
        { confirmLabel: "Sign out" }
      );
      if (!ok) return;
    }
    setRevokingAllSessionId(id);
    setAllSessionsError(null);
    try {
      await api.delete(`/auth/sessions/all/${id}`);
      if (isCurrent) {
        window.location.href = "/login";
        return;
      }
      setAllSessions((prev) => (prev ? prev.filter((s) => s.id !== id) : prev));
    } catch (err: any) {
      setAllSessionsError(err?.response?.data?.detail || "Could not revoke that session.");
    } finally {
      setRevokingAllSessionId(null);
    }
  };

  const revokeSession = async (id: string, isCurrent: boolean) => {
    if (isCurrent) {
      const ok = await confirm(
        "This is your current session. Revoking it will sign you out immediately. Continue?",
        { confirmLabel: "Sign out" }
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
        // leaving a dead session in place. The refresh cookie itself was
        // already revoked server-side by DELETE /auth/sessions/{id}.
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

  // Approximates "last active" -- refresh tokens rotate on every use (see
  // POST /auth/refresh), so `created_at` on the current row for a device
  // is effectively when that device last talked to the API.
  const timeAgo = (iso: string) => {
    const diff = Date.now() - new Date(iso).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    return `${Math.floor(hrs / 24)}d ago`;
  };

  const isMobileDevice = (device: string | null) =>
    !!device && /android|ios|iphone|ipad/i.test(device);

  const [revokingAll, setRevokingAll] = useState(false);
  const otherSessions = (sessions ?? []).filter((s) => !s.current);

  const revokeAllOthers = async () => {
    if (otherSessions.length === 0) return;
    const ok = await confirm(
      `Sign out of ${otherSessions.length} other session${otherSessions.length === 1 ? "" : "s"}? You'll stay signed in here.`,
      { confirmLabel: "Sign out of others" }
    );
    if (!ok) return;
    setRevokingAll(true);
    setSessionsError(null);
    try {
      await Promise.all(otherSessions.map((s) => api.delete(`/auth/sessions/${s.id}`)));
      setSessions((prev) => (prev ? prev.filter((s) => s.current) : prev));
    } catch (err: any) {
      setSessionsError(err?.response?.data?.detail || "Could not revoke all other sessions.");
      loadSessions();
    } finally {
      setRevokingAll(false);
    }
  };

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

  const setPin = async (e: React.FormEvent) => {
    e.preventDefault();
    setPinError(null);
    setPinMessage(null);
    if (newPin !== newPinConfirm) {
      setPinError("PINs don't match.");
      return;
    }
    setPinBusy(true);
    try {
      await api.post("/auth/security-pin", { password: pinPassword, pin: newPin });
      setPinPassword("");
      setNewPin("");
      setNewPinConfirm("");
      setShowPinSetup(false);
      setPinMessage("Security PIN saved. Turn on \"Require for terminal & critical actions\" below to start enforcing it.");
      await refreshMe();
    } catch (err: any) {
      setPinError(err?.response?.data?.detail || "Failed to set PIN.");
    } finally {
      setPinBusy(false);
    }
  };

  const togglePinRequired = async (nextValue: boolean) => {
    setPinError(null);
    setPinMessage(null);
    setPinBusy(true);
    try {
      await api.post("/auth/security-pin/require", { pin_required: nextValue });
      await refreshMe();
    } catch (err: any) {
      setPinError(err?.response?.data?.detail || "Failed to update PIN enforcement.");
    } finally {
      setPinBusy(false);
    }
  };

  const disablePin = async (e: React.FormEvent) => {
    e.preventDefault();
    setPinError(null);
    setPinMessage(null);
    setPinBusy(true);
    try {
      await api.delete("/auth/security-pin", { data: { password: disablePinPassword } });
      setDisablePinPassword("");
      setShowDisablePin(false);
      setPinMessage("Security PIN removed.");
      await refreshMe();
    } catch (err: any) {
      setPinError(err?.response?.data?.detail || "Incorrect password.");
    } finally {
      setPinBusy(false);
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

      <h2 className="text-lg font-bold text-navy mt-8 mb-1">Security PIN</h2>
      <p className="text-sm text-slate-500 mb-4">
        An extra numeric PIN, separate from your password, that can be required immediately before opening a
        device terminal or performing a critical action (device delete, config rollback). Optional -- nothing
        changes for you until you set a PIN and turn on enforcement below.
      </p>

      {pinError && <p className="text-riskcrit text-sm mb-4">{pinError}</p>}
      {pinMessage && <p className="text-sm mb-4 text-green-700">{pinMessage}</p>}

      <div className="bg-white border border-slate-200 rounded-xl p-6">
        <div className="flex items-center justify-between mb-4">
          <div>
            <p className="text-sm font-semibold text-navy">PIN status</p>
            <p className="text-xs text-slate-500 mt-0.5">
              {user?.pin_set ? (
                <span className="text-green-700 font-medium">PIN set</span>
              ) : (
                <span className="text-slate-500">No PIN set</span>
              )}
            </p>
          </div>
          <div className="flex items-center gap-2">
            {!showPinSetup && (
              <button
                onClick={() => setShowPinSetup(true)}
                disabled={pinBusy}
                className="bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-navy transition-colors disabled:opacity-50"
              >
                {user?.pin_set ? "Change PIN" : "Set PIN"}
              </button>
            )}
          </div>
        </div>

        {showPinSetup && (
          <form onSubmit={setPin} className="border-t border-slate-100 pt-4 space-y-3">
            <p className="text-xs text-slate-500">Enter your account password to confirm, then choose a 4-8 digit PIN.</p>
            <input
              className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
              type="password"
              placeholder="Account password"
              value={pinPassword}
              onChange={(e) => setPinPassword(e.target.value)}
              required
            />
            <input
              className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm tracking-widest text-center"
              inputMode="numeric"
              maxLength={8}
              placeholder="New PIN"
              value={newPin}
              onChange={(e) => setNewPin(e.target.value.replace(/\D/g, ""))}
              required
            />
            <input
              className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm tracking-widest text-center"
              inputMode="numeric"
              maxLength={8}
              placeholder="Confirm PIN"
              value={newPinConfirm}
              onChange={(e) => setNewPinConfirm(e.target.value.replace(/\D/g, ""))}
              required
            />
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => { setShowPinSetup(false); setPinPassword(""); setNewPin(""); setNewPinConfirm(""); }}
                className="w-full bg-white border border-slate-200 text-slate-500 rounded-lg px-4 py-2 text-sm font-semibold hover:bg-slate-50 transition-colors"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={pinBusy || newPin.length < 4 || newPinConfirm.length < 4}
                className="w-full bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-navy transition-colors disabled:opacity-50"
              >
                Save PIN
              </button>
            </div>
          </form>
        )}

        {user?.pin_set && !showPinSetup && (
          <div className="border-t border-slate-100 pt-4 mt-4 space-y-4">
            <label className="flex items-center justify-between cursor-pointer">
              <span className="text-sm text-navy">
                Require for terminal access &amp; critical actions
                <span className="block text-xs text-slate-400 mt-0.5">
                  Applies to opening a device terminal and to device delete / config rollback.
                </span>
              </span>
              <input
                type="checkbox"
                checked={!!user?.pin_required}
                disabled={pinBusy}
                onChange={(e) => togglePinRequired(e.target.checked)}
                className="w-4 h-4 shrink-0"
              />
            </label>

            {!showDisablePin ? (
              <button
                onClick={() => setShowDisablePin(true)}
                disabled={pinBusy}
                className="text-xs font-semibold text-riskcrit hover:underline"
              >
                Remove PIN
              </button>
            ) : (
              <form onSubmit={disablePin} className="space-y-3">
                <p className="text-xs text-slate-500">Enter your password to remove the PIN and turn off enforcement.</p>
                <input
                  className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
                  type="password"
                  placeholder="Password"
                  value={disablePinPassword}
                  onChange={(e) => setDisablePinPassword(e.target.value)}
                  required
                />
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => { setShowDisablePin(false); setDisablePinPassword(""); }}
                    className="w-full bg-white border border-slate-200 text-slate-500 rounded-lg px-4 py-2 text-sm font-semibold hover:bg-slate-50 transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={pinBusy}
                    className="w-full bg-white border border-riskcrit text-riskcrit rounded-lg px-4 py-2 text-sm font-semibold hover:bg-red-50 transition-colors disabled:opacity-50"
                  >
                    Remove PIN
                  </button>
                </div>
              </form>
            )}
          </div>
        )}
      </div>

      <h2 className="text-lg font-bold text-navy mt-8 mb-1">Active Sessions</h2>
      <p className="text-sm text-slate-500 mb-4">
        These are the devices and browsers currently signed in to your account. Revoke any you don't recognize.
      </p>

      <div className="bg-white border border-slate-200 rounded-xl p-6">
        {sessionsError && <p className="text-riskcrit text-sm mb-3">{sessionsError}</p>}

        {sessions === null && !sessionsError && (
          <p className="text-sm text-slate-500">Loading sessions...</p>
        )}

        {sessions !== null && sessions.length === 0 && (
          <p className="text-sm text-slate-500">No active sessions found.</p>
        )}

        {otherSessions.length > 0 && (
          <button
            onClick={revokeAllOthers}
            disabled={revokingAll}
            className="text-xs font-semibold text-riskcrit hover:underline disabled:opacity-50 mb-4"
          >
            {revokingAll ? "Signing out…" : `Sign out of all other sessions (${otherSessions.length})`}
          </button>
        )}

        {sessions !== null && sessions.length > 0 && (
          <ul className="divide-y divide-slate-100">
            {sessions.map((s) => (
              <li key={s.id} className="flex items-center justify-between gap-3 py-3.5 first:pt-0 last:pb-0">
                <div className="flex items-center gap-3 min-w-0">
                  <div className="w-9 h-9 rounded-lg bg-slate-50 border border-slate-200 flex items-center justify-center text-slate-400 shrink-0">
                    {isMobileDevice(s.device) ? (
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                        <rect x="7" y="2" width="10" height="20" rx="2" />
                        <path d="M11 18h2" strokeLinecap="round" />
                      </svg>
                    ) : (
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                        <rect x="2" y="4" width="20" height="13" rx="2" />
                        <path d="M8 21h8M12 17v4" strokeLinecap="round" />
                      </svg>
                    )}
                  </div>
                  <div className="min-w-0">
                    <p className="text-sm font-semibold text-navy flex items-center gap-2 flex-wrap">
                      {s.device || `Session ${s.id.slice(0, 8)}`}
                      {s.current && (
                        <span className="text-[10px] uppercase tracking-wide font-semibold text-green-700 bg-green-50 border border-green-200 rounded-full px-2 py-0.5">
                          This device
                        </span>
                      )}
                    </p>
                    <p className="text-xs text-slate-500 mt-0.5 truncate">
                      {[s.ip_address, s.location].filter(Boolean).join(" · ") || "IP unknown"}
                      {" · last active "}
                      {timeAgo(s.created_at)}
                    </p>
                    <p className="text-[11px] text-slate-400 mt-0.5">
                      Signed in {formatDate(s.created_at)} &middot; expires {formatDate(s.expires_at)}
                    </p>
                  </div>
                </div>
                <button
                  onClick={() => revokeSession(s.id, s.current)}
                  disabled={revokingId === s.id}
                  className="text-xs font-semibold text-riskcrit border border-riskcrit rounded-lg px-3 py-1.5 hover:bg-red-50 transition-colors disabled:opacity-50 shrink-0"
                >
                  {revokingId === s.id ? "Revoking..." : s.current ? "Sign out" : "Revoke"}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {isAdmin && (
        <>
          <div className="flex items-center justify-between mt-8 mb-1">
            <h2 className="text-lg font-bold text-navy">All User Sessions</h2>
            <button
              onClick={() => setShowAllSessions((v) => !v)}
              className="text-xs font-semibold text-brandblue hover:underline"
            >
              {showAllSessions ? "Hide" : "Show"}
            </button>
          </div>
          <p className="text-sm text-slate-500 mb-4">
            Every active session across every user -- for signing someone out remotely (lost device,
            offboarding, suspected compromise).
          </p>

          {showAllSessions && (
            <div className="bg-white border border-slate-200 rounded-xl p-6">
              {allSessionsError && <p className="text-riskcrit text-sm mb-3">{allSessionsError}</p>}

              {allSessions === null && !allSessionsError && (
                <p className="text-sm text-slate-500">Loading sessions...</p>
              )}

              {allSessions !== null && allSessions.length === 0 && (
                <p className="text-sm text-slate-500">No active sessions found.</p>
              )}

              {allSessions !== null && allSessions.length > 0 && (
                <ul className="divide-y divide-slate-100">
                  {allSessions.map((s) => (
                    <li key={s.id} className="flex items-center justify-between gap-3 py-3.5 first:pt-0 last:pb-0">
                      <div className="min-w-0">
                        <p className="text-sm font-semibold text-navy flex items-center gap-2 flex-wrap">
                          {s.user_email || "Unknown user"}
                          {s.current && (
                            <span className="text-[10px] uppercase tracking-wide font-semibold text-green-700 bg-green-50 border border-green-200 rounded-full px-2 py-0.5">
                              This device
                            </span>
                          )}
                        </p>
                        <p className="text-xs text-slate-500 mt-0.5 truncate">
                          {s.device || `Session ${s.id.slice(0, 8)}`}
                          {" · "}
                          {[s.ip_address, s.location].filter(Boolean).join(" · ") || "IP unknown"}
                          {" · last active "}
                          {timeAgo(s.created_at)}
                        </p>
                        <p className="text-[11px] text-slate-400 mt-0.5">
                          Signed in {formatDate(s.created_at)} &middot; expires {formatDate(s.expires_at)}
                        </p>
                      </div>
                      <button
                        onClick={() => revokeAnySession(s.id, s.current)}
                        disabled={revokingAllSessionId === s.id}
                        className="text-xs font-semibold text-riskcrit border border-riskcrit rounded-lg px-3 py-1.5 hover:bg-red-50 transition-colors disabled:opacity-50 shrink-0"
                      >
                        {revokingAllSessionId === s.id ? "Revoking..." : s.current ? "Sign out" : "Revoke"}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </>
      )}

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