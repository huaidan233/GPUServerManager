"use client";
import { useEffect, useState } from "react";
import { featureRequest } from "./PersonalQuotaPanel";
type Agent = { id: string; display_name: string; status: string; last_seen_at?: string; version?: string };
type Printer = { id: string; display_name: string; status: string; agent_id?: string };
export function PrintDeploymentPanel() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [printers, setPrinters] = useState<Printer[]>([]);
  const [name, setName] = useState("");
  const [id, setId] = useState("lab-printer");
  const [credential, setCredential] = useState<{ agent_id: string; token: string } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [checkedAt, setCheckedAt] = useState(0);
  const [origin, setOrigin] = useState("");
  async function load() { const value = await featureRequest<{ agents: Agent[]; printers: Printer[] }>("print-admin/agents"); setAgents(value.agents); setPrinters(value.printers); setCheckedAt(Date.now()); }
  useEffect(() => { setOrigin(window.location.origin); void load().catch(e => setError(e.message)); }, []);
  async function action(kind: "create" | "rotate" | "revoke", agentId = id) {
    setBusy(true); setError("");
    try { const value = await featureRequest<{ agent_id: string; token?: string }>(kind === "create" ? "print-admin/agents" : `print-admin/agents/${agentId}/${kind}`, "POST", kind === "create" ? { id, display_name: name } : {}); setCredential(value.token ? { agent_id: value.agent_id, token: value.token } : null); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : "操作失败"); } finally { setBusy(false); }
  }
  const command = credential ? `.\\scripts\\deploy\\install_print_agent_windows.ps1 -CentralUrl '${origin}' -AgentId '${credential.agent_id}' -AgentToken '${credential.token}' -RendererPath 'C:\\Program Files\\SumatraPDF\\SumatraPDF.exe'` : "";
  return <section className="panel"><div className="panel-heading"><div><h2>打印机部署与设置</h2><p>在连接打印机的 Windows 电脑上安装 Print Agent，该电脑主动连接管理服务。</p></div><button className="secondary-button" disabled={busy} onClick={() => void load().catch(e => setError(e.message))}>刷新状态</button></div>
    <form className="quota-form" onSubmit={e => { e.preventDefault(); void action("create"); }}><label>电脑标识<input required pattern="[A-Za-z0-9][A-Za-z0-9._-]*" maxLength={128} value={id} onChange={e => setId(e.target.value)} /></label><label>显示名称<input required value={name} onChange={e => setName(e.target.value)} placeholder="实验室打印电脑" /></label><button className="primary-button" disabled={busy}>添加打印电脑</button></form>
    {error && <p role="alert" className="create-error">{error}</p>}
    <p><a download href="/api/gsm/print-admin/installer" className="secondary-button">下载 Windows 部署包</a></p>
    <ol><li>确认 Windows 本机可以正常打印，安装 Python 3.10+ 和 SumatraPDF。</li><li>解压部署包，以管理员身份打开 PowerShell，进入解压目录。</li><li>运行下面的安装命令；安装后刷新本页，检查心跳和打印机状态。</li></ol>
    {credential && <div className="container-notice"><strong>本次连接凭据</strong><p>复制到打印电脑执行；更新凭据后需要同步更新该电脑配置。</p><pre className="quota-request-purpose">{command}</pre><button className="secondary-button" onClick={() => void navigator.clipboard.writeText(command).catch(() => setError("复制失败，请手动复制命令。"))}>复制安装命令</button></div>}
    <div className="table-wrap"><table><thead><tr><th>打印电脑</th><th>最近心跳</th><th>状态</th><th>操作</th></tr></thead><tbody>{agents.map(a => <tr key={a.id}><td>{a.display_name}<small> · {a.id}</small></td><td>{a.last_seen_at ? new Date(a.last_seen_at).toLocaleString("zh-CN") : "尚未连接"}</td><td>{a.status === "revoked" ? "已禁用" : a.last_seen_at && checkedAt - Date.parse(a.last_seen_at) < 180000 ? "在线" : "离线"}</td><td><button className="secondary-button" disabled={busy} onClick={() => void action("rotate", a.id)}>更新连接凭据</button> <button className="secondary-button" disabled={busy || a.status === "revoked"} onClick={() => void action("revoke", a.id)}>禁用</button></td></tr>)}</tbody></table></div>
    <h3>已发现打印机</h3>{printers.length === 0 ? <p className="empty-state">暂无打印机。Agent 安装成功后会自动发现 Windows 本机打印机。</p> : <ul>{printers.map(p => <li key={p.id}>{p.display_name} · {p.status}</li>)}</ul>}
  </section>;
}
