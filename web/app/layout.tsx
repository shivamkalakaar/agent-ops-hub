import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Agent Ops Hub",
  description: "Governed, observable, budget-capped agents: LangGraph + OpenRouter + Langfuse.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">
        <header className="border-b border-line">
          <nav className="mx-auto flex max-w-5xl flex-wrap items-center gap-x-6 gap-y-1 px-4 py-3 text-sm">
            <Link href="/" className="font-semibold">Agent Ops Hub</Link>
            <Link href="/" className="text-ink-2 hover:text-ink">Overview</Link>
            <Link href="/agents" className="text-ink-2 hover:text-ink">Agents</Link>
            <Link href="/queue" className="text-ink-2 hover:text-ink">Approval queue</Link>
            <Link href="/evals" className="text-ink-2 hover:text-ink">Evals</Link>
            <a href="https://github.com/shivamkalakaar/agent-ops-hub" className="ml-auto text-ink-2 hover:text-ink">GitHub</a>
          </nav>
        </header>
        <main className="mx-auto max-w-5xl px-4 py-8">{children}</main>
      </body>
    </html>
  );
}
