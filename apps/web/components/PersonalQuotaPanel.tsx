"use client";

import "./PersonalQuota.css";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import Link from "next/link";
import { authenticatedFetch } from "../lib/authenticatedFetch";

type Limits = { cpu: number; memory_gb: number; gpus: number };
type Application = { id: string; user_id: string; status: string; purpose: string; limits: Limits;
  starts_at: string; ends_at: string; administrator_id: string | null; teacher_id: string | null;
  reviews: Array<{ stage: number; username: string; decision: string; reason: string; at: string }> };
type Quota = { baseline: Limits; limits: Limits; used: Limits; remaining: Limits; superuser: boolean;
  over_quota: boolean; running_containers: number; grants: Application[] };
type Member = { id: string; username: string; display_name?: string; role: string; status: string; superuser: boolean };
type Policy = { baseline: Limits; administrator_id: string | null; teacher_id: string | null; members: Member[] };
const dimensions: Array<[keyof Limits, string, string]> = [["cpu", "CPU", "核"], ["memory_gb", "内存", "GiB"], ["gpus", "GPU", "张"]];
const statusLabels: Record<string, string> = { pending_admin: "待一级管理员审批", pending_teacher: "待二级教师审批", approved: "已通过", rejected: "已拒绝", expired: "已到期" };

export async function featureRequest<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await authenticatedFetch(`/api/gsm/${path}`, { method,
    ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }) });
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : "操作未完成，请检查输入后重试。");
  return value as T;
}
const dateText = (value: string) => new Date(value).toLocaleString("zh-CN", { hour12: false });
function localTime(date: Date) { return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16); }

export function PersonalQuotaPanel({ compact = false, refreshKey, enabled = true }: { compact?: boolean; refreshKey?: unknown; enabled?: boolean }) {
  const [quota, setQuota] = useState<Quota | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    featureRequest<Quota>("quota/me").then(value => { if (!cancelled) { setQuota(value); setError(""); } })
      .catch(e => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [enabled, refreshKey]);
  return <section className={`quota-summary ${compact ? "is-compact" : ""}`} aria-label="个人运行额度">
    <div className="quota-summary-title"><strong>个人运行额度</strong><span>{quota?.superuser ? "超级用户 · 不限额度" : "仅统计运行中的容器配置"}</span></div>
    {error ? <p role="alert">{error}</p> : !quota ? <p className="form-help">{enabled ? "正在读取额度…" : "登录后显示个人额度"}</p> : <>
      <div className="quota-dimensions">{dimensions.map(([key, label, unit]) => <div key={key} className="quota-dimension">
        <span>{label}</span><strong>{quota.used[key]}<small> / {quota.superuser ? "不限" : quota.limits[key]} {unit}</small></strong>
        {!compact && <><meter min={0} max={Math.max(1, quota.limits[key], quota.used[key])} value={quota.used[key]} aria-label={`${label}已分配`} />
          <small>基础 {quota.baseline[key]} {unit} · {quota.superuser ? "不限" : `剩余 ${quota.remaining[key]} ${unit}`}</small></>}
      </div>)}</div>
      {!compact && quota.grants.map(grant => <p className="form-help" key={grant.id}>临时额度至 {dateText(grant.ends_at)}；到期保留运行中的任务，后续启动按回退后的额度检查。</p>)}
      {quota.over_quota && <p className="quota-over-limit" role="status">当前运行配置超过有效额度。已有任务继续运行；请停止部分容器或申请提额后再启动。</p>}
    </>}
    <Link className="quota-link" href="/?workspace=approvals">申请与额度记录 →</Link>
  </section>;
}

