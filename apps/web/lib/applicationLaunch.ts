import { containerJson, containerRequest } from "./containerManagement";

export type LaunchJob = { job_id: string; application: string; status: "running" | "ready" | "failed"; phase: string; error_code?: string };
export const applicationPhases: Record<string, [string, string]> = {
  queued: ["等待启动", "启动请求已收到，正在等待可用的准备任务。"],
  checking: ["检查容器", "正在确认运行状态和应用配置。"],
  network: ["连接应用入口", "正在检查容器的独立应用网络。"],
  checking_environment: ["检查 Python 环境", "正在检查所选解释器和 JupyterLab 依赖。"],
  preparing: ["准备 JupyterLab", "正在独立应用环境中安装依赖，优先使用离线安装包。首次准备通常需要约一分钟。"],
  starting: ["启动应用", "正在容器中启动应用进程。"],
  waiting: ["等待应用响应", "应用已启动，正在等待服务健康检查通过。"],
  ready: ["应用已就绪", "正在验证登录并打开应用。"],
  failed: ["启动未完成", "请根据下方原因处理后重试。"],
};
const launchErrors: Record<string, string> = {
  app_python_invalid: "Python 路径无效，请填写容器内的绝对路径。",
  app_python_missing: "没有找到可用的 Python，请选择已发现的环境或填写 Python 路径。",
  app_install_failed: "Jupyter 环境准备失败。请检查 Python venv/pip、磁盘空间或安装日志 /tmp/gsm-jupyter-install.log；不支持的 Python 版本可能需要联网安装。",
  app_jupyter_missing: "准备后仍未找到 JupyterLab，请检查安装日志 /tmp/gsm-jupyter-install.log。",
  app_jupyter_outdated: "当前 JupyterLab 版本过旧，请重新准备 Jupyter 环境。",
  app_restart_required: "Jupyter 已在另一个 Python 环境运行。请先保存 Notebook 并停止 Jupyter，再切换环境。",
  app_not_ready: "应用未能就绪。请检查 /tmp/gsm-jupyter.log 或 /tmp/gsm-tensorboard.log；TensorBoard 需要事先安装。",
  app_start_failed: "应用启动失败，请确认容器仍在运行并检查应用日志。",
  app_start_interrupted: "服务器代理重启，启动进度已中断。请重试，系统会先检查应用是否已经就绪。",
  app_start_busy: "已有应用准备任务正在执行，请稍后再试。",
};

export async function launchContainerApplication(base: string, payload: { application: string; python_path?: string; prepare?: boolean }, signal: AbortSignal, progress: (job: LaunchJob) => void) {
  const first = await containerRequest<LaunchJob | { url: string }>(`${base}/applications`, { ...containerJson({ ...payload, async_start: true }), signal });
  if ("url" in first) return first.url;
  let current = first; progress(current);
  while (current.status === "running") {
    await new Promise<void>((resolve, reject) => {
      const cancel = () => { clearTimeout(timer); reject(signal.reason); };
      const timer = setTimeout(() => { signal.removeEventListener("abort", cancel); resolve(); }, 1500);
      if (signal.aborted) cancel(); else signal.addEventListener("abort", cancel, { once: true });
    });
    current = await containerRequest<LaunchJob>(`${base}/applications/${payload.application}/jobs/${first.job_id}`, { signal });
    progress(current);
  }
  if (current.status === "failed") throw new Error(launchErrors[current.error_code ?? ""] ?? launchErrors.app_start_failed);
  // The normal launch rechecks health and owner/session before creating a short-lived ticket.
  const result = await containerRequest<{ url: string }>(`${base}/applications`, { ...containerJson(payload), signal });
  return result.url;
}
