"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { BookOpen, Boxes, ChartNoAxesCombined, FilePenLine, GitBranch, LayoutDashboard, Network, ScanSearch, Workflow } from "lucide-react";
import { BrandMark } from "@/components/ui/brand-mark";

const navItems = [
  { href: "/", label: "Workspace", icon: LayoutDashboard },
  { href: "/knowledge", label: "项目知识", icon: BookOpen },
  { href: "/rag", label: "RAG 工作室", icon: ScanSearch },
  { href: "/repos", label: "Repos", icon: GitBranch },
  { href: "/action-drafts", label: "Drafts", icon: FilePenLine },
  { href: "/workflow-runs", label: "Runs", icon: Workflow },
  { href: "/workspaces", label: "Reports", icon: ChartNoAxesCombined },
  { href: "/code-graph", label: "Code Graph", icon: Network },
  { href: "/evals", label: "Evals", icon: Boxes }
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  return (
    <div className="app-shell min-h-screen">
      <a href="#page-content" className="skip-link">跳转到主内容</a>
      <header className="app-header">
        <div className="app-brand" aria-label="RepoPilot AI">
          <span className="brand-emblem"><BrandMark /></span>
          <div>
            <div className="text-sm font-semibold tracking-tight text-ink">RepoPilot AI</div>
            <div className="brand-caption">MISSION CONTROL</div>
          </div>
        </div>
        <nav className="app-navigation" aria-label="主导航">
          {navItems.map((item) => {
            const active = item.href === "/" ? pathname === "/" : pathname === item.href || pathname.startsWith(`${item.href}/`);
            const Icon = item.icon;
            return (
              <Link key={item.href} href={item.href} aria-current={active ? "page" : undefined} className={`nav-link ${active ? "is-active" : ""}`}>
                <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                {item.label}
              </Link>
            );
          })}
        </nav>
      </header>
      <main id="page-content" className={`app-content ${pathname === "/" ? "app-content-workspace" : ""}`}>{children}</main>
    </div>
  );
}
