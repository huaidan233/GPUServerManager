"use client";

import { useEffect, useId, useRef, useState } from "react";
import { Activity, ArrowUpRight, Clock3 } from "lucide-react";
import { authenticatedFetch } from "../lib/authenticatedFetch";

type Point = { at: string; utilization: number | null; memory_used_mb: number | null; memory_total_mb: number | null };
type Series = { gpu_uuid: string; index: number; latest_observed_at?: string | null; latest_sample?: Point | null; points: Point[] };
type History = { start: string; end: string; series: Series[] };

const PLOT = { left: 40, top: 18, bottom: 174, width: 600, height: 208 };
const RANGES = [{ minutes: 60, label: "1 小时" }, { minutes: 360, label: "6 小时" }, { minutes: 1440, label: "24 小时" }];
const xAt = (index: number, count: number, width: number = PLOT.width) => PLOT.left + index / Math.max(1, count - 1) * (width - 16 - PLOT.left);
const yAt = (value: number) => PLOT.bottom - Math.min(100, Math.max(0, value)) / 100 * (PLOT.bottom - PLOT.top);
const memoryPercent = (p: Point) => p.memory_used_mb !== null && p.memory_total_mb ? p.memory_used_mb / p.memory_total_mb * 100 : null;
const percent = (value: number | null | undefined) => value == null ? "—" : Number(value.toFixed(1)).toString();
const timeLabel = (at: string | number, date = false) => new Date(at).toLocaleString("zh-CN", {
  ...(date ? { month: "2-digit", day: "2-digit" } as const : {}), hour: "2-digit", minute: "2-digit", hour12: false,
});

export function chartSegments(points: Point[], value: (p: Point) => number | null, width: number = PLOT.width): string[] {
  const segments: string[] = [];
  let segment: string[] = [];
  points.forEach((point, index) => {
    const y = value(point);
    if (y === null || !Number.isFinite(y)) {
      if (segment.length) segments.push(segment.join(" "));
      segment = [];
      return;
    }
    segment.push(`${xAt(index, points.length, width)},${yAt(y)}`);
  });
  if (segment.length) segments.push(segment.join(" "));
  return segments;
}

