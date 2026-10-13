import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, test, vi } from "vitest";

import { AgentSettingsPanel } from "../components/AgentSettingsPanel";


const configuredSettings = {
  version: 1,
  endpoint: "https://provider.example/v1",
  model: "gpt-test",
  primary_configured: true,
  primary_fingerprint: "1eb90d4a288b",
  fallback_configured: true,
  fallback_fingerprint: "a288df15c5fc",
  active_slot: "primary",
  primary_status: "healthy",
  fallback_status: "standby",
  updated_at: "2026-08-10T08:00:00+00:00",
  updated_by: "member-admin",
};


afterEach(() => {
  vi.unstubAllGlobals();
});


test("shows masked primary and fallback settings without rendering stored secrets", async () => {
  const fetcher = vi.fn(async () => Response.json(configuredSettings));
  vi.stubGlobal("fetch", fetcher);

  render(<AgentSettingsPanel />);

  expect(await screen.findByRole("heading", { name: "Agent 模型配置" })).toBeInTheDocument();
  expect(screen.getByDisplayValue("https://provider.example/v1")).toHaveAttribute("name", "agent-endpoint");
  expect(screen.getByDisplayValue("https://provider.example/v1")).toHaveAttribute("autocomplete", "off");
  expect(screen.getByDisplayValue("gpt-test")).toHaveAttribute("name", "agent-model");
  expect(screen.getByDisplayValue("gpt-test")).toHaveAttribute("autocomplete", "off");
  expect(screen.getByText("指纹 1eb90d4a288b")).toBeInTheDocument();
  expect(screen.getByText("指纹 a288df15c5fc")).toBeInTheDocument();
  expect(screen.queryByDisplayValue("primary-secret")).not.toBeInTheDocument();
  expect(screen.queryByDisplayValue("fallback-secret")).not.toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledWith("/api/gsm/admin/agent-settings");
});


test("replacement requires administrator reauthentication then resumes without sending the other key", async () => {
  let putCount = 0;
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = input.toString();
    if (url === "/api/gsm/admin/agent-settings" && (!init || init.method === undefined)) {
      return Response.json(configuredSettings);
    }
    if (url === "/api/gsm/admin/agent-settings/reauthenticate") {
      expect(JSON.parse(String(init?.body))).toEqual({ totp_code: "123456" });
      return Response.json({ status: "reauthenticated" });
    }
    if (url === "/api/gsm/admin/agent-settings" && init?.method === "PUT") {
      putCount += 1;
      if (putCount === 1) {
        return Response.json(
          { detail: "recent administrator authentication required" },
          { status: 403 },
        );
      }
      return Response.json({ ...configuredSettings, version: 2, model: "gpt-next" });
    }
    return Response.json({ detail: "unexpected" }, { status: 500 });
  });
  vi.stubGlobal("fetch", fetcher);
  render(<AgentSettingsPanel />);
  await screen.findByDisplayValue("gpt-test");

  fireEvent.change(screen.getByLabelText("模型"), { target: { value: "gpt-next" } });
  fireEvent.click(screen.getByRole("button", { name: "替换主密钥" }));
  expect(screen.getByLabelText("新的主密钥")).toHaveAttribute("name", "agent-primary-key");
  fireEvent.change(screen.getByLabelText("新的主密钥"), {
    target: { value: "replacement-secret" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存 Agent 设置" }));

  expect(await screen.findByRole("dialog", { name: "管理员重新认证" })).toBeInTheDocument();
  expect(screen.queryByLabelText("管理员密码")).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("动态验证码"), { target: { value: "123456" } });
  fireEvent.click(screen.getByRole("button", { name: "重新认证并继续" }));

  await waitFor(() => expect(putCount).toBe(2));
  expect(await screen.findByText("Agent 设置已保存")).toBeInTheDocument();
  expect(screen.queryByLabelText("新的主密钥")).not.toBeInTheDocument();
  const bodies = fetcher.mock.calls
    .filter(([input, init]) => input.toString() === "/api/gsm/admin/agent-settings" && init?.method === "PUT")
    .map(([, init]) => JSON.parse(String(init?.body)));
  expect(bodies).toEqual([
    {
      expected_version: 1,
      endpoint: "https://provider.example/v1",
      model: "gpt-next",
      primary_key: "replacement-secret",
    },
    {
      expected_version: 1,
      endpoint: "https://provider.example/v1",
      model: "gpt-next",
      primary_key: "replacement-secret",
    },
  ]);
  expect(JSON.stringify(bodies)).not.toContain("fallback_key");
});


test("clear fallback requires the exact confirmation phrase", async () => {
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (!init) return Response.json(configuredSettings);
    return Response.json({
      ...configuredSettings,
      version: 2,
      fallback_configured: false,
      fallback_fingerprint: null,
    });
  });
  vi.stubGlobal("fetch", fetcher);
  render(<AgentSettingsPanel />);
  await screen.findByText("指纹 a288df15c5fc");

  fireEvent.click(screen.getByRole("button", { name: "清除备用密钥" }));
  const submit = screen.getByRole("button", { name: "确认清除" });
  expect(submit).toBeDisabled();
  fireEvent.change(screen.getByLabelText("输入确认短语"), {
    target: { value: "clear fallback key" },
  });
  expect(submit).toBeDisabled();
  fireEvent.change(screen.getByLabelText("输入确认短语"), {
    target: { value: "CLEAR FALLBACK KEY" },
  });
  expect(submit).toBeEnabled();
  fireEvent.click(submit);

  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
  const [, init] = fetcher.mock.calls[1];
  expect(JSON.parse(String(init?.body))).toEqual({
    expected_version: 1,
    endpoint: "https://provider.example/v1",
    model: "gpt-test",
    clear_fallback: true,
    confirmation: "CLEAR FALLBACK KEY",
  });
  expect(await screen.findByText("备用密钥已清除")).toBeInTheDocument();
});


test("tests each configured slot without putting key material in the request", async () => {
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (!init) return Response.json(configuredSettings);
    return Response.json({ slot: "primary", status: "healthy", latency_ms: 18 });
  });
  vi.stubGlobal("fetch", fetcher);
  render(<AgentSettingsPanel />);
  await screen.findByText("指纹 1eb90d4a288b");

  fireEvent.click(screen.getByRole("button", { name: "测试主密钥" }));

  expect(await screen.findByText("主密钥连接正常，18 ms")).toBeInTheDocument();
  const [, init] = fetcher.mock.calls[1];
  expect(JSON.parse(String(init?.body))).toEqual({ slot: "primary" });
  expect(String(init?.body)).not.toContain("secret");
});
