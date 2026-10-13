"use client";
import { ApprovalDetails } from "./ApprovalDetails";

import { Check, LogOut, Play, RefreshCw, UserRoundPlus, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { authenticatedFetch } from "../lib/authenticatedFetch";
import type {
  ActorSummary,
  ApprovalSummary,
  GpuRecommendation,
  GpuState,
  MemoryTier,
  QueueSummary,
  ServerSummary,
} from "../lib/mockData";

type Props = {
  actor: ActorSummary;
  servers: ServerSummary[];
  initialRecommendations: GpuRecommendation[];
  initialQueue: QueueSummary[];
  initialApprovals: ApprovalSummary[];
  mode?: "all" | "queue" | "approvals";
  onUpdated?: () => void;
  refreshing?: boolean;
};

const memoryTiers: Array<[MemoryTier, string]> = [
  ["lt_8", "低于 8 GB"],
  ["8_24", "8–24 GB"],
  ["24_48", "24–48 GB"],
  ["full_card", "整卡"],
];

const stateLabels: Record<GpuState, string> = {
  free: "空闲",
  shareable: "可安全共享",
  busy: "繁忙",
  exclusive: "独占模式",
  unknown: "未知",
};

const reasonLabels: Record<string, string> = {
  mounted_without_process: "已挂载但无计算进程",
  memory_available: "显存余量满足",
  low_recent_utilization: "近期利用率较低",
  advisory_only: "建议用途",
  stale_observation: "遥测数据过期",
};

export function ResourceGovernancePanel({
  actor,
  servers,
  initialRecommendations,
  initialQueue,
  initialApprovals,
  mode = "all",
  onUpdated,
  refreshing = false,
}: Props) {
  const [memoryTier, setMemoryTier] = useState<MemoryTier>("8_24");
  const [durationMinutes, setDurationMinutes] = useState(120);
  const [serverId, setServerId] = useState(servers[0]?.id ?? "server1");
  const [recommendations, setRecommendations] = useState(initialRecommendations);
  const [queue, setQueue] = useState(initialQueue);
  const [approvals, setApprovals] = useState(initialApprovals);
  const [approvalReasons, setApprovalReasons] = useState<Record<string, string>>({});
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busyAction, setBusyAction] = useState("");

  useEffect(() => { setApprovals(initialApprovals); }, [initialApprovals]);
  useEffect(() => { setQueue(initialQueue); }, [initialQueue]);
  useEffect(() => { setRecommendations(initialRecommendations); }, [initialRecommendations]);

  const ownQueue = useMemo(
    () => queue.find((item) => item.userId === actor.userId && ["waiting", "notified"].includes(item.status)),
    [actor.userId, queue],
  );
  const canQueue = ["user", "power_user", "teacher", "admin"].includes(actor.role);

  async function refreshRecommendations() {
    setBusyAction("recommend");
    setError("");
    try {
      const response = await authenticatedFetch("/api/gsm/governance/recommendations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          gpu_count: 1,
          memory_tier: memoryTier,
          estimated_duration_minutes: durationMinutes,
        }),
      });
      const result = await response.json();
      if (!response.ok) {
        throw new Error(String(result.detail ?? "推荐请求失败"));
      }
      setRecommendations((result.recommendations ?? []).map(mapRecommendation));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "推荐请求失败");
    } finally {
      setBusyAction("");
    }
  }

  async function joinQueue() {
    setBusyAction("join");
    setError("");
    try {
      const response = await authenticatedFetch("/api/gsm/governance/queue", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          gpu_count: 1,
          memory_tier: memoryTier,
          estimated_duration_minutes: durationMinutes,
          compatible_server_ids: [serverId],
          workload_profile: "standard",
        }),
      });
      const result = await response.json();
      if (!response.ok) {
        throw new Error(String(result.detail ?? "加入候补失败"));
      }
      const queued = mapQueue(result, queue.length + 1);
      setQueue((current) => [...current, queued]);
      setMessage(`已加入候补，第 ${queued.position} 位`);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "加入候补失败");
    } finally {
      setBusyAction("");
    }
  }

  async function withdrawQueue(requestId: string) {
    setBusyAction(`withdraw:${requestId}`);
    setError("");
    try {
      const response = await authenticatedFetch(`/api/gsm/governance/queue/${requestId}`, {
        method: "DELETE",
      });
      const result = await response.json();
      if (!response.ok) {
        throw new Error(String(result.detail ?? "退出候补失败"));
      }
      setQueue((current) => current.filter((item) => item.id !== requestId));
      setMessage("已退出候补");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "退出候补失败");
    } finally {
      setBusyAction("");
    }
  }

  async function decideApproval(approvalId: string, decision: "approved" | "rejected") {
    const approvalReason = approvalReasons[approvalId] ?? "";
    if (!approvalReason.trim()) {
      setError("请填写审批理由");
      return;
    }
    setBusyAction(`decision:${approvalId}`);
    setError("");
    try {
      const response = await authenticatedFetch(`/api/gsm/approvals/${approvalId}/decision`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision, reason: approvalReason.trim() }),
      });
      const result = await response.json();
      if (!response.ok) {
        throw new Error(String(result.detail ?? "审批失败"));
      }
      setApprovals((current) =>
        current.map((item) => (item.id === approvalId ? { ...item, status: result.status,
          reviewerUserId: result.reviewer_user_id ?? actor.userId, reviewReason: result.review_reason ?? approvalReason.trim(),
          decidedAt: result.decided_at } : item)),
      );
      setMessage(`申请 ${approvalId} 已${decision === "approved" ? "批准" : "拒绝"}`);
      onUpdated?.();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "审批失败");
    } finally {
      setBusyAction("");
    }
  }

  async function executeApproval(approvalId: string) {
    setBusyAction(`execute:${approvalId}`);
    setError("");
    try {
      const response = await authenticatedFetch(`/api/gsm/approvals/${approvalId}/execute`, {
        method: "POST",
      });
      const result = await response.json();
      if (!response.ok) {
        throw new Error(String(result.detail ?? "创建已批准容器失败"));
      }
      setApprovals((current) => current.map((item) => item.id === approvalId ? { ...item, status: "consumed", containerId: result.container.id } : item));
      setMessage(`容器 ${result.container.name} 已创建${result.container.status === "running" ? "并启动" : ""}${result.container.ssh_command ? `，连接命令：${result.container.ssh_command}` : ""}`);
      onUpdated?.();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "创建已批准容器失败");
    } finally {
      setBusyAction("");
    }
  }

  return (
    <section className="panel governance-panel" aria-labelledby="governance-heading">
      <div className="panel-heading">
        <div>
          <h2 id="governance-heading">{mode === "approvals" ? actor.role === "admin" ? "资源申请审批" : "我的申请" : "GPU 调度与申请"}</h2>
          <p>{mode === "approvals" ? "查看申请配置、审批进度和处理记录。" : "按实时进程证据推荐，候补顺序不锁定 GPU。"}</p>
        </div>
        {mode !== "approvals" && <span className="advisory-badge">不锁定 GPU</span>}
      </div>

      {error && <p className="form-help create-error" role="alert">{error}</p>}
      {message && <p className="table-message" role="status">{message}</p>}

      {mode !== "approvals" && <>
      <div className="governance-controls">
        <fieldset>
          <legend className="field-label">显存需求</legend>
          <div className="segmented-control" aria-label="推荐显存档位">
            {memoryTiers.map(([value, label]) => (
              <button
                key={value}
                type="button"
                aria-pressed={memoryTier === value}
                onClick={() => setMemoryTier(value)}
              >
                {label}
              </button>
            ))}
          </div>
        </fieldset>
        <label>
          服务器
          <select value={serverId} onChange={(event) => setServerId(event.currentTarget.value)}>
            {servers.map((server) => (
              <option key={server.id} value={server.id}>{server.name}</option>
            ))}
          </select>
        </label>
        <label>
          预计分钟
          <input
            type="number"
            min={1}
            step={1}
            value={durationMinutes}
            onChange={(event) => setDurationMinutes(Math.max(1, Number(event.currentTarget.value) || 1))}
          />
        </label>
        <button className="secondary-button" type="button" onClick={refreshRecommendations}>
          <RefreshCw size={16} aria-hidden="true" />
          {busyAction === "recommend" ? "刷新中" : "刷新推荐"}
        </button>
        {!ownQueue && (
          <button className="primary-button" type="button" disabled={!canQueue || busyAction === "join"} onClick={joinQueue}>
            <UserRoundPlus size={16} aria-hidden="true" />
            加入候补
          </button>
        )}
      </div>

      <div className="governance-columns">
        <div className="governance-block">
          <h3>推荐</h3>
          {recommendations.length === 0 ? (
            <p className="empty-state">暂无匹配推荐</p>
          ) : (
            <div className="table-wrap">
              <table aria-label="GPU 推荐列表">
                <thead><tr><th>GPU</th><th>状态</th><th>可用显存</th><th>依据</th></tr></thead>
                <tbody>
                  {recommendations.map((item) => (
                    <tr key={`${item.serverId}:${item.gpuUuid}`}>
                      <td>{item.serverId} / GPU {item.index}</td>
                      <td><span className={`status-pill gpu-${item.state}`}>{stateLabels[item.state]}</span></td>
                      <td>{item.memoryFreeGb.toFixed(1)} GB</td>
                      <td>
                        <span className="recommendation-reasons">
                          {item.reasonCodes.map((reason) => (
                            <span key={reason}>{reasonLabels[reason] ?? reason}</span>
                          ))}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div className="governance-block">
          <h3>候补顺序</h3>
          {ownQueue && (
            <div className="queue-summary">
              <strong>候补第 {ownQueue.position} 位</strong>
              <button
                className="icon-button"
                type="button"
                aria-label={`退出候补 ${ownQueue.id}`}
                title="退出候补"
                onClick={() => withdrawQueue(ownQueue.id)}
              >
                <LogOut size={16} aria-hidden="true" />
              </button>
            </div>
          )}
          {queue.length === 0 ? (
            <p className="empty-state">当前无人候补</p>
          ) : (
            <ol className="queue-list">
              {queue.map((item) => (
                <li key={item.id}><span>{item.position}</span>{item.userId} · {tierLabel(item.memoryTier)}</li>
              ))}
            </ol>
          )}
        </div>
      </div>
      </>}

      {(
        <div className="governance-block approval-block">
          <div className="approval-heading">
            <h3>{actor.role === "admin" ? "大配额审批" : "我的资源申请"}</h3>
            <span>{approvals.length} 项</span>
            {onUpdated && <button type="button" className="secondary-button" disabled={refreshing || !!busyAction} onClick={onUpdated}>
              <RefreshCw size={14} aria-hidden="true" />{refreshing ? "刷新中…" : "刷新申请"}
            </button>}
          </div>
          <div className="table-wrap">
            <table aria-label={actor.role === "admin" ? "大配额审批列表" : "我的资源申请列表"}>
              <thead><tr><th>申请</th><th>用户</th><th>触发项</th><th>状态</th><th>操作</th></tr></thead>
              <tbody>
                {approvals.length === 0 && <tr><td colSpan={5} className="empty-state">暂无资源申请</td></tr>}
                {approvals.map((approval) => (
                  <tr key={approval.id}>
                    <td><strong>{approval.specification?.name || approval.id}</strong><br /><ApprovalDetails approval={approval} servers={servers} /></td>
                    <td>{approval.ownerUserId}</td>
                    <td>{approval.triggerReasons.map(reasonLabel).join("、")}</td>
                    <td>{approvalStatusLabel(approval.status)}</td>
                    <td>
                      <div className="table-actions">
                        {approval.status === "pending" && actor.role === "admin" && (
                          <>
                            <input aria-label={`审批理由 ${approval.id}`} placeholder="填写本次审批理由…" value={approvalReasons[approval.id] ?? ""}
                              onChange={(event) => setApprovalReasons((current) => ({ ...current, [approval.id]: event.target.value }))} />
                            <button
                              className="icon-button"
                              type="button"
                              aria-label={`批准申请 ${approval.id}`}
                              title="批准"
                              disabled={!!busyAction}
                              onClick={() => decideApproval(approval.id, "approved")}
                            ><Check size={16} aria-hidden="true" /></button>
                            <button
                              className="icon-button danger-icon"
                              type="button"
                              aria-label={`拒绝申请 ${approval.id}`}
                              title="拒绝"
                              disabled={!!busyAction}
                              onClick={() => decideApproval(approval.id, "rejected")}
                            ><X size={16} aria-hidden="true" /></button>
                          </>
                        )}
                        {approval.status === "approved" && (actor.role === "admin" || approval.ownerUserId === actor.userId) && (
                          <button
                            className="secondary-button"
                            type="button"
                            aria-label={`创建已批准容器 ${approval.id}`}
                            disabled={!!busyAction}
                            onClick={() => executeApproval(approval.id)}
                          >
                            <Play size={16} aria-hidden="true" />
                            {busyAction === `execute:${approval.id}` ? "创建中…" : "创建已批准容器"}
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}

function mapRecommendation(item: Record<string, unknown>): GpuRecommendation {
  return {
    serverId: String(item.server_id ?? item.serverId),
    gpuUuid: String(item.gpu_uuid ?? item.gpuUuid),
    index: Number(item.index),
    state: String(item.state) as GpuState,
    safeToShare: Boolean(item.safe_to_share ?? item.safeToShare),
    memoryFreeGb: Number(item.memory_free_mb ?? Number(item.memoryFreeGb) * 1024) / 1024,
    reasonCodes: (item.reason_codes ?? item.reasonCodes ?? []) as string[],
  };
}

function mapQueue(item: Record<string, unknown>, fallbackPosition: number): QueueSummary {
  return {
    id: String(item.id),
    userId: String(item.user_id ?? item.userId),
    gpuCount: Number(item.gpu_count ?? item.gpuCount),
    memoryTier: String(item.memory_tier ?? item.memoryTier) as MemoryTier,
    estimatedDurationMinutes: Number(item.estimated_duration_minutes ?? item.estimatedDurationMinutes),
    compatibleServerIds: (item.compatible_server_ids ?? item.compatibleServerIds ?? []) as string[],
    status: String(item.status),
    createdAt: String(item.created_at ?? item.createdAt),
    position: Number(item.position ?? fallbackPosition),
  };
}

function tierLabel(tier: MemoryTier): string {
  return memoryTiers.find(([value]) => value === tier)?.[1] ?? tier;
}

function reasonLabel(reason: string): string {
  return reason === "gpu_count" ? "GPU 数量" : reason === "memory_gb" ? "内存" : reason;
}

function approvalStatusLabel(status: string): string {
  return {
    pending: "待审批",
    approved: "已批准",
    rejected: "已拒绝",
    expired: "已过期",
    consumed: "已创建",
    executing: "创建中",
  }[status] ?? status;
}
