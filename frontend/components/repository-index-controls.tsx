"use client";

import { useEffect, useRef, useState } from "react";
import { Database, Download, RefreshCw } from "lucide-react";
import { apiGet, apiPost } from "@/lib/api";

type IndexState = {
  status: string;
  docs_indexed: number;
  last_scan_at?: string | null;
  error_message?: string | null;
  can_scan: boolean;
  checkout?: { status: string; error_message?: string | null; commit_sha?: string | null; updated_at?: string | null } | null;
};

export function RepositoryIndexControls({ repoId, onUpdated }: {
  repoId: string;
  onUpdated?: () => void | Promise<unknown>;
}) {
  const [state, setState] = useState<IndexState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [operationError, setOperationError] = useState<string | null>(null);
  const [action, setAction] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const callback = useRef(onUpdated);
  const notifyWhenReady = useRef(false);
  callback.current = onUpdated;

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let polling = false;
    let lastScanAt: string | null | undefined;
    async function load() {
      try {
        const next = await apiGet<IndexState>(`/api/repos/${repoId}/memory-index/state`);
        if (cancelled) return;
        setState(next);
        setError(null);
        const wasPolling = polling;
        polling = next.checkout?.status === "downloading" || next.status === "building";
        const documentsChanged = lastScanAt !== undefined && lastScanAt !== (next.last_scan_at ?? null);
        lastScanAt = next.last_scan_at ?? null;
        if (!polling && (notifyWhenReady.current || wasPolling || documentsChanged)) {
          notifyWhenReady.current = false;
          void Promise.resolve(callback.current?.()).catch(() => undefined);
        }
      } catch (cause) {
        if (cancelled) return;
        setError(cause instanceof Error ? cause.message : "无法读取源码与扫描状态");
      }
      if (!cancelled) timer = setTimeout(load, polling ? 1500 : 10000);
    }
    void load();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [repoId, revision]);

  async function run(kind: "checkout" | "scan") {
    setAction(kind);
    setOperationError(null);
    try {
      await apiPost(kind === "checkout"
        ? `/api/repos/${repoId}/checkout/retry`
        : `/api/repos/${repoId}/memory-index/scan`, {});
      notifyWhenReady.current = true;
    } catch (cause) {
      setOperationError(cause instanceof Error ? cause.message : "操作失败，请重试");
    } finally {
      setAction(null);
      setRevision((value) => value + 1);
    }
  }

  const source = state?.checkout?.status;
  const downloading = source === "downloading";
  const scanning = state?.status === "building";
  const busy = Boolean(action) || downloading || scanning;
  const sourceLabel = { pending: "源码未准备", downloading: "源码准备中", ready: "源码已就绪", failed: "源码同步失败" }[source ?? "pending"];
  const indexLabel = scanning ? "资料扫描中" : state?.status === "ready"
    ? `资料已入库：${state.docs_indexed} 条` : state?.status === "stale"
      ? "项目资料待更新" : state?.status === "failed" ? "资料扫描失败" : "项目资料未扫描";
  const detail = operationError || error || state?.checkout?.error_message || state?.error_message;

  return (
    <div className="mt-3 text-xs" aria-live="polite">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-muted">
        <span title={state?.checkout?.updated_at ? `源码上次同步：${new Date(state.checkout.updated_at).toLocaleString()}` : undefined}>
          {sourceLabel}{state?.checkout?.commit_sha ? ` · ${state.checkout.commit_sha.slice(0, 7)}` : ""}
        </span>
        <span>{indexLabel}</span>
        <button type="button" onClick={() => void run("checkout")} disabled={busy || !state}
          title="从远端更新后台源码副本，并更新已启用的项目资料；本地仓库模式只读取当前文件"
          className="inline-flex items-center gap-1 text-accent-text hover:underline disabled:opacity-50">
          <Download className="h-3.5 w-3.5" />{source === "failed" ? "重试同步源码" : source === "pending" ? "准备源码" : "同步源码"}
        </button>
        <button type="button" onClick={() => void run("scan")} disabled={busy || !state?.can_scan || Boolean(error)}
          title={state?.can_scan ? "扫描 README 和项目文档；语义检索仍需建立向量索引" : "源码就绪后才能扫描项目资料"}
          className="inline-flex items-center gap-1 text-accent-text hover:underline disabled:cursor-not-allowed disabled:opacity-50">
          <Database className="h-3.5 w-3.5" />{scanning ? "扫描中…" : state?.status === "ready" || state?.status === "stale" ? "更新项目资料" : "扫描项目资料"}
        </button>
        <button type="button" onClick={() => setRevision((value) => value + 1)} disabled={Boolean(action)}
          title="仅查询当前状态，不拉取代码或扫描资料" aria-label="刷新源码与扫描状态"
          className="inline-flex items-center gap-1 hover:text-ink disabled:opacity-50">
          <RefreshCw className={`h-3.5 w-3.5 ${downloading || scanning ? "animate-spin" : ""}`} />刷新状态
        </button>
      </div>
      {detail && <p className="mt-2 break-words text-danger">{detail}</p>}
    </div>
  );
}
