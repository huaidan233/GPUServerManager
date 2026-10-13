import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, expect, test, vi } from "vitest";

import { ContainerOperationsTable } from "../components/ContainerOperationsTable";
import { ContainerTerminal } from "../components/ContainerTerminal";
import { mockDashboardData } from "../lib/mockData";
import { proxyContainerManagement } from "../lib/apiProxy";
import { panelReadResponse } from "./containerPanelFixtures";

const emulator = vi.hoisted(() => ({
  input: (_data: string) => {}, resize: () => {}, write: vi.fn(), dispose: vi.fn(),
}));
vi.mock("@xterm/xterm", () => ({ Terminal: class {
  cols = 100;
  rows = 30;
  options = { disableStdin: false };
  loadAddon() {}
  open(host: HTMLElement) { const input = document.createElement("textarea"); input.className = "xterm"; host.appendChild(input); }
  onData(callback: (data: string) => void) { emulator.input = callback; }
  onResize(callback: () => void) { emulator.resize = callback; }
  write = emulator.write;
  writeln = emulator.write;
  dispose = emulator.dispose;
  focus() {}
  clear() {}
} }));
vi.mock("@xterm/addon-fit", () => ({ FitAddon: class { fit() {} } }));

const container = { ...mockDashboardData.containers[0], status: "running" };
const details = {
  container: { image: "demo/image:latest", status: "running" },
  runtime_available: true, terminal_available: true,
  usage: { cpu_percent: 25, memory_bytes: 1024 ** 3 },
  ports: [{ container_port: "22/tcp", host_ip: "0.0.0.0", host_port: "22001" }],
  networks: [{ name: "bridge", ip: "172.17.0.2" }],
  mounts: [{ container_path: "/workspace", mode: "rw" }],
};
const sessionId = "a".repeat(32);

afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks(); });

function mockTerminalRequests() {
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const panel = panelReadResponse(input);
    if (panel) return panel;
    if (url.endsWith("/details")) return Response.json(details);
    if (url.endsWith("/terminal") && init?.method === "POST") return Response.json({ session_id: sessionId });
    if (url.includes("?cursor=")) return Response.json({ output: btoa("$ "), cursor: 2, truncated: false, closed: false });
    return Response.json({ status: "ok" });
  });
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
}

test("container name opens details without adding a new column and restores focus", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => panelReadResponse(input) ?? Response.json(details)));
  render(<ContainerOperationsTable containers={[container]} />);
  const trigger = screen.getByRole("button", { name: `管理 ${container.name}` });
  trigger.focus();
  fireEvent.click(trigger);
  expect(document.body.style.overflow).toBe("hidden");
  expect(await screen.findByText("demo/image:latest")).toBeVisible();
  expect(screen.getByText("实际占用请查看下方资源趋势")).toBeVisible();
  expect(screen.getByText("当前使用 1.00 GiB")).toBeVisible();
  const tab = screen.getByRole("tab", { name: "概览" });
  fireEvent.keyDown(tab, { key: "End" });
  expect(screen.getByRole("tab", { name: "连接与存储" })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByText("0.0.0.0:22001")).toBeVisible();
  expect(screen.getByText("/workspace")).toBeVisible();
  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(document.body.style.overflow).not.toBe("hidden");
  expect(trigger).toHaveFocus();
});

test("preview disables the real terminal with an actionable explanation", async () => {
  const fetcher = vi.fn(async (input: RequestInfo | URL) => panelReadResponse(input) ?? Response.json({ ...details, runtime_available: false, terminal_available: false, usage: null }));
  vi.stubGlobal("fetch", fetcher);
  render(<ContainerOperationsTable containers={[container]} />);
  fireEvent.click(screen.getByRole("button", { name: `管理 ${container.name}` }));
  await screen.findByText(/未连接真实 Docker/);
  fireEvent.click(screen.getByRole("tab", { name: "终端" }));
  expect(screen.getByRole("button", { name: "连接终端" })).toBeDisabled();
  expect(screen.getByText(/不能执行真实命令/)).toBeVisible();
  expect(fetcher).toHaveBeenCalledTimes(1);
});

