"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Download, RefreshCw, RotateCcw, ShieldCheck } from "lucide-react";
import { authenticatedFetch } from "../lib/authenticatedFetch";
import { AccessibleDialog } from "./AccessibleDialog";

type Operation = { id: string; action: string; state: "idle" | "running" | "succeeded" | "failed" | "rolled_back" | "recovery_required";
  phase: string; message: string; target_version: string; updated_at: string; rollback_available: boolean; rollback_id: string | null; failure_code?: string | null };
type Manager = { available: boolean; current_version?: string; executor: { online: boolean }; prepared: { id: string; version: string; sha256: string; expires_at: string } | null; operation: Operation | null; message?: string };
type UpdateStatus = {
  current_version: string; latest_version?: string; repository: string;
  channel: string; status: "unconfigured" | "available" | "current" | "error" | "no_release";
  message: string; release_url?: string; cached?: boolean; manager?: Manager;
};
type Intent = { action: "prepare" | "apply" | "rollback"; id: string; target: string; version?: string };
const labels = { unconfigured: "尚未配置", available: "发现新版本", current: "当前通道暂无更新", error: "检查未完成", no_release: "暂无发布版本" };
const phases: Record<string, string> = { preparing: "校验并准备发布包", prepared: "准备完成", building: "构建新版本", maintenance: "等待控制面进入维护", backing_up: "保存升级前恢复点", migrating: "迁移控制数据库", starting: "启动新版本服务", health_check: "检查新版本健康状态", committing: "确认升级结果", complete: "操作结束", rolling_back: "正在恢复旧版本", recovering: "正在恢复控制面" };
const states = { idle: "等待操作", running: "正在执行", succeeded: "操作成功", failed: "操作失败", rolled_back: "已恢复升级前版本", recovery_required: "需要人工恢复" };
const failureMessages: Record<string, string> = { control_plane_drain_timed_out: "控制面仍有任务执行，未能在规定时间内进入维护。", insufficient_upgrade_disk_space: "升级所需磁盘空间不足。", prepared_release_expired: "准备包已过期，请重新准备。", release_health_check_failed: "新版本未通过健康检查。", automatic_rollback_failed: "自动回退未完成，请联系主机维护人员。", automatic_recovery_failed: "自动恢复未完成，请联系主机维护人员。", rollback_failed: "回退未完成，请联系主机维护人员。", interrupted_before_maintenance: "操作在进入维护前被中断。" };
const titles = { prepare: "准备升级版本", apply: "确认在线升级", rollback: "确认回退版本" };
const pendingKey = "gsm-system-update-request";

class RequestError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}

async function jsonRequest<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await authenticatedFetch(url, init);
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) {
      const payload = await response.json().catch(() => null);
      throw new RequestError(typeof payload?.detail === "string" ? payload.detail : "登录或管理员验证已失效，请重新登录后查看升级状态。", response.status);
    }
    const payload = await response.json().catch(() => null);
    throw new RequestError(typeof payload?.detail === "string" ? payload.detail : "连接暂时中断。正在重新检查升级状态，请勿重复提交。", response.status);
  }
  return response.json();
}

