import { authenticatedFetch } from "./authenticatedFetch";

export async function containerRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await authenticatedFetch(path, {
    cache: "no-store", signal: AbortSignal.timeout(12000), ...init,
  });
  const result = await response.json();
  if (!response.ok) {
    const message = response.status === 401 ? "登录已过期，请重新登录。"
      : response.status === 403 ? "没有操作权限，或登录状态已改变，请刷新页面。"
        : response.status === 404 ? "容器或终端会话已不存在，请刷新列表或重新连接。"
        : String(result.detail ?? "请求失败，请稍后重试。");
    throw new Error(message);
  }
  return result as T;
}

export function containerJson(body: unknown): RequestInit {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

export function managementError(error: unknown): string {
  return error instanceof Error && error.name !== "TypeError" && error.name !== "TimeoutError"
    ? error.message : "连接中断，请检查网络后重试。";
}
