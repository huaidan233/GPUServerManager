"use client";

import { AlertCircle, CheckCircle2, FileSearch, RefreshCw, Stethoscope } from "lucide-react";
import { useMemo, useState } from "react";

import { authenticatedFetch } from "../lib/authenticatedFetch";
import type {
  ActorSummary,
  ContainerSummary,
  HealthEventSummary,
  HealthStatus,
  HealthSummary,
} from "../lib/mockData";

type Props = {
  actor: ActorSummary;
  health: HealthSummary[];
  events: HealthEventSummary[];
  containers: ContainerSummary[];
};

const statusLabels: Record<HealthStatus, string> = {
  ok: "正常",
  degraded: "降级",
  failed: "故障",
  unknown: "未知",
};

type DiagnosticReport = {
  id: string;
  source: "rules" | "model";
  findings: Array<{
    code: string;
    cause: string;
    confidence: number;
    suggestions?: string[];
  }>;
};

export function HealthOperationsPanel({ actor, health, events, containers }: Props) {
  const [tab, setTab] = useState<"health" | "diagnostics">("health");
  const [expandedEvent, setExpandedEvent] = useState<string | null>(null);
  const [reports, setReports] = useState<Record<string, DiagnosticReport>>({});
  const [busyContainer, setBusyContainer] = useState("");
  const [error, setError] = useState("");

  const visibleContainers = useMemo(
    () =>
      actor.role === "admin"
        ? containers
        : containers.filter((container) => container.user === actor.userId),
    [actor.role, actor.userId, containers],
  );

  async function diagnoseContainer(container: ContainerSummary) {
    setBusyContainer(container.id);
    setError("");
    try {
      const response = await authenticatedFetch(`/api/gsm/containers/${container.id}/diagnostics`, { method: "POST" });
      const result = await response.json();
      if (!response.ok) {
        throw new Error(String(result.detail ?? "诊断失败"));
      }
      setReports((current) => ({ ...current, [container.id]: result as DiagnosticReport }));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "诊断失败");
    } finally {
      setBusyContainer("");
    }
  }

  return (
    <section className="operations-section" aria-labelledby="health-operations-heading">
      <div className="panel-heading operations-heading">
        <div>
          <h2 id="health-operations-heading">服务状态</h2>
          <p>查看服务运行情况，展开异常或诊断容器。</p>
        </div>
        <div className="operations-tabs" role="tablist" aria-label="运维视图">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "health"}
            onClick={() => setTab("health")}
          >
            <CheckCircle2 size={16} aria-hidden="true" />
            健康
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "diagnostics"}
            onClick={() => setTab("diagnostics")}
          >
            <Stethoscope size={16} aria-hidden="true" />
            诊断
          </button>
        </div>
      </div>

      {error && <p className="form-help create-error" role="alert">{error}</p>}

      {tab === "health" ? (
        <div role="tabpanel" aria-label="健康">
          <div className="table-wrap">
            <table aria-label="服务器健康状态">
              <thead><tr><th>服务器</th><th>状态</th><th>警告</th><th>严重</th><th>观测</th></tr></thead>
              <tbody>
                {health.length === 0 ? (
                  <tr><td colSpan={5}>暂无健康数据</td></tr>
                ) : health.map((item) => (
                  <tr key={item.serverId}>
                    <td>{item.serverId}</td>
                    <td>
                      <span className={`health-status health-${item.status}`}>
                        {item.status === "failed" ? <AlertCircle size={15} aria-hidden="true" /> : <CheckCircle2 size={15} aria-hidden="true" />}
                        {statusLabels[item.status]}
                      </span>
                    </td>
                    <td>{item.warningCount}</td>
                    <td>{item.criticalCount}</td>
                    <td>{item.stale ? "数据过期" : "数据新鲜"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {events.length > 0 && (
            <div className="health-events" aria-label="健康事件">
              <h3>事件</h3>
              {events.map((event) => (
                <div className="health-event-row" key={event.id}>
                  <span className={`event-marker event-${event.status}`}>{event.status === "recovered" ? "已恢复" : "处理中"}</span>
                  <span>{event.serverId} · {event.code === "runtime_storage_low" ? "运行环境存储容量预警" : event.code === "runtime_storage_unavailable" ? "运行环境存储无法检查" : event.code}</span>
                  {actor.role === "admin" && (
                    <button
                      className="icon-button"
                      type="button"
                      aria-label={`查看原始证据 ${event.id}`}
                      title="查看原始证据"
                      onClick={() => setExpandedEvent((current) => current === event.id ? null : event.id)}
                    >
                      <FileSearch size={16} aria-hidden="true" />
                    </button>
                  )}
                  {expandedEvent === event.id && actor.role === "admin" && (
                    <pre className="health-evidence">{JSON.stringify(event.evidence ?? {}, null, 2)}</pre>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      ) : (
        <div role="tabpanel" aria-label="诊断">
          <div className="diagnostic-list">
            {visibleContainers.length === 0 ? (
              <p className="empty-state">暂无可诊断容器</p>
            ) : visibleContainers.map((container) => {
              const report = reports[container.id];
              const finding = report?.findings?.[0];
              return (
                <div className="diagnostic-row" key={container.id}>
                  <div>
                    <strong>{container.name}</strong>
                    <span>{container.server} · {container.statusText}</span>
                  </div>
                  <button
                    className="secondary-button"
                    type="button"
                    aria-label={`诊断 ${container.name}`}
                    disabled={busyContainer === container.id}
                    onClick={() => diagnoseContainer(container)}
                  >
                    <RefreshCw size={16} aria-hidden="true" />
                    {busyContainer === container.id ? "诊断中" : "诊断"}
                  </button>
                  {report && finding && (
                    <div className="diagnostic-result">
                      <span className="diagnostic-source">{report.source === "rules" ? "规则" : "模型"}</span>
                      <strong>{finding.cause}</strong>
                      <span>{Math.round(finding.confidence * 100)}%</span>
                      <span className="mono-cell">{report.id}</span>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </section>
  );
}
