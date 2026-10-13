"use client";

import {
  CheckCircle2,
  CircleAlert,
  FileCheck2,
  RefreshCw,
  ScanSearch,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { authenticatedFetch } from "../lib/authenticatedFetch";
import { AccessibleDialog } from "./AccessibleDialog";

type ReconciliationOutcome = "executed" | "not_executed" | "unknown";

type ReconciliationEvidence = {
  observed_at: string;
  source: string;
  central_container?: {
    present: boolean;
    id?: string;
    server_id?: string;
    status?: string;
    version?: number;
    agent_container_id?: string;
    gpu_devices?: string[];
  };
  target: {
    present?: boolean | null;
    operation_id?: string;
    action?: string;
    target_id?: string;
    receipt_present?: boolean | null;
    binding_valid?: boolean;
    response_valid?: boolean;
    state?: "in_progress" | "succeeded" | "failed" | string;
    outcome?: "inconclusive" | "executed" | "not_executed" | string;
    result_code?: string | null;
    created_at?: string | null;
    completed_at?: string | null;
    status?: string;
    agent_container_id?: string;
    reported_agent_container_id?: string;
    expected_agent_container_id?: string;
    central_container_id?: string;
    identity_valid?: boolean;
    inventory_valid?: boolean;
    match_count?: number;
    exit_code?: number | null;
    oom_killed?: boolean;
    health?: string | null;
    container_id?: string;
    current_gpu_devices?: string[];
    requested_gpu_devices?: string[];
    confirmation_id?: string;
    backup_key?: string;
    ticket_id?: string;
    ticket_backup_key?: string;
  };
};

type ReconciliationItem = {
  id: string;
  status: string;
  action_type: string;
  target: { type: string; id: string };
  reason?: string | null;
  unknown_at?: string | null;
  action?: { requested_gpu_devices?: string[] };
  evidence?: ReconciliationEvidence | null;
  suggested_outcome: ReconciliationOutcome | null;
  inspected_at?: string | null;
  resolved_outcome?: ReconciliationOutcome | null;
  resolution_note?: string | null;
  resolved_at?: string | null;
  supported: boolean;
};

type ReconciliationListResponse = {
  reconciliations: ReconciliationItem[];
};

const actionLabels: Record<string, string> = {
  stop_container: "停止容器",
  delete_container: "删除容器",
  update_container_gpus: "修改 GPU",
  authorize_backup_restore: "授权备份恢复",
};

const outcomeLabels: Record<ReconciliationOutcome, string> = {
  executed: "目标侧已执行",
  not_executed: "目标侧未执行",
  unknown: "证据不足",
};

export function ExecutionReconciliationPanel() {
  const [items, setItems] = useState<ReconciliationItem[] | null>(null);
  const [busyId, setBusyId] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [selected, setSelected] = useState<ReconciliationItem | null>(null);
  const [note, setNote] = useState("");
  const [dialogError, setDialogError] = useState("");
  const resolveTriggerRef = useRef<HTMLButtonElement>(null);

  const loadItems = useCallback(async () => {
    setError("");
    try {
      const response = await authenticatedFetch("/api/gsm/operations/reconciliations");
      const payload = (await response.json()) as ReconciliationListResponse | ReconciliationItem[] | { detail?: string };
      if (!response.ok) {
        throw new Error("detail" in payload ? String(payload.detail ?? "对账列表加载失败") : "对账列表加载失败");
      }
      if (Array.isArray(payload)) {
        setItems(payload);
      } else if ("reconciliations" in payload) {
        setItems(payload.reconciliations);
      } else {
        throw new Error("对账列表响应无效");
      }
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "对账列表加载失败");
    }
  }, []);

  useEffect(() => {
    void loadItems();
  }, [loadItems]);

  async function inspect(item: ReconciliationItem) {
    setBusyId(item.id);
    setError("");
    setNotice("");
    try {
      const response = await authenticatedFetch(
        `/api/gsm/operations/reconciliations/${encodeURIComponent(item.id)}/inspect`,
        { method: "POST" },
      );
      const payload = (await response.json()) as { reconciliation?: ReconciliationItem; detail?: string };
      if (!response.ok) {
        throw new Error("detail" in payload ? String(payload.detail ?? "目标证据核验失败") : "目标证据核验失败");
      }
      if (!payload.reconciliation) throw new Error("目标证据响应无效");
      const inspected = payload.reconciliation;
      setItems((current) => current?.map((value) => value.id === inspected.id ? inspected : value) ?? [inspected]);
      setNotice(`${displayAction(inspected.action_type)}已完成目标侧核验。`);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "目标证据核验失败");
    } finally {
      setBusyId("");
    }
  }

  function openResolution(item: ReconciliationItem, trigger: HTMLButtonElement) {
    resolveTriggerRef.current = trigger;
    setSelected(item);
    setNote("");
    setError("");
    setDialogError("");
  }

  async function resolveItem() {
    if (!selected || !conclusive(selected) || note.trim().length < 1) return;
    setBusyId(selected.id);
    setError("");
    setDialogError("");
    setNotice("");
    try {
      const response = await authenticatedFetch(
        `/api/gsm/operations/reconciliations/${encodeURIComponent(selected.id)}/resolve`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            outcome: selected.suggested_outcome,
            note: note.trim(),
          }),
        },
      );
      const payload = (await response.json()) as { detail?: string };
      if (!response.ok) {
        throw new Error(String(payload.detail ?? "对账结案失败"));
      }
      setItems((current) => current?.filter((value) => value.id !== selected.id) ?? []);
      setNotice(`${displayAction(selected.action_type)}已按最新目标证据结案。`);
      setSelected(null);
      setNote("");
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : "对账结案失败";
      setDialogError(message);
      try {
        const refreshResponse = await authenticatedFetch(
          `/api/gsm/operations/reconciliations/${encodeURIComponent(selected.id)}/inspect`,
          { method: "POST" },
        );
        const refreshPayload = (await refreshResponse.json()) as { reconciliation?: ReconciliationItem };
        if (refreshResponse.ok && refreshPayload.reconciliation) {
          const refreshed = refreshPayload.reconciliation;
          setSelected(refreshed);
          setItems((current) => current?.map((value) => value.id === refreshed.id ? refreshed : value) ?? [refreshed]);
        }
      } catch {
        // Keep the visible resolution error when fresh evidence is unavailable.
      }
    } finally {
      setBusyId("");
    }
  }

  const conclusiveCount = items?.filter(conclusive).length ?? 0;
  const inconclusiveCount = items?.filter((item) => item.inspected_at && !conclusive(item)).length ?? 0;

  return (
    <section className="reconciliation-section" aria-labelledby="reconciliation-heading">
      <div className="panel-heading reconciliation-heading">
        <div>
          <h2 id="reconciliation-heading">执行对账</h2>
          <p>目标侧证据与控制面状态分离留存，结案不会重新执行原操作。</p>
        </div>
        <button
          className="icon-button"
          type="button"
          aria-label="刷新执行对账"
          title="刷新"
          onClick={() => void loadItems()}
        >
          <RefreshCw size={17} aria-hidden="true" />
        </button>
      </div>

      {items && items.length > 0 && (
        <dl className="reconciliation-summary" aria-label="执行对账摘要">
          <div><dt>待核验</dt><dd>{items.length}</dd></div>
          <div><dt>可结案</dt><dd>{conclusiveCount}</dd></div>
          <div><dt>证据不足</dt><dd>{inconclusiveCount}</dd></div>
        </dl>
      )}

      {error && <p className="workspace-error" role="alert">{error}</p>}
      {notice && <p className="reconciliation-notice" role="status">{notice}</p>}
      {items === null && !error && <p className="workspace-loading" role="status">正在加载待核验操作...</p>}
      {items?.length === 0 && <p className="empty-state">没有需要核验的异常执行。</p>}

      {items && items.length > 0 && (
        <div className="reconciliation-list">
          {items.map((item) => {
            const evidence = item.evidence;
            const ready = conclusive(item);
            const evidenceOutcome = item.suggested_outcome ?? "unknown";
            const evidenceRows = visibleEvidenceRows(item);
            return (
              <article className="reconciliation-row" key={item.id}>
                <div className="reconciliation-row-heading">
                  <div>
                    <span className="status-pill reconciliation-unknown">结果未知</span>
                    <h3>{displayAction(item.action_type)}</h3>
                    <code>{item.target.type} / {item.target.id}</code>
                  </div>
                  <span className="reconciliation-time">{formatDateTime(item.unknown_at)}</span>
                </div>

                <div className="reconciliation-track" aria-label="执行状态轨迹">
                  <span data-state="complete"><CheckCircle2 size={15} aria-hidden="true" />已发起</span>
                  <i aria-hidden="true" />
                  <span data-state="warning"><CircleAlert size={15} aria-hidden="true" />结果未知</span>
                  <i aria-hidden="true" />
                  <span data-state={item.inspected_at ? (ready ? "complete" : "warning") : "pending"}>
                    <FileCheck2 size={15} aria-hidden="true" />
                    {item.inspected_at ? outcomeLabel(item, evidenceOutcome) : "等待核验"}
                  </span>
                </div>

                <dl className="reconciliation-meta">
                  <div><dt>确认编号</dt><dd>{item.id}</dd></div>
                  <div><dt>原因</dt><dd>{reasonLabel(item.reason)}</dd></div>
                  <div><dt>证据支持</dt><dd>{item.supported ? "支持自动核验" : "需要其他处置"}</dd></div>
                  <div><dt>核验时间</dt><dd>{formatDateTime(item.inspected_at)}</dd></div>
                </dl>

                {item.inspected_at && (
                  <div className={`reconciliation-evidence evidence-${evidenceOutcome}`}>
                    <strong>{outcomeLabel(item, evidenceOutcome)}</strong>
                    <span>{evidenceSummary(item)}</span>
                    {evidenceRows.length > 0 && <dl>
                      {evidenceRows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
                    </dl>}
                  </div>
                )}

                <div className="reconciliation-actions">
                  <button
                    className="secondary-button"
                    type="button"
                    disabled={busyId === item.id}
                    onClick={() => void inspect(item)}
                  >
                    <ScanSearch size={16} aria-hidden="true" />
                    {busyId === item.id ? "核验中" : evidence ? "重新核验" : "核验目标状态"}
                  </button>
                  {ready && (
                    <button
                      className="primary-button"
                      type="button"
                      ref={selected?.id === item.id ? resolveTriggerRef : undefined}
                      onClick={(event) => openResolution(item, event.currentTarget)}
                    >
                      <FileCheck2 size={16} aria-hidden="true" />
                      按证据结案
                    </button>
                  )}
                </div>
              </article>
            );
          })}
        </div>
      )}

      {selected && (
        <AccessibleDialog
          title="确认对账结案"
          onClose={() => {
            setSelected(null);
            setDialogError("");
          }}
          triggerRef={resolveTriggerRef}
          destructiveBusy={busyId === selected.id}
          className="reconciliation-dialog"
        >
          <div className="reconciliation-resolution">
            <span>核验判断</span>
            <strong>{outcomeLabel(selected, selected.suggested_outcome ?? "unknown")}</strong>
            <code>{selected.target.type} / {selected.target.id}</code>
          </div>
          {dialogError && <p className="workspace-error reconciliation-dialog-error" role="alert">{dialogError}</p>}
          <div className="reconciliation-note">
            <label htmlFor="reconciliation-note">结案备注</label>
            <textarea
              id="reconciliation-note"
              name="reconciliation_note"
              value={note}
              minLength={1}
              maxLength={500}
              required
              aria-describedby="reconciliation-note-help"
              onChange={(event) => setNote(event.currentTarget.value)}
            />
            <span id="reconciliation-note-help">需填写结案依据，最多 500 字。</span>
          </div>
          <div className="dialog-actions">
            <button className="secondary-button" type="button" disabled={busyId === selected.id} onClick={() => {
              setSelected(null);
              setDialogError("");
            }}>取消</button>
            <button className="primary-button" type="button" disabled={busyId === selected.id || !conclusive(selected) || note.trim().length < 1} onClick={() => void resolveItem()}>
              <FileCheck2 size={16} aria-hidden="true" />
              {busyId === selected.id ? "结案中" : "确认结案"}
            </button>
          </div>
        </AccessibleDialog>
      )}
    </section>
  );
}

