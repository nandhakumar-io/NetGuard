import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth, UserRole } from "../lib/auth";

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

function ShieldMark() {
  return (
    <div className="relative w-11 h-11 flex items-center justify-center shrink-0">
      <svg width="44" height="44" viewBox="0 0 24 24" fill="none" className="absolute inset-0">
        <path
          d="M12 2l8 4v6c0 5-3.4 8.4-8 10-4.6-1.6-8-5-8-10V6l8-4z"
          fill="#0E131C"
          stroke="#22D3EE"
          strokeWidth="1.4"
        />
      </svg>
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#22D3EE" strokeWidth="2" className="relative">
        <path d="M9 11l3 3L22 4" />
        <path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11" />
      </svg>
    </div>
  );
}

function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <label className="noc-label block text-[10px] text-slate-500 dark:text-noc-muted uppercase mb-1.5">
      {children}
    </label>
  );
}

const inputClass =
  "w-full bg-slate-50 dark:bg-noc-panel2 border border-slate-200 dark:border-noc-border rounded-md px-3 py-2.5 text-sm text-slate-900 dark:text-noc-text placeholder:text-slate-400 dark:text-noc-faint outline-none transition-colors focus:border-blue-600 dark:border-noc-cyan focus:ring-1 focus:ring-blue-600/40 dark:ring-noc-cyan/40";

// Brand-side highlights, shown on the wide-screen split layout only. Purely
// presentational -- reinforces what NetGuard actually does while the person
// authenticates instead of leaving that side of the screen empty.
const HIGHLIGHTS: { title: string; body: string; icon: JSX.Element }[] = [
  {
    title: "Approval-gated change control",
    body: "Every config push runs through review, diff preview, and rollback safety nets.",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path d="M9 11l3 3L22 4" /><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11" />
      </svg>
    ),
  },
  {
    title: "Live fleet visibility",
    body: "Topology, syslog, drift, and traffic telemetry in one console, refreshed in real time.",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <circle cx="12" cy="5" r="2" /><circle cx="5" cy="19" r="2" /><circle cx="19" cy="19" r="2" />
        <path d="M12 7v6M12 13l-5.5 4M12 13l5.5 4" />
      </svg>
    ),
  },
  {
    title: "ChatOps + GitOps native",
    body: "Approve from Slack, sync templates from Git — the console stays the source of truth.",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path d="M8 9h8M8 13h5" /><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" />
      </svg>
    ),
  },
];

