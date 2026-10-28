"use client";

import { useEffect, useMemo, useState } from "react";
import { Check, CheckCircle2, Clock3, FileText, Send, ShieldAlert, X } from "lucide-react";

import { apiGet, apiPost } from "@/lib/api";
import type { Repository } from "@/lib/types";
import { PageTitle, PrimaryButton } from "@/components/ui/card";

type ActionDraft = {
  id: string;
  repo_id: string;
  draft_type: string;
  target_type: string;
  target_id?: string | null;
  title: string;
  content: string;
  risk_level: string;
  status: string;
  execution_result?: Record<string, unknown> | null;
  error_message?: string | null;
  created_at: string;
  executed_at?: string | null;
  meta?: { target_number?: number; issue_number?: number; pr_number?: number; assignees?: string[]; labels?: string[] };
  execution?: ActionExecution | null;
};

type ActionExecution = {
  id: string;
  operator_id: string;
  write_status: string;
  verification_status: string;
  started_at: string;
  write_completed_at?: string | null;
  verified_at?: string | null;
  request_json: Record<string, unknown>;
  expected_state: Record<string, unknown>;
  write_result?: unknown;
  write_error?: string | null;
  verification_checks: Array<{ status: string; message: string; checked_at: string; [key: string]: unknown }>;
};

type AuditLog = { id: string; action: string; status: string; target_type?: string | null; target_id?: string | null; created_at: string };

const statusMeta: Record<string, { label: string; className: string }> = {
  pending_confirmation: { label: "等待人工确认", className: "bg-warning-soft text-warning border-warning-line" },
  executing: { label: "正在执行", className: "bg-warning-soft text-warning border-warning-line" },
  executed: { label: "执行请求已完成", className: "bg-elevated text-content border-line" },
  unknown: { label: "写入结果待确认", className: "bg-warning-soft text-warning border-warning-line" },
  cancelled: { label: "已取消", className: "bg-elevated text-muted border-line" },
  failed: { label: "执行失败", className: "bg-danger-soft text-danger border-danger-line" }
};

