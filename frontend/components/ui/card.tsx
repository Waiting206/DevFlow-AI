import { PilotBackdrop } from "@/components/ui/pilot-backdrop";
import { Orbit } from "lucide-react";
import { clsx } from "clsx";

export function Card({ className, children }: { className?: string; children: React.ReactNode }) {
  return <section className={clsx("min-w-0 rounded-lg border border-line bg-surface p-5 shadow-sm", className)}>{children}</section>;
}

export function PageTitle({ title, description }: { title: string; description: string }) {
  return (
    <div className="page-title mb-6 flex flex-col gap-2 border-b border-line pb-5">
      <PilotBackdrop variant="heading" />
      <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-[0.16em] text-accent-text"><Orbit className="h-3.5 w-3.5" aria-hidden="true" />RepoPilot AI / Mission Control</div>
      <h1 className="text-2xl font-semibold tracking-tight text-ink">{title}</h1>
      <p className="max-w-3xl text-sm leading-6 text-muted">{description}</p>
    </div>
  );
}

export function PrimaryButton({ children, className, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button className={clsx("pilot-button-primary inline-flex min-h-9 items-center justify-center gap-2 rounded-md bg-accent px-4 py-2 text-sm font-medium text-on-accent shadow-sm transition hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-50", className)} {...props}>{children}</button>
  );
}

export function GhostButton({ children, className, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button className={clsx("pilot-button-secondary w-full min-h-9 rounded-md border border-line bg-surface px-3 py-2 text-left text-sm text-content transition hover:border-accent-line hover:bg-elevated hover:text-ink disabled:cursor-not-allowed disabled:opacity-50", className)} {...props}>{children}</button>
  );
}
