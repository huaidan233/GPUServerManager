export type GpuCardProps = {
  serverId?: string;
  serverName?: string;
  index: number;
  user?: string;
  container?: string;
  memoryUsedGb: number;
  memoryTotalGb: number;
  utilization: number;
  sharedBy?: string[];
  state: "free" | "shareable" | "busy" | "exclusive" | "unknown";
  processCount: number;
  declaredContainerCount: number;
  freshness: string;
};

const stateLabels = {
  free: "空闲",
  shareable: "可共享",
  busy: "繁忙",
  exclusive: "独占模式",
  unknown: "未知",
} as const;

export function GpuCard({
  serverName,
  index,
  user,
  container,
  memoryUsedGb,
  memoryTotalGb,
  utilization,
  sharedBy = [],
  state,
  processCount,
  declaredContainerCount,
  freshness,
}: GpuCardProps) {
  const percent = memoryTotalGb > 0 ? Math.round((memoryUsedGb / memoryTotalGb) * 100) : 0;
  const cappedPercent = Math.min(Math.max(percent, 0), 100);

  return (
    <article className={`gpu-card ${state}`} aria-label={`${serverName ? `${serverName} ` : ""}GPU ${index} ${stateLabels[state]}`}>
      <div className="gpu-title">
        <strong>{serverName ? `${serverName} · ` : ""}GPU {index}</strong>
        <span className="gpu-state">{stateLabels[state]}</span>
      </div>
      <div className="gpu-user">{user ?? "无声明用户"}</div>
      <div className="gpu-container">{container ?? "无声明容器"}</div>
      <div
        className="meter"
        role="meter"
        aria-label={`GPU ${index} 显存使用`}
        aria-valuemin={0}
        aria-valuemax={memoryTotalGb}
        aria-valuenow={memoryUsedGb}
        aria-valuetext={`${memoryUsedGb} / ${memoryTotalGb} GB (${percent}%)`}
      >
        <span style={{ width: `${cappedPercent}%` }} />
      </div>
      <div className="gpu-meta">
        <span>{memoryUsedGb} / {memoryTotalGb} GB</span>
        <span>{utilization}%</span>
      </div>
      <div className="gpu-counts">
        <span>进程 {processCount}</span>
        <span>声明容器 {declaredContainerCount}</span>
        <span>{freshness === "fresh" ? "数据新鲜" : "数据过期"}</span>
        {sharedBy.length > 1 && <span>共享用户 {sharedBy.length}</span>}
      </div>
    </article>
  );
}
