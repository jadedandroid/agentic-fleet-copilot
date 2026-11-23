"use client";

import { createContext, ReactNode, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { API_BASE, readError } from "@/lib/api";

export type Session = {
  token: string;
  // Null for the platform operator, which belongs to no tenant.
  tenantId: string | null;
  role: "admin" | "analyst" | "platform_admin";
  email: string;
  expiresAt: string;
  // The tenant an operator is acting on. Sent as X-Tenant-Id on every request.
  scopeTenantId?: string | null;
};

type AuthContextValue = {
  session: Session | null;
  // False until sessionStorage has been read, so guarded pages do not redirect on the first render.
  ready: boolean;
  login: (email: string, password: string) => Promise<void>;
  signInAsOperator: (bootstrapKey: string) => Promise<void>;
  selectTenant: (tenantId: string | null) => void;
  logout: () => Promise<void>;
  authorizedFetch: (path: string, init?: RequestInit) => Promise<Response>;
};

const STORAGE_KEY = "afc.session";
// The operator key does not expire on the server, so the browser session is capped here.
const OPERATOR_SESSION_HOURS = 8;

const AuthContext = createContext<AuthContextValue | null>(null);

function readStoredSession(): Session | null {
  const raw = window.sessionStorage.getItem(STORAGE_KEY);
  if (!raw) {
    return null;
  }
  try {
    const stored = JSON.parse(raw) as Session;
    return new Date(stored.expiresAt).getTime() > Date.now() ? stored : null;
  } catch {
    return null;
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    setSession(readStoredSession());
    setReady(true);
  }, []);

  const clearSession = useCallback(() => {
    window.sessionStorage.removeItem(STORAGE_KEY);
    setSession(null);
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const response = await fetch(`${API_BASE}/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password })
    });
    if (!response.ok) {
      throw new Error(await readError(response));
    }
    const body = (await response.json()) as {
      token: string;
      tenant_id: string;
      role: "admin" | "analyst";
      email: string;
      expires_at: string;
    };
    const next: Session = {
      token: body.token,
      tenantId: body.tenant_id,
      role: body.role,
      email: body.email,
      expiresAt: body.expires_at
    };
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    setSession(next);
  }, []);

  const signInAsOperator = useCallback(async (bootstrapKey: string) => {
    const response = await fetch(`${API_BASE}/api/v1/auth/me`, {
      headers: { authorization: `Bearer ${bootstrapKey}` }
    });
    if (!response.ok) {
      throw new Error(await readError(response));
    }
    const body = (await response.json()) as { role: string };
    if (body.role !== "platform_admin") {
      throw new Error("That key is not a platform operator key.");
    }
    const next: Session = {
      token: bootstrapKey,
      tenantId: null,
      role: "platform_admin",
      email: "",
      expiresAt: new Date(Date.now() + OPERATOR_SESSION_HOURS * 3600_000).toISOString(),
      scopeTenantId: null
    };
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    setSession(next);
  }, []);

  const selectTenant = useCallback(
    (tenantId: string | null) => {
      if (!session) {
        return;
      }
      const next = { ...session, scopeTenantId: tenantId };
      window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      setSession(next);
    },
    [session]
  );

  const logout = useCallback(async () => {
    if (session) {
      // Best effort: the local session is cleared even if the server is unreachable.
      await fetch(`${API_BASE}/api/v1/auth/logout`, {
        method: "POST",
        headers: { authorization: `Bearer ${session.token}` }
      }).catch(() => undefined);
    }
    clearSession();
  }, [session, clearSession]);

  const authorizedFetch = useCallback(
    async (path: string, init: RequestInit = {}) => {
      const headers = new Headers(init.headers);
      if (session) {
        headers.set("Authorization", `Bearer ${session.token}`);
        if (session.scopeTenantId) {
          headers.set("X-Tenant-Id", session.scopeTenantId);
        }
      }
      const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
      // A rejected token means the session expired or the tenant was deleted. Drop it so the guard redirects.
      if (response.status === 401) {
        clearSession();
      }
      return response;
    },
    [session, clearSession]
  );

  const value = useMemo(
    () => ({ session, ready, login, signInAsOperator, selectTenant, logout, authorizedFetch }),
    [session, ready, login, signInAsOperator, selectTenant, logout, authorizedFetch]
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return context;
}
