import "./globals.css";
import type { Metadata } from "next";
import { ReactNode } from "react";

import { AuthProvider } from "@/components/AuthProvider";
import { NavBar } from "@/components/NavBar";

export const metadata: Metadata = {
  title: "Agentic Fleet Copilot",
  description: "Deterministic multi-tenant operations copilot"
};

type LayoutProps = { children: ReactNode };

export default function RootLayout({ children }: LayoutProps) {
  return (
    <html lang="en">
      <body>
        <AuthProvider>
          <NavBar />
          {children}
        </AuthProvider>
      </body>
    </html>
  );
}