export default function Login() {
  const { login, verifyMfa, register } = useAuth();
  const navigate = useNavigate();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [role, setRole] = useState<UserRole>("network_engineer");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // MFA challenge step: set once /auth/login responds with mfa_required.
  const [mfaToken, setMfaToken] = useState<string | null>(null);
  const [mfaCode, setMfaCode] = useState("");

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      if (mode === "login") {
        const outcome = await login(email, password);
        if (outcome.mfaRequired) {
          setMfaToken(outcome.mfaToken);
          setLoading(false);
          return;
        }
      } else {
        await register(email, fullName, password, role);
      }
      navigate("/");
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
  const strengthColor = ["#F87171", "#F87171", "#FBBF24", "#34D399", "#22D3EE"][passwordScore];

  return (
    <div className="noc-root w-full flex items-stretch relative overflow-hidden" style={{ minHeight: "100vh", backgroundColor: "#080B10" }}>
      {/* faint scanning grid backdrop, consistent with the console identity used across the app */}
      <div
        className="pointer-events-none absolute inset-0 opacity-[0.05]"
        style={{
          backgroundImage:
            "linear-gradient(#22D3EE 1px, transparent 1px), linear-gradient(90deg, #22D3EE 1px, transparent 1px)",
          backgroundSize: "42px 42px",
        }}
      />

      {/* Left brand panel -- wide screens only. Gives the auth screen a
          sense of place instead of a lone card floating on black, and
          doubles as a quick reminder of what the product actually does. */}
      <div className="hidden lg:flex flex-col justify-between w-[46%] max-w-xl relative px-14 py-14 border-r border-slate-200 dark:border-noc-border">
        <div className="flex items-center gap-3">
          <ShieldMark />
          <div>
            <p className="font-display text-2xl font-bold tracking-widest uppercase text-slate-900 dark:text-noc-text leading-none">
              NetGuard
            </p>
            <p className="noc-label text-[10px] text-blue-600 dark:text-noc-cyan mt-1">Intelligent Network Change Management</p>
          </div>
        </div>

        <div className="space-y-7">
          <p className="text-slate-900 dark:text-noc-text text-2xl font-display font-semibold leading-snug max-w-sm">
            Command your network fleet with confidence, not luck.
          </p>
          <div className="space-y-5">
            {HIGHLIGHTS.map((h) => (
              <div key={h.title} className="flex gap-3.5">
                <div className="w-9 h-9 rounded-lg bg-blue-600/10 dark:bg-noc-cyan/10 border border-blue-600/20 dark:border-noc-cyan/20 text-blue-600 dark:text-noc-cyan flex items-center justify-center shrink-0">
                  {h.icon}
                </div>
                <div>
                  <p className="text-sm font-semibold text-slate-900 dark:text-noc-text">{h.title}</p>
                  <p className="text-xs text-slate-500 dark:text-noc-muted mt-0.5 leading-relaxed max-w-xs">{h.body}</p>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="flex items-center gap-1.5">
          <span className="noc-live-dot inline-block w-1.5 h-1.5 rounded-full bg-emerald-600 dark:bg-noc-good" />
          <p className="noc-label text-[10px] text-slate-400 dark:text-noc-faint">All systems monitored</p>
        </div>
      </div>

      {/* Right / auth column */}
      <div className="relative flex-1 flex items-center justify-center px-4 py-10">
      <div className="relative w-full max-w-sm">
        {/* Brand header -- shown only when the wide brand panel is hidden */}
        <div className="flex lg:hidden items-center gap-3 mb-6 justify-center">
          <ShieldMark />
          <div>
            <p className="font-display text-2xl font-bold tracking-widest uppercase text-slate-900 dark:text-noc-text leading-none">
              NetGuard
            </p>
            <p className="noc-label text-[10px] text-blue-600 dark:text-noc-cyan mt-1">
              Intelligent Network Change Management
            </p>
          </div>
        </div>

        <div className="noc-panel lit rounded-lg p-7 shadow-[0_0_40px_rgba(34,211,238,0.05)]">
          {mfaToken ? (
            <>
              <div className="flex items-center gap-2 mb-1">
                <span className="noc-live-dot inline-block w-1.5 h-1.5 rounded-full bg-emerald-600 dark:bg-noc-good" />
                <p className="noc-label text-[10px] text-slate-500 dark:text-noc-muted">Identity Verification</p>
              </div>
              <p className="text-base font-semibold text-slate-900 dark:text-noc-text mb-1">Two-factor code required</p>
              <p className="text-xs text-slate-500 dark:text-noc-muted mb-5">
                Enter the 6-digit code from your authenticator app.
              </p>
              <form onSubmit={submitMfa} className="space-y-4">
                <input
                  className={`${inputClass} text-center text-xl tracking-[0.5em] noc-num`}
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
                  <p className="text-xs text-red-600 dark:text-noc-crit bg-red-600/10 dark:bg-noc-crit/10 border border-red-600/30 dark:border-noc-crit/30 rounded-md px-3 py-2">
                    {error}
                  </p>
                )}
                <button
                  type="submit"
                  disabled={loading || mfaCode.length < 6}
                  className="w-full bg-blue-600 dark:bg-noc-cyan text-slate-50 dark:text-noc-bg rounded-md px-4 py-2.5 text-sm font-semibold hover:brightness-110 transition disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  {loading ? "Verifying…" : "Verify & Continue"}
                </button>
                <button
                  type="button"
                  className="w-full text-xs text-slate-500 dark:text-noc-muted hover:text-slate-900 dark:text-noc-text transition-colors"
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
              <div className="flex mb-6 rounded-md bg-slate-50 dark:bg-noc-panel2 border border-slate-200 dark:border-noc-border p-1 text-sm font-medium">
                <button
                  type="button"
                  className={`flex-1 py-1.5 rounded transition-colors ${
                    mode === "login" ? "bg-blue-600 dark:bg-noc-cyan text-slate-50 dark:text-noc-bg" : "text-slate-500 dark:text-noc-muted hover:text-slate-900 dark:text-noc-text"
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
                  className={`flex-1 py-1.5 rounded transition-colors ${
                    mode === "register" ? "bg-blue-600 dark:bg-noc-cyan text-slate-50 dark:text-noc-bg" : "text-slate-500 dark:text-noc-muted hover:text-slate-900 dark:text-noc-text"
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
                    <FieldLabel>Full name</FieldLabel>
                    <input
                      className={inputClass}
                      placeholder="Jordan Alvarez"
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
                    placeholder="you@company.com"
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
                      placeholder="••••••••"
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      autoComplete={mode === "login" ? "current-password" : "new-password"}
                      required
                      minLength={8}
                    />
                    <button
                      type="button"
                      onClick={() => setShowPassword((s) => !s)}
                      className="absolute right-2.5 top-1/2 -translate-y-1/2 text-[11px] font-medium text-slate-500 dark:text-noc-muted hover:text-blue-600 dark:text-noc-cyan transition-colors"
                      tabIndex={-1}
                    >
                      {showPassword ? "Hide" : "Show"}
                    </button>
                  </div>
                  {mode === "register" && password.length > 0 && (
                    <div className="mt-2">
                      <div className="h-1 w-full bg-slate-50 dark:bg-noc-panel2 rounded-full overflow-hidden flex gap-0.5">
                        {[0, 1, 2, 3].map((i) => (
                          <div
                            key={i}
                            className="h-full flex-1 rounded-full transition-colors"
                            style={{
                              backgroundColor: i < passwordScore ? strengthColor : "#1D2532",
                            }}
                          />
                        ))}
                      </div>
                      <p className="text-[10px] mt-1" style={{ color: strengthColor }}>
                        {strengthLabel}
                      </p>
                    </div>
                  )}
                  {mode === "login" && (
                    <div className="text-right mt-1.5">
                      <button
                        type="button"
                        className="text-[11px] text-slate-500 dark:text-noc-muted hover:text-blue-600 dark:text-noc-cyan transition-colors"
                        onClick={() => setError("Ask a Network Administrator to reset your password from Access Control.")}
                      >
                        Forgot password?
                      </button>
                    </div>
                  )}
                </div>
                {mode === "register" && (
                  <div>
                    <FieldLabel>Requested role</FieldLabel>
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
                {error && (
                  <p className="text-xs text-red-600 dark:text-noc-crit bg-red-600/10 dark:bg-noc-crit/10 border border-red-600/30 dark:border-noc-crit/30 rounded-md px-3 py-2">
                    {error}
                  </p>
                )}
                <button
                  type="submit"
                  disabled={loading}
                  className="w-full bg-blue-600 dark:bg-noc-cyan text-slate-50 dark:text-noc-bg rounded-md px-4 py-2.5 text-sm font-semibold hover:brightness-110 transition disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  {loading ? "Please wait…" : mode === "login" ? "Sign In" : "Create Account"}
                </button>
              </form>

              <p className="text-[11px] text-slate-400 dark:text-noc-faint mt-5 text-center leading-relaxed">
                Approving change requests requires a Network Administrator role.
                Admin and Security accounts are granted by an existing admin, not self-selected at sign-up.
              </p>
            </>
          )}
        </div>

        <div className="flex lg:hidden items-center justify-center gap-1.5 mt-5">
          <span className="noc-live-dot inline-block w-1.5 h-1.5 rounded-full bg-emerald-600 dark:bg-noc-good" />
          <p className="noc-label text-[10px] text-slate-400 dark:text-noc-faint">All systems monitored</p>
        </div>
      </div>
      </div>
    </div>
  );
}