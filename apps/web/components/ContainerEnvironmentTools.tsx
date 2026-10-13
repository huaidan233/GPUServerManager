"use client";
import { useEffect, useRef, useState } from "react";
import { containerJson, containerRequest, managementError } from "../lib/containerManagement";
import { downloadRecipe, parseEnvironmentRecipe, type EnvironmentRecipe } from "../lib/environmentRecipe";
import { PythonEnvironmentPicker } from "./PythonEnvironmentPicker";
import { applicationPhases, launchContainerApplication, type LaunchJob } from "../lib/applicationLaunch";

type EnvironmentToolsProps = { containerId: string; running: boolean; onReuse?: (recipe: EnvironmentRecipe) => void };

export function ContainerEnvironmentTools(props: EnvironmentToolsProps) {
  return <ContainerEnvironmentToolsSession key={props.containerId} {...props} />;
}

function ContainerEnvironmentToolsSession({ containerId, running, onReuse }: EnvironmentToolsProps) {
  const [busy, setBusy] = useState(""); const [error, setError] = useState("");
  const [applicationUrl, setApplicationUrl] = useState("");
  const [pythonPath, setPythonPath] = useState("");
  const [job, setJob] = useState<LaunchJob | null>(null);
  const launchController = useRef<AbortController | null>(null);
  const progress = job ? applicationPhases[job.phase] ?? applicationPhases.checking : null;
  useEffect(() => () => { launchController.current?.abort(); }, [containerId, running]);
  const [commands, setCommands] = useState(""); const [notesOpen, setNotesOpen] = useState(false); const [saved, setSaved] = useState(false);
  async function editNotes() {
    setBusy("notes"); setError("");
    try {
      const value = parseEnvironmentRecipe(await containerRequest(`/api/gsm/containers/${encodeURIComponent(containerId)}/environment`));
      setCommands((value.startup_commands ?? []).join("\n")); setNotesOpen(true); setSaved(false);
    } catch (e) { setError(managementError(e)); } finally { setBusy(""); }
  }
  async function saveNotes() {
    setBusy("notes"); setError("");
    try {
      await containerRequest(`/api/gsm/containers/${encodeURIComponent(containerId)}/environment`, { ...containerJson({ startup_commands: commands.split("\n").filter(Boolean) }), method: "PUT" });
      setSaved(true);
    } catch (e) { setError(managementError(e)); } finally { setBusy(""); }
  }
  async function recipe(reuse: boolean) {
    setBusy(reuse ? "reuse" : "export"); setError("");
    try {
      const result = parseEnvironmentRecipe(await containerRequest(`/api/gsm/containers/${encodeURIComponent(containerId)}/environment`));
      if (reuse) onReuse?.(result); else downloadRecipe(result);
    } catch (e) { setError(managementError(e)); } finally { setBusy(""); }
  }
  async function launch(application: string, prepare = false) {
    setBusy(application); setError(""); setApplicationUrl(""); setJob(null);
    const controller = new AbortController(); launchController.current = controller;
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(720000)]);
    // Opening synchronously avoids browser popup blocking while the app starts.
    const tab = window.open("about:blank", "_blank"); if (tab) tab.opener = null;
    try {
      const url = await launchContainerApplication(`/api/gsm/containers/${encodeURIComponent(containerId)}`, { application, ...(pythonPath.trim() && application === "jupyter" ? { python_path: pythonPath.trim() } : {}), ...(prepare ? { prepare: true } : {}) }, signal, setJob);
      if (tab && !tab.closed) tab.location.replace(url); else setApplicationUrl(url);
    } catch (e) { tab?.close(); if (!controller.signal.aborted) setError(signal.aborted ? "等待已超时，后台任务可能仍在继续。请重新打开应用查看状态。" : managementError(e)); } finally { setBusy(""); }
  }
  return <section className="environment-tools"><h3>应用与 Python 环境</h3>
    <PythonEnvironmentPicker key={containerId} containerId={containerId} running={running} disabled={!!busy} onChange={setPythonPath} />
    <div className="container-quick-actions">
      <button className="primary-button" disabled={!running || !!busy} onClick={() => void launch("jupyter")}>{busy === "jupyter" ? "正在打开…" : "打开 JupyterLab"}</button>
      <button className="secondary-button" disabled={!running || !!busy} onClick={() => void launch("tensorboard")}>{busy === "tensorboard" ? "正在打开…" : "打开 TensorBoard"}</button>
      <button className="secondary-button" disabled={!!busy} onClick={() => void recipe(false)}>{busy === "export" ? "正在导出…" : "导出环境"}</button>
      {onReuse && <button className="secondary-button" disabled={!!busy} onClick={() => void recipe(true)}>{busy === "reuse" ? "正在读取配置…" : "复用配置"}</button>}
      <button className="secondary-button" disabled={!!busy} onClick={() => void editNotes()}>编辑启动命令说明</button>
    </div>
    {progress && <div className={`application-progress application-progress-${job?.status}`} role="status" aria-live="polite"><span className="application-progress-dot" aria-hidden="true" /><div><strong>{progress[0]}</strong><p>{progress[1]}</p></div></div>}
    <p className="form-help">首次打开 JupyterLab 会自动准备缺少的应用依赖，可能需要约一分钟。JupyterLab 使用 /workspace，TensorBoard 读取 /workspace/runs 且需镜像已安装。停止容器后应用也会停止。</p>
    {!running && <p role="status">请先启动容器，再打开 JupyterLab。</p>}
    <details><summary>高级：重新准备 JupyterLab</summary>
      <p className="form-help">留空使用已准备的应用环境或默认 Python。首次准备会把 JupyterLab 安装到独立环境，优先使用服务器的离线安装包，并读取所选 Python 的项目依赖；不会升级项目里的 torch 等库。准备后可直接点“打开 JupyterLab”。</p>
      <button className="secondary-button" disabled={!running || !!busy} onClick={() => void launch("jupyter", true)}>准备 Jupyter 环境并打开</button>
    </details>
    <p className="form-help">{running ? "导出镜像、资源和当前 Python 依赖版本。" : "容器已停止：仅导出已保存配置；读取已安装依赖需先启动。"}复用时不会复制文件、密码或证书。</p>
    {applicationUrl && <a href={applicationUrl} target="_blank" rel="noopener noreferrer">点击继续打开应用（链接 60 秒有效）</a>}
    {notesOpen && <div><label htmlFor={`environment-notes-${containerId}`}>启动命令参考（只记录，不执行）</label><textarea id={`environment-notes-${containerId}`} value={commands} maxLength={50000} onChange={e => { setCommands(e.currentTarget.value); setSaved(false); }} placeholder="例如：python train.py --config config.yaml" /><p className="form-help">说明会包含在导出的环境文件中，请勿填写密码、令牌或其他私密信息。</p><button className="secondary-button" disabled={!!busy} onClick={() => void saveNotes()}>保存说明</button>{saved && <p role="status">启动命令说明已保存。</p>}</div>}
    {error && <p role="alert" className="create-error">{error}</p>}
  </section>;
}
