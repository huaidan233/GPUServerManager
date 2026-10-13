"use client";

import { FormEvent, useEffect, useId, useRef, useState } from "react";
import { Copy, Download, KeyRound, Maximize2, Minimize2, RefreshCw, RotateCw } from "lucide-react";

import { AccessibleDialog } from "./AccessibleDialog";
import { ContainerTerminal } from "./ContainerTerminal";
import { SSHConnectionDialog } from "./SSHConnectionDialog";
import { ContainerSSHAccess } from "./ContainerSSHAccess";
import { ContainerEnvironmentTools } from "./ContainerEnvironmentTools";
import { ContainerRecoveryPanel, type LifecycleReport } from "./ContainerRecoveryPanel";
import { ResourceMonitor } from "./ResourceMonitor";
import type { EnvironmentRecipe } from "../lib/environmentRecipe";
import { containerJson, containerRequest, managementError } from "../lib/containerManagement";
import type { ContainerSummary } from "../lib/mockData";

type Details = {
  container: { image?: string; status: string; cpu_limit?: number; memory_limit_gb?: number; gpu_devices?: string[];
    version?: number; ssh_user?: string; ssh_host?: string; ssh_port?: number; ssh_command?: string };
  runtime_available: boolean;
  terminal_available: boolean;
  terminal_unavailable_reason?: string | null;
  status?: string;
  image?: string;
  usage?: { memory_bytes: number; memory_limit_bytes?: number; cpu_percent: number | null } | null;
  ports?: { container_port: string; host_ip: string; host_port: string }[];
  networks?: { name: string; ip: string }[];
  mounts?: { container_path: string; mode: string }[];
  lifecycle_report?: LifecycleReport;
};
type Tab = "overview" | "configuration" | "logs" | "terminal" | "connections";
const tabs: { key: Tab; label: string }[] = [
  { key: "overview", label: "概览" }, { key: "logs", label: "日志" },
  { key: "configuration", label: "配置" },
  { key: "terminal", label: "终端" }, { key: "connections", label: "连接与存储" },
];

