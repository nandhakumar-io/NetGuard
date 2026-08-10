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

  if (mfaToken) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-50">
        <div className="w-full max-w-sm bg-white border border-slate-200 rounded-xl p-8 shadow-sm">
          <div className="text-center mb-6">
            <p className="text-xl font-bold text-navy">Two-Factor Verification</p>
            <p className="text-xs text-accent mt-1">Enter the 6-digit code from your authenticator app</p>
          </div>
          <form onSubmit={submitMfa} className="space-y-3">
            <input
              className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm tracking-widest text-center text-lg bg-white text-slate-900 placeholder:text-slate-400"
              inputMode="numeric"
              pattern="[0-9]*"
              maxLength={6}
              placeholder="000000"
              value={mfaCode}
              onChange={(e) => setMfaCode(e.target.value.replace(/\D/g, ""))}
              autoFocus
              required
            />
            {error && <p className="text-riskcrit text-sm">{error}</p>}
            <button
              type="submit"
              disabled={loading || mfaCode.length < 6}
              className="w-full bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-navy transition-colors disabled:opacity-50"
            >
              {loading ? "Verifying…" : "Verify"}
            </button>
            <button
              type="button"
              className="w-full text-xs text-slate-400 hover:text-slate-600"
              onClick={() => {
                setMfaToken(null);
                setMfaCode("");
                setError(null);
              }}
            >
              Back to sign in
            </button>
          </form>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-50">
      <div className="w-full max-w-sm bg-white border border-slate-200 rounded-xl p-8 shadow-sm">
        <div className="text-center mb-6">
          <p className="text-xl font-bold text-navy">NetGuard</p>
          <p className="text-xs text-accent mt-1">Intelligent Network Change Management</p>
        </div>

        <div className="flex mb-5 rounded-lg bg-slate-100 p-1 text-sm font-medium">
          <button
            type="button"
            className={`flex-1 py-1.5 rounded-md transition-colors ${mode === "login" ? "bg-white shadow text-navy" : "text-slate-500"}`}
            onClick={() => setMode("login")}
          >
            Sign In
          </button>
          <button
            type="button"
            className={`flex-1 py-1.5 rounded-md transition-colors ${mode === "register" ? "bg-white shadow text-navy" : "text-slate-500"}`}
            onClick={() => setMode("register")}
          >
            Register
          </button>
        </div>

        <form onSubmit={submit} className="space-y-3">
          {mode === "register" && (
            <input
              className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm bg-white text-slate-900 placeholder:text-slate-400"
              placeholder="Full name"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              required
            />
          )}
          <input
            className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm bg-white text-slate-900 placeholder:text-slate-400"
            type="email"
            placeholder="Email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
          <div className="relative">
            <input
              className="w-full border border-slate-300 rounded-lg px-3 py-2 pr-16 text-sm bg-white text-slate-900 placeholder:text-slate-400"
              type={showPassword ? "text" : "password"}
              placeholder="Password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              minLength={8}
            />
            <button
              type="button"
              onClick={() => setShowPassword((s) => !s)}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-[11px] font-medium text-slate-400 hover:text-slate-600"
              tabIndex={-1}
            >
              {showPassword ? "Hide" : "Show"}
            </button>
          </div>
          {mode === "register" && (
            <select
              className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm bg-white text-slate-900 placeholder:text-slate-400"
              value={role}
              onChange={(e) => setRole(e.target.value as UserRole)}
            >
              {ROLES.map((r) => (
                <option key={r.value} value={r.value}>
                  {r.label}
                </option>
              ))}
            </select>
          )}
          {error && <p className="text-riskcrit text-sm">{error}</p>}
          <button
            type="submit"
            disabled={loading}
            className="w-full bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-navy transition-colors disabled:opacity-50"
          >
            {loading ? "Please wait…" : mode === "login" ? "Sign In" : "Create Account"}
          </button>
        </form>
        <p className="text-[11px] text-slate-400 mt-4 text-center">
          Approving change requests requires a Network Administrator role.
          Admin and Security accounts are granted by an existing admin, not self-selected at sign-up.
        </p>
      </div>
    </div>
  );
}