function GpuPlot({ gpu, start, end, sampleNow, selectedAt, onSelect }: {
  gpu: Series; start: string; end: string; sampleNow: number; selectedAt: number | null; onSelect: (at: number | null) => void;
}) {
  const gradientId = useId();
  const plotRef = useRef<HTMLDivElement>(null);
  const [plotWidth, setPlotWidth] = useState<number>(PLOT.width);
  useEffect(() => {
    const element = plotRef.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry.contentRect.width > 0) setPlotWidth(entry.contentRect.width);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const values = gpu.points.flatMap(point => point.utilization === null ? [] : [point.utilization]);
  const average = values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
  const peak = values.length ? Math.max(...values) : null;
  const lastIndex = gpu.points.findLastIndex(point => point.utilization !== null);
  const selectedIndex = selectedAt === null ? -1 : gpu.points.reduce((best, point, index) =>
    Math.abs(new Date(point.at).getTime() - selectedAt) < Math.abs(new Date(gpu.points[best].at).getTime() - selectedAt) ? index : best, 0);
  const rawLatest = gpu.latest_sample;
  const latestIndex = rawLatest && gpu.points.length ? gpu.points.reduce((best, point, index) =>
    Math.abs(Date.parse(point.at) - Date.parse(rawLatest.at)) < Math.abs(Date.parse(gpu.points[best].at) - Date.parse(rawLatest.at)) ? index : best, 0) : lastIndex;
  const activeIndex = selectedAt === null ? latestIndex : selectedIndex;
  const active = selectedAt === null && rawLatest ? rawLatest : gpu.points[activeIndex];
  const latest = gpu.points[lastIndex];
  const latestAt = gpu.latest_observed_at === undefined ? latest?.at : gpu.latest_observed_at;
  const stale = !latestAt || !Number.isFinite(Date.parse(latestAt)) || sampleNow - Date.parse(latestAt) > 180_000;
  const utilizationSegments = chartSegments(gpu.points, point => point.utilization, plotWidth);
  const memorySegments = chartSegments(gpu.points, memoryPercent, plotWidth);
  const selectIndex = (index: number) => {
    const point = gpu.points[Math.min(gpu.points.length - 1, Math.max(0, index))];
    if (point) onSelect(new Date(point.at).getTime());
  };
  const inspectPosition = (clientX: number, element: HTMLDivElement) => {
    const rect = element.getBoundingClientRect();
    if (!rect.width) return;
    const x = (clientX - rect.left) / rect.width * plotWidth;
    selectIndex(Math.round((x - PLOT.left) / (plotWidth - 16 - PLOT.left) * (gpu.points.length - 1)));
  };

  return <figure className="gpu-history-card">
    <figcaption className="gpu-history-card-heading">
      <h4><span className="gpu-history-device">GPU</span> {String(gpu.index).padStart(2, "0")}</h4>
      <span className="gpu-history-sample" data-stale={selectedAt === null && stale}>
        {selectedAt !== null ? "选定时刻" : stale ? "历史采样" : "最近采样"}
        {(selectedAt !== null ? active?.at : latestAt) && <time dateTime={selectedAt !== null ? active?.at : latestAt || undefined}>{timeLabel(selectedAt !== null ? active!.at : latestAt!, true)}</time>}
      </span>
    </figcaption>
    <div className="gpu-history-readout">
      <div className="gpu-history-primary">
        <span className="gpu-history-metric-label">计算利用率</span>
        <strong>{percent(active?.utilization)}<small>%</small></strong>
      </div>
      <div className="gpu-history-memory">
        <span className="gpu-history-metric-label">显存占用</span>
        <strong>{active?.memory_used_mb == null ? "—" : (active.memory_used_mb / 1024).toFixed(2)}<small>GiB</small></strong>
        <span className="gpu-history-capacity">/ {active?.memory_total_mb == null ? "—" : Number((active.memory_total_mb / 1024).toFixed(1))} GiB</span>
      </div>
    </div>
    <div ref={plotRef} className="gpu-history-plot" role="slider" tabIndex={gpu.points.length ? 0 : -1}
      aria-label={`GPU ${gpu.index} 历史采样时间`} aria-valuemin={0} aria-valuemax={Math.max(0, gpu.points.length - 1)}
      aria-valuenow={Math.max(0, activeIndex)} aria-disabled={!gpu.points.length}
      aria-valuetext={active ? `${timeLabel(active.at, true)}，计算利用率 ${active.utilization === null ? "无采样" : `${percent(active.utilization)}%`}，显存 ${active.memory_used_mb === null ? "无采样" : `${(active.memory_used_mb / 1024).toFixed(2)} GiB`}` : "无采样"}
      aria-describedby={`${gradientId}-hint`}
      onPointerMove={event => inspectPosition(event.clientX, event.currentTarget)}
      onClick={event => inspectPosition(event.clientX, event.currentTarget)}
      onPointerLeave={event => { if (document.activeElement !== event.currentTarget) onSelect(null); }}
      onFocus={() => selectIndex(Math.max(0, lastIndex))} onBlur={() => onSelect(null)}
      onKeyDown={event => {
        if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "Escape"].includes(event.key)) {
          event.preventDefault();
          if (event.key === "Escape") onSelect(null);
          else if (event.key === "Home") selectIndex(0);
          else if (event.key === "End") selectIndex(gpu.points.length - 1);
          else selectIndex(Math.max(0, activeIndex) + (["ArrowRight", "ArrowUp"].includes(event.key) ? 1 : -1));
        }
      }}>
      <svg viewBox={`0 0 ${plotWidth} ${PLOT.height}`} role="img" aria-label={`GPU ${gpu.index} 计算利用率与显存占用历史曲线`}>
        <defs><linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--history-compute)" stopOpacity="0.16" />
          <stop offset="100%" stopColor="var(--history-compute)" stopOpacity="0.015" />
        </linearGradient></defs>
        {[0, 25, 50, 75, 100].map(value => <g key={value} className="gpu-history-gridline">
          <line x1={PLOT.left} x2={plotWidth - 16} y1={yAt(value)} y2={yAt(value)} />
          <text x={PLOT.left - 10} y={yAt(value) + 4} textAnchor="end">{value}</text>
        </g>)}
        {utilizationSegments.map((points, i) => {
          const vertices = points.split(" ");
          return <g key={i}>
            {vertices.length > 1 && <polygon points={`${vertices[0].split(",")[0]},${PLOT.bottom} ${points} ${vertices.at(-1)!.split(",")[0]},${PLOT.bottom}`} fill={`url(#${gradientId})`} />}
            <polyline className="gpu-history-compute-line" points={points} />
            {vertices.length === 1 && <circle cx={vertices[0].split(",")[0]} cy={vertices[0].split(",")[1]} r="3" fill="var(--history-compute)" />}
          </g>;
        })}
        {memorySegments.map((points, i) => <g key={i}>
          <polyline className="gpu-history-memory-line" points={points} />
          {!points.includes(" ") && <circle cx={points.split(",")[0]} cy={points.split(",")[1]} r="3" fill="var(--history-memory)" />}
        </g>)}
        {active && (selectedAt !== null || !rawLatest) && <g>
          {selectedAt !== null && <line className="gpu-history-cursor" x1={xAt(activeIndex, gpu.points.length, plotWidth)} x2={xAt(activeIndex, gpu.points.length, plotWidth)} y1={PLOT.top} y2={PLOT.bottom} />}
          {active.utilization !== null && <circle className="gpu-history-point" cx={xAt(activeIndex, gpu.points.length, plotWidth)} cy={yAt(active.utilization)} r="4" fill="var(--history-compute)" />}
          {memoryPercent(active) !== null && <circle className="gpu-history-point" cx={xAt(activeIndex, gpu.points.length, plotWidth)} cy={yAt(memoryPercent(active)!)} r="4" fill="var(--history-memory)" />}
        </g>}
        {(plotWidth < 330 ? [0, 0.5, 1] : [0, 0.25, 0.5, 0.75, 1]).map(fraction => <text className="gpu-history-time" key={fraction}
          x={PLOT.left + fraction * (plotWidth - 16 - PLOT.left)} y="199" textAnchor={fraction === 0 ? "start" : fraction === 1 ? "end" : "middle"}>
          {timeLabel(new Date(start).getTime() + fraction * (new Date(end).getTime() - new Date(start).getTime()))}
        </text>)}
      </svg>
      {!values.length && <span className="gpu-history-no-samples">此时间段暂无采样</span>}
      {!!values.length && selectedAt !== null && active?.utilization === null && <span className="gpu-history-no-samples">此时刻无采样</span>}
    </div>
    <div className="gpu-history-card-footer">
      <span>区间均值 <strong>{percent(average)}<small>%</small></strong></span>
      <span><ArrowUpRight size={13} aria-hidden="true" />采样峰值 <strong>{percent(peak)}<small>%</small></strong></span>
    </div>
    <span id={`${gradientId}-hint`} className="gpu-history-sr-only">移动指针或按左右方向键查看采样，各张图的时间同步。按 Escape 返回最近采样。</span>
  </figure>;
}

export function GpuHistoryChart({ serverId, serverName }: { serverId: string; serverName: string }) {
  const [minutes, setMinutes] = useState(60);
  const [data, setData] = useState<History | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [selectedAt, setSelectedAt] = useState<number | null>(null);
  const [receivedAt, setReceivedAt] = useState(0);
  const [clock, setClock] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let busy = false;
    setData(null); setError(""); setSelectedAt(null);
    async function refresh() {
      setClock(Date.now());
      if (busy || document.visibilityState === "hidden") return;
      busy = true;
      try {
        const response = await authenticatedFetch(`/api/gsm/servers/${encodeURIComponent(serverId)}/gpu-history?minutes=${minutes}`, { signal: controller.signal });
        if (!response.ok) throw new Error("历史采样暂时无法加载，请重试。");
        const result: History = await response.json();
        if (!Array.isArray(result.series)) throw new Error("历史采样暂时无法加载，请重试。");
        if (!controller.signal.aborted) { setData(result); setReceivedAt(Date.now()); setError(""); }
      } catch (e) { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "读取曲线失败，请重试。"); }
      finally { busy = false; }
    }
    void refresh();
    const timer = window.setInterval(() => void refresh(), 30000);
    document.addEventListener("visibilitychange", refresh);
    return () => { controller.abort(); window.clearInterval(timer); document.removeEventListener("visibilitychange", refresh); };
  }, [serverId, minutes, retry]);

  return <section className="gpu-history" aria-label={`${serverName} GPU 使用曲线`}>
    <div className="gpu-history-heading">
      <div className="gpu-history-title"><Activity size={19} aria-hidden="true" /><div><h3>GPU 使用趋势</h3><p>{serverName}</p></div></div>
      <div className="gpu-history-ranges" role="group" aria-label={`${serverName} 曲线时间范围`}>
        {RANGES.map(range => <button type="button" key={range.minutes} aria-pressed={range.minutes === minutes} onClick={() => setMinutes(range.minutes)}>{range.label}</button>)}
      </div>
    </div>
    <div className="gpu-history-toolbar">
      <div className="gpu-history-legend"><span><i />计算利用率</span><span><i />显存占用率</span><span className="gpu-history-unit">单位 %</span></div>
      <span className="gpu-history-refresh"><Clock3 size={12} aria-hidden="true" />每 30 秒更新</span>
    </div>
    {error && <div className="gpu-history-error" role="alert"><span>{error}{data ? " 当前展示上次成功读取的数据。" : ""}</span><button className="secondary-button" type="button" onClick={() => setRetry(value => value + 1)}>重试</button></div>}
    {!data && !error && <div className="gpu-history-placeholder" role="status"><Activity size={24} aria-hidden="true" /><span>正在加载使用曲线…</span></div>}
    {data?.series.length === 0 && <div className="gpu-history-placeholder"><Activity size={24} aria-hidden="true" /><strong>暂无历史采样</strong><span>采集到 GPU 数据后，曲线会自动显示。</span></div>}
    {data && data.series.length > 0 && <>
      <div className="gpu-history-grid">{data.series.map(gpu => <GpuPlot key={gpu.gpu_uuid} gpu={gpu} start={data.start} end={data.end} sampleNow={Date.parse(data.end) + Math.max(0, clock - receivedAt)} selectedAt={selectedAt} onSelect={setSelectedAt} />)}</div>
      <div className="gpu-history-footnote"><span>曲线为分段均值 · 空白表示无采样</span><span>移动指针或轻触图表查看时刻</span></div>
    </>}
  </section>;
}
