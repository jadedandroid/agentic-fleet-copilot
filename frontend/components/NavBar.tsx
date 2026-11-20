"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import { useAuth } from "@/components/AuthProvider";

const LINKS = [
  { href: "/", label: "Home" },
  { href: "/about", label: "About" },
  { href: "/agent-flow", label: "Agent-flow" },
  { href: "/copilot", label: "Agent co-pilot" },
  { href: "/source", label: "Source" },
  { href: "/admin", label: "Admin" }
];

export function NavBar() {
  const pathname = usePathname();
  const router = useRouter();
  const { session, logout } = useAuth();

  const signOut = async () => {
    await logout();
    router.replace("/login");
  };

  return (
    <nav className="border-b border-slate-800 bg-slate-950/90">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-2 px-6 py-3">
        <span className="mr-4 font-semibold text-cyan-300">Agentic Fleet Copilot</span>
        {LINKS.map((link) => {
          const active = link.href === "/" ? pathname === "/" : pathname.startsWith(link.href);
          return (
            <Link
              key={link.href}
              href={link.href}
              aria-current={active ? "page" : undefined}
              className={`rounded-md px-3 py-1.5 text-sm transition ${
                active ? "bg-cyan-500 text-slate-900 font-semibold" : "text-slate-300 hover:bg-slate-800"
              }`}
            >
              {link.label}
            </Link>
          );
        })}
        <div className="ml-auto flex items-center gap-3 text-sm">
          {session ? (
            <>
              <span className="text-slate-300">
                {session.email} · <span className="text-cyan-300">{session.tenantId}</span> ·{" "}
                <span className="text-slate-400">{session.role}</span>
              </span>
              <button
                onClick={signOut}
                className="rounded-md border border-slate-700 px-3 py-1.5 text-slate-200 hover:bg-slate-800"
              >
                Sign out
              </button>
            </>
          ) : (
            <Link href="/login" className="rounded-md border border-slate-700 px-3 py-1.5 text-slate-200 hover:bg-slate-800">
              Sign in
            </Link>
          )}
        </div>
      </div>
    </nav>
  );
}
