"use client";
import { useEffect, useState } from "react";
import { containerRequest, managementError } from "../lib/containerManagement";
import "./ContainerEnvironmentTools.css";

type Environment = { path: string; version: string; kind: "system" | "venv" | "conda" | "managed"; torch_version: string | null; jupyter_version: string | null; default: boolean };
type Discovery = { status: string; environments: Environment[]; truncated: boolean };
type Probe = { path: string; status: string; python_version?: string; torch_version?: string | null; torch_status?: "available" | "absent" | "import_error"; cuda_build?: string | null; cuda_available?: boolean | null; device_count?: number | null };
const kindNames = { system: "系统", venv: "venv", conda: "conda", managed: "Jupyter 应用环境" };

export function PythonEnvironmentPicker({ containerId, running, disabled, onChange }: { containerId: string; running: boolean; disabled: boolean; onChange: (path: string) => void }) {
  const [choice, setChoice] = useState("auto"); const [manualPath, setManualPath] = useState("");
  const [discovery, setDiscovery] = useState<Discovery | null>(null); const [scanNumber, setScanNumber] = useState(0);
  const [scanning, setScanning] = useState(running); const [error, setError] = useState("");
  const [checking, setChecking] = useState(false); const [probe, setProbe] = useState<Probe | null>(null);
  const environments = discovery?.environments ?? [];
  const selected = choice === "auto" ? environments.find(item => item.kind === "managed") ?? environments.find(item => item.default) : environments.find(item => item.path === choice);
  const path = choice === "custom" ? manualPath.trim() : choice === "auto" ? selected?.path : choice;
  const base = `/api/gsm/containers/${encodeURIComponent(containerId)}/python-environments`;

  useEffect(() => {
    if (!running) return;
    const controller = new AbortController();
    containerRequest<Discovery>(base, { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(40000)]) })
      .then(value => { if (!controller.signal.aborted) { setDiscovery(value); setError(""); } })
      .catch(e => { if (!controller.signal.aborted) setError(managementError(e)); })
      .finally(() => { if (!controller.signal.aborted) setScanning(false); });
    return () => controller.abort();
  }, [base, running, scanNumber]);

  async function check() {
    if (!path) return;
    setChecking(true); setError(""); setProbe(null);
    try { setProbe(await containerRequest<Probe>(`${base}/check?${new URLSearchParams({ python_path: path })}`, { signal: AbortSignal.timeout(30000) })); }
    catch (e) { setError(managementError(e)); } finally { setChecking(false); }
  }
  function choose(value: string) { setChoice(value); setProbe(null); onChange(value === "auto" ? "" : value === "custom" ? manualPath : value); }
  return <div className="python-environment-panel">
    <div className="python-environment-heading"><div><strong>选择项目环境</strong><p>自动发现 Python、venv 和 conda 环境。</p></div><button className="secondary-button" disabled={!running || scanning || disabled || checking} onClick={() => { setScanning(true); setScanNumber(value => value + 1); }}>{scanning && running ? "正在发现…" : "重新扫描"}</button></div>
    <label htmlFor={`python-environment-${containerId}`}>Jupyter 使用的 Python 环境</label>
    <select id={`python-environment-${containerId}`} value={choice} disabled={!running || disabled || checking} onChange={e => choose(e.target.value)}>
      <option value="auto">自动选择 · 已准备的应用环境或默认 Python</option>
      {environments.map(item => <option key={item.path} value={item.path}>Python {item.version} · {kindNames[item.kind]} · {item.path}</option>)}
      <option value="custom">手动填写 Python 路径</option>
    </select>
    {choice === "custom" && <div className="python-manual-path"><label htmlFor={`app-python-${containerId}`}>Python 路径（可选）</label><input id={`app-python-${containerId}`} value={manualPath} disabled={disabled || checking} maxLength={241} onChange={e => { setManualPath(e.target.value); setProbe(null); onChange(e.target.value); }} placeholder="/home/researcher/venvs/myproject/bin/python" /></div>}
    {selected && <div className="python-environment-facts"><span>Python <b>{selected.version}</b></span><span>torch <b>{selected.torch_version ?? "未安装"}</b></span><span>JupyterLab <b>{selected.jupyter_version ?? "首次打开时准备"}</b></span></div>}
    <div className="python-check-action"><button className="secondary-button" disabled={!running || disabled || checking || !path} onClick={() => void check()}>{checking ? "正在检查…" : "检查 torch / CUDA"}</button><span>仅检查所选环境，不安装依赖。</span></div>
    {probe && <div className={`python-probe-result ${probe.status === "failed" || probe.torch_status === "import_error" ? "python-probe-warning" : ""}`} role="status">
      <strong>{probe.status === "failed" ? "环境检查未完成" : probe.torch_status === "absent" ? "此环境尚未安装 torch" : probe.torch_status === "import_error" ? "torch 已安装，但导入失败" : probe.cuda_available ? `CUDA 可用 · ${probe.device_count ?? "—"} 张 GPU` : "torch 可用 · 当前使用 CPU"}</strong>
      <p>{probe.status === "failed" ? "解释器不可用或检查超时，请确认路径和环境依赖后重试。" : probe.torch_status === "absent" ? "可以正常运行 Python；需要深度学习时请在此环境安装 torch。" : probe.torch_status === "import_error" ? "请在容器终端运行 import torch 查看错误；常见原因是动态库缺失或版本不兼容。" : `Python ${probe.python_version} · torch ${probe.torch_version} · CUDA 构建版本 ${probe.cuda_build ?? "无（CPU 版本）"}${probe.cuda_build && !probe.cuda_available ? "。未检测到可用 GPU，请检查容器 GPU 分配与驱动。" : ""}`}</p><code>{probe.path}</code>
    </div>}
    {running && discovery && !environments.length && <p className="form-help">未发现可用 Python 环境，可以手动填写解释器路径。</p>}
    {discovery?.truncated && <p className="form-help">已达到扫描上限。未列出的环境可手动填写路径。</p>}
    {error && <p role="status" className="form-help">环境检查暂不可用：{error}仍可手动指定路径。</p>}
  </div>;
}
