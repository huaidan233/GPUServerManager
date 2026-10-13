"use client";

import { Play, Square, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";

import { authenticatedFetch } from "../lib/authenticatedFetch";
import { ContainerDetailsDialog } from "./ContainerDetailsDialog";
import { AccessibleDialog } from "./AccessibleDialog";
import { ContainerSSHAccess } from "./ContainerSSHAccess";
import type { EnvironmentRecipe } from "../lib/environmentRecipe";
import { containerStatusLabel, type ActorSummary, type ContainerSummary } from "../lib/mockData";

type Props = {
  containers: ContainerSummary[];
  actor?: ActorSummary;
  canOperate?: boolean;
  onOperationComplete?: () => void;
  onReuse?: (recipe: EnvironmentRecipe) => void;
};

type PendingConfirmation = {
  id: string;
  containerId: string;
  requiredPhrase: string;
  containerName: string;
  action: "stop" | "delete";
};

export function ContainerOperationsTable({ containers, actor, canOperate = true, onOperationComplete, onReuse }: Props) {
  const [rows, setRows] = useState(containers);
  const [managedContainerId, setManagedContainerId] = useState<string | null>(null);
  const [initialTab, setInitialTab] = useState<"overview" | "terminal" | "connections">("overview");
  const [credentialRevision, setCredentialRevision] = useState(0);
  function owns(container: ContainerSummary) {
    return !!actor && (container.ownerMemberId ? container.ownerMemberId === actor.memberId : container.user === actor.userId);
  }
  function open(containerId: string, tab: typeof initialTab) {
    setInitialTab(tab); setManagedContainerId(containerId);
  }
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [pendingConfirmation, setPendingConfirmation] = useState<PendingConfirmation | null>(null);
  const [confirmText, setConfirmText] = useState("");
  const [busyContainerId, setBusyContainerId] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<"start" | "request" | "confirm" | null>(null);

  useEffect(() => {
    setRows(containers);
  }, [containers]);

  function clearBusy() {
    setBusyContainerId(null);
    setBusyAction(null);
  }

  async function startContainer(container: ContainerSummary) {
    setMessage("");
    setError("");
    setBusyContainerId(container.id);
    setBusyAction("start");
    try {
      const response = await authenticatedFetch(`/api/gsm/containers/${container.id}/start`, { method: "POST" });
      const result = await response.json();
      if (!response.ok) {
        setError(String(result.detail ?? "启动容器失败。"));
        return;
      }
      const status = String(result.container?.status ?? result.status ?? "running");
      updateContainerStatus(container.id, status);
      setMessage(operationResultMessage(result, `容器 ${container.name} 已启动。`));
      onOperationComplete?.();
    } catch {
      setError("无法连接 Web API 代理。");
    } finally {
      clearBusy();
    }
  }

  async function requestConfirmation(container: ContainerSummary, action: "stop" | "delete") {
    setMessage("");
    setError("");
    setBusyContainerId(container.id);
    setBusyAction("request");
    try {
      const response = await authenticatedFetch(
        `/api/gsm/containers/${container.id}${action === "stop" ? "/stop" : ""}`,
        { method: action === "delete" ? "DELETE" : "POST" },
      );
      const result = await response.json();
      if (!response.ok) {
        setError(String(result.detail ?? "操作请求失败。"));
        return;
      }
      setConfirmText("");
      setPendingConfirmation({
        id: result.confirmation.id,
        containerId: container.id,
        requiredPhrase: result.confirmation.required_phrase,
        containerName: container.name,
        action,
      });
    } catch {
      setError("无法连接 Web API 代理。");
    } finally {
      clearBusy();
    }
  }

  async function confirmAction() {
    if (!pendingConfirmation) {
      return;
    }
    setMessage("");
    setError("");
    setBusyContainerId(pendingConfirmation.id);
    setBusyAction("confirm");
    try {
      const response = await authenticatedFetch(`/api/gsm/confirmations/${pendingConfirmation.id}/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phrase: confirmText }),
      });
      const result = await response.json();
      if (!response.ok) {
        setError(String(result.detail ?? "确认操作失败。"));
        return;
      }
      if (["execution_unknown", "provisioning_unknown", "unknown"].includes(String(result.status))) {
        setError(operationResultMessage(result, ""));
        onOperationComplete?.();
        return;
      }
      if (pendingConfirmation.action === "delete") {
        setRows((current) => current.filter((row) => row.id !== pendingConfirmation.containerId && row.id !== result.container?.id));
      } else if (result.container?.id && result.container?.status) {
        updateContainerStatus(result.container.id, result.container.status);
      }
      setPendingConfirmation(null);
      setConfirmText("");
      setMessage(operationResultMessage(result, "操作已执行。"));
      onOperationComplete?.();
    } catch {
      setError("无法连接 Web API 代理。");
    } finally {
      clearBusy();
    }
  }

  function updateContainerStatus(containerId: string, status: string) {
    setRows((current) =>
      current.map((row) =>
        row.id === containerId
          ? { ...row, status, statusText: containerStatusLabel(status) }
          : row,
      ),
    );
  }

  return (
    <>
      {message && (
        <p className="table-message" role="status">
          {message}
        </p>
      )}
      {error && !pendingConfirmation && (
        <p className="form-help create-error" role="alert">
          {error}
        </p>
      )}
      <div className="table-wrap">
        <table>
          <caption className="sr-only">容器列表</caption>
          <thead>
            <tr>
              <th>容器</th>
              <th>用户</th>
              <th>服务器</th>
              <th>资源</th>
              <th>GPU</th>
              <th>SSH</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((container) => (
              <tr key={container.id}>
                <td><button className="container-name-button" type="button" aria-label={`管理 ${container.name}`} onClick={() => open(container.id, "overview")}>{container.name}</button></td>
                <td>{container.user}</td>
                <td>{container.server}</td>
                <td>
                  {container.cpu}C / {container.memoryGb}G
                </td>
                <td>{container.gpus.length ? container.gpus.join(", ") : "无"}</td>
                <td>{container.sshCommand ? <ContainerSSHAccess key={`${container.id}-${credentialRevision}`} containerId={container.id} name={container.name}
                  command={container.sshCommand} isOwner={owns(container)}
                  onConnect={() => open(container.id, "terminal")} canConnect={canOperate && container.status === "running"}
                  onSettings={() => open(container.id, "connections")} /> : "未开启"}</td>
                <td>
                  <span className={`status-pill ${container.status}`}>
                    {container.statusText}
                  </span>
                </td>
                <td>
                  <div className="table-actions">
                    {container.status === "running" ? (
                      <button
                        className="secondary-button"
                        type="button"
                        aria-label={`停止 ${container.name}`}
                        aria-busy={busyContainerId === container.id}
                        disabled={busyContainerId !== null || !canOperate}
                        onClick={() => requestConfirmation(container, "stop")}
                      >
                        <Square size={15} aria-hidden="true" />
                        {busyContainerId === container.id && busyAction === "request" ? "请求中…" : "停止"}
                      </button>
                    ) : ["created", "stopped", "exited"].includes(container.status) ? (
                      <button
                        className="secondary-button"
                        type="button"
                        aria-label={`启动 ${container.name}`}
                        aria-busy={busyContainerId === container.id}
                        disabled={busyContainerId !== null || !canOperate}
                        onClick={() => startContainer(container)}
                      >
                        <Play size={15} aria-hidden="true" />
                        {busyContainerId === container.id && busyAction === "start" ? "启动中…" : "启动"}
                      </button>
                    ) : <span className="form-help">请刷新状态</span>}
                    <button
                      className="danger-button"
                      type="button"
                      aria-label={`删除 ${container.name}`}
                      aria-busy={busyContainerId === container.id}
                      disabled={busyContainerId !== null || !canOperate}
                      onClick={() => requestConfirmation(container, "delete")}
                    >
                      <Trash2 size={15} aria-hidden="true" />
                      {busyContainerId === container.id && busyAction === "request" ? "请求中…" : "删除"}
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.filter((container) => container.id === managedContainerId).map((container) => (
        <ContainerDetailsDialog key={container.id} container={container} canOperate={canOperate} canManageSSH={owns(container)} initialTab={initialTab} onReuse={onReuse ? recipe => { setManagedContainerId(null); onReuse(recipe); } : undefined}
          onUpdated={() => { setCredentialRevision((value) => value + 1); onOperationComplete?.(); }} onClose={() => setManagedContainerId(null)} />
      ))}

      {pendingConfirmation && (
        <AccessibleDialog title="确认容器操作" destructiveBusy={busyAction === "confirm"} onClose={() => setPendingConfirmation(null)}>
          <form onSubmit={(event) => { event.preventDefault(); if (confirmText === pendingConfirmation.requiredPhrase && busyAction !== "confirm") void confirmAction(); }}>
            <p>{pendingConfirmation.action === "delete" ? "删除" : "停止"}容器：<strong>{pendingConfirmation.containerName}</strong></p>
            <p>输入以下短语继续：</p>
            <p>
              <strong>{pendingConfirmation.requiredPhrase}</strong>
            </p>
            <label htmlFor="container-action-confirm">确认短语</label>
            <input
              id="container-action-confirm"
              type="text"
              autoComplete="off"
              name="confirmation"
              spellCheck={false}
              value={confirmText}
              disabled={busyAction === "confirm"}
              onChange={(event) => setConfirmText(event.target.value)}
            />
            {error && <p className="form-help create-error" role="alert">{error}</p>}
            <div className="dialog-actions">
              <button
                className="secondary-button"
                type="button"
                disabled={busyAction === "confirm"}
                onClick={() => setPendingConfirmation(null)}
              >
                取消
              </button>
              <button
                className="danger-button"
                type="submit"
                aria-busy={busyAction === "confirm"}
                disabled={busyAction === "confirm" || confirmText !== pendingConfirmation.requiredPhrase}
              >
                {busyAction === "confirm" ? "执行中…" : "确认执行"}
              </button>
            </div>
          </form>
        </AccessibleDialog>
      )}
    </>
  );
}

function operationResultMessage(result: Record<string, unknown>, successMessage: string): string {
  const operationId = typeof result.operation_id === "string"
    ? result.operation_id
    : typeof result.execution_id === "string"
      ? result.execution_id
      : undefined;
  const status = typeof result.status === "string" ? result.status : "";
  const suffix = operationId ? `（操作 ID：${operationId}）` : "";
  if (["execution_unknown", "provisioning_unknown", "unknown"].includes(status)) {
    return `操作结果未知${suffix}，请刷新资源状态核验。`;
  }
  return `${successMessage}${suffix}`;
}