function conclusive(item: ReconciliationItem): boolean {
  return item.suggested_outcome === "executed" || item.suggested_outcome === "not_executed";
}

function displayAction(value: string): string {
  return actionLabels[value] ?? value;
}

function reasonLabel(value: string | null | undefined): string {
  if (value === "server_agent_response_unknown") return "Agent 响应未确认";
  if (value === "execution_lease_expired") return "执行租约超时";
  return value || "未记录";
}

function evidenceSummary(item: ReconciliationItem): string {
  if (item.evidence?.source === "server_agent.operation_receipt") {
    const target = item.evidence.target;
    if (target.receipt_present === false) {
      return "Agent 未找到这次操作的持久化回执，结果仍需核验。";
    }
    if (target.binding_valid === false) {
      return "回执与历史 operation ID、动作或目标不一致，不能据此结案。";
    }
    if (item.suggested_outcome === "executed") {
      return "持久化回执证明这次具体操作已经执行。";
    }
    if (item.suggested_outcome === "not_executed") {
      return "持久化回执证明这次具体操作没有执行。";
    }
    return "该操作回执仍在处理中或结果不确定，不能安全结案。";
  }
  if (item.action_type === "authorize_backup_restore") {
    if (item.suggested_outcome === "executed") {
      return "对应的恢复授权票据已经生成；结案不会执行恢复。";
    }
    if (item.suggested_outcome === "not_executed") {
      return "没有找到对应的恢复授权票据。";
    }
    return "恢复票据数量或备份身份不一致，不能安全结案。";
  }
  if (item.suggested_outcome === "executed") {
    return "最新目标状态与原操作的完成条件一致。";
  }
  if (item.suggested_outcome === "not_executed") {
    return "最新目标状态表明原操作没有完成。";
  }
  return item.supported
    ? "当前目标状态不能唯一证明原操作结果。"
    : "该操作暂不支持自动取证结案。";
}

