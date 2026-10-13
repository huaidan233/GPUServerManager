import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, expect, test, vi } from "vitest";
import { UpdateSettingsPanel } from "../components/UpdateSettingsPanel";

afterEach(() => { vi.unstubAllGlobals(); sessionStorage.clear(); });

const operation = { id: "a".repeat(32), action: "apply", state: "succeeded", phase: "complete", message: "升级完成",
  target_version: "0.3.0-rc.2", rollback_available: true, rollback_id: "transaction-1" };
const manager = { available: true, executor: { online: true }, prepared: { id: "prepared-1", version: "0.3.0-rc.2", sha256: "a".repeat(64), expires_at: "2099-01-01T00:00:00Z" }, operation };
const release = { current_version: "0.3.0-rc.1", latest_version: "0.3.0-rc.2", repository: "example/gsm", channel: "prerelease", status: "available", message: "请阅读发布说明", manager };

test("an update service failure never presents an up-to-date result", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ current_version: "0.3.0-rc.1", repository: "example/gsm", channel: "stable", status: "error", message: "网络连接失败" })));
  render(<UpdateSettingsPanel />);
  expect(await screen.findByRole("alert")).toHaveTextContent("检查未完成");
  expect(screen.queryByText("当前已是此通道最新版本")).toBeNull();
  expect(screen.queryByRole("link", { name: /下载/ })).toBeNull();
});

test("the prerelease channel displays a review link without claiming automatic installation", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ current_version: "0.3.0-rc.1", latest_version: "0.3.0-rc.2", repository: "example/gsm", channel: "prerelease", status: "available", message: "请阅读发布说明", release_url: "https://github.com/example/gsm/releases/tag/v0.3.0-rc.2" })));
  render(<UpdateSettingsPanel />);
  expect(await screen.findByRole("status")).toHaveTextContent("发现新版本");
  expect(screen.getByText("预发布（含候选版本）")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /查看发布说明/ })).toHaveAttribute("href", "https://github.com/example/gsm/releases/tag/v0.3.0-rc.2");
  expect(screen.getByRole("button", { name: "准备新版本" })).toBeDisabled();
  expect(screen.getByText(/未部署主机升级服务时仍可检查发布说明/)).toBeInTheDocument();
});

test("prepare requires authenticator code and submission is shown as running until host completion", async () => {
  let started = false;
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") {
      const body = JSON.parse(String(init.body));
      expect(body).toEqual({ version: "0.3.0-rc.2", totp_code: "123456", request_id: expect.stringMatching(/^[a-f0-9]{32}$/) });
      started = true;
      return Response.json({ accepted: true, operation: { ...operation, id: body.request_id, action: "prepare", state: "running", phase: "preparing", message: "正在校验" } });
    }
    return Response.json(input.toString().endsWith("/status") ? { ...manager, operation: { ...operation, state: "running", phase: "preparing", message: "正在校验" } } : release);
  });
  vi.stubGlobal("fetch", fetcher);
  render(<UpdateSettingsPanel />);
  fireEvent.click(await screen.findByRole("button", { name: "准备新版本" }));
  expect(screen.queryByLabelText("管理员密码")).toBeNull();
  expect(screen.getByRole("button", { name: "验证并提交" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("动态验证码"), { target: { value: "123456" } });
  fireEvent.click(screen.getByRole("button", { name: "验证并提交" }));
  await waitFor(() => expect(started).toBe(true));
  expect(await screen.findByText(/正在执行 · 校验并准备发布包/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "准备新版本" })).toBeDisabled();
  expect(sessionStorage.getItem("gsm-system-update-request")).toBeNull();
});

test("apply needs exact confirmation and sends only prepared id with a fresh code", async () => {
  const posted: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") { const body = JSON.parse(String(init.body)); posted.push(body); return Response.json({ operation: { ...operation, id: body.request_id, state: "running" } }); }
    return Response.json(input.toString().endsWith("/status") ? manager : release);
  }));
  render(<UpdateSettingsPanel />);
  fireEvent.click(await screen.findByRole("button", { name: "升级到 0.3.0-rc.2" }));
  fireEvent.change(screen.getByLabelText("动态验证码"), { target: { value: "123456" } });
  fireEvent.change(screen.getByLabelText("确认短语"), { target: { value: "yes" } });
  expect(screen.getByRole("button", { name: "验证并提交" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("确认短语"), { target: { value: "UPGRADE 0.3.0-rc.2" } });
  fireEvent.click(screen.getByRole("button", { name: "验证并提交" }));
  await waitFor(() => expect(posted).toEqual([{ prepared_id: "prepared-1", confirmation: "UPGRADE 0.3.0-rc.2", totp_code: "123456", request_id: expect.any(String) }]));
});

