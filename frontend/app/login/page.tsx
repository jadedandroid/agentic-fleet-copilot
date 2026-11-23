"use client";

import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";

import { useAuth } from "@/components/AuthProvider";

export default function LoginPage() {
  const { login, signInAsOperator, session } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [operatorKey, setOperatorKey] = useState("");
  const [operatorError, setOperatorError] = useState("");

  const submitOperator = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitting(true);
    setOperatorError("");
    try {
      await signInAsOperator(operatorKey.trim());
      setOperatorKey("");
      router.replace("/copilot");
    } catch (caught) {
      setOperatorError(caught instanceof Error ? caught.message : "Sign-in failed");
    } finally {
      setSubmitting(false);
    }
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      await login(email.trim(), password);
      router.replace("/copilot");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Sign-in failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="mx-auto flex w-full max-w-md flex-col gap-6 p-6">
      <header className="space-y-2 pt-6">
        <p className="text-sm uppercase tracking-wide text-cyan-300">Sign in</p>
        <h1 className="text-3xl font-bold">Access your tenant</h1>
        <p className="text-sm text-slate-400">
          Your tenant and role come from your account. Tenant accounts are created by the platform administrator.
        </p>
      </header>

      {session && (
        <p className="rounded-md border border-slate-700 p-3 text-sm text-slate-300">
          Already signed in as {session.email || "the platform operator"}.
        </p>
      )}

      <form onSubmit={submit} className="space-y-4 rounded-xl border border-slate-800 bg-panel p-5">
        <label className="block text-sm">
          Email
          <input
            type="email"
            autoComplete="username"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            className="mt-1 w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-2"
          />
        </label>
        <label className="block text-sm">
          Password
          <input
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            className="mt-1 w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-2"
          />
        </label>
        {error && <p className="rounded-md bg-rose-900/60 p-3 text-sm text-rose-100">{error}</p>}
        <button
          type="submit"
          disabled={submitting}
          className="w-full rounded-md bg-cyan-500 px-4 py-2 font-semibold text-slate-900 transition hover:bg-cyan-400 disabled:opacity-50"
        >
          {submitting ? "Signing in…" : "Sign in"}
        </button>
      </form>

      <form onSubmit={submitOperator} className="space-y-4 rounded-xl border border-slate-800 bg-panel p-5">
        <div>
          <p className="text-sm font-semibold">Platform operator</p>
          <p className="text-sm text-slate-400">
            Sees every tenant&apos;s data. Pick a tenant after signing in. Uses the bootstrap key.
          </p>
        </div>
        <label className="block text-sm">
          Bootstrap key
          <input
            type="password"
            autoComplete="off"
            required
            value={operatorKey}
            onChange={(event) => setOperatorKey(event.target.value)}
            className="mt-1 w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-2"
          />
        </label>
        {operatorError && <p className="rounded-md bg-rose-900/60 p-3 text-sm text-rose-100">{operatorError}</p>}
        <button
          type="submit"
          disabled={submitting}
          className="w-full rounded-md border border-cyan-500 px-4 py-2 font-semibold text-cyan-300 transition hover:bg-cyan-500/10 disabled:opacity-50"
        >
          Sign in as operator
        </button>
      </form>
    </main>
  );
}