export default function ActionDraftsPage() {
  const [repos, setRepos] = useState<Repository[]>([]);
  const [repoId, setRepoId] = useState("");
  const [drafts, setDrafts] = useState<ActionDraft[]>([]);
  const [audits, setAudits] = useState<AuditLog[]>([]);
  const [title, setTitle] = useState("发布检查报告");
  const [content, setContent] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function load(repo = repoId) {
    const suffix = repo ? `?repo_id=${repo}` : "";
    const [draftItems, auditItems] = await Promise.all([
      apiGet<ActionDraft[]>(`/api/action-drafts${suffix}`),
      apiGet<AuditLog[]>(`/api/action-drafts/audit-logs${suffix}`).catch(() => [])
    ]);
    setDrafts(draftItems);
    setAudits(auditItems);
  }

  useEffect(() => {
    apiGet<Repository[]>("/api/repos")
      .then((items) => {
        setRepos(items);
        setRepoId(items[0]?.id || "");
        return load(items[0]?.id || "");
      })
      .catch(() => setRepos([]));
  }, []);

  async function createDraft() {
    if (!repoId || !content.trim()) return;
    setLoading(true);
    setError("");
    try {
      await apiPost<ActionDraft>("/api/action-drafts", {
        repo_id: repoId,
        draft_type: "send_report",
        target_type: "repository",
        title: title.trim() || "发布检查报告",
        content,
        risk_level: "medium",
        metadata: { source: "manual_review" }
      });
      setContent("");
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "创建草稿失败");
    } finally {
      setLoading(false);
    }
  }

  async function changeDraft(id: string, action: "confirm" | "cancel" | "verify") {
    setLoading(true);
    setError("");
    try {
      await apiPost(`/api/action-drafts/${id}/${action}`, {});
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作未能完成，请刷新查看执行记录");
      await load().catch(() => undefined);
    } finally {
      setLoading(false);
    }
  }

  const pendingCount = useMemo(() => drafts.filter((draft) => draft.status === "pending_confirmation").length, [drafts]);
  const executedCount = useMemo(() => drafts.filter((draft) => draft.status === "executed").length, [drafts]);

  return (
    <>
      <PageTitle title="安全草稿与人工确认" description="人工确认后执行 GitHub 操作，再独立读取实际结果验证，并保存执行与验证记录。" />
      {error && <div role="alert" className="mt-3 rounded-md bg-danger-soft p-3 text-sm text-danger">{error}</div>}

      <section className="grid gap-3 border-y border-line bg-surface px-4 py-4 sm:grid-cols-3">
        <Summary icon={FileText} label="全部草稿" value={drafts.length} />
        <Summary icon={Clock3} label="待确认" value={pendingCount} tone="amber" />
        <Summary icon={CheckCircle2} label="已执行" value={executedCount} tone="green" />
      </section>

      <div className="mt-4 grid gap-5 xl:grid-cols-[360px_minmax(0,1fr)]">
        <section className="self-start rounded-md border border-line bg-surface p-5">
          <div className="flex items-center gap-2 text-sm font-semibold text-ink"><ShieldAlert className="h-4 w-4 text-warning" />创建安全草稿</div>
          <p className="mt-2 text-xs leading-5 text-muted">这里只保存建议内容，不会直接写入外部系统。</p>
          <label className="mt-4 block text-sm font-medium text-content">仓库
            <select className="mt-2 w-full rounded-md border border-line bg-surface px-3 py-2 outline-none focus:border-accent-line" value={repoId} onChange={(event) => { setRepoId(event.target.value); void load(event.target.value); }}>
              {repos.map((repo) => <option key={repo.id} value={repo.id}>{repo.full_name}</option>)}
            </select>
          </label>
          <label className="mt-4 block text-sm font-medium text-content">标题
            <input className="mt-2 w-full rounded-md border border-line px-3 py-2 outline-none focus:border-accent-line" value={title} onChange={(event) => setTitle(event.target.value)} />
          </label>
          <label className="mt-4 block text-sm font-medium text-content">待审查内容
            <textarea className="mt-2 min-h-40 w-full resize-none rounded-md border border-line px-3 py-2 text-sm leading-6 outline-none focus:border-accent-line" value={content} onChange={(event) => setContent(event.target.value)} placeholder="输入准备发送的报告或评论内容" />
          </label>
          <PrimaryButton className="mt-4 w-full gap-2" disabled={!repoId || !content.trim() || loading} onClick={createDraft}><FileText className="h-4 w-4" />保存为待确认草稿</PrimaryButton>
        </section>

        <div className="space-y-4">
          {drafts.map((draft) => {
            const meta = statusMeta[draft.status] || { label: draft.status, className: "bg-panel text-muted border-line" };
            const targetNumber = draft.meta?.target_number || draft.meta?.issue_number || draft.meta?.pr_number;
            const missingTarget = ["comment", "issue_comment", "pr_comment", "close_issue", "label", "add_label", "add_labels"].includes(draft.draft_type)
              && !targetNumber && !draft.target_id;
            return (
              <article key={draft.id} className="rounded-md border border-line bg-surface p-5">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <h2 className="font-semibold text-ink">{draft.title || draft.draft_type}</h2>
                      <span className={`rounded-full border px-2.5 py-1 text-xs font-medium ${meta.className}`}>{meta.label}</span>
                      <span className={`rounded-full px-2.5 py-1 text-xs ${draft.risk_level === "high" ? "bg-danger-soft text-danger" : "bg-elevated text-muted"}`}>{draft.risk_level === "high" ? "高风险" : "中低风险"}</span>
                    </div>
                    <div className="mt-2 text-xs text-muted">{draft.draft_type} · {draft.target_type} · {new Date(draft.created_at).toLocaleString("zh-CN")}</div>
                    {draft.draft_type === "create_issue" && <p className="mt-2 text-xs text-content">创建新 Issue，编号由 GitHub 自动分配。</p>}
                    {targetNumber && <p className="mt-2 text-xs text-content">目标：{draft.target_type === "pull_request" ? "PR" : "Issue"} #{targetNumber}</p>}
                    {Boolean(draft.meta?.assignees?.length) && <p className="mt-2 text-xs text-content">GitHub 负责人：{draft.meta?.assignees?.map((login) => `@${login}`).join("、")}</p>}
                    {Boolean(draft.meta?.labels?.length) && <p className="mt-2 text-xs text-content">标签：{draft.meta?.labels?.join("、")}</p>}
                  </div>
                  {draft.status === "pending_confirmation" && (
                    <div className="flex gap-2">
                      <button className="inline-flex items-center gap-1 rounded-md bg-accent px-3 py-2 text-sm font-medium text-on-accent transition hover:bg-accent disabled:opacity-60" disabled={loading || missingTarget} onClick={() => changeDraft(draft.id, "confirm")}><Check className="h-4 w-4" />确认执行</button>
                      <button className="inline-flex items-center gap-1 rounded-md border border-line px-3 py-2 text-sm text-muted transition hover:bg-panel disabled:opacity-60" disabled={loading} onClick={() => changeDraft(draft.id, "cancel")}><X className="h-4 w-4" />取消</button>
                    </div>
                  )}
                </div>
                <div className="mt-4 whitespace-pre-wrap rounded-md border-l-4 border-accent-line bg-panel px-4 py-3 text-sm leading-7 text-content">{draft.content}</div>
                {draft.status === "pending_confirmation" && missingTarget && <p className="mt-3 text-sm text-warning">此旧草稿未绑定 Issue/PR 目标，无法执行。请取消后，在聊天中说明完整操作和目标，重新生成草稿；创建新 Issue 不需要已有编号。</p>}
                {draft.execution ? (
                  <ExecutionRecord execution={draft.execution} loading={loading} onVerify={() => changeDraft(draft.id, "verify")} />
                ) : draft.status === "executed" ? (
                  <p className="mt-3 text-sm text-muted">历史执行记录：没有独立回读验证结果。</p>
                ) : null}
                {draft.error_message && <div className="mt-3 rounded-md bg-danger-soft p-3 text-sm text-danger">{draft.error_message}</div>}
              </article>
            );
          })}
          {drafts.length === 0 && <div className="rounded-md border border-dashed border-line bg-surface p-5 text-sm text-muted">暂无安全草稿。</div>}

          <section className="rounded-md border border-line bg-surface p-5">
            <div className="flex items-center gap-2 font-semibold text-ink"><Send className="h-4 w-4 text-accent-text" />审计时间线</div>
            <div className="mt-4 space-y-0">
              {audits.slice(0, 10).map((item, index) => (
                <div key={item.id} className="relative flex gap-3 pb-4">
                  {index < Math.min(audits.length, 10) - 1 && <span className="absolute left-[7px] top-4 h-full w-px bg-line" />}
                  <span className={`relative mt-1 h-4 w-4 shrink-0 rounded-full border-4 border-white ${["success", "verified"].includes(item.status) ? "bg-success" : item.status === "failed" ? "bg-danger" : "bg-warning"}`} />
                  <div><div className="text-sm font-medium text-ink">{auditLabel(item.action)}</div><div className="mt-1 text-xs text-muted">{item.status} · {new Date(item.created_at).toLocaleString("zh-CN")}</div></div>
                </div>
              ))}
              {audits.length === 0 && <div className="text-sm text-muted">暂无审计记录。</div>}
            </div>
          </section>
        </div>
      </div>
    </>
  );
}

