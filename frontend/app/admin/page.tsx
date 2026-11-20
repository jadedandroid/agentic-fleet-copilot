"use client";

import Link from "next/link";
import { FormEvent, useEffect, useState } from "react";

import { useAuth } from "@/components/AuthProvider";
import { API_BASE, readError } from "@/lib/api";

type Tenant = {
  tenant_id: string;
  name: string;
  status: "active" | "deleted";
  created_at: string;
  deleted_at: string | null;
};

type TenantUser = { user_id: string; email: string; role: string; created_at: string };

const inputClass = "mt-1 w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-2";

export default function AdminPage() {
  return (
    <main className="mx-auto flex w-full max-w-5xl flex-col gap-8 p-6">
      <header className="space-y-2 pt-4">
        <p className="text-sm uppercase tracking-wide text-cyan-300">Administration</p>
        <h1 className="text-3xl font-bold">Tenants and users</h1>
        <p className="text-slate-300">
          Platform administrators create and delete tenants with the bootstrap key. Tenant admins manage their own
          users. Deleting a tenant removes its telemetry and identities. The audit log is kept.
        </p>
      </header>
      <PlatformSection />
      <TenantUsersSection />
    </main>
  );
}

function PlatformSection() {
  // The bootstrap key lives only in this component's memory. It is never written to storage.
  const [bootstrapKey, setBootstrapKey] = useState("");
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [form, setForm] = useState({ name: "", adminEmail: "", adminPassword: "" });

  const headers = (): HeadersInit => ({ "Content-Type": "application/json", "x-bootstrap-key": bootstrapKey });

  const loadTenants = async () => {
    setError("");
    const response = await fetch(`${API_BASE}/api/v1/admin/tenants`, { headers: headers() });
    if (!response.ok) {
      setError(await readError(response));
      return;
    }
    setTenants(((await response.json()) as { tenants: Tenant[] }).tenants);
    setLoaded(true);
  };

  const createTenant = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError("");
    setMessage("");
    const response = await fetch(`${API_BASE}/api/v1/admin/tenants`, {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({
        name: form.name.trim(),
        admin_email: form.adminEmail.trim(),
        admin_password: form.adminPassword
      })
    });
    if (!response.ok) {
      setError(await readError(response));
      return;
    }
    const created = (await response.json()) as { tenant_id: string };
    setMessage(`Created tenant ${created.tenant_id}. Its admin can now sign in.`);
    setForm({ name: "", adminEmail: "", adminPassword: "" });
    await loadTenants();
  };

  const deleteTenant = async (tenant: Tenant) => {
    if (!window.confirm(`Delete tenant "${tenant.tenant_id}"? Its telemetry and users are removed. This cannot be undone.`)) {
      return;
    }
    setError("");
    setMessage("");
    const response = await fetch(`${API_BASE}/api/v1/admin/tenants/${encodeURIComponent(tenant.tenant_id)}`, {
      method: "DELETE",
      headers: headers()
    });
    if (!response.ok) {
      setError(await readError(response));
      return;
    }
    const body = (await response.json()) as { removed_rows: Record<string, number> };
    const total = Object.values(body.removed_rows).reduce((sum, count) => sum + count, 0);
    setMessage(`Deleted ${tenant.tenant_id}: removed ${total} rows. Audit history is kept.`);
    await loadTenants();
  };

  return (
    <section className="space-y-4 rounded-xl border border-slate-800 bg-panel p-5">
      <h2 className="text-xl font-semibold text-cyan-300">Platform administration</h2>
      <div className="flex flex-col gap-3 md:flex-row md:items-end">
        <label className="flex-1 text-sm">
          Bootstrap key
          <input
            type="password"
            value={bootstrapKey}
            onChange={(event) => setBootstrapKey(event.target.value)}
            className={inputClass}
          />
        </label>
        <button
          onClick={loadTenants}
          disabled={!bootstrapKey}
          className="rounded-md bg-cyan-500 px-4 py-2 font-semibold text-slate-900 hover:bg-cyan-400 disabled:opacity-50"
        >
          Load tenants
        </button>
      </div>

      {message && <p className="rounded-md bg-emerald-900/60 p-3 text-sm text-emerald-100">{message}</p>}
      {error && <p className="rounded-md bg-rose-900/60 p-3 text-sm text-rose-100">{error}</p>}

      {loaded && (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="text-xs uppercase text-slate-400">
              <tr>
                <th className="py-2">Tenant</th>
                <th>Name</th>
                <th>Status</th>
                <th>Created</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {tenants.map((tenant) => (
                <tr key={tenant.tenant_id} className="border-t border-slate-800">
                  <td className="py-2 font-mono">{tenant.tenant_id}</td>
                  <td>{tenant.name}</td>
                  <td className={tenant.status === "active" ? "text-emerald-300" : "text-rose-300"}>{tenant.status}</td>
                  <td className="text-slate-400">{new Date(tenant.created_at).toLocaleString()}</td>
                  <td className="text-right">
                    {tenant.status === "active" && (
                      <button onClick={() => deleteTenant(tenant)} className="rounded-md bg-rose-600 px-3 py-1 text-white hover:bg-rose-500">
                        Delete
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {loaded && (
        <form onSubmit={createTenant} className="grid gap-3 border-t border-slate-800 pt-4 md:grid-cols-4">
          <label className="text-sm">
            Tenant name
            <input required minLength={2} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className={inputClass} />
          </label>
          <label className="text-sm">
            Admin email
            <input type="email" required value={form.adminEmail} onChange={(e) => setForm({ ...form, adminEmail: e.target.value })} className={inputClass} />
          </label>
          <label className="text-sm">
            Admin password (12+)
            <input type="password" required minLength={12} value={form.adminPassword} onChange={(e) => setForm({ ...form, adminPassword: e.target.value })} className={inputClass} />
          </label>
          <div className="flex items-end">
            <button type="submit" className="w-full rounded-md bg-emerald-500 px-4 py-2 font-semibold text-slate-900 hover:bg-emerald-400">
              Create tenant
            </button>
          </div>
        </form>
      )}
    </section>
  );
}

function TenantUsersSection() {
  const { session, authorizedFetch } = useAuth();
  const [users, setUsers] = useState<TenantUser[]>([]);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [form, setForm] = useState({ email: "", password: "", role: "analyst" });
  const isAdmin = session?.role === "admin";

  const loadUsers = async () => {
    const response = await authorizedFetch("/api/v1/tenant/users");
    if (!response.ok) {
      setError(await readError(response));
      return;
    }
    setUsers(((await response.json()) as { users: TenantUser[] }).users);
  };

  useEffect(() => {
    if (isAdmin) {
      void loadUsers();
    }
    // loadUsers is recreated per render; reload only when the signed-in admin changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAdmin, session?.token]);

  const createUser = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError("");
    setMessage("");
    const response = await authorizedFetch("/api/v1/tenant/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: form.email.trim(), password: form.password, role: form.role })
    });
    if (!response.ok) {
      setError(await readError(response));
      return;
    }
    setMessage(`Added ${form.email.trim()} as ${form.role}.`);
    setForm({ email: "", password: "", role: "analyst" });
    await loadUsers();
  };

  return (
    <section className="space-y-4 rounded-xl border border-slate-800 bg-panel p-5">
      <h2 className="text-xl font-semibold text-cyan-300">Tenant users</h2>
      {!session && (
        <p className="text-sm text-slate-300">
          <Link href="/login" className="text-cyan-300 underline">Sign in</Link> as a tenant admin to manage users.
        </p>
      )}
      {session && !isAdmin && (
        <p className="text-sm text-slate-300">Signed in as {session.role}. Only tenant admins can manage users.</p>
      )}
      {isAdmin && (
        <>
          {message && <p className="rounded-md bg-emerald-900/60 p-3 text-sm text-emerald-100">{message}</p>}
          {error && <p className="rounded-md bg-rose-900/60 p-3 text-sm text-rose-100">{error}</p>}
          <ul className="divide-y divide-slate-800 text-sm">
            {users.map((user) => (
              <li key={user.user_id} className="flex justify-between py-2">
                <span>{user.email}</span>
                <span className="text-slate-400">{user.role}</span>
              </li>
            ))}
          </ul>
          <form onSubmit={createUser} className="grid gap-3 border-t border-slate-800 pt-4 md:grid-cols-4">
            <label className="text-sm">
              Email
              <input type="email" required value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} className={inputClass} />
            </label>
            <label className="text-sm">
              Password (12+)
              <input type="password" required minLength={12} value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} className={inputClass} />
            </label>
            <label className="text-sm">
              Role
              <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })} className={inputClass}>
                <option value="analyst">Analyst (ask and review, no approvals)</option>
                <option value="admin">Admin (approve actions, manage users)</option>
              </select>
            </label>
            <div className="flex items-end">
              <button type="submit" className="w-full rounded-md bg-emerald-500 px-4 py-2 font-semibold text-slate-900 hover:bg-emerald-400">
                Add user
              </button>
            </div>
          </form>
        </>
      )}
    </section>
  );
}