test("logs render as text, retain the last successful window on failure, and support retry", async () => {
  let failing = false;
  vi.stubGlobal("fetch", vi.fn(async (input) => panelReadResponse(input) ?? (String(input).endsWith("/details")
    ? Response.json(details)
    : failing ? Response.json({ detail: "读取日志失败" }, { status: 502 })
      : Response.json({ logs: "<script>alert('x')</script>\nhello", truncated: true }))));
  render(<ContainerOperationsTable containers={[container]} />);
  fireEvent.click(screen.getByRole("button", { name: `管理 ${container.name}` }));
  await screen.findByText("demo/image:latest");
  fireEvent.click(screen.getByRole("tab", { name: "日志" }));
  await screen.findByText(/<script>alert/);
  expect(screen.getByLabelText("容器日志").querySelector("script")).toBeNull();
  expect(screen.getByText(/输出已截断/)).toBeVisible();
  fireEvent.click(screen.getByRole("checkbox", { name: "自动刷新（3 秒）" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "刷新日志" })).toBeEnabled());
  failing = true;
  fireEvent.click(screen.getByRole("button", { name: "刷新日志" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("下方保留上次读取的日志");
  expect(screen.getByLabelText("容器日志")).toHaveTextContent("hello");
  failing = false;
  fireEvent.click(screen.getByRole("button", { name: "刷新日志" }));
  await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
});

test("terminal connects only on request, sends input and closes on tab change", async () => {
  const fetcher = mockTerminalRequests();
  render(<ContainerOperationsTable containers={[container]} />);
  fireEvent.click(screen.getByRole("button", { name: `管理 ${container.name}` }));
  await screen.findByText("demo/image:latest");
  fireEvent.click(screen.getByRole("tab", { name: "终端" }));
  expect(fetcher.mock.calls.filter(([url]) => String(url).endsWith("/details"))).toHaveLength(1);
  expect(fetcher.mock.calls.filter(([url]) => String(url).includes("/terminal"))).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "连接终端" }));
  await screen.findByText("已连接 · /bin/sh");
  await waitFor(() => expect(emulator.write).toHaveBeenCalled());
  act(() => emulator.input("pwd\n"));
  await waitFor(() => expect(fetcher.mock.calls.some(([url, init]) => String(url).endsWith(sessionId) && String(init?.body).includes("pwd"))).toBe(true));
  const input = screen.getByLabelText("容器交互终端").querySelector("textarea")!;
  fireEvent.keyDown(input, { key: "Escape" });
  expect(screen.getByRole("dialog")).toBeVisible();
  fireEvent.keyDown(input, { key: "x", ctrlKey: true, shiftKey: true });
  expect(screen.getByRole("button", { name: "断开连接" })).toHaveFocus();
  fireEvent.click(screen.getByRole("tab", { name: "概览" }));
  await waitFor(() => expect(fetcher.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(true));
  expect(emulator.dispose).toHaveBeenCalled();
});

test("terminal releases a session that finishes opening after the panel closes", async () => {
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  let complete: ((response: Response) => void) | undefined;
  const fetcher = vi.fn((input: RequestInfo | URL) => String(input).endsWith("/terminal")
    ? new Promise<Response>((resolve) => { complete = resolve; })
    : Promise.resolve(Response.json({ status: "closed" })));
  vi.stubGlobal("fetch", fetcher);
  const view = render(<ContainerTerminal containerId="c1" />);
  fireEvent.click(screen.getByRole("button", { name: "连接终端" }));
  await waitFor(() => expect(complete).toBeDefined());
  view.unmount();
  await act(async () => { complete!(Response.json({ session_id: sessionId })); });
  expect(fetcher).toHaveBeenLastCalledWith(`/api/gsm/containers/c1/terminal/${sessionId}`, { method: "DELETE", keepalive: true });
});

test("terminal disconnects on network error and permission loss without replaying commands", async () => {
  const fetcher = mockTerminalRequests();
  const view = render(<ContainerTerminal containerId="c1" />);
  fireEvent.click(screen.getByRole("button", { name: "连接终端" }));
  await screen.findByText("已连接 · /bin/sh");
  fetcher.mockImplementation(async (_url, init) => {
    if (init?.method === "POST") throw new TypeError("network failed");
    return Response.json({ output: "", cursor: 2, closed: false });
  });
  act(() => emulator.input("run-once\n"));
  expect(await screen.findByRole("alert")).toHaveTextContent("连接中断");
  expect(screen.getByRole("button", { name: "重新连接" })).toBeEnabled();
  expect(fetcher.mock.calls.filter(([, init]) => String(init?.body).includes("run-once"))).toHaveLength(1);
  view.rerender(<ContainerTerminal containerId="c1" disabledReason="没有操作权限" />);
  expect(screen.getByRole("button", { name: "重新连接" })).toBeDisabled();
});