export function ContainerDetailsDialog({ container, canOperate, canManageSSH = canOperate, initialTab = "overview", onClose, onUpdated, onReuse }: {
  container: ContainerSummary; canOperate: boolean; onClose: () => void; onUpdated?: () => void;
  canManageSSH?: boolean; initialTab?: Tab;
  onReuse?: (recipe: EnvironmentRecipe) => void;
}) {
  const [tab, setTab] = useState<Tab>(initialTab);
  const [autoConnect, setAutoConnect] = useState(initialTab === "terminal");
  const [expanded, setExpanded] = useState(false);
  const [details, setDetails] = useState<Details | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [updatedAt, setUpdatedAt] = useState("");
  const [sshDialogOpen, setSshDialogOpen] = useState(false);
  const [isRestarting, setIsRestarting] = useState(false);
  const [confirmRestart, setConfirmRestart] = useState(false);
  const [restartMessage, setRestartMessage] = useState("");
  const [restartError, setRestartError] = useState("");
  const tabsId = useId();

  useEffect(() => {
    let cancelled = false;
    void containerRequest<Details>(`/api/gsm/containers/${encodeURIComponent(container.id)}/details`)
      .then((result) => {
        if (cancelled) return;
        setDetails(result);
        setError("");
        setUpdatedAt(new Date().toLocaleTimeString("zh-CN", { hour12: false }));
      }).catch((reason) => { if (!cancelled) setError(managementError(reason)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [container.id, revision]);

  const runtimeStatus = details?.status ?? details?.container.status ?? container.status;
  const sshCommand = details?.container.ssh_command ?? container.sshCommand;
  const statusText = ({ running: "运行中", stopped: "已停止", exited: "已停止", created: "待启动", paused: "已暂停", restarting: "重启中" } as Record<string, string>)[runtimeStatus] ?? runtimeStatus;
  const disabledReason = !canOperate ? "当前账户没有终端操作权限。"
    : loading ? "正在检查容器连接…"
      : error ? "无法确认容器状态，请先刷新详情后重试。"
        : !details?.runtime_available ? "当前为预览或未连接 Docker 的环境，不能执行真实命令。"
          : !details.terminal_available ? details.terminal_unavailable_reason ?? "容器未运行，请关闭面板并启动容器后再连接。"
            : runtimeStatus !== "running" ? "容器未运行，请关闭面板并启动容器后再连接。" : undefined;
  const usage = details?.usage;
  const mounts = details?.mounts ?? container.mounts?.map((mount) => ({ container_path: mount.containerPath, mode: mount.mode })) ?? [];
  const canRestart = canOperate && !loading && !error && !isRestarting && details?.runtime_available && runtimeStatus === "running";

  async function handleRestart() {
    if (!canRestart) return;
    setIsRestarting(true);
    setRestartMessage("");
    setRestartError("");
    try {
      const pending = await containerRequest<{ confirmation?: { id: string; required_phrase: string } }>(`/api/gsm/containers/${encodeURIComponent(container.id)}/stop`, { method: "POST" });
      if (!pending.confirmation?.id || !pending.confirmation.required_phrase) {
        throw new Error("无法创建重启操作，请刷新详情后重试。");
      }
      const stopped = await containerRequest<{ status?: string }>(`/api/gsm/confirmations/${encodeURIComponent(pending.confirmation.id)}/confirm`, containerJson({ phrase: pending.confirmation.required_phrase }));
      if (["execution_unknown", "unknown"].includes(stopped.status ?? "")) {
        throw new Error("停止结果尚未确认，请刷新状态后再重启。");
      }
      await containerRequest(`/api/gsm/containers/${encodeURIComponent(container.id)}/start`, { method: "POST" });
      setRestartMessage("重启请求已提交，正在刷新容器状态。");
      setRevision((value) => value + 1);
      onUpdated?.();
    } catch (reason) {
      setRestartError(managementError(reason));
    } finally {
      setIsRestarting(false);
      setConfirmRestart(false);
    }
  }

  return (
    <>
    <AccessibleDialog title={container.name} onClose={onClose} className={`container-details-dialog${expanded ? " is-expanded" : ""}`}>
      <div className="container-detail-identity">
        <span className={`status-pill ${runtimeStatus}`}>{statusText}</span>
        <span>{container.server} / {container.user}</span>
        <button type="button" className="secondary-button detail-expand" aria-label={expanded ? "还原面板" : "放大面板"} onClick={() => setExpanded(!expanded)}>
          {expanded ? <Minimize2 size={15} aria-hidden="true" /> : <Maximize2 size={15} aria-hidden="true" />}{expanded ? "还原" : "放大"}
        </button>
      </div>
      <div role="tablist" aria-label="容器管理" className="container-detail-tabs">
        {tabs.map((item, index) => <button key={item.key} type="button" role="tab"
          id={`${tabsId}-${item.key}`} aria-controls={`${tabsId}-panel`} aria-selected={tab === item.key}
          tabIndex={tab === item.key ? 0 : -1} onClick={() => setTab(item.key)}
          onKeyDown={(event) => {
            const next = event.key === "ArrowRight" ? (index + 1) % tabs.length
              : event.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length
                : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : -1;
            if (next < 0) return;
            event.preventDefault();
            setTab(tabs[next].key);
            document.getElementById(`${tabsId}-${tabs[next].key}`)?.focus();
          }}>{item.label}</button>)}
      </div>
      <div role="tabpanel" id={`${tabsId}-panel`} aria-labelledby={`${tabsId}-${tab}`} className="container-detail-content">
        {tab !== "terminal" && <div className="container-detail-toolbar">
          <span className="form-help" role="status">{loading ? "正在读取容器信息…" : error ? "信息未更新" : `最近采样 ${updatedAt}`}</span>
          <button type="button" className="secondary-button" disabled={loading} onClick={() => { setLoading(true); setRevision((value) => value + 1); }}><RefreshCw size={14} aria-hidden="true" />刷新详情</button>
        </div>}
        {error && <p className="create-error" role="alert">{error}</p>}
        {!loading && details && !details.runtime_available && tab !== "terminal" && <p className="container-notice">未连接真实 Docker：下方仅展示已保存的配置，实时用量、日志和终端暂不可用。</p>}
        {tab === "overview" && <>
          <div className="container-resource-grid">
            <div><span>CPU 配额</span><strong>{details?.container.cpu_limit ?? container.cpu}<small> 核</small></strong><p>实际占用请查看下方资源趋势</p></div>
            <div><span>内存配额</span><strong>{details?.container.memory_limit_gb ?? container.memoryGb}<small> GiB</small></strong><p>{usage ? `当前使用 ${(usage.memory_bytes / 1024 ** 3).toFixed(2)} GiB` : "实时用量暂不可用"}</p></div>
            <div><span>GPU 分配</span><strong>{(details?.container.gpu_devices ?? container.gpus).length}<small> 张</small></strong><p>{(details?.container.gpu_devices ?? container.gpus).join(" · ") || "CPU 容器"}</p></div>
          </div>
          <dl className="container-facts">
            <div><dt>镜像</dt><dd className="container-monospace">{details?.container.image ?? details?.image ?? (loading ? "读取中…" : "暂无镜像信息")}</dd></div>
            <div><dt>容器 ID</dt><dd className="container-monospace">{container.id}</dd></div>
          </dl>
          <div className="container-quick-actions">
            <button type="button" className="primary-button" onClick={() => setTab("terminal")}>打开终端</button>
            <button type="button" className="secondary-button" disabled={!canRestart} onClick={() => setConfirmRestart(true)}>
              <RotateCw size={14} aria-hidden="true" />{isRestarting ? "重启中…" : "重启容器"}
            </button>
          </div>
          {restartMessage && <p className="form-help" role="status">{restartMessage}</p>}
          {details?.runtime_available && <ResourceMonitor containerId={container.id} />}
          {details?.runtime_available && <ContainerRecoveryPanel containerId={container.id} running={runtimeStatus === "running"}
            canStart={canOperate && !loading && !error && ["created", "stopped", "exited"].includes(runtimeStatus)}
            report={details.lifecycle_report} onLogs={() => setTab("logs")} onConfigure={() => setTab("configuration")}
            onUpdated={() => { setRevision(value => value + 1); onUpdated?.(); }} />}
          {canManageSSH && <ContainerEnvironmentTools containerId={container.id} running={runtimeStatus === "running"} onReuse={onReuse} />}
          {restartError && <p className="create-error" role="alert">{restartError}</p>}
          <p className="form-help">遇到启动失败，先看日志；需要执行命令，再连接终端。停止和删除仍在容器列表操作。</p>
        </>}
        {tab === "logs" && (details?.runtime_available
          ? <ContainerLogs containerId={container.id} containerName={container.name} />
          : <p className="container-empty">{loading ? "正在检查日志来源…" : "日志暂不可用。连接真实 Docker 后可查看最近输出。"}</p>)}
        {tab === "terminal" && <ContainerTerminal containerId={container.id} disabledReason={disabledReason} autoConnect={autoConnect} />}
        {tab === "configuration" && (details ? <ContainerConfiguration key={details.container.version} containerId={container.id} details={details}
          canEdit={canOperate && !!details.runtime_available && ["created", "stopped", "exited", "configuration_unknown"].includes(runtimeStatus)}
          onSaved={() => { setRevision((value) => value + 1); onUpdated?.(); }} /> : <p>正在读取配置…</p>)}
        {tab === "connections" && <div className="container-connections">
          <section><h3>SSH 连接</h3>{sshCommand ? <>
            <dl className="container-facts"><div><dt>地址</dt><dd>{details?.container.ssh_host ?? "见下方命令"}</dd></div><div><dt>端口</dt><dd>{details?.container.ssh_port ?? "见下方命令"}</dd></div><div><dt>用户名</dt><dd>{details?.container.ssh_user ?? "researcher"}</dd></div></dl>
            <ContainerSSHAccess key={`${container.id}-${revision}`} containerId={container.id} name={container.name} command={sshCommand} isOwner={canManageSSH}
              canConnect={!disabledReason} onConnect={() => { setAutoConnect(true); setTab("terminal"); }} />
            {canManageSSH && <SSHPasswordForm containerId={container.id} disabled={!canOperate} onSaved={() => { setRevision((value) => value + 1); onUpdated?.(); }} />}
            {canManageSSH && <button type="button" className="secondary-button" disabled={!canOperate} onClick={() => setSshDialogOpen(true)}><KeyRound size={14} aria-hidden="true" />使用 SSH 证书连接</button>}
          </> : <p className="form-help">此容器未提供 SSH 连接命令，可使用网页终端。</p>}<p className="form-help">支持独立 SSH 密码或短期证书。密码由容器所有者设置，与网页登录密码无关；连接前请启动容器。</p></section>
          <section><h3>端口映射</h3>{details?.ports?.length ? <ul className="container-connection-list">{details.ports.map((port, index) => <li key={`${port.container_port}-${index}`}><code>{port.host_ip || "0.0.0.0"}:{port.host_port}</code><span aria-hidden="true">→</span><code>{port.container_port}</code></li>)}</ul> : <p className="form-help">{details?.runtime_available ? "没有已发布的端口。" : "连接 Docker 后可查看端口映射。"}</p>}<p className="form-help">0.0.0.0 / :: 表示监听地址，访问时请使用服务器实际地址。此处暂不支持修改端口映射。</p></section>
          <section><h3>容器地址</h3>{details?.networks?.some((network) => network.ip) ? details.networks.filter((network) => network.ip).map((network) => <div key={network.name}><span className="form-help">{network.name}</span><CopyValue value={network.ip} /></div>) : <p className="form-help">暂无可用的容器 IP。</p>}<p className="form-help">容器 IP 通常仅供所在网络访问；能否互通还取决于服务监听地址和防火墙。</p></section>
          <section><h3>挂载目录</h3>{mounts.length ? <ul className="container-connection-list">{mounts.map((mount) => <li key={mount.container_path}><code>{mount.container_path}</code><span>{mount.mode === "ro" ? "只读" : "读写"}</span></li>)}</ul> : <p className="form-help">暂无挂载目录。</p>}</section>
        </div>}
      </div>
      </AccessibleDialog>
      {confirmRestart && <AccessibleDialog title="重启容器" destructiveBusy={isRestarting} onClose={() => setConfirmRestart(false)}>
        <p>确认重启 <strong>{container.name}</strong>？正在运行的任务和 SSH 连接会中断。</p>
        <div className="dialog-actions">
          <button className="secondary-button" type="button" disabled={isRestarting} onClick={() => setConfirmRestart(false)}>取消</button>
          <button className="danger-button" type="button" disabled={isRestarting} onClick={() => void handleRestart()}>{isRestarting ? "重启中…" : "确认重启"}</button>
        </div>
      </AccessibleDialog>}
      {sshDialogOpen && sshCommand && <SSHConnectionDialog containerId={container.id} sshCommand={sshCommand} onClose={() => setSshDialogOpen(false)} />}
    </>
  );
}

function ContainerConfiguration({ containerId, details, canEdit, onSaved }: {
  containerId: string; details: Details; canEdit: boolean; onSaved: () => void;
}) {
  const [cpu, setCpu] = useState(details.container.cpu_limit ?? 1);
  const [memory, setMemory] = useState(details.container.memory_limit_gb ?? 4);
  const [gpus, setGpus] = useState((details.container.gpu_devices ?? []).join(", "));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError(""); setMessage("");
    try {
      await containerRequest(`/api/gsm/containers/${encodeURIComponent(containerId)}/configuration`, { ...containerJson({
        expected_version: details.container.version, cpu_limit: cpu, memory_limit_gb: memory,
        gpu_devices: gpus.trim() ? gpus.split(/[,，\s]+/).filter(Boolean) : [],
      }), signal: AbortSignal.timeout(240000) });
      setMessage("配置已保存，启动容器后使用新配置。"); onSaved();
    } catch (reason) { setError(managementError(reason)); }
    finally { setBusy(false); }
  }
  return <form className="dialog-form" onSubmit={save}>
    <h3>计算资源配置</h3><p className="form-help">请先停止容器。调整 GPU 时会保留容器内已有文件、工作目录和 SSH 设置，保存后仍保持停止状态。</p>
    {!canEdit && <p className="container-notice">当前状态无法修改，请先停止容器并刷新详情。</p>}
    <label htmlFor="config-cpu">CPU 核数</label><input id="config-cpu" type="number" min={1} required disabled={!canEdit || busy} value={cpu} onChange={(event) => setCpu(Number(event.currentTarget.value))} />
    <label htmlFor="config-memory">内存（GiB）</label><input id="config-memory" type="number" min={1} required disabled={!canEdit || busy} value={memory} onChange={(event) => setMemory(Number(event.currentTarget.value))} />
    <label htmlFor="config-gpus">GPU 编号（逗号分隔，留空表示不使用 GPU）</label><input id="config-gpus" disabled={!canEdit || busy} value={gpus} placeholder="例如 0 或 0, 1" onChange={(event) => setGpus(event.currentTarget.value)} />
    {error && <p role="alert" className="create-error">{error}</p>}{message && <p role="status">{message}</p>}
    <button className="primary-button" type="submit" disabled={!canEdit || busy || !details.container.version}>{busy ? "正在保存配置…" : "保存配置"}</button>
  </form>;
}

function SSHPasswordForm({ containerId, disabled, onSaved }: { containerId: string; disabled: boolean; onSaved: () => void }) {
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError(""); setMessage("");
    if (password !== confirmation) { setError("两次 SSH 密码不一致。"); return; }
    setBusy(true);
    try {
      await containerRequest(`/api/gsm/containers/${encodeURIComponent(containerId)}/ssh-password`, containerJson({ password }));
      setPassword(""); setConfirmation(""); setMessage("SSH 密码已设置，可使用上面的地址、端口和用户名连接。");
      onSaved();
    } catch (reason) { setError(managementError(reason)); }
    finally { setBusy(false); }
  }
  return <details><summary>设置或重置独立 SSH 密码</summary><form className="dialog-form" onSubmit={save}>
    <label htmlFor="ssh-password">新 SSH 密码（至少 12 位）</label><input id="ssh-password" type="password" autoComplete="new-password" minLength={12} maxLength={128} required disabled={disabled || busy} value={password} onChange={(event) => setPassword(event.currentTarget.value)} />
    <label htmlFor="ssh-password-confirm">再次输入 SSH 密码</label><input id="ssh-password-confirm" type="password" autoComplete="new-password" required disabled={disabled || busy} value={confirmation} onChange={(event) => setConfirmation(event.currentTarget.value)} />
    {error && <p role="alert" className="create-error">{error}</p>}{message && <p role="status">{message}</p>}
    <button className="primary-button" type="submit" disabled={disabled || busy}>{busy ? "正在设置…" : "保存 SSH 密码"}</button>
  </form></details>;
}

function CopyValue({ value }: { value: string }) {
  const [message, setMessage] = useState("");
  return <div className="container-copy-value"><code>{value}</code><button type="button" className="secondary-button" aria-label={`复制 ${value}`} onClick={async () => {
    try { await navigator.clipboard.writeText(value); setMessage("已复制"); }
    catch { setMessage("复制失败，请选中文本后手动复制。"); }
  }}><Copy size={14} aria-hidden="true" />复制</button><span role="status" className="form-help">{message}</span></div>;
}

function ContainerLogs({ containerId, containerName }: { containerId: string; containerName: string }) {
  const [logs, setLogs] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [auto, setAuto] = useState(true);
  const [follow, setFollow] = useState(true);
  const [tail, setTail] = useState("200");
  const [revision, setRevision] = useState(0);
  const [truncated, setTruncated] = useState(false);
  const [updatedAt, setUpdatedAt] = useState("");
  const output = useRef<HTMLPreElement>(null);

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function refresh() {
      if (document.hidden && auto) {
        timer = setTimeout(refresh, 3000);
        return;
      }
      try {
        const result = await containerRequest<{ logs: string; truncated: boolean }>(`/api/gsm/containers/${encodeURIComponent(containerId)}/logs?tail=${tail}`);
        if (disposed) return;
        setLogs(result.logs); setTruncated(result.truncated); setError("");
        setUpdatedAt(new Date().toLocaleTimeString("zh-CN", { hour12: false }));
      } catch (reason) { if (!disposed) setError(managementError(reason)); }
      finally {
        if (!disposed) {
          setLoading(false);
          if (auto) timer = setTimeout(refresh, 3000);
        }
      }
    }
    void refresh();
    return () => { disposed = true; clearTimeout(timer); };
  }, [containerId, tail, revision, auto]);

  useEffect(() => { if (follow && output.current) output.current.scrollTop = output.current.scrollHeight; }, [logs, follow]);

  function download() {
    const url = URL.createObjectURL(new Blob([logs], { type: "text/plain;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `${containerName.replace(/[^\p{L}\p{N}._-]/gu, "_")}.log`;
    link.click();
    URL.revokeObjectURL(url);
  }

  return <div className="container-logs">
    <div className="container-detail-toolbar container-log-controls">
      <label>最近 <select aria-label="日志行数" value={tail} onChange={(event) => { setLoading(true); setTail(event.target.value); }}><option value="100">100 行</option><option value="200">200 行</option><option value="500">500 行</option></select></label>
      <label><input type="checkbox" checked={auto} onChange={(event) => setAuto(event.target.checked)} />自动刷新（3 秒）</label>
      <label><input type="checkbox" checked={follow} onChange={(event) => setFollow(event.target.checked)} />跟随末尾</label>
      <button type="button" className="secondary-button" disabled={loading} onClick={() => { setLoading(true); setRevision((value) => value + 1); }}>刷新日志</button>
      <button type="button" className="secondary-button" disabled={!logs} onClick={download}><Download size={14} aria-hidden="true" />下载</button>
    </div>
    {error && <p role="alert" className="create-error">{error} {logs && "下方保留上次读取的日志。"}</p>}
    <pre ref={output} className="container-log-output" tabIndex={0} aria-label="容器日志">{logs || (loading ? "正在读取日志…" : error ? "暂无可显示的日志，请重试。" : "容器尚未输出日志。启动程序后，输出会显示在这里。")}</pre>
    <p className="form-help" role="status">{auto ? "自动刷新已开启" : "自动刷新已暂停"}{updatedAt && ` · 日志更新于 ${updatedAt}`} · 最多显示 64 KiB{truncated && " · 输出已截断"}。下载仅包含当前日志窗口。</p>
  </div>;
}
