"use client";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { featureRequest } from "./PersonalQuotaPanel";

type Subscription = { id: string; gpu_count: number; exact_memory_gb?: number; status: string; expires_at: string; estimated_duration_minutes: number };
type Candidate = { server_id: string; index: number; state: string; memory_free_mb: number };
export function GpuAvailabilityPanel({ servers, enabled = true }: { servers: Array<{ id: string; name: string }>; enabled?: boolean }) {
  const [server, setServer] = useState(servers[0]?.id ?? "");
  const [count, setCount] = useState(1);
  const [memory, setMemory] = useState(8);
  const [duration, setDuration] = useState(120);
  const [records, setRecords] = useState<Subscription[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const demand = { gpu_count: count, memory_tier: "full_card", exact_memory_gb: memory, estimated_duration_minutes: duration };
  const load = useCallback(async () => { setRecords(await featureRequest<Subscription[]>("governance/queue")); }, []);
  useEffect(() => { if (enabled) void load().catch(e => setError(e.message)); }, [enabled, load]);
  async function action(type: "check" | "subscribe" | "withdraw" | "renew", id?: string) {
    setBusy(true); setError(""); setMessage("");
    try {
      if (type === "check") { const value = await featureRequest<{ recommendations: Candidate[] }>("governance/recommendations", "POST", demand); setCandidates(value.recommendations.filter(r => r.server_id === server && r.state === "free")); setMessage("已更新当前可用 GPU；启动时仍会检查实际状态和个人额度。"); }
      if (type === "subscribe") { await featureRequest("governance/queue", "POST", { ...demand, compatible_server_ids: [server], workload_profile: "standard" }); setMessage("已订阅。发现匹配空闲后提醒一次，不预留 GPU。"); }
      if (type === "withdraw") await featureRequest(`governance/queue/${id}`, "DELETE");
      if (type === "renew") { await featureRequest(`governance/queue/${id}/renew`, "POST", {}); setMessage("已重新开启提醒，有效期延长至 24 小时后。"); }
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : "操作失败"); } finally { setBusy(false); }
  }
  return <section className="panel"><div className="panel-heading"><div><h2>GPU 空闲提醒</h2><p>符合条件时发送通知，不锁卡、不保证排到后仍空闲。先启动者先用。</p></div><Link href="/?workspace=account" className="secondary-button">设置微信通知</Link></div>
    <form className="quota-form" onSubmit={e => { e.preventDefault(); void action("subscribe"); }}>
      <label>服务器<select value={server} onChange={e => setServer(e.target.value)}>{servers.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
      <label>需要几张 GPU<input type="number" required min={1} max={64} value={count} onChange={e => setCount(Number(e.target.value))} /></label>
      <label>每张至少显存（GiB）<input type="number" required min={1} value={memory} onChange={e => setMemory(Number(e.target.value))} /></label>
      <label>预计使用时间（分钟）<input type="number" required min={1} value={duration} onChange={e => setDuration(Number(e.target.value))} /></label>
      <button className="secondary-button" type="button" disabled={busy || !enabled} onClick={() => void action("check")}>查看当前空闲</button>
      <button className="primary-button" disabled={busy || !enabled}>订阅空闲提醒</button>
    </form>
    <p className="form-help">提醒有效期 24 小时，可随时取消或续订。使用 GPU 仍须具备有效的个人 GPU 额度。</p>
    {error && <p role="alert" className="create-error">{error}</p>}{message && <p role="status">{message}</p>}
    {candidates.length > 0 && <div className="container-usage-compact">{candidates.map(c => <span key={`${c.server_id}:${c.index}`}>GPU {c.index} · 可用 {(c.memory_free_mb / 1024).toFixed(1)} GiB</span>)}</div>}
    <h3>我的提醒</h3>{records.length === 0 && <p className="empty-state">尚未订阅。填写需求，资源空闲后会通知你。</p>}
    <div className="quota-request-list">{records.map(r => <article className="quota-request" key={r.id}><strong>{r.gpu_count} 张 GPU · {r.exact_memory_gb ?? "不限"} GiB/张</strong><p>{r.status === "notified" ? "已发现匹配资源并生成通知" : "等待匹配空闲资源"} · {new Date(r.expires_at).toLocaleString("zh-CN")} 到期</p><div className="container-quick-actions"><button className="secondary-button" disabled={busy} onClick={() => void action("renew", r.id)}>续订并再次提醒</button><button className="secondary-button" disabled={busy} onClick={() => void action("withdraw", r.id)}>取消提醒</button></div></article>)}</div>
  </section>;
}