export function UpdateSettingsPanel() {
  const [result, setResult] = useState<UpdateStatus | null>(null);
  const [manager, setManager] = useState<Manager | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [connection, setConnection] = useState("");
  const [intent, setIntent] = useState<Intent | null>(null);
  const [totp, setTotp] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [pending, setPending] = useState("");
  const [statusCheckedAt, setStatusCheckedAt] = useState(0);
  const alive = useRef(true);
  const clearPending = useCallback(() => {
    setPending("");
    try { sessionStorage.removeItem(pendingKey); } catch { /* Optional persistence. */ }
  }, []);
  const applyStatus = useCallback((value: Manager) => {
    if (!alive.current) return;
    if (value.available) {
      setManager(value); setConnection(""); setStatusCheckedAt(Date.now());
      if (value.operation) {
        let stored = "";
        try { stored = sessionStorage.getItem(pendingKey) ?? ""; } catch { /* Storage may be unavailable. */ }
        if (value.operation.id === stored) clearPending();
      }
    } else {
      setManager(current => current?.operation?.state === "running" ? current : value);
      setConnection("主机升级服务暂时未连接，正在重新检查。升级期间请保留此页，不要重复提交。");
    }
  }, [clearPending]);
  const refreshStatus = useCallback(async () => {
    try { applyStatus(await jsonRequest<Manager>("/api/gsm/admin/updates/status", { cache: "no-store" })); }
    catch (e) { if (alive.current) setConnection(e instanceof Error ? e.message : "连接暂时中断，正在重新检查。"); }
  }, [applyStatus]);
  const refresh = useCallback(async () => {
    setBusy(true); setError("");
    try {
      const value = await jsonRequest<UpdateStatus>("/api/gsm/admin/updates", { cache: "no-store" });
      if (alive.current) { setResult(value); if (value.manager) applyStatus(value.manager); }
    } catch (e) { if (alive.current) setError(e instanceof Error ? e.message : "检查失败，请稍后重试。"); }
    finally { if (alive.current) setBusy(false); }
  }, [applyStatus]);
  useEffect(() => {
    alive.current = true;
    try { setPending(sessionStorage.getItem(pendingKey) ?? ""); } catch { /* Optional persistence. */ }
    void refresh();
    let timer: ReturnType<typeof setTimeout>;
    let stopped = false;
    async function poll() { await refreshStatus(); if (!stopped) timer = setTimeout(poll, 5000); }
    timer = setTimeout(poll, 5000);
    return () => { alive.current = false; stopped = true; clearTimeout(timer); };
  }, [refresh, refreshStatus]);
  const operation = manager?.operation;
  const running = operation?.state === "running";
  const connected = manager?.available && manager.executor.online && !connection;
  const prepared = manager?.prepared;
  const latestInstalled = !!result?.latest_version && result.latest_version === manager?.current_version;
  const ready = prepared && Date.parse(prepared.expires_at) > statusCheckedAt;
  const locked = busy || !!running || !!pending || !connected || operation?.state === "recovery_required";
  const phrase = intent?.action === "apply" ? `UPGRADE ${intent.version}` : intent?.action === "rollback" ? `ROLLBACK ${intent.target}` : "";
  function open(action: Intent["action"], target: string, version?: string) {
    setIntent({ action, target, version, id: crypto.randomUUID().replaceAll("-", "") });
    setTotp(""); setConfirmation(""); setAcknowledged(false); setError("");
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!intent) return;
    setBusy(true); setError("");
    const body = { request_id: intent.id, totp_code: totp,
      ...(intent.action === "prepare" ? { version: intent.target } : intent.action === "apply" ? { prepared_id: intent.target, confirmation }
        : { transaction_id: intent.target, confirmation, acknowledge_data_loss: acknowledged }) };
    try {
      try { sessionStorage.setItem(pendingKey, intent.id); } catch { /* Optional persistence. */ }
      setPending(intent.id);
      const accepted = await jsonRequest<{ operation: Operation }>(`/api/gsm/admin/updates/${intent.action}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (alive.current) {
        setManager(current => current ? { ...current, operation: accepted.operation } : current);
        clearPending(); setIntent(null); setTotp("");
      }
      await refreshStatus();
    } catch (e) {
      if (alive.current) {
        setError(e instanceof Error ? e.message : "未能确认提交结果，请先检查状态。"); setTotp("");
        if (e instanceof RequestError && e.status >= 400 && e.status < 500 && e.status !== 408) clearPending();
        else setIntent(null);
      }
      // Do not replay a possibly accepted host mutation after a broken connection.
      await refreshStatus();
    } finally { if (alive.current) setBusy(false); }
  }
  return <section className="update-settings" aria-label="版本与更新">
    <div className="panel-heading"><div><h2>版本与更新</h2><p>检查发布版本，准备后在维护期间升级控制面。</p></div>
      <button className="secondary-button" disabled={busy} onClick={() => void refresh()}><RefreshCw size={15} />{busy ? "正在处理…" : "检查更新"}</button>
    </div>
    {error && !intent && <p role="alert" className="create-error">{error}</p>}
    {result && <>
      <dl className="update-details"><div><dt>当前版本</dt><dd>{manager?.current_version || result.current_version}</dd></div><div><dt>更新通道</dt><dd>{result.channel === "prerelease" ? "预发布（含候选版本）" : "稳定版"}</dd></div>
        <div><dt>发布仓库</dt><dd>{result.repository || "待配置"}</dd></div>{result.latest_version && <div><dt>最新发布</dt><dd>{result.latest_version}</dd></div>}</dl>
      <p role={result.status === "error" ? "alert" : "status"}><strong>{latestInstalled ? "该发布版本已安装" : labels[result.status]}</strong> · {latestInstalled ? "主机已运行此版本，可查看升级任务确认结果。" : result.message}</p>
      {result.release_url && <a className="secondary-button" href={result.release_url} target="_blank" rel="noreferrer"><Download size={15} />查看发布说明与下载</a>}
      {result.cached && <p className="form-help">版本检查显示最近 5 分钟内的结果；升级进度每 5 秒刷新。</p>}
    </>}
    {connection && <p role="status">{connection}</p>}
    {pending && <div><p role="status">正在确认操作 {pending} 的状态。请勿重复提交。</p>
      {connected && !running && !busy && <><p className="form-help">若上方操作 ID 与此请求不同，请先核对任务结果。确认没有任务正在执行后，可解除等待；这不会重新发送先前的请求。</p><button className="secondary-button" onClick={clearPending}>已核对任务状态，解除等待</button></>}
    </div>}
    {manager && <section className="settings-storage-note"><div><strong><ShieldCheck size={15} /> 主机升级服务 · {connected ? "已连接" : "未连接"}</strong>
      <p>先准备并校验发布包；确认升级后自动备份控制数据库、迁移并检查服务。迁移或健康检查失败时尝试回退，无法恢复时保留维护状态。</p>
      <p>升级期间面板、微信和打印管理会短暂不可用。个人文件和用户容器数据不作为控制面升级的替换内容。</p>
    </div></section>}
    {prepared && <dl className="update-details" aria-label="已准备的版本"><div><dt>准备版本</dt><dd>{prepared.version}</dd></div><div><dt>有效期</dt><dd>{new Date(prepared.expires_at).toLocaleString("zh-CN")}{!ready && "（已过期，请重新准备）"}</dd></div><div><dt>SHA-256</dt><dd><code>{prepared.sha256}</code></dd></div></dl>}
    {operation && <section className="settings-storage-note" aria-label="升级任务"><div><strong>{states[operation.state]} · {phases[operation.phase] ?? "正在核对状态"}</strong><p>{failureMessages[operation.message] ?? phases[operation.message] ?? operation.message}</p><p>目标 {operation.target_version || "—"} · 操作 ID {operation.id}</p>
      {operation.failure_code && <p>未完成原因：{failureMessages[operation.failure_code] ?? "主机执行器检测到错误"}（{operation.failure_code}）</p>}
      {operation.state === "recovery_required" && <p role="alert">自动恢复未完成，请联系主机维护人员。不要继续升级或手动删除恢复材料。</p>}
    </div></section>}
    <div className="container-quick-actions">
      {result?.status === "available" && result.latest_version && !latestInstalled && <button className="secondary-button" disabled={locked} onClick={() => open("prepare", result.latest_version!)}><Download size={15} />准备新版本</button>}
      {prepared && <button className="primary-button" disabled={locked || !ready || operation?.state === "recovery_required"} onClick={() => open("apply", prepared.id, prepared.version)}>升级到 {prepared.version}</button>}
      {operation?.rollback_available && operation.rollback_id && <button className="secondary-button" disabled={locked} onClick={() => open("rollback", operation.rollback_id!)}><RotateCcw size={15} />回退到升级前</button>}
    </div>
    <p className="form-help">稳定版通道不接收预发布。未部署主机升级服务时仍可检查发布说明，由维护人员按部署手册操作。</p>
    {intent && <AccessibleDialog title={titles[intent.action]} onClose={() => { if (!busy) { setIntent(null); setTotp(""); } }} destructiveBusy={busy}>
      <form onSubmit={submit}>
        <p>{intent.action === "prepare" ? `准备版本 ${intent.target}，不会立即切换运行中的服务。` : intent.action === "apply" ? `将升级到 ${intent.version}。请确认团队已知晓维护安排，升级器会保存恢复点后执行迁移。` : "回退会恢复升级前的代码、配置和控制数据库。之后新增或修改的管理记录不会保留；个人文件不随控制数据库回退。"}</p>
        {phrase && <><p>请输入 <code>{phrase}</code> 确认本次操作。</p><label>确认短语<input value={confirmation} onChange={e => setConfirmation(e.target.value)} autoComplete="off" spellCheck={false} /></label></>}
        {intent.action === "rollback" && <label><input type="checkbox" checked={acknowledged} onChange={e => setAcknowledged(e.target.checked)} />我已了解升级后的管理记录会被恢复点覆盖</label>}
        <label>动态验证码<input inputMode="numeric" pattern="[0-9]{6}" maxLength={6} autoComplete="one-time-code" value={totp} onChange={e => setTotp(e.target.value)} required /></label>
        <p className="form-help">使用当前管理员验证器的 6 位验证码，无需重复输入管理员密码。每组验证码只能用于一次升级操作；准备完成后，请等待验证器显示下一组验证码再确认升级。</p>
        {error && <p role="alert" className="create-error">{error}</p>}
        <div className="dialog-actions"><button type="button" className="secondary-button" disabled={busy} onClick={() => { setIntent(null); setTotp(""); }}>取消</button><button className="primary-button" disabled={busy || !!pending || !/^\d{6}$/.test(totp) || (!!phrase && confirmation !== phrase) || (intent.action === "rollback" && !acknowledged)}>{busy ? "正在提交…" : "验证并提交"}</button></div>
      </form>
    </AccessibleDialog>}
  </section>;
}
