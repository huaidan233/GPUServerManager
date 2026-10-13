"use client";

import { useState } from "react";
import { Clock3, Download, Play, TriangleAlert } from "lucide-react";
import { containerRequest, managementError } from "../lib/containerManagement";
import "./container-recovery.css";

export type LifecycleReport = {
  observed_at?: string | null;
  last_stop?: { at: string | null; code: string; title: string; description: string;
    confidence: string; exit_code: number | null; oom_killed: boolean | null } | null;
  events: Array<{ id: string; at: string; kind: string; title: string; description: string }>;
  restart_count?: number | null;
  host_booted_at?: string | null;
};

const formatDate = (at?: string | null) => at && !Number.isNaN(new Date(at).getTime())
  ? new Date(at).toLocaleString("zh-CN", { hour12: false }) : "时间未记录";

export function ContainerRecoveryPanel({ containerId, running, canStart, report, onLogs, onConfigure, onUpdated }: {
  containerId: string; running: boolean; canStart: boolean; report?: LifecycleReport;
  onLogs: () => void; onConfigure: () => void; onUpdated: () => void;
}) {
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const stop = report?.last_stop;
  async function start() {
    setBusy("start"); setError(""); setMessage("");
    try {
      const result = await containerRequest<{ container?: { status?: string } }>(`/api/gsm/containers/${encodeURIComponent(containerId)}/start`, { method: "POST" });
      setMessage(result.container?.status === "running" ? "容器已启动。训练任务是否续跑取决于项目的 checkpoint 与启动方式。" : "启动请求已处理，请刷新确认容器状态。");
      onUpdated();
    } catch (reason) { setError(managementError(reason)); }
    finally { setBusy(""); }
  }
  async function download() {
    setBusy("download"); setError("");
    try {
      const logs = await containerRequest<{ logs: string; truncated?: boolean }>(`/api/gsm/containers/${encodeURIComponent(containerId)}/logs?tail=500`);
      const body = JSON.stringify({ container_id: containerId, exported_at: new Date().toISOString(), lifecycle: report, logs }, null, 2);
      const url = URL.createObjectURL(new Blob([body], { type: "application/json" }));
      const link = document.createElement("a"); link.href = url; link.download = `container-${containerId.slice(0, 12)}-diagnostics.json`;
      link.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      setMessage("诊断记录已下载，包含最近 500 行以内的运行日志。分享前请检查日志中的项目私密信息。");
    } catch (reason) { setError(managementError(reason)); }
    finally { setBusy(""); }
  }
  return <section className="container-recovery" aria-label="运行记录与恢复">
    <div className="container-recovery-heading"><h3><Clock3 size={17} aria-hidden="true" />运行记录与恢复</h3>
      {report?.host_booted_at && <span>服务器启动于 {formatDate(report.host_booted_at)}</span>}
    </div>
    {stop ? <div className="container-stop-reason" data-confidence={stop.confidence}>
      <TriangleAlert size={18} aria-hidden="true" /><div><strong>{running ? "上次停止：" : ""}{stop.title}</strong><p>{stop.description}</p>
        <div className="container-stop-facts"><span>{formatDate(stop.at)}</span><span>退出码 {stop.exit_code ?? "未记录"}</span>
          <span>OOM {stop.oom_killed === true ? "已记录" : stop.oom_killed === false ? "未记录到" : "未知"}</span>
          <span>{stop.confidence === "confirmed" ? "有直接证据" : stop.confidence === "correlated" ? "关联线索" : "原因待确认"}</span></div></div>
    </div> : <p className="form-help">{running ? "暂无已记录的停止事件。" : "尚无足够的停止证据；刷新详情后查看最新记录。"}</p>}
    <div className="container-quick-actions">
      <button type="button" className="secondary-button" onClick={onLogs}>查看运行日志</button>
      <button type="button" className="secondary-button" disabled={!!busy} onClick={() => void download()}><Download size={14} aria-hidden="true" />{busy === "download" ? "正在导出…" : "下载诊断记录"}</button>
      {!running && <><button type="button" className="secondary-button" onClick={onConfigure}>检查资源配置</button>
        <button type="button" className="primary-button" disabled={!canStart || !!busy} onClick={() => void start()}><Play size={14} aria-hidden="true" />{busy === "start" ? "正在启动…" : "检查并启动容器"}</button></>}
    </div>
    {!running && <p className="form-help">启动前会重新校验权限和配置容量。重启容器不会自动恢复中断的训练，请按项目的 checkpoint 继续任务。</p>}
    {error && <p className="create-error" role="alert">{error}</p>}{message && <p className="form-help" role="status">{message}</p>}
    {!!report?.events.length && <details className="container-lifecycle-events"><summary>事件时间线 · {report.events.length} 条</summary>
      <ol>{report.events.map(event => <li key={event.id}><time dateTime={event.at}>{formatDate(event.at)}</time><div><strong>{event.title}</strong><p>{event.description}</p></div></li>)}</ol>
    </details>}
  </section>;
}