export function QuotaRequestsPanel({ memberId, enabled = true }: { memberId?: string; enabled?: boolean }) {
  const [records, setRecords] = useState<Application[]>([]);
  const [limits, setLimits] = useState<Limits>({ cpu: 8, memory_gb: 8, gpus: 1 });
  const [purpose, setPurpose] = useState("");
  const [starts, setStarts] = useState(() => localTime(new Date()));
  const [ends, setEnds] = useState(() => localTime(new Date(Date.now() + 7 * 86400000)));
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const refresh = useCallback(async () => { setRecords(await featureRequest<Application[]>("quota/requests")); }, []);
  useEffect(() => { if (enabled) void refresh().catch(e => setError(e.message)); }, [enabled, refresh]);
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy("apply"); setError(""); setMessage("");
    try {
      const result = await featureRequest<Application>("quota/requests", "POST", { limits, purpose, starts_at: new Date(starts).toISOString(), ends_at: new Date(ends).toISOString() });
      setMessage(result.status === "approved" ? "未配置审批人，申请已自动通过；额度按申请时间生效。" : `已提交，${statusLabels[result.status]}。`); setPurpose(""); await refresh();
    } catch (e) { setError(e instanceof Error ? e.message : "申请失败"); } finally { setBusy(""); }
  }
  async function decide(id: string, decision: string) {
    setBusy(id); setError(""); setMessage("");
    try { await featureRequest(`quota/requests/${id}/decision`, "POST", { decision, reason: reasons[id] }); await refresh(); setMessage("审批结果已保存。"); }
    catch (e) { setError(e instanceof Error ? e.message : "审批失败"); } finally { setBusy(""); }
  }
  return <div className="quota-workspace">
    <PersonalQuotaPanel enabled={enabled} refreshKey={records} />
    <section className="panel quota-application"><div className="panel-heading"><div><h2>申请临时额度</h2><p>填写使用期间需要的总额度。管理员初审 → 教师复审 → 到期回退。未配置审批人的环节自动跳过。</p></div></div>
      <form onSubmit={submit} className="quota-form"><LimitInputs value={limits} onChange={setLimits} />
        <label>开始时间<input type="datetime-local" required value={starts} onChange={e => setStarts(e.target.value)} /></label>
        <label>结束时间<input type="datetime-local" required min={starts} value={ends} onChange={e => setEnds(e.target.value)} /></label>
        <label className="quota-form-wide">用途说明<textarea required minLength={5} maxLength={2000} value={purpose} onChange={e => setPurpose(e.target.value)} placeholder="例如：训练遥感分类模型，预计运行 3 天，需要 1 张 GPU。" /></label>
        <button className="primary-button" disabled={!!busy || !enabled} type="submit">{busy === "apply" ? "正在提交…" : "提交额度申请"}</button>
      </form>
    </section>
    {error && <p className="create-error" role="alert">{error}</p>}{message && <p className="table-message" role="status">{message}</p>}
    <section className="panel"><div className="panel-heading"><div><h2>额度申请记录</h2><p>可查看自己的申请；指定审批人可查看需要处理的申请。</p></div><button className="secondary-button" disabled={!enabled || !!busy} onClick={() => void refresh().catch(e => setError(e.message))}>刷新</button></div>
      {records.length === 0 && <p className="empty-state">暂无申请。基础额度内的容器可直接启动。</p>}
      <div className="quota-request-list">{records.map(record => {
        const canReview = (record.status === "pending_admin" && memberId === record.administrator_id) || (record.status === "pending_teacher" && memberId === record.teacher_id);
        return <article key={record.id} className="quota-request"><div className="quota-request-heading"><strong>{record.user_id}</strong><span className="status-pill">{statusLabels[record.status] ?? record.status}</span></div>
          <p className="quota-request-amount">{record.limits.cpu} 核 CPU / {record.limits.memory_gb} GiB / {record.limits.gpus} 张 GPU</p>
          <p className="quota-request-purpose">{record.purpose}</p><p className="form-help">{dateText(record.starts_at)} — {dateText(record.ends_at)}</p>
          <ol className="quota-review-history">{[1, 2].map(stage => { const review = record.reviews.find(r => r.stage === stage); return <li key={stage} data-complete={!!review}>
            <strong>{stage === 1 ? "一级管理员" : "二级教师"}</strong><span>{review ? `${review.username} · ${review.decision === "approved" ? "通过" : "拒绝"} · ${dateText(review.at)}` : !(stage === 1 ? record.administrator_id : record.teacher_id) ? "未配置 · 已跳过" : ["rejected", "expired"].includes(record.status) ? "申请已结束" : "待处理"}</span>{review && <p>{review.reason}</p>}</li>; })}</ol>
          {canReview && <div className="quota-review-actions"><label>审批说明<input required maxLength={500} value={reasons[record.id] ?? ""} onChange={e => setReasons({ ...reasons, [record.id]: e.target.value })} /></label>
            <button className="primary-button" disabled={!!busy || !reasons[record.id]?.trim()} onClick={() => void decide(record.id, "approved")}>通过本级审批</button>
            <button className="secondary-button" disabled={!!busy || !reasons[record.id]?.trim()} onClick={() => void decide(record.id, "rejected")}>拒绝</button></div>}
        </article>;
      })}</div>
    </section>
  </div>;
}