function Summary({ icon: Icon, label, value, tone = "teal" }: { icon: typeof FileText; label: string; value: number; tone?: "teal" | "amber" | "green" }) {
  const color = tone === "amber" ? "text-warning" : tone === "green" ? "text-success" : "text-accent-text";
  return <div className="flex items-center gap-3"><Icon className={`h-5 w-5 ${color}`} /><div><div className="text-xs text-muted">{label}</div><div className="mt-1 text-xl font-semibold text-ink">{value}</div></div></div>;
}

function auditLabel(action: string) {
  if (action === "action_draft.execute.start") return "已保存执行请求，开始执行";
  if (action === "action_draft.execute.interrupted") return "执行中断，结果待确认";
  if (action === "action_draft.execute.recovered") return "已恢复中断记录，结果待确认";
  if (action === "action_draft.verify.start") return "开始独立回读验证";
  if (action === "action_draft.verify") return "独立回读验证";
  if (action.startsWith("action_draft.confirm")) return "人工确认并执行草稿";
  if (action === "action_draft.create") return "创建待确认草稿";
  if (action === "action_draft.cancel") return "取消草稿";
  return action;
}

function ExecutionRecord({ execution, loading, onVerify }: { execution: ActionExecution; loading: boolean; onVerify: () => void }) {
  const latest = execution.verification_checks.at(-1);
  const labels: Record<string, string> = {
    verified: "验证通过", failed: "验证不通过", unknown: "暂时无法确认",
    pending: "等待验证", checking: "正在验证", not_applicable: "不适用外部验证"
  };
  const status = execution.verification_status;
  const color = status === "verified" ? "text-success" : status === "failed" ? "text-danger" : "text-muted";
  const canVerify = execution.write_status !== "running" && execution.write_status !== "failed"
    && execution.request_json.action !== "send_report" && status !== "checking";
  return (
    <div className="mt-4 space-y-2 border-t border-line pt-3 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className={`font-medium ${color}`}>{labels[status] || status}</span>
        {canVerify && <button className="rounded-md border border-line px-3 py-1.5 text-xs text-content disabled:opacity-60" disabled={loading} onClick={onVerify}>仅重新验证</button>}
      </div>
      {latest && <p className="text-content">{latest.message}</p>}
      {status === "unknown" && <p className="text-xs text-muted">不会自动重复写入 GitHub；重新验证只读取数据。</p>}
      <p className="break-all text-xs text-muted">执行记录：{execution.id} · {new Date(execution.started_at).toLocaleString("zh-CN")}</p>
      <details className="rounded-md bg-panel p-3">
        <summary className="cursor-pointer text-xs text-content">查看请求、预期结果、GitHub 返回值及历次验证依据</summary>
        <pre className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap break-all text-xs text-muted">{JSON.stringify(execution, null, 2)}</pre>
      </details>
    </div>
  );
}
