import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { api, setAccessToken } from "./api";

export type UserRole = "network_engineer" | "noc_engineer" | "network_admin" | "security" | "auditor";

export interface CurrentUser {
  id: string;
  email: string;
  full_name: string;
  role: UserRole;
  mfa_enabled: boolean;
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

  return (
    <AuthContext.Provider value={{ user, loading, login, verifyMfa, register, logout, refreshMe: fetchMe }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}