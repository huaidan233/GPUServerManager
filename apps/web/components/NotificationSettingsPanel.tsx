"use client";
import { useEffect, useState } from "react";
import { featureRequest } from "./PersonalQuotaPanel";

type Settings = { configured: boolean; enabled: boolean; daily_limit: number; key_hint: string };
type Notice = { id: string; text: string; created_at: string; status: string };

export function NotificationSettingsPanel() {
  const [settings, setSettings] = useState<Settings>({ configured: false, enabled: false, daily_limit: 5, key_hint: "" });
  const [key, setKey] = useState("");
  const [notices, setNotices] = useState<Notice[]>([]);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  async function load() { const [value, inbox] = await Promise.all([featureRequest<Settings>("notifications/settings"), featureRequest<Notice[]>("notifications")]); setSettings(value); setNotices(inbox); }
  useEffect(() => { void load().catch(e => setError(e.message)); }, []);
  async function act(action: "save" | "test" | "remove") {
    setBusy(true); setError(""); setMessage("");
    try {
      if (action === "save") await featureRequest("notifications/settings", "PUT", { enabled: settings.enabled, daily_limit: settings.daily_limit, ...(key ? { sendkey: key.trim() } : {}) });
      if (action === "test") await featureRequest("notifications/test", "POST", {});
      if (action === "remove") await featureRequest("notifications/settings", "DELETE");
      setKey(""); await load(); setMessage(action === "test" ? "测试通知已提交，请在微信中查看。" : action === "remove" ? "已解除 Server酱绑定。" : "通知设置已保存。");
    } catch (e) { setError(e instanceof Error ? e.message : "操作失败"); } finally { setBusy(false); }
  }
  return <section className="panel"><div className="panel-heading"><div><h2>通知与微信提醒</h2><p>接收 GPU 空闲、额度审批和到期提醒。站内始终保留通知记录。</p></div><button className="secondary-button" disabled={busy} onClick={() => void load().catch(e => setError(e.message))}>刷新</button></div>
    <form className="quota-form" onSubmit={e => { e.preventDefault(); void act("save"); }}>
      <label className="quota-form-wide">Server酱 SendKey<input type="password" autoComplete="off" placeholder={settings.configured ? `${settings.key_hint}（已保存，留空保留）` : "SCT…"} value={key} onChange={e => setKey(e.target.value)} /></label>
      <label>每日最多发送条数<input type="number" min={1} max={1000} required value={settings.daily_limit} onChange={e => setSettings({ ...settings, daily_limit: Number(e.target.value) })} /></label>
      <label><span>启用 Server酱</span><input type="checkbox" checked={settings.enabled} onChange={e => setSettings({ ...settings, enabled: e.target.checked })} /></label>
      <button className="primary-button" disabled={busy}>保存通知设置</button>
    </form>
    <p className="form-help"><a href="https://sct.ftqq.com/" target="_blank" rel="noreferrer">获取 SendKey 并设置接收通道</a>。免费版每天最多 5 条；订阅用户可按套餐调整限额。消息正文会经 Server酱发送。</p>
    <div className="container-quick-actions"><button className="secondary-button" disabled={busy || !settings.configured || !settings.enabled} onClick={() => void act("test")}>发送测试通知</button><button className="secondary-button" disabled={busy || !settings.configured} onClick={() => void act("remove")}>解除绑定</button></div>
    {error && <p role="alert" className="create-error">{error}</p>}{message && <p role="status">{message}</p>}
    <h3>最近通知</h3>{notices.length === 0 && <p className="empty-state">暂无通知。</p>}
    <div className="quota-request-list">{notices.map(notice => <article key={notice.id} className="quota-request"><small>{new Date(notice.created_at).toLocaleString("zh-CN")}</small><p className="quota-request-purpose">{notice.text}</p><small className="form-help">{({ pending: "待推送", dispatching: "推送中", sent: "已处理", unknown: "推送结果待核实", cancelled: "已取消" } as Record<string, string>)[notice.status] ?? notice.status}</small></article>)}</div>
  </section>;
}
