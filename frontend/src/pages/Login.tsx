import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth, UserRole } from "../lib/auth";

const ROLES: { value: UserRole; label: string }[] = [
  { value: "network_engineer", label: "Network Engineer" },
  { value: "noc_engineer", label: "NOC Engineer" },
  { value: "network_admin", label: "Network Administrator" },
  { value: "security", label: "Security Team" },
  { value: "auditor", label: "Enterprise Auditor" },
];

export default function Login() {
  const { login, register } = useAuth();
  const navigate = useNavigate();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<UserRole>("network_engineer");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      if (mode === "login") {
        await login(email, password);
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

  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-50">
      <div className="w-full max-w-sm bg-white border border-slate-200 rounded-xl p-8 shadow-sm">
        <div className="text-center mb-6">
          <p className="text-xl font-bold text-navy">NetGuard AI</p>
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
              className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
              placeholder="Full name"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              required
            />
          )}
          <input
            className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
            type="email"
            placeholder="Email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
          <input
            className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
            type="password"
            placeholder="Password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            minLength={8}
          />
          {mode === "register" && (
            <select
              className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
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
        </p>
      </div>
    </div>
  );
}