function LimitInputs({ value, onChange }: { value: Limits; onChange: (value: Limits) => void }) {
  return <>{dimensions.map(([key, label, unit]) => <label key={key}>{label}（{unit}）<input type="number" required min={key === "gpus" ? 0 : 1} step={1} value={value[key]} onChange={e => onChange({ ...value, [key]: Number(e.target.value) })} /></label>)}</>;
}

export function QuotaSettingsPanel({ section = "all" }: { section?: "all" | "rules" | "superusers" }) {
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  useEffect(() => { featureRequest<Policy>("quota/settings").then(setPolicy).catch(e => setError(e.message)); }, []);
  async function save(event: FormEvent) {
    event.preventDefault(); if (!policy) return; setBusy(true); setError("");
    try { await featureRequest("quota/settings", "PUT", { baseline: policy.baseline, administrator_id: policy.administrator_id || null, teacher_id: policy.teacher_id || null }); setMessage("全站配额规则已保存。待审批环节同步使用新的审批人，已完成的审批记录保留。"); }
    catch (e) { setError(e instanceof Error ? e.message : "保存失败"); } finally { setBusy(false); }
  }
  async function toggle(member: Member) {
    setBusy(true); setError("");
    try { await featureRequest(`quota/members/${member.id}`, "PUT", { superuser: !member.superuser }); setPolicy(current => current && ({ ...current, members: current.members.map(m => m.id === member.id ? { ...m, superuser: !m.superuser } : m) })); }
    catch (e) { setError(e instanceof Error ? e.message : "更新失败"); } finally { setBusy(false); }
  }
  const members = (policy?.members ?? []).filter(m => m.status !== "archived" && `${m.username} ${m.display_name ?? ""}`.toLowerCase().includes(search.toLowerCase()));
  const maxPage = Math.max(0, Math.ceil(members.length / 8) - 1);
  const currentPage = Math.min(page, maxPage);
  return <section className="panel"><div className="panel-heading"><div><h2>{section === "superusers" ? "超级用户" : "个人配额与审批设置"}</h2><p>{section === "superusers" ? "独立设置资源权限，不改变成员角色。" : "仅管理员配置，全站共用。留空则跳过该级；两级都留空时自动通过。指定审批人也可审批自己的申请。"}</p></div></div>
    {error && <p className="create-error" role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    {policy && <>{section !== "superusers" && <form className="quota-form" onSubmit={save}><LimitInputs value={policy.baseline} onChange={baseline => setPolicy({ ...policy, baseline })} />
      {([['administrator_id', '一级审批：管理员', 'admin'], ['teacher_id', '二级审批：教师', 'teacher']] as const).map(([key, label, role]) => <label key={key}>{label}<select value={policy[key] ?? ""} onChange={e => setPolicy({ ...policy, [key]: e.target.value })}><option value="">不设置，跳过本级审批</option>{policy.members.filter(m => m.role === role && m.status === "active").map(m => <option key={m.id} value={m.id}>{m.display_name || m.username}（{m.username}）</option>)}</select></label>)}
      <button className="primary-button" disabled={busy}>保存配额规则</button></form>}
      {section !== "rules" && <>{section === "all" && <h3>超级用户</h3>}<p className="form-help">仅豁免个人资源配额和提额审批，不授予管理员权限。单个容器仍受服务器容量限制。</p>
      <label className="settings-search">搜索成员<input type="search" value={search} onChange={e => { setSearch(e.target.value); setPage(0); }} placeholder="姓名或用户名" /></label>
      <div className="table-wrap"><table><thead><tr><th>成员</th><th>角色</th><th>不限个人额度</th></tr></thead><tbody>{members.slice(currentPage * 8, currentPage * 8 + 8).map(m => <tr key={m.id}><td>{m.display_name || m.username}<small> · {m.username}</small></td><td>{m.role}</td><td><input type="checkbox" aria-label={`${m.username}超级用户`} checked={m.superuser} disabled={busy} onChange={() => void toggle(m)} /></td></tr>)}</tbody></table></div>
      {!members.length && <p className="empty-state">没有符合条件的成员。</p>}
      <div className="settings-pagination"><span>共 {members.length} 人 · 第 {currentPage + 1} / {maxPage + 1} 页</span><button className="secondary-button" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>上一页</button><button className="secondary-button" disabled={currentPage === maxPage} onClick={() => setPage(currentPage + 1)}>下一页</button></div></>}
    </>}
  </section>;
}
