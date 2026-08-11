import axios, { AxiosError, InternalAxiosRequestConfig } from "axios";

const baseURL = import.meta.env.VITE_API_BASE_URL || "http://localhost:7001/api/v1";

// The access token lives in memory only (module-level variable), never in
// localStorage/sessionStorage -- either one is readable by any script that
// runs on the page (a compromised npm dependency, a future XSS bug), which
// would hand over the session outright. The refresh token never reaches JS
// at all: the backend sets it as an httpOnly cookie (see /auth/login etc.),
// so it can't be read or exfiltrated by page script even if one of those
// bugs exists. The tradeoff is that in-memory state doesn't survive a full
// page reload -- `bootstrapSession()` below covers that by calling
// /auth/refresh on startup, which succeeds silently as long as the httpOnly
// cookie is still valid.
let inMemoryAccessToken: string | null = null;

export function getAccessToken(): string | null {
  return inMemoryAccessToken;
}

export function setAccessToken(token: string | null): void {
  inMemoryAccessToken = token;
}

export const api = axios.create({
  baseURL,
  headers: { "Content-Type": "application/json" },
  // Required so the browser attaches/receives the httpOnly refresh-token
  // cookie on cross-origin requests to the API host.
  withCredentials: true,
});

api.interceptors.request.use((config) => {
  if (inMemoryAccessToken) {
    config.headers.Authorization = `Bearer ${inMemoryAccessToken}`;
  }
  return config;
});

// --- Refresh-on-401 handling ---
//
// A plain axios instance (not the interceptor-wrapped `api`) is used for the
// refresh call itself so a failed refresh never recurses back into this
// interceptor. Concurrent 401s are coalesced onto a single in-flight refresh
// request so a burst of requests doesn't try to rotate the refresh token
// multiple times (rotation is one-shot -- see backend /auth/refresh).
const rawClient = axios.create({ baseURL, withCredentials: true });

let refreshPromise: Promise<string | null> | null = null;

function clearSession() {
  inMemoryAccessToken = null;
}

async function performRefresh(): Promise<string | null> {
  try {
    // No body: the refresh token travels only as the httpOnly cookie the
    // browser attaches automatically.
    const res = await rawClient.post("/auth/refresh");
    inMemoryAccessToken = res.data.access_token;
    return res.data.access_token as string;
  } catch {
    clearSession();
    return null;
  }
}

interface RetryableConfig extends InternalAxiosRequestConfig {
  _retried?: boolean;
}

api.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const originalRequest = error.config as RetryableConfig | undefined;
    const status = error.response?.status;
    const isAuthEndpoint =
      originalRequest?.url?.includes("/auth/login") || originalRequest?.url?.includes("/auth/refresh");

    if (status === 401 && originalRequest && !originalRequest._retried && !isAuthEndpoint) {
      originalRequest._retried = true;

      if (!refreshPromise) {
        refreshPromise = performRefresh().finally(() => {
          refreshPromise = null;
        });
      }
      const newAccessToken = await refreshPromise;

      if (newAccessToken) {
        originalRequest.headers = originalRequest.headers ?? {};
        originalRequest.headers.Authorization = `Bearer ${newAccessToken}`;
        return api(originalRequest);
      }

      if (typeof window !== "undefined") {
        window.location.href = "/login";
      }
    }

    return Promise.reject(error);
  }
);