test("management proxy forwards session and CSRF but never browser-supplied owner query", async () => {
  const fetcher = vi.fn(async () => Response.json({ status: "ok" }));
  const request = new Request(`https://gsm.test/api/gsm/containers/c1/terminal/${sessionId}?owner=spoof&cursor=7`, {
    headers: { Cookie: "gsm_session=session", "X-CSRF-Token": "csrf", Origin: "https://gsm.test" },
  });
  const response = await proxyContainerManagement("c1", "terminal", request, sessionId, { apiUrl: "http://api:8000", fetcher });
  expect(fetcher.mock.calls[0][0]).toBe(`http://api:8000/api/containers/c1/terminal/${sessionId}?cursor=7`);
  expect(response.headers.get("cache-control")).toBe("no-store");
  const invalid = await proxyContainerManagement("c1", "terminal", request, "../other", { fetcher });
  expect(invalid.status).toBe(400);
  expect(fetcher).toHaveBeenCalledTimes(1);
});


test("stopped configuration submits real limits and refreshes details", async () => {
  const stoppedDetails = { ...details, container: { ...container, status: "stopped", version: 3, cpu_limit: 2, memory_limit_gb: 4, gpu_devices: ["0"] } };
  const fetcher = vi.fn(async (input: RequestInfo | URL) => panelReadResponse(input) ?? Response.json(String(input).endsWith("/details") ? stoppedDetails : { container: stoppedDetails.container }));
  vi.stubGlobal("fetch", fetcher);
  render(<ContainerOperationsTable containers={[{ ...container, status: "stopped" }]} />);
  fireEvent.click(screen.getByRole("button", { name: `管理 ${container.name}` }));
  await screen.findByText(/最近采样/);
  fireEvent.click(screen.getByRole("tab", { name: "配置" }));
  fireEvent.change(screen.getByLabelText("CPU 核数"), { target: { value: "4" } });
  fireEvent.change(screen.getByLabelText("内存（GiB）"), { target: { value: "8" } });
  fireEvent.change(screen.getByLabelText(/GPU 编号/), { target: { value: "1" } });
  fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledWith(`/api/gsm/containers/${container.id}/configuration`, expect.objectContaining({
    body: JSON.stringify({ expected_version: 3, cpu_limit: 4, memory_limit_gb: 8, gpu_devices: ["1"] }),
  })));
  await waitFor(() => expect(fetcher.mock.calls.filter(([url]) => String(url).endsWith("/details"))).toHaveLength(2));
});

test("SSH password form preserves certificate option and clears secrets after save", async () => {
  const connected = { ...container, ssh_command: "ssh researcher@203.0.113.10 -p 23001" };
  const fetcher = vi.fn(async (input: RequestInfo | URL) => panelReadResponse(input) ?? Response.json(String(input).endsWith("/details")
    ? { ...details, container: { ...connected, ssh_host: "203.0.113.10", ssh_user: "researcher", ssh_port: 23001 } } : { status: "updated" }));
  vi.stubGlobal("fetch", fetcher);
  render(<ContainerOperationsTable containers={[connected]} actor={{ userId: "alice", memberId: container.ownerMemberId, role: "user" }} />);
  fireEvent.click(screen.getByRole("button", { name: `管理 ${container.name}` }));
  await screen.findByText(/最近采样/);
  fireEvent.click(screen.getByRole("tab", { name: "连接与存储" }));
  expect(screen.getByRole("button", { name: "使用 SSH 证书连接" })).toBeEnabled();
  fireEvent.click(screen.getByText("设置或重置独立 SSH 密码"));
  fireEvent.change(screen.getByLabelText("新 SSH 密码（至少 12 位）"), { target: { value: "independent-ssh-password" } });
  fireEvent.change(screen.getByLabelText("再次输入 SSH 密码"), { target: { value: "independent-ssh-password" } });
  fireEvent.click(screen.getByRole("button", { name: "保存 SSH 密码" }));
  await screen.findByText(/SSH 密码已设置/);
  expect(screen.getByLabelText("新 SSH 密码（至少 12 位）")).toHaveValue("");
  expect(fetcher).toHaveBeenCalledWith(`/api/gsm/containers/${container.id}/ssh-password`, expect.objectContaining({ body: JSON.stringify({ password: "independent-ssh-password" }) }));
});

test("one click from the SSH list opens and connects the terminal, then closes the session", async () => {
  const fetcher = mockTerminalRequests();
  render(<ContainerOperationsTable containers={[{ ...container, sshCommand: "ssh researcher@203.0.113.10 -p 23002" }]} />);
  fireEvent.click(screen.getByRole("button", { name: "一键连接网页终端" }));
  await waitFor(() => expect(fetcher.mock.calls.some(([url, init]) => String(url).endsWith("/terminal") && init?.method === "POST")).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: `关闭${container.name}` }));
  await waitFor(() => expect(fetcher.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(true));
});
