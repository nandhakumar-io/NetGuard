import { useState, useEffect } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useAuth, UserRole } from "../lib/auth";
import { api } from "../lib/api";

// Network Administrator and Security are privileged roles and are
// deliberately excluded from self-registration -- the backend now
// downgrades either one to network_engineer if sent anyway (see
// UserCreate.sanitized_role in app/schemas/auth.py). Granting those roles
// requires an existing admin to call PATCH /auth/users/{id}/role.
const ROLES: { value: UserRole; label: string }[] = [
  { value: "network_engineer", label: "Network Engineer" },
  { value: "noc_engineer", label: "NOC Engineer" },
  { value: "auditor", label: "Enterprise Auditor" },
];

function BrandMark() {
  return (
    <div className="w-11 h-11 rounded-xl bg-brandblue flex items-center justify-center shrink-0">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2">
        <path d="M12 2l8 4v6c0 5-3.4 8.4-8 10-4.6-1.6-8-5-8-10V6l8-4z" />
        <path d="M9 12l2 2 4-4" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </div>
  );
}

function GoogleIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24">
      <path fill="#4285F4" d="M23.52 12.27c0-.85-.08-1.66-.22-2.45H12v4.64h6.47c-.28 1.5-1.13 2.77-2.4 3.62v3h3.88c2.27-2.09 3.57-5.17 3.57-8.81z" />
      <path fill="#34A853" d="M12 24c3.24 0 5.96-1.07 7.95-2.92l-3.88-3c-1.08.72-2.45 1.15-4.07 1.15-3.13 0-5.78-2.11-6.73-4.95H1.27v3.1C3.25 21.3 7.31 24 12 24z" />
      <path fill="#FBBC05" d="M5.27 14.28A7.2 7.2 0 0 1 4.9 12c0-.79.14-1.56.37-2.28v-3.1H1.27A11.98 11.98 0 0 0 0 12c0 1.94.46 3.77 1.27 5.38l4-3.1z" />
      <path fill="#EA4335" d="M12 4.77c1.77 0 3.35.61 4.6 1.8l3.44-3.44C17.95 1.19 15.24 0 12 0 7.31 0 3.25 2.7 1.27 6.62l4 3.1C6.22 6.88 8.87 4.77 12 4.77z" />
    </svg>
  );
}

function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <label className="block text-[13px] font-semibold text-slate-600 mb-1.5 tracking-wide">
      {children}
    </label>
  );
}

const inputClass =
  "w-full bg-slate-50 border border-slate-200 rounded-lg px-3.5 py-2.5 text-sm text-slate-900 placeholder:text-slate-400 placeholder:font-normal outline-none transition-colors focus:border-brandblue focus:ring-2 focus:ring-brandblue/20 focus:bg-white";

