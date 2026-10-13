import { RefreshCw } from "lucide-react";

import type { GpuSummary } from "../lib/mockData";
import { GpuCard } from "./GpuCard";
import { GpuHistoryChart } from "./GpuHistoryChart";
import { ResourceMonitor } from "./ResourceMonitor";

type Props = {
  gpus: GpuSummary[];
  serverCount: number;
  runningCount: number;
  refreshing: boolean;
  updatedAt?: string;
  onRefresh: () => void;
  historyServers?: Array<{ id: string; name: string }>;
};

const stateLabels = {
  free: "空闲",
  shareable: "可共享",
  busy: "繁忙",
  exclusive: "独占",
  unknown: "未知",
} as const;

export function ResourceUsageSummary({
  gpus,
  serverCount,
  runningCount,
  refreshing,
  updatedAt,
  onRefresh,
  historyServers = [],
}: Props) {
  return (
    <section className="resource-usage-summary" aria-label="资源概览">
      <div className="resource-usage-heading">
        <div>
          <h2>资源占用</h2>
          <p>{serverCount} 台服务器 · GPU 观测 {gpus.length} 张 · {runningCount} 个运行中容器</p>
        </div>
        <div className="resource-refresh">
          <span aria-live="polite">{updatedAt ? `最近刷新 ${updatedAt}` : ""}</span>
          <button className="secondary-button" type="button" aria-label="刷新资源" disabled={refreshing} onClick={onRefresh}>
            <RefreshCw size={15} aria-hidden="true" />
            {refreshing ? "刷新中" : "刷新"}
          </button>
        </div>
      </div>
      {gpus.length > 0 ? (
        <>
          <ul className="gpu-usage-strip" aria-label="GPU 占用速览">
            {gpus.map((gpu) => {
              const fresh = gpu.freshness === "fresh" && gpu.state !== "unknown";
              const utilization = Math.min(100, Math.max(0, gpu.utilization));
              return (
                <li key={`${gpu.serverId}:${gpu.index}`} className="gpu-usage-chip" data-state={fresh ? gpu.state : "unknown"}>
                  <div><strong>GPU {gpu.index}</strong><span>{fresh ? stateLabels[gpu.state] : "未知"}</span></div>
                  <small>{fresh ? `${utilization}% · ${gpu.memoryUsedGb}/${gpu.memoryTotalGb} GB` : "暂无有效观测"}</small>
                  <span className="gpu-usage-meter" aria-hidden="true"><span style={{ width: fresh ? `${utilization}%` : "0%" }} /></span>
                </li>
              );
            })}
          </ul>
          <details className="resource-gpu-details">
            <summary>查看 GPU 详情</summary>
            <div className="gpu-grid">{gpus.map((gpu) => <GpuCard key={`${gpu.serverId}:${gpu.index}`} {...gpu} />)}</div>
          </details>
        </>
      ) : (
        <p className="resource-observation-empty"><strong>暂无 GPU 观测</strong> · 检查 Server Agent 连接与 NVIDIA 运行时；无观测不代表 GPU 空闲。</p>
      )}
      {historyServers.map(server => <div key={server.id}>
        <ResourceMonitor serverId={server.id} serverName={server.name} />
        <GpuHistoryChart serverId={server.id} serverName={server.name} />
      </div>)}
    </section>
  );
}