test("rollback requires explicit acknowledgement of management record loss", async () => {
  const posted: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") { const body = JSON.parse(String(init.body)); posted.push(body); return Response.json({ operation: { ...operation, action: "rollback", id: body.request_id, state: "running" } }); }
    return Response.json(input.toString().endsWith("/status") ? manager : release);
  }));
  render(<UpdateSettingsPanel />);
  fireEvent.click(await screen.findByRole("button", { name: "回退到升级前" }));
  fireEvent.change(screen.getByLabelText("动态验证码"), { target: { value: "123456" } });
  fireEvent.change(screen.getByLabelText("确认短语"), { target: { value: "ROLLBACK transaction-1" } });
  expect(screen.getByRole("button", { name: "验证并提交" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "验证并提交" }));
  await waitFor(() => expect(posted).toEqual([{ transaction_id: "transaction-1", confirmation: "ROLLBACK transaction-1", acknowledge_data_loss: true, totp_code: "123456", request_id: expect.any(String) }]));
});

test("explicit authentication rejection clears pending state and permits a fresh code", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") return Response.json({ detail: "动态验证码无效或已过期。" }, { status: 401 });
    return Response.json(input.toString().endsWith("/status") ? manager : release);
  }));
  render(<UpdateSettingsPanel />);
  fireEvent.click(await screen.findByRole("button", { name: "准备新版本" }));
  fireEvent.change(screen.getByLabelText("动态验证码"), { target: { value: "123456" } });
  fireEvent.click(screen.getByRole("button", { name: "验证并提交" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("动态验证码无效");
  expect(screen.getByLabelText("动态验证码")).toHaveValue("");
  expect(sessionStorage.getItem("gsm-system-update-request")).toBeNull();
  fireEvent.change(screen.getByLabelText("动态验证码"), { target: { value: "654321" } });
  await waitFor(() => expect(screen.getByRole("button", { name: "验证并提交" })).toBeEnabled());
});

test("a lost acknowledgement never automatically repeats a mutation or stores the code", async () => {
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") throw new TypeError("network unavailable");
    return Response.json(input.toString().endsWith("/status") ? manager : release);
  });
  vi.stubGlobal("fetch", fetcher);
  render(<UpdateSettingsPanel />);
  fireEvent.click(await screen.findByRole("button", { name: "准备新版本" }));
  fireEvent.change(screen.getByLabelText("动态验证码"), { target: { value: "123456" } });
  fireEvent.click(screen.getByRole("button", { name: "验证并提交" }));
  expect(await screen.findByText(/正在确认操作/)).toBeInTheDocument();
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(screen.getByRole("button", { name: "准备新版本" })).toBeDisabled();
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  expect(sessionStorage.getItem("gsm-system-update-request")).toMatch(/^[a-f0-9]{32}$/);
  expect(JSON.stringify(sessionStorage)).not.toContain("123456");
  fireEvent.click(await screen.findByRole("button", { name: "已核对任务状态，解除等待" }));
  expect(screen.getByRole("button", { name: "准备新版本" })).toBeEnabled();
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

test("recovery required disables all new operations and shows manual recovery instruction", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ...release, manager: { ...manager, operation: { ...operation, state: "recovery_required", phase: "recovering" } } })));
  render(<UpdateSettingsPanel />);
  expect(await screen.findByRole("alert")).toHaveTextContent("自动恢复未完成");
  expect(screen.getByRole("button", { name: "准备新版本" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "升级到 0.3.0-rc.2" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "回退到升级前" })).toBeDisabled();
});

test("automatic rollback shows the failure cause without claiming a successful upgrade", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ...release, manager: { ...manager, operation: { ...operation, state: "rolled_back", message: "complete", failure_code: "release_health_check_failed" } } })));
  render(<UpdateSettingsPanel />);
  expect(await screen.findByText(/已恢复升级前版本/)).toBeInTheDocument();
  expect(screen.getByText(/新版本未通过健康检查/)).toBeInTheDocument();
  expect(screen.queryByText(/操作成功/)).toBeNull();
});

test("host current version takes precedence over the cached release version after upgrade", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ...release, manager: { ...manager, current_version: "0.3.0-rc.2", prepared: null } })));
  render(<UpdateSettingsPanel />);
  expect(await screen.findByText("该发布版本已安装")).toBeInTheDocument();
  expect(screen.queryByText("0.3.0-rc.1")).toBeNull();
  expect(screen.queryByRole("button", { name: "准备新版本" })).toBeNull();
});