function visibleEvidenceRows(item: ReconciliationItem): Array<[string, string]> {
  const evidence = item.evidence;
  if (!evidence) return [];
  const rows: Array<[string, string]> = [];
  const central = evidence.central_container;
  const target = evidence.target;
  const requestedGpus = target.requested_gpu_devices ?? item.action?.requested_gpu_devices;
  rows.push(["证据源", evidence.source]);
  if (item.action_type === "authorize_backup_restore") {
    rows.push(["恢复授权票据", target.present === null ? "证据冲突" : target.present ? "存在" : "不存在"]);
    if (target.match_count !== undefined) rows.push(["票据匹配数", String(target.match_count)]);
    if (target.ticket_id) rows.push(["票据编号", target.ticket_id]);
    if (target.backup_key) rows.push(["请求备份", target.backup_key]);
    if (target.ticket_backup_key) rows.push(["票据备份", target.ticket_backup_key]);
    if (target.identity_valid !== undefined) rows.push(["备份身份匹配", target.identity_valid ? "一致" : "不一致"]);
    return rows;
  }
  if (central) {
    rows.push(["控制面对象", central.present ? "存在" : "不存在"]);
    if (central.status) rows.push(["控制面状态", central.status]);
  }
  if (evidence.source === "server_agent.operation_receipt") {
    if (target.operation_id) rows.push(["Operation ID", target.operation_id]);
    rows.push([
      "持久化回执",
      target.receipt_present === true
        ? "存在"
        : target.receipt_present === false
          ? "未找到"
          : "未确认",
    ]);
    if (target.action) rows.push(["回执动作", displayAction(target.action)]);
    if (target.target_id) rows.push(["历史目标", target.target_id]);
    if (target.binding_valid !== undefined) {
      rows.push(["回执绑定", target.binding_valid ? "一致" : "不一致"]);
    }
    if (target.state) rows.push(["回执状态", receiptStateLabel(target.state)]);
    if (target.outcome) rows.push(["回执结果", receiptOutcomeLabel(target.outcome)]);
    if (target.result_code) rows.push(["结果代码", target.result_code]);
    return rows;
  }
  rows.push([
    "目标对象",
    target.present === null || target.present === undefined
      ? "未确认"
      : target.present
        ? "存在"
        : "不存在",
  ]);
  if (target.status) rows.push(["目标侧状态", target.status]);
  if (target.identity_valid !== undefined) rows.push(["身份匹配", target.identity_valid ? "一致" : "不一致"]);
  if (target.match_count !== undefined) rows.push(["匹配数量", String(target.match_count)]);
  if (requestedGpus) rows.push(["请求 GPU", requestedGpus.join(", ") || "无"]);
  if (target.current_gpu_devices) rows.push(["当前 GPU", target.current_gpu_devices.join(", ") || "无"]);
  return rows;
}

function receiptStateLabel(value: string): string {
  if (value === "succeeded") return "已完成";
  if (value === "failed") return "已失败";
  if (value === "in_progress") return "处理中";
  return value;
}

function receiptOutcomeLabel(value: string): string {
  if (value === "executed") return "已执行";
  if (value === "not_executed") return "未执行";
  if (value === "inconclusive") return "不确定";
  return value;
}

function outcomeLabel(item: ReconciliationItem, outcome: ReconciliationOutcome): string {
  if (item.action_type !== "authorize_backup_restore") return outcomeLabels[outcome];
  if (outcome === "executed") return "恢复授权已生成";
  if (outcome === "not_executed") return "恢复授权未生成";
  return "票据证据不足";
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) return "尚未核验";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : new Intl.DateTimeFormat("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      }).format(parsed);
}
