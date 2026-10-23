"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ThemeToggle } from "@/components/ThemeToggle";

const LINKS = [
  { href: "/", label: "Overview" },
  { href: "/agents", label: "Agents" },
  { href: "/queue", label: "Approval queue" },
  { href: "/evals", label: "Evals" },
  { href: "/spend", label: "Spending" },
];

export function Nav() {
  const path = usePathname();
  const current = (href: string) => (href === "/" ? path === "/" : path.startsWith(href));
  return (
    <nav className="mx-auto flex max-w-5xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-4 text-sm">
      <Link href="/" className="font-semibold tracking-tight">Agent Ops Hub</Link>
      {LINKS.map((l) => (
        <Link key={l.href} href={l.href} aria-current={current(l.href) ? "page" : undefined}
          className={current(l.href) ? "text-ink underline decoration-2 underline-offset-[10px]" : "text-ink-2 hover:text-ink"}>
          {l.label}
        </Link>
      ))}
      <div className="ml-auto flex items-center gap-4">
        <a href="https://github.com/shivamkalakaar/agent-ops-hub" className="text-ink-2 hover:text-ink">GitHub</a>
        <ThemeToggle />
      </div>
    </nav>
  );
}