export default function Login() {
  const { login, verifyMfa, register, loginWithGoogle, loginWithKeycloak } = useAuth();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [role, setRole] = useState<UserRole>("network_engineer");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [googleEnabled, setGoogleEnabled] = useState(false);
  const [keycloakEnabled, setKeycloakEnabled] = useState(false);
  const [tenants, setTenants] = useState<{ id: string; name: string }[]>([]);
  const [tenantId, setTenantId] = useState("");
  // Set once POST /auth/register succeeds -- registration no longer logs
  // the user in immediately (see app.api.auth.register), so this is
  // shown instead of navigating away.
  const [pendingMessage, setPendingMessage] = useState<string | null>(null);

  // Surfaces a friendly message if we just bounced back from a failed
  // GET /sso/google/callback (see app/api/sso.py's _login_error_redirect).
  useEffect(() => {
    const ssoError = searchParams.get("sso_error");
    if (ssoError) {
      const messages: Record<string, string> = {
        sso_not_configured: "Sign-in is not set up for this deployment.",
        invalid_or_expired_state: "That sign-in link expired. Please try again.",
        google_login_failed: "Sign-in failed. Please try again.",
        account_disabled: "This account has been disabled. Contact your administrator.",
        access_denied: "Sign-in was cancelled.",
      };
      setError(messages[ssoError] || "Sign-in failed. Please try again.");
    }
  }, [searchParams]);

  // Only show the button if the backend actually has Google OAuth
  // credentials configured -- avoids a dead button in deployments that
  // haven't set GOOGLE_CLIENT_ID/SECRET/REDIRECT_URI yet.
  useEffect(() => {
    api
      .get("/sso/providers")
      .then((res) => {
        setGoogleEnabled(!!res.data?.google);
        setKeycloakEnabled(!!res.data?.keycloak);
      })
      .catch(() => {
        setGoogleEnabled(false);
        setKeycloakEnabled(false);
      });
  }, []);

  // Fetch active tenant list for the register dropdown.
  // Gracefully falls back to an empty list if the user is not logged in yet
  // or the endpoint returns nothing (e.g. no tenants created yet).
  useEffect(() => {
    api
      .get("/tenants/public-list")
      .then((res) => setTenants(res.data ?? []))
      .catch(() => setTenants([]));
  }, []);

  // MFA challenge step: set once /auth/login responds with mfa_required.
  const [mfaToken, setMfaToken] = useState<string | null>(null);
  const [mfaCode, setMfaCode] = useState("");

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setPendingMessage(null);
    setLoading(true);
    try {
      if (mode === "login") {
        const outcome = await login(email, password);
        if (outcome.mfaRequired) {
          setMfaToken(outcome.mfaToken);
          setLoading(false);
          return;
        }
        navigate("/");
      } else {
        const message = await register(email, fullName, password, role, tenantId || undefined);
        // Stay on this screen and drop back into login mode with the
        // fields cleared -- there's no session yet, the new account
        // needs an admin to approve it first.
        setPendingMessage(message);
        setMode("login");
        setPassword("");
        setFullName("");
      }
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Authentication failed.");
    } finally {
      setLoading(false);
    }
  };

  const submitMfa = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!mfaToken) return;
    setError(null);
    setLoading(true);
    try {
      await verifyMfa(mfaToken, mfaCode);
      navigate("/");
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Invalid code. Please try again.");
    } finally {
      setLoading(false);
    }
  };

  const passwordScore = (() => {
    if (!password) return 0;
    let s = 0;
    if (password.length >= 8) s++;
    if (password.length >= 12) s++;
    if (/[A-Z]/.test(password) && /[0-9]/.test(password)) s++;
    if (/[^A-Za-z0-9]/.test(password)) s++;
    return Math.min(s, 4);
  })();
  const strengthLabel = ["Too short", "Weak", "Fair", "Good", "Strong"][passwordScore];
  const strengthColor = ["#EF4444", "#EF4444", "#F59E0B", "#10B981", "#1565C0"][passwordScore];

  return (
    <div className="w-full min-h-screen flex items-center justify-center bg-slate-100 px-4 py-10">
      <div className="w-full max-w-sm bg-white rounded-2xl shadow-xl border border-slate-200 p-8">
        {/* Brand header */}
        <div className="flex flex-col items-center text-center mb-6">
          <BrandMark />
          <p className="text-xl font-bold text-slate-900 mt-3 tracking-tight">NetGuard</p>
          <p className="text-xs font-medium text-slate-400 mt-0.5">Network Ops Console</p>
        </div>

        {mfaToken ? (
          <>
            <h1 className="text-lg font-bold text-brandblue text-center mb-1">Two-Factor Verification</h1>
            <p className="text-sm text-slate-500 text-center mb-6">
              Enter the 6-digit code from your authenticator app.
            </p>
            <form onSubmit={submitMfa} className="space-y-4">
              <input
                className={`${inputClass} text-center text-xl tracking-[0.5em]`}
                inputMode="numeric"
                pattern="[0-9]*"
                maxLength={6}
                placeholder="000000"
                value={mfaCode}
                onChange={(e) => setMfaCode(e.target.value.replace(/\D/g, ""))}
                autoFocus
                required
              />
              {error && (
                <p className="text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg px-3.5 py-2.5">
                  {error}
                </p>
              )}
              <button
                type="submit"
                disabled={loading || mfaCode.length < 6}
                className="w-full bg-brandblue text-white rounded-lg px-4 py-3 text-[15px] font-semibold hover:bg-blue-700 transition disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {loading ? "Verifying…" : "Verify & Continue"}
              </button>
              <button
                type="button"
                className="w-full text-sm text-slate-500 hover:text-slate-800 transition-colors"
                onClick={() => {
                  setMfaToken(null);
                  setMfaCode("");
                  setError(null);
                }}
              >
                ← Back to sign in
              </button>
            </form>
          </>
        ) : (
          <>
            <h1 className="text-xl font-bold text-brandblue text-center mb-6">
              {mode === "login" ? "Hi, Welcome Back!" : "Create Your Account"}
            </h1>

            {pendingMessage && (
              <p className="text-sm text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-lg px-3.5 py-2.5 mb-4">
                {pendingMessage}
              </p>
            )}

            <div className="flex mb-6 rounded-lg bg-slate-100 p-1 text-sm font-medium">
              <button
                type="button"
                className={`flex-1 py-1.5 rounded-md transition-colors ${
                  mode === "login" ? "bg-brandblue text-white shadow-sm" : "text-slate-500 hover:text-slate-800"
                }`}
                onClick={() => {
                  setMode("login");
                  setError(null);
                }}
              >
                Sign In
              </button>
              <button
                type="button"
                className={`flex-1 py-1.5 rounded-md transition-colors ${
                  mode === "register" ? "bg-brandblue text-white shadow-sm" : "text-slate-500 hover:text-slate-800"
                }`}
                onClick={() => {
                  setMode("register");
                  setError(null);
                }}
              >
                Register
              </button>
            </div>

            <form onSubmit={submit} className="space-y-4">
              {mode === "register" && (
                <div>
                  <FieldLabel>Full Name</FieldLabel>
                  <input
                    className={inputClass}
                    value={fullName}
                    onChange={(e) => setFullName(e.target.value)}
                    required
                  />
                </div>
              )}

              <div>
                <FieldLabel>Email</FieldLabel>
                <input
                  className={inputClass}
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  autoComplete="username"
                  required
                />
              </div>
              <div>
                <FieldLabel>Password</FieldLabel>
                <div className="relative">
                  <input
                    className={`${inputClass} pr-14`}
                    type={showPassword ? "text" : "password"}
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    autoComplete={mode === "login" ? "current-password" : "new-password"}
                    required
                    minLength={8}
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword((s) => !s)}
                    className="absolute right-3 top-1/2 -translate-y-1/2 text-xs font-medium text-slate-400 hover:text-brandblue transition-colors"
                    tabIndex={-1}
                  >
                    {showPassword ? "Hide" : "Show"}
                  </button>
                </div>
                {mode === "register" && password.length > 0 && (
                  <div className="mt-2">
                    <div className="h-1 w-full bg-slate-100 rounded-full overflow-hidden flex gap-0.5">
                      {[0, 1, 2, 3].map((i) => (
                        <div
                          key={i}
                          className="h-full flex-1 rounded-full transition-colors"
                          style={{
                            backgroundColor: i < passwordScore ? strengthColor : "#E2E8F0",
                          }}
                        />
                      ))}
                    </div>
                    <p className="text-xs mt-1" style={{ color: strengthColor }}>
                      {strengthLabel}
                    </p>
                  </div>
                )}
                {mode === "login" && (
                  <div className="text-right mt-1.5">
                    <button
                      type="button"
                      className="text-xs text-slate-500 hover:text-brandblue transition-colors"
                      onClick={() => setError("Ask a Network Administrator to reset your password from Access Control.")}
                    >
                      Forgot password?
                    </button>
                  </div>
                )}
              </div>
              {mode === "register" && (
                <div>
                  <FieldLabel>Requested Role</FieldLabel>
                  <select
                    className={inputClass}
                    value={role}
                    onChange={(e) => setRole(e.target.value as UserRole)}
                  >
                    {ROLES.map((r) => (
                      <option key={r.value} value={r.value}>
                        {r.label}
                      </option>
                    ))}
                  </select>
                </div>
              )}
              {mode === "register" && tenants.length > 0 && (
                <div>
                  <FieldLabel>Tenant</FieldLabel>
                  <select
                    className={inputClass}
                    value={tenantId}
                    onChange={(e) => setTenantId(e.target.value)}
                  >
                    <option value="">Select a tenant…</option>
                    {tenants.map((t) => (
                      <option key={t.id} value={t.id}>{t.name}</option>
                    ))}
                  </select>
                </div>
              )}
              {error && (
                <p className="text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg px-3.5 py-2.5">
                  {error}
                </p>
              )}
              <button
                type="submit"
                disabled={loading}
                className="w-full bg-brandblue text-white rounded-lg px-4 py-3 text-[15px] font-semibold hover:bg-blue-700 transition disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {loading ? "Please wait…" : mode === "login" ? "Login" : "Create Account"}
              </button>
            </form>

            {mode === "login" && (googleEnabled || keycloakEnabled) && (
              <>
                <div className="flex items-center gap-3 my-5">
                  <div className="h-px bg-slate-200 flex-1" />
                  <span className="text-xs text-slate-400 font-medium">OR</span>
                  <div className="h-px bg-slate-200 flex-1" />
                </div>
                <div className="space-y-3">
                  {keycloakEnabled && (
                    <button
                      type="button"
                      onClick={loginWithKeycloak}
                      className="w-full flex items-center justify-center gap-2.5 bg-slate-800 text-white rounded-lg px-4 py-3 text-[15px] font-semibold hover:bg-slate-900 transition"
                    >
                      Sign In with SSO
                    </button>
                  )}
                  {googleEnabled && (
                    <button
                      type="button"
                      onClick={loginWithGoogle}
                      className="w-full flex items-center justify-center gap-2.5 border border-slate-200 rounded-lg px-4 py-3 text-[15px] font-semibold text-slate-700 hover:bg-slate-50 transition"
                    >
                      <GoogleIcon />
                      Sign in with Google
                    </button>
                  )}
                </div>
              </>
            )}

            <p className="text-xs text-slate-400 mt-5 text-center leading-relaxed">
              Approving change requests requires a Network Administrator role.
              Admin and Security accounts are granted by an existing admin, not self-selected at sign-up.
            </p>
          </>
        )}
      </div>
    </div>
  );
}