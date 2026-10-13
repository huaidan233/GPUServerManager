"use client";

import { DatabaseBackup, FlaskConical, RotateCcw, ShieldCheck } from "lucide-react";
import { useState } from "react";

import { mapBackups } from "../lib/api";
import { authenticatedFetch } from "../lib/authenticatedFetch";
import type { ActorRole, BackupOperationsSummary, BackupRunSummary } from "../lib/mockData";
import { AccessibleDialog } from "./AccessibleDialog";

type Props = {
  actorRole: ActorRole;
  initialStatus: BackupOperationsSummary | null;
  onUpdated?: () => void;
};

type PendingConfirmation = {
  id: string;
  phrase: string;
  key: string;
};

export function BackupOperationsPanel({ actorRole, initialStatus, onUpdated }: Props) {
  const [status, setStatus] = useState(initialStatus);
  const [integrity, setIntegrity] = useState<Record<string, boolean>>({});
  const [pending, setPending] = useState<PendingConfirmation | null>(null);
  const [confirmText, setConfirmText] = useState("");
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  if (actorRole !== "admin" || !status) {
    return null;
  }

  const latest = status.latestSuccess;
  const latestDrill = status.latestDrill;

  async function request(path: string, init?: RequestInit) {
    const response = await authenticatedFetch(path, init);
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(String(payload.detail ?? payload.message ?? "备份操作失败"));
    }
    return payload;
  }

  async function createBackup() {
    setBusy("create");
    setError("");
    try {
      const result = await request("/api/gsm/operations/backups", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "manual" }),
      });
      setMessage(result.retention_warning ?? `已创建 ${result.key}，自动保留最近 10 个备份。`);
      setStatus(mapBackups(await request("/api/gsm/operations/backups")));
      onUpdated?.();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "备份失败");
    } finally {
      setBusy("");
    }
  }

  async function verify(key: string) {
    setBusy(`verify:${key}`);
    setError("");
    try {
      await request(`/api/gsm/operations/backups/${encodeURIComponent(key)}/verify`, { method: "POST" });
      setIntegrity((current) => ({ ...current, [key]: true }));
    } catch (requestError) {
      setIntegrity((current) => ({ ...current, [key]: false }));
      setError(requestError instanceof Error ? requestError.message : "校验失败");
    } finally {
      setBusy("");
    }
  }

  async function drill(key: string) {
    setBusy(`drill:${key}`);
    setError("");
    try {
      const result = await request(`/api/gsm/operations/backups/${encodeURIComponent(key)}/drill`, { method: "POST" });
      setMessage(result.status === "success" ? "恢复演练通过" : "恢复演练失败");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "演练失败");
    } finally {
      setBusy("");
    }
  }

  async function requestRestore(key: string) {
    setBusy(`restore:${key}`);
    setError("");
    try {
      const result = await request(`/api/gsm/operations/backups/${encodeURIComponent(key)}/restore-request`, { method: "POST" });
      setPending({
        id: result.confirmation.id,
        phrase: result.confirmation.required_phrase,
        key,
      });
      setConfirmText("");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "申请恢复失败");
    } finally {
      setBusy("");
    }
  }

  async function confirmRestore() {
    if (!pending || confirmText !== pending.phrase) return;
    setBusy("confirm");
    setError("");
    try {
      const result = await request(`/api/gsm/confirmations/${pending.id}/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phrase: confirmText }),
      });
      setPending(null);
      setMessage(result.command ?? "恢复 ticket 已签发");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "确认失败");
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="operations-section backup-operations" aria-labelledby="backup-operations-heading">
      <div className="panel-heading operations-heading">
        <div>
          <h2 id="backup-operations-heading">控制面备份</h2>
          <p>只保留最近 10 个备份，旧备份自动清理。正在恢复的备份暂时保留。仅含控制库与配置，不包含个人数据。</p>
        </div>
        <button className="icon-button" type="button" aria-label="立即备份" title="立即备份" onClick={createBackup} disabled={busy === "create"}>
          <DatabaseBackup size={18} aria-hidden="true" />
        </button>
      </div>

      {status.backend === "local" && <p className="warning-text">仅本机，不能抵御整机磁盘故障</p>}
      {message && <p className="table-message" role="status">{message}</p>}
      {error && <p className="form-help create-error" role="alert">{error}</p>}

      <div className="backup-summary" aria-label="备份摘要">
        <span>最近成功：{latest?.finishedAt ?? "暂无"}</span>
        <span>最近大小：{latest?.sizeBytes ? `${(latest.sizeBytes / 1024 / 1024).toFixed(1)} MB` : "暂无"}</span>
        <span>最近演练：{latestDrill?.checkedAt ?? "暂无"}</span>
      </div>

      <div className="table-wrap">
        <table aria-label="控制面备份列表">
          <thead><tr><th>备份</th><th>状态</th><th>完整性</th><th>操作</th></tr></thead>
          <tbody>
            {status.runs.filter(run => !run.key || status.inventory.some(item => item.key === run.key)).length === 0 ? (
              <tr><td colSpan={4}>暂无备份记录</td></tr>
            ) : status.runs.filter(run => !run.key || status.inventory.some(item => item.key === run.key)).slice(0, 30).map((run) => (
              <BackupRow
                key={run.id}
                run={run}
                integrity={run.key
                  ? integrity[run.key] ?? (run.integrityStatus === "valid" ? true : undefined)
                  : undefined}
                busy={busy}
                onVerify={verify}
                onDrill={drill}
                onRestore={requestRestore}
              />
            ))}
          </tbody>
        </table>
      </div>

      {pending && (
        <AccessibleDialog title="确认备份恢复" onClose={() => setPending(null)} destructiveBusy={busy === "confirm"}>
          <p>输入完整短语后签发一次性恢复 ticket。</p>
          <strong>{pending.phrase}</strong>
          <label htmlFor="backup-restore-confirm">确认短语</label>
          <input id="backup-restore-confirm" aria-label="确认短语" value={confirmText} onChange={(event) => setConfirmText(event.target.value)} spellCheck={false} />
          <div className="dialog-actions">
            <button className="secondary-button" type="button" disabled={busy === "confirm"} onClick={() => setPending(null)}>取消</button>
            <button className="danger-button" type="button" disabled={confirmText !== pending.phrase || busy === "confirm"} onClick={confirmRestore}>确认申请恢复</button>
          </div>
        </AccessibleDialog>
      )}
    </section>
  );
}

function BackupRow({
  run,
  integrity,
  busy,
  onVerify,
  onDrill,
  onRestore,
}: {
  run: BackupRunSummary;
  integrity?: boolean;
  busy: string;
  onVerify: (key: string) => void;
  onDrill: (key: string) => void;
  onRestore: (key: string) => void;
}) {
  const key = run.key;
  const statusLabel = run.status === "success" ? "成功" : run.status === "failed" ? "失败" : "处理中";
  return (
    <tr>
      <td><strong>{key ?? run.kind}</strong>{run.message && <span className="form-help">{run.message}</span>}</td>
      <td>{statusLabel}</td>
      <td>{integrity === true ? "完整性检查通过" : integrity === false ? "完整性检查失败" : "未校验"}</td>
      <td className="backup-actions">
        {key && <>
          <button className="icon-button" type="button" aria-label={`校验 ${key}`} title="校验" onClick={() => onVerify(key)} disabled={busy === `verify:${key}`}><ShieldCheck size={16} /></button>
          <button className="icon-button" type="button" aria-label={`演练 ${key}`} title="恢复演练" onClick={() => onDrill(key)} disabled={busy === `drill:${key}`}><FlaskConical size={16} /></button>
          <button className="icon-button" type="button" aria-label={`申请恢复 ${key}`} title="申请恢复" onClick={() => onRestore(key)} disabled={busy === `restore:${key}`}><RotateCcw size={16} /></button>
        </>}
      </td>
    </tr>
  );
}
