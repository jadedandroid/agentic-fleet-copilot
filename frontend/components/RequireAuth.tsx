"use client";

import { useRouter } from "next/navigation";
import { ReactNode, useEffect } from "react";

import { useAuth } from "@/components/AuthProvider";

// Guards a page behind sign-in. Redirects to /login once the stored session has been read.
export function RequireAuth({ children }: { children: ReactNode }) {
  const { session, ready } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (ready && !session) {
      router.replace("/login");
    }
  }, [ready, session, router]);

  if (!ready || !session) {
    return <p className="p-6 text-sm text-slate-400">Checking sign-in…</p>;
  }
  return <>{children}</>;
}
