"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { AccessibleDialog } from "./AccessibleDialog";
import { featureRequest } from "./PersonalQuotaPanel";

type Entry = { name: string; kind: string; size: number; modified_at: number };
type Storage = { total_bytes: number; available_bytes: number; disk_used_bytes: number; data_root: string; container_path: string };
type Sftp = { configured: boolean; username?: string; password?: string; public_keys: string[]; host: string; port: number };
function sizeText(value: number) { return value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(1)} GiB` : value >= 1024 ** 2 ? `${(value / 1024 ** 2).toFixed(1)} MiB` : `${(value / 1024).toFixed(1)} KiB`; }
export function PersonalDataPanel({ enabled = true }: { enabled?: boolean }) {
  const [path, setPath] = useState("");
  const [entries, setEntries] = useState<Entry[]>([]);
  const [storage, setStorage] = useState<Storage | null>(null);
  const [sftp, setSftp] = useState<Sftp | null>(null);
  const [password, setPassword] = useState("");
  const [keys, setKeys] = useState("");
  const [renaming, setRenaming] = useState<Entry | null>(null);
  const [newName, setNewName] = useState("");
  const [renameError, setRenameError] = useState("");
  const [folder, setFolder] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState("");
  const [progress, setProgress] = useState(0);
  const cancel = useRef(false);
  const join = (name: string) => path ? `${path}/${name}` : name;
  const load = useCallback(async () => {
    const [listing, disk] = await Promise.all([featureRequest<{ entries: Entry[] }>("data/list", "POST", { path }), featureRequest<Storage>("data/status")]);
    setEntries(listing.entries); setStorage(disk);
  }, [path]);
  useEffect(() => { if (enabled) void load().catch(e => setError(e.message)); }, [enabled, load]);
  useEffect(() => { if (enabled) featureRequest<Sftp>("data-sftp").then(value => { setSftp(value); setKeys(value.public_keys.join("\n")); }).catch(e => setError(e.message)); }, [enabled]);
  async function upload(file?: File) {
    if (!file) return;
    if (file.size > 100 * 1024 ** 2) { setError("网页上传上限为 100 MiB，大文件请使用下方 SFTP 连接。"); return; }
    setBusy(`上传 ${file.name}`); setError(""); setMessage(""); setProgress(0); cancel.current = false;
    let uploadId: string | undefined;
    try {
      const started = await featureRequest<{ upload_id: string }>("data/begin", "POST", { path: join(file.name), size: file.size }); uploadId = started.upload_id;
      for (let offset = 0; offset < file.size; offset += 256 * 1024) {
        if (cancel.current) throw new Error("已取消上传。");
        const bytes = new Uint8Array(await file.slice(offset, offset + 256 * 1024).arrayBuffer());
        let text = ""; for (const byte of bytes) text += String.fromCharCode(byte);
        await featureRequest("data/chunk", "POST", { upload_id: uploadId, offset, data: btoa(text) });
        setProgress(Math.round(Math.min(file.size, offset + bytes.length) / Math.max(1, file.size) * 100));
      }
      if (cancel.current) throw new Error("已取消上传。");
      const result = await featureRequest<{ container_path: string }>("data/finish", "POST", { upload_id: uploadId }); uploadId = undefined;
      setMessage(`上传完成，容器中访问：${result.container_path}`); await load();
    } catch (e) { setError(e instanceof Error ? e.message : "上传失败"); }
    finally { if (uploadId) await featureRequest("data/cancel", "POST", { upload_id: uploadId }).catch(() => {}); setBusy(""); }
  }
  async function download(entry: Entry) {
    setBusy(`下载 ${entry.name}`); setError(""); setProgress(0);
    try {
      const parts: Uint8Array<ArrayBuffer>[] = []; let offset = 0; let eof = false; let version: string | undefined;
      while (!eof) {
        const value = await featureRequest<{ data: string; size: number; eof: boolean; version: string }>("data/read", "POST", { path: join(entry.name), offset, version });
        version = value.version;
        const bytes = Uint8Array.from(atob(value.data), c => c.charCodeAt(0)); parts.push(bytes); offset += bytes.length; eof = value.eof;
        if (!bytes.length && !eof) throw new Error("文件在下载过程中发生变化，请使用 SFTP 重试。");
        setProgress(Math.round(offset / Math.max(1, value.size) * 100));
      }
      const url = URL.createObjectURL(new Blob(parts)); const link = document.createElement("a"); link.href = url; link.download = entry.name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 30000);
    } catch (e) { setError(e instanceof Error ? e.message : "下载失败"); } finally { setBusy(""); }
  }
  async function mkdir() {
    setBusy("创建目录"); setError("");
    try { await featureRequest("data/mkdir", "POST", { path: join(folder) }); setFolder(""); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : "创建失败"); } finally { setBusy(""); }
  }
  async function rename() {
    if (!renaming) return;
    setBusy("重命名"); setRenameError("");
    try { await featureRequest("data/rename", "POST", { path: join(renaming.name), new_name: newName }); setRenaming(null); setMessage("名称已更新。"); await load(); }
    catch (e) { setRenameError(e instanceof Error ? e.message : "重命名失败"); } finally { setBusy(""); }
  }
  async function saveSftp() {
    setBusy("保存 SFTP 连接"); setError("");
    try { const value = await featureRequest<Sftp>("data-sftp", "PUT", { ...(password ? { password } : {}), public_keys: keys.split("\n").map(s => s.trim()).filter(Boolean) }); setSftp(value); setPassword(""); setMessage("SFTP 连接已就绪。上传目录与容器 /workspace 相同。"); }
    catch (e) { setError(e instanceof Error ? e.message : "保存失败"); } finally { setBusy(""); }
  }
  async function copy(value: string) { try { await navigator.clipboard.writeText(value); setMessage(`已复制 ${value}`); } catch { setError("复制失败，请手动复制路径。"); } }
  return <div className="quota-workspace">
    <section className="panel"><div className="panel-heading"><div><h2>我的数据</h2><p>每人独立的私有目录，其他成员不可访问。同一账号的容器从 /workspace 使用这些文件，停止容器后仍可上传。</p></div><button className="secondary-button" disabled={!!busy || !enabled} onClick={() => void load().catch(e => setError(e.message))}>刷新</button></div>
      {storage && <div className="container-usage-compact"><span>数据盘总容量 {sizeText(storage.total_bytes)}</span><span>全盘可用 {sizeText(storage.available_bytes)}</span><span>我的独立目录 {storage.data_root}</span></div>}
      <div className="container-detail-toolbar"><div className="container-quick-actions"><button className="secondary-button" disabled={!path || !!busy} onClick={() => setPath(path.split("/").slice(0, -1).join("/"))}>上一级</button><code>/workspace{path && `/${path}`}</code><button className="secondary-button" onClick={() => void copy(`/workspace${path && `/${path}`}`)}>复制路径</button></div>
        <label className="secondary-button">上传文件<input type="file" disabled={!!busy || !enabled} onChange={e => { void upload(e.target.files?.[0]); e.target.value = ""; }} /></label></div>
      <form className="quota-review-actions" onSubmit={e => { e.preventDefault(); void mkdir(); }}><label>新建目录<input required pattern="[^/\\\\]+" placeholder="例如 datasets" value={folder} onChange={e => setFolder(e.target.value)} /></label><button className="secondary-button" disabled={!!busy || !enabled}>创建目录</button></form>
      {busy && <p role="status">{busy} · {progress}% {busy.startsWith("上传") && <button className="secondary-button" onClick={() => { cancel.current = true; }}>取消上传</button>}</p>}
      {error && <p role="alert" className="create-error">{error}</p>}{message && <p role="status" className="table-message">{message}</p>}
      <div className="table-wrap"><table><thead><tr><th>名称</th><th>大小</th><th>修改时间</th><th>操作</th></tr></thead><tbody>{entries.map(e => <tr key={e.name}><td>{e.kind === "directory" ? <button className="container-name-button" disabled={!!busy} onClick={() => setPath(join(e.name))}>{e.name}/</button> : e.name}</td><td>{e.kind === "file" ? sizeText(e.size) : "—"}</td><td>{new Date(e.modified_at * 1000).toLocaleString("zh-CN")}</td><td><button className="secondary-button" onClick={() => void copy(`/workspace/${join(e.name)}`)}>复制路径</button> {e.kind === "file" && <button className="secondary-button" disabled={!!busy || e.size > 100 * 1024 ** 2} onClick={() => void download(e)}>下载</button>}{e.kind !== "unsupported" && <button className="secondary-button" aria-label={`重命名 ${e.name}`} disabled={!!busy || !enabled} onClick={() => { setRenaming(e); setNewName(e.name); setRenameError(""); }}>重命名</button>}{e.kind === "unsupported" && <small>不支持的文件类型</small>}</td></tr>)}</tbody></table></div>
      {!entries.length && <p className="empty-state">目录为空，上传数据或创建目录后即可在容器中使用。</p>}
      <p className="form-help">网页支持 100 MiB 以内的文件，文件重名时不会覆盖。可新建子目录和重命名；大文件、移动和删除可使用 SFTP。</p>
    </section>
    <section className="panel"><div className="panel-heading"><div><h2>SFTP 数据连接</h2><p>使用 Xftp、WinSCP 或 FileZilla 上传大文件，无需启动容器。</p></div></div>
      {!sftp?.configured ? <p className="empty-state">管理员尚未部署独立 SFTP 服务。</p> : <><form className="quota-form" onSubmit={e => { e.preventDefault(); void saveSftp(); }}><label>SFTP 独立密码<input type="password" autoComplete="new-password" minLength={12} maxLength={128} value={password} onChange={e => setPassword(e.target.value)} placeholder={sftp.username ? "留空保留当前密码" : "至少 12 位"} /></label><label className="quota-form-wide">SSH 公钥（可选，每行一条）<textarea value={keys} onChange={e => setKeys(e.target.value)} placeholder="ssh-ed25519 AAAA…" /></label><button className="primary-button" disabled={!!busy}>保存并启用 SFTP</button></form>
        {sftp.username && <dl className="container-facts"><div><dt>主机 / 端口</dt><dd>{sftp.host} : {sftp.port}</dd></div><div><dt>用户名</dt><dd><code>{sftp.username}</code></dd></div><div><dt>密码</dt><dd><code>{sftp.password ?? "仅使用 SSH 公钥"}</code></dd></div><div><dt>连接命令</dt><dd><code>sftp -P {sftp.port} {sftp.username}@{sftp.host}</code></dd></div><div><dt>路径对应</dt><dd>SFTP 根目录 / → 容器 /workspace</dd></div></dl>}</>}
    </section>
    {renaming && <AccessibleDialog title="重命名" onClose={() => setRenaming(null)} destructiveBusy={!!busy}>
      <p className="form-help">仅修改你自己目录中的名称，不覆盖同名文件。</p>
      <form className="quota-form" onSubmit={e => { e.preventDefault(); void rename(); }}><label className="quota-form-wide">新名称<input required maxLength={255} pattern="[^/\\\\]+" value={newName} onChange={e => setNewName(e.target.value)} /></label>
        {renameError && <p className="quota-form-wide create-error" role="alert">{renameError}</p>}
        <button className="primary-button" disabled={!!busy || newName === renaming.name}>保存名称</button></form>
    </AccessibleDialog>}
  </div>;
}
