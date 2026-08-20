import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { api, setAccessToken } from "./api";

export type UserRole = "network_engineer" | "noc_engineer" | "network_admin" | "security" | "auditor";

export interface CurrentUser {
  id: string;
  email: string;
  full_name: string;
  role: UserRole;
  mfa_enabled: boolean;
  // Fine-grained grants beyond base role -- see backend app.core.permissions.
  // extra_roles: whole other roles' worth of access. extra_permissions:
  // individual capability/page keys (e.g. "config_management", "page:backups").
  extra_roles: string[];
  extra_permissions: string[];
}

/** True if `user` has network_admin either as their base role or via a
 *  whole-role extra_roles grant -- the "can do everything" check used
 *  throughout the app before this permissions system existed. */
export function isAdmin(user: CurrentUser | null): boolean {
  return !!user && (user.role === "network_admin" || user.extra_roles.includes("network_admin"));
}

/** True if `user` can see/use `permissionKey` (e.g. "page:backups",
 *  "config_management") -- via base role admin, an extra_roles grant that
 *  implies it, or a direct extra_permissions grant. Mirrors the backend's
 *  require_roles/require_permission logic in app.core.deps, but frontend
 *  side this only ever gates *visibility* -- the backend is still the
 *  real enforcement point. */
export function hasPermission(user: CurrentUser | null, permissionKey: string): boolean {
  if (!user) return false;
  if (isAdmin(user)) return true;
  return user.extra_permissions.includes(permissionKey);
}

/** Result of a login attempt: either the session is established immediately,
 *  or the account has MFA enabled and a 6-digit code is required next. */
export type LoginOutcome = { mfaRequired: false } | { mfaRequired: true; mfaToken: string };

interface AuthContextValue {
  user: CurrentUser | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<LoginOutcome>;
  verifyMfa: (mfaToken: string, code: string) => Promise<void>;
  register: (email: string, full_name: string, password: string, role: UserRole) => Promise<void>;
  logout: () => Promise<void>;
  refreshMe: () => Promise<void>;
  /** Redirects the browser to the backend's Google OIDC login endpoint.
   *  See app/api/sso.py -- the whole round trip happens server-side;
   *  the frontend only needs to kick it off and later read the token
   *  back off the callback URL fragment (handled in App.tsx routing). */
  loginWithGoogle: () => void;
  /** Called by the SSO callback route once the backend has redirected
   *  back with #access_token=... in the URL. Same effect as a normal
   *  login/verifyMfa resolving. */
  completeSsoLogin: (accessToken: string) => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchMe = async () => {
    try {
      const res = await api.get<CurrentUser>("/auth/me");
      setUser(res.data);
    } catch {
      setUser(null);
      setAccessToken(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    // No access token in memory yet on a fresh page load (it's never
    // persisted) -- silently try the httpOnly refresh cookie before
    // deciding the user is logged out.
    (async () => {
      try {
        const res = await api.post("/auth/refresh");
        setAccessToken(res.data.access_token);
        await fetchMe();
      } catch {
        setLoading(false);
      }
    })();
  }, []);

  /** POST /auth/login. Backend returns either a Token (no MFA, refresh
   *  token set as an httpOnly cookie) or a MfaRequiredResponse
   *  ({ mfa_required: true, mfa_token }) — never both. */
  const login = async (email: string, password: string): Promise<LoginOutcome> => {
    const res = await api.post("/auth/login", { email, password });
    if (res.data?.mfa_required) {
      return { mfaRequired: true, mfaToken: res.data.mfa_token };
    }
    setAccessToken(res.data.access_token);
    await fetchMe();
    return { mfaRequired: false };
  };

  /** POST /auth/mfa/verify to exchange the challenge token + TOTP code for a
   *  real access token, completing the login started above. */
  const verifyMfa = async (mfaToken: string, code: string) => {
    const res = await api.post("/auth/mfa/verify", { mfa_token: mfaToken, code });
    setAccessToken(res.data.access_token);
    await fetchMe();
  };

  const register = async (email: string, full_name: string, password: string, role: UserRole) => {
    const res = await api.post("/auth/register", { email, full_name, password, role });
    // /auth/register only returns a single access_token (no refresh cookie) --
    // storing it lets the new user land on their dashboard immediately, but
    // they'll need to log in again once it expires since there is no
    // refresh token yet.
    setAccessToken(res.data.access_token);
    await fetchMe();
  };

  const logout = async () => {
    setAccessToken(null);
    setUser(null);
    try {
      // No body needed -- the backend reads and revokes the refresh token
      // from the httpOnly cookie, then clears it.
      await api.post("/auth/logout");
    } catch {
      // best-effort -- local session is already cleared either way
    }
  };

  const loginWithGoogle = () => {
    const baseURL = (import.meta.env.VITE_API_BASE_URL || "http://localhost:8000/api/v1").replace(/\/$/, "");
    // Full page navigation, not an axios call -- this has to leave the
    // SPA entirely so Google's consent screen can render.
    window.location.href = `${baseURL}/sso/google/login`;
  };

  const completeSsoLogin = async (accessToken: string) => {
    setAccessToken(accessToken);
    await fetchMe();
  };

  return (
    <AuthContext.Provider value={{ user, loading, login, verifyMfa, register, logout, refreshMe: fetchMe, loginWithGoogle, completeSsoLogin }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}