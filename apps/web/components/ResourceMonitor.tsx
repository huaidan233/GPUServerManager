"use client";

import { useEffect, useId, useRef, useState } from "react";
import { Activity, Cpu, HardDrive, MemoryStick } from "lucide-react";
import { authenticatedFetch } from "../lib/authenticatedFetch";
import "./ResourceMonitor.css";

type Point = { at: string; cpu_percent: number | null; cpu_used_cores: number | null; cpu_limit_cores: number | null;
  cpu_total_cores: number | null; memory_used_bytes: number | null; memory_total_bytes: number | null;
  disk_read_bps: number | null; disk_write_bps: number | null };
type DiskPoint = { at: string; used_bytes: number | null; total_bytes: number | null; free_bytes: number | null; read_bps: number | null; write_bps: number | null };
type Disk = DiskPoint & { id: string; label: string; labels: string[]; locations?: Array<{ path: string; purpose: string }>; points: DiskPoint[] };
type History = { start: string; end: string; scope: "server" | "container"; stale: boolean;
  latest: (Point & { observed_at: string; warnings?: string[]; disks?: Disk[] }) | null; points: Point[]; disks: Disk[];
  quota?: { cpu_limit_cores: number | null; memory_limit_bytes: number | null } };
type Line = { name: string; color: string; values: Array<number | null>; dashed?: boolean };
const GIB = 1024 ** 3;
const MIB = 1024 ** 2;
const RANGES = [{ minutes: 60, label: "1 小时" }, { minutes: 360, label: "6 小时" }, { minutes: 1440, label: "24 小时" }];
const number = (value: number | null | undefined, places = 1) => value == null || !Number.isFinite(value) ? "—" : Number(value.toFixed(places)).toString();
const time = (value: string | number) => new Date(value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
const gib = (value: number | null | undefined) => value == null ? null : value / GIB;

export function resourceSegments(values: Array<number | null>, width: number, maximum: number) {
  const groups: string[][] = [[]];
  values.forEach((value, index) => {
    if (value === null || !Number.isFinite(value)) { if (groups.at(-1)!.length) groups.push([]); return; }
    groups.at(-1)!.push(`${42 + index / Math.max(1, values.length - 1) * (width - 60)},${144 - Math.min(maximum, Math.max(0, value)) / maximum * 126}`);
  });
  return groups.filter(group => group.length).map(group => group.join(" "));
}

function Trend({ title, unit, maximum, lines, times, selected, onSelect }: {
  title: string; unit: string; maximum: number; lines: Line[]; times: string[];
  selected: number | null; onSelect: (value: number | null) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const id = useId();
  const [width, setWidth] = useState(480);
  useEffect(() => {
    if (!ref.current || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => { if (entry.contentRect.width > 0) setWidth(entry.contentRect.width); });
    observer.observe(ref.current); return () => observer.disconnect();
  }, []);
  const last = times.findLastIndex((_, i) => lines.some(line => line.values[i] != null));
  const active = selected === null ? last : selected;
  const select = (value: number) => onSelect(Math.max(0, Math.min(times.length - 1, value)));
  const inspection = active < 0 ? "暂无采样" : `${time(times[active])}，${lines.map(line => `${line.name} ${line.values[active] == null ? "无采样" : number(line.values[active], 2) + unit}`).join("，")}`;
  const empty = !lines.some(line => line.values.some(value => value !== null));
  return <div ref={ref} className="resource-monitor-plot" role="slider" tabIndex={0} aria-label={`${title} 历史采样时间`}
    aria-valuemin={0} aria-valuemax={Math.max(0, times.length - 1)} aria-valuenow={Math.max(0, active)} aria-valuetext={inspection}
    onPointerMove={event => { const box = event.currentTarget.getBoundingClientRect(); if (box.width) select(Math.round(((event.clientX - box.left) / box.width * width - 42) / (width - 60) * (times.length - 1))); }}
    onPointerLeave={() => onSelect(null)} onBlur={() => onSelect(null)}
    onKeyDown={event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End", "Escape"].includes(event.key)) return;
      event.preventDefault();
      if (event.key === "Escape") onSelect(null);
      else select(event.key === "Home" ? 0 : event.key === "End" ? times.length - 1 : Math.max(0, active) + (event.key === "ArrowRight" ? 1 : -1));
    }}>
    <svg viewBox={`0 0 ${width} 176`} role="img" aria-label={`${title} 使用曲线`}>
      <defs>{lines.map((line, i) => <linearGradient key={line.name} id={`${id}-${i}`} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor={line.color} stopOpacity=".12" /><stop offset="100%" stopColor={line.color} stopOpacity=".015" /></linearGradient>)}</defs>
      {[0, .5, 1].map(fraction => <g key={fraction}><line className="resource-monitor-gridline" x1={42} x2={width - 18} y1={144 - fraction * 126} y2={144 - fraction * 126} /><text x={34} y={148 - fraction * 126} textAnchor="end">{number(maximum * fraction, 1)}</text></g>)}
      {lines.map((line, i) => <g key={line.name}>{resourceSegments(line.values, width, maximum).map((points, j) => <g key={j}>
        {!line.dashed && <polygon points={`${points.split(" ")[0].split(",")[0]},144 ${points} ${points.split(" ").at(-1)!.split(",")[0]},144`} fill={`url(#${id}-${i})`} />}
        <polyline points={points} fill="none" stroke={line.color} strokeWidth={2} strokeDasharray={line.dashed ? "5 4" : undefined} strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
        {!points.includes(" ") && <circle cx={points.split(",")[0]} cy={points.split(",")[1]} r={3} fill={line.color} />}
      </g>)}</g>)}
      {selected !== null && <line className="resource-monitor-cursor" x1={42 + selected / Math.max(1, times.length - 1) * (width - 60)} x2={42 + selected / Math.max(1, times.length - 1) * (width - 60)} y1={18} y2={144} />}
      {[0, .5, 1].map(fraction => <text key={fraction} x={42 + fraction * (width - 60)} y={169} textAnchor={fraction === 0 ? "start" : fraction === 1 ? "end" : "middle"}>{times.length > 0 ? time(times[Math.round(fraction * (times.length - 1))]) : ""}</text>)}
    </svg>
    {empty && <span className="resource-monitor-empty-plot">此时间段暂无采样</span>}
    <div className="resource-monitor-inspection"><time>{active < 0 ? "等待采样" : time(times[active])}</time>{lines.map(line => <span key={line.name}><i style={{ background: line.color }} />{line.name} <strong>{number(line.values[active], 2)}<small>{unit}</small></strong></span>)}</div>
  </div>;
}

export function ResourceMonitor({ serverId, serverName, containerId }: { serverId?: string; serverName?: string; containerId?: string }) {
  const [minutes, setMinutes] = useState(60);
  const [data, setData] = useState<History | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [selected, setSelected] = useState<number | null>(null);
  const [receivedAt, setReceivedAt] = useState(0);
  const [clock, setClock] = useState(0);
  const container = !!containerId;
  const name = container ? "容器资源" : serverName || "服务器资源";
  useEffect(() => {
    const abort = new AbortController();
    let busy = false;
    setData(null); setError(""); setSelected(null);
    async function refresh() {
      setClock(Date.now());
      if (busy || document.visibilityState === "hidden") return;
      busy = true;
      try {
        const url = containerId ? `containers/${encodeURIComponent(containerId)}` : `servers/${encodeURIComponent(serverId || "")}`;
        const response = await authenticatedFetch(`/api/gsm/${url}/resource-history?minutes=${minutes}`, { signal: abort.signal });
        if (!response.ok) throw new Error("资源历史暂时无法加载，请重试。");
        const result: History = await response.json();
        if (!Array.isArray(result.points) || !Array.isArray(result.disks)) throw new Error("资源历史暂时无法加载，请重试。");
        if (!abort.signal.aborted) { setData(result); setReceivedAt(Date.now()); setError(""); }
      } catch (failure) { if (!abort.signal.aborted) setError(failure instanceof Error ? failure.message : "资源历史暂时无法加载，请重试。"); }
      finally { busy = false; }
    }
    void refresh();
    const timer = window.setInterval(() => void refresh(), 30000);
    document.addEventListener("visibilitychange", refresh);
    return () => { abort.abort(); window.clearInterval(timer); document.removeEventListener("visibilitychange", refresh); };
  }, [serverId, containerId, minutes, retry]);
  const sampleAge = data?.latest ? Date.parse(data.end) - Date.parse(data.latest.observed_at) + Math.max(0, clock - receivedAt) : Infinity;
  const stale = !!data?.stale || !Number.isFinite(sampleAge) || sampleAge > 180000;
  const current = selected !== null ? data?.points[selected] : data?.latest;
  const cpu = container ? current?.cpu_used_cores : current?.cpu_percent;
  const cpuLimit = container ? data?.quota?.cpu_limit_cores : current?.cpu_total_cores;
  const cpuValues = data?.points.map(point => container ? point.cpu_used_cores : point.cpu_percent) || [];
  const memoryValues = data?.points.map(point => gib(point.memory_used_bytes)) || [];
  const memoryLimit = container ? data?.quota?.memory_limit_bytes : current?.memory_total_bytes;
  const cpuMax = container ? Math.max(1, cpuLimit || 0, ...cpuValues.map(value => value || 0)) : 100;
  const memoryMax = Math.max(1, gib(memoryLimit) || 0, ...memoryValues.map(value => value || 0));
  const times = data?.points.map(point => point.at) || [];
  return <section className="resource-monitor" aria-label={`${name} CPU 内存和磁盘监控`}>
    <div className="resource-monitor-heading"><div className="resource-monitor-title"><Activity size={19} aria-hidden="true" /><div><h3>{container ? "容器资源趋势" : "CPU、内存与存储"}</h3><p>{container ? "实际占用与分配额度分开查看" : serverName}</p></div></div>
      <div className="resource-monitor-ranges" role="group" aria-label={`${name} 资源时间范围`}>{RANGES.map(range => <button type="button" key={range.minutes} aria-pressed={range.minutes === minutes} onClick={() => setMinutes(range.minutes)}>{range.label}</button>)}</div>
    </div>
    {error && <div className="resource-monitor-alert" role="alert"><span>{error}{data ? " 当前保留上次读取的数据。" : ""}</span><button type="button" className="secondary-button" onClick={() => setRetry(value => value + 1)}>重试</button></div>}
    {!data && !error && <p className="resource-monitor-placeholder" role="status">正在读取资源采样…</p>}
    {data && !data.latest && <p className="resource-monitor-placeholder">暂无历史采样。开始采集后，曲线会自动显示。</p>}
    {data?.latest && <>
      <div className="resource-monitor-status"><span data-stale={stale}>{stale ? "采样已过期" : "最近采样"} · {time(data.latest.observed_at)}</span><span>30 秒刷新 · 保留 7 天</span></div>
      {stale && <p className="resource-monitor-note">当前显示历史记录，不能代表此刻的资源占用。恢复采集后会自动更新。</p>}
      <div className="resource-monitor-grid">
        <article className="resource-monitor-card"><header><span><Cpu size={15} aria-hidden="true" />CPU</span><small>{container ? `分配 ${number(cpuLimit)} 核` : `${number(cpuLimit, 0)} 逻辑核心`}</small></header>
          <div className="resource-monitor-value"><strong>{number(cpu, container ? 2 : 1)}</strong><span>{container ? "核使用中" : "%"}</span><small>{selected === null ? "最近采样" : "选定时刻"}</small></div>
          <Trend title="CPU" unit={container ? " 核" : "%"} maximum={cpuMax} lines={[{ name: "实际使用", color: "#3b5ccc", values: cpuValues }]} times={times} selected={selected} onSelect={setSelected} />
        </article>
        <article className="resource-monitor-card"><header><span><MemoryStick size={15} aria-hidden="true" />内存</span><small>{container ? "分配" : "物理总量"} {number(gib(memoryLimit))} GiB</small></header>
          <div className="resource-monitor-value"><strong>{number(gib(current?.memory_used_bytes), 2)}</strong><span>GiB 使用中</span><small>{container ? "不含可回收缓存" : "总量 − 可用量"}</small></div>
          <Trend title="内存" unit=" GiB" maximum={memoryMax} lines={[{ name: "实际使用", color: "#22806b", values: memoryValues }]} times={times} selected={selected} onSelect={setSelected} />
        </article>
      </div>
      {!container && data.disks.length > 0 && <div className="resource-monitor-disks">{data.disks.map(disk => {
        const latest = data.latest?.disks?.find(value => value.id === disk.id);
        const active = selected === null ? latest : disk.points[selected];
        const used = active?.used_bytes; const total = active?.total_bytes;
        const fullness = used != null && total ? used / total * 100 : null;
        const reads = disk.points.map(point => point.read_bps == null ? null : point.read_bps / MIB);
        const writes = disk.points.map(point => point.write_bps == null ? null : point.write_bps / MIB);
        return <article key={disk.id} className="resource-monitor-card resource-monitor-disk"><header><span><HardDrive size={15} aria-hidden="true" />{disk.labels?.join(" / ") || disk.label}</span><small>{fullness == null ? "空间未知" : `${number(fullness)}% 已用`}</small></header>
          {disk.locations?.map(location => <p key={location.path} className="resource-monitor-note"><code>{location.path}</code> · {location.purpose || "此位置所在文件系统的总用量"}</p>)}
          <div className="resource-monitor-storage"><strong>{number(gib(used))}<small> / {number(gib(total))} GiB</small></strong><span>可用 {number(gib(active?.free_bytes))} GiB</span></div>
          <div className="resource-monitor-capacity" aria-label={`磁盘已用 ${number(fullness)}%`}><i style={{ width: `${fullness == null ? 0 : Math.min(100, fullness)}%` }} data-warning={fullness !== null && fullness > 90} /></div>
          <Trend title={`${disk.label} 磁盘 I/O`} unit=" MiB/s" maximum={Math.max(1, ...[...reads, ...writes].map(value => value || 0))} lines={[{ name: "读取", color: "#3b5ccc", values: reads }, { name: "写入", color: "#bc7b23", values: writes, dashed: true }]} times={disk.points.map(point => point.at)} selected={selected} onSelect={setSelected} />
        </article>;
      })}</div>}
      {!container && (!data.latest.disks?.length || data.latest.warnings?.length) && <p className="resource-monitor-note">部分资源尚未取得采样，空白不代表空闲。磁盘仅统计已配置的存储位置；同一文件系统合并显示。</p>}
      <div className="resource-monitor-footnote"><span>曲线为分段均值 · 空白表示无采样{container ? " · 配额调整不会改写过去的实际用量" : " · 磁盘 I/O 为对应设备的读写速率"}</span><span>移动指针或方向键查看同一时刻</span></div>
    </>}
  </section>;
}
