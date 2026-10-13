import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, test, vi } from "vitest";

import { AccountPanel } from "../components/AccountPanel";


afterEach(() => {
  vi.unstubAllGlobals();
});


test("loads only the signed-in member account resources", async () => {
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    const url = input.toString();
    const bodies: Record<string, unknown> = {
      "/api/gsm/account": {
        id: "member-alice",
        username: "alice",
        display_name: "Alice Zhang",
        role: "user",
        status: "active",
        current_program: {
          id: "program-alice",
          level: "master",
          enrollment_date: "2026-09-01",
          expected_graduation_date: "2029-07-01",
        },
      },
      "/api/gsm/account/programs": [
        {
          id: "program-alice",
          level: "master",
          enrollment_date: "2026-09-01",
          expected_graduation_date: "2029-07-01",
        },
      ],
      "/api/gsm/account/wechat-binding": {
        binding: { route_key: "wechat-bot:chat-1:wx-alice", status: "active" },
      },
      "/api/gsm/account/sessions": [
        {
          id: "session-current",
          created_at: "2026-08-07T08:00:00Z",
          revoked_at: null,
          browser_family: "Chromium",
          os_family: "Windows",
          is_current: true,
        },
      ],
    };
    return Response.json(bodies[url]);
  });
  vi.stubGlobal("fetch", fetcher);

  render(<AccountPanel />);

  expect(await screen.findByRole("heading", { name: "Alice Zhang" })).toBeInTheDocument();
  expect(screen.getByText("硕士")).toBeInTheDocument();
  expect(screen.getByText("2026-09-01")).toBeInTheDocument();
  expect(screen.getByText("2029-07-01")).toBeInTheDocument();
  expect(screen.getByText("微信已绑定")).toBeInTheDocument();
  expect(screen.getByText("session-current")).toBeInTheDocument();
  expect(screen.getByText("Chromium · Windows")).toBeInTheDocument();
  expect(screen.getByText("当前会话")).toBeInTheDocument();
  expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
    "/api/gsm/account",
    "/api/gsm/account/programs",
    "/api/gsm/account/wechat-binding",
    "/api/gsm/account/sessions",
  ]);
  expect(fetcher.mock.calls.every(([url]) => !url.toString().includes("member_id"))).toBe(true);
});


test("revokes other sessions and logs out without accepting a target identity", async () => {
  const loggedOut = vi.fn();
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    const url = input.toString();
    if (url === "/api/gsm/account") {
      return Response.json({
        id: "member-alice",
        username: "alice",
        display_name: "Alice",
        role: "user",
        status: "active",
        current_program: null,
      });
    }
    if (url.endsWith("wechat-binding")) return Response.json({ binding: null });
    if (url.endsWith("sessions")) {
      return Response.json([
        { id: "current", is_current: true, revoked_at: null },
        { id: "other", is_current: false, revoked_at: null },
      ]);
    }
    if (url.endsWith("revoke-others")) {
      return Response.json({ status: "revoked", revoked_count: 1 });
    }
    if (url.endsWith("logout")) return Response.json({ status: "logged_out" });
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetcher);
  render(<AccountPanel onLoggedOut={loggedOut} />);
  await screen.findByRole("heading", { name: "Alice" });

  fireEvent.click(screen.getByRole("button", { name: "撤销其他会话" }));
  fireEvent.change(screen.getByLabelText("确认当前密码"), {
    target: { value: "correct horse battery staple" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认撤销" }));

  await waitFor(() => expect(screen.getByText("已撤销 1 个其他会话。")).toBeInTheDocument());
  expect(fetcher).toHaveBeenCalledWith("/api/gsm/auth/sessions/revoke-others", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: "correct horse battery staple" }),
  });

  fireEvent.click(screen.getByRole("button", { name: "退出登录" }));
  await waitFor(() => expect(loggedOut).toHaveBeenCalledTimes(1));
  expect(fetcher).toHaveBeenCalledWith("/api/gsm/auth/logout", { method: "POST" });
  const mutationBodies = fetcher.mock.calls
    .map(([, init]) => init?.body?.toString() ?? "")
    .join(" ");
  expect(mutationBodies).not.toMatch(/member_id|user_id|role/);
});


test("creates a one-time WeChat pairing code and unbinds with password confirmation", async () => {
  let binding: { route_key: string; status: string } | null = null;
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    const url = input.toString();
    if (url === "/api/gsm/account") {
      return Response.json({
        id: "member-alice",
        username: "alice",
        display_name: "Alice",
        role: "user",
        status: "active",
        current_program: null,
      });
    }
    if (url.endsWith("wechat-binding")) return Response.json({ binding });
    if (url.endsWith("wechat-pairing-code")) {
      return Response.json({ wechat_pairing_code: "pair-once" }, { status: 201 });
    }
    if (url.endsWith("wechat-unbind")) {
      binding = null;
      return Response.json({ wechat_binding: null });
    }
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetcher);
  const { rerender } = render(<AccountPanel />);
  await screen.findByRole("heading", { name: "Alice" });

  fireEvent.click(screen.getByRole("button", { name: "生成微信配对码" }));
  fireEvent.change(screen.getByLabelText("确认当前密码"), {
    target: { value: "password-value" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认生成" }));
  expect(await screen.findByText("pair-once")).toBeInTheDocument();
  expect(screen.getByText("关闭后不再显示")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "已妥善记录" }));

  binding = { route_key: "wechat-bot:chat-1:wx-alice", status: "active" };
  rerender(<AccountPanel />);
  fireEvent.click(screen.getByRole("button", { name: "刷新微信状态" }));
  expect(await screen.findByRole("button", { name: "解绑微信" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "解绑微信" }));
  fireEvent.change(screen.getByLabelText("确认当前密码"), {
    target: { value: "password-value" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认解绑" }));

  await waitFor(() => expect(screen.getByText("微信已解绑。")).toBeInTheDocument());
  expect(fetcher).toHaveBeenCalledWith(
    "/api/gsm/account/wechat-pairing-code",
    expect.objectContaining({ body: JSON.stringify({ password: "password-value" }) }),
  );
  expect(fetcher).toHaveBeenCalledWith(
    "/api/gsm/account/wechat-unbind",
    expect.objectContaining({ body: JSON.stringify({ password: "password-value" }) }),
  );
});


test("changes the signed-in password without accepting a target member id", async () => {
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    const url = input.toString();
    if (url === "/api/gsm/auth/password/change") {
      return Response.json({ status: "changed" });
    }
    if (url === "/api/gsm/account") {
      return Response.json({
        id: "member-alice",
        username: "alice",
        display_name: "Alice",
        role: "user",
        status: "active",
        current_program: null,
      });
    }
    if (url.endsWith("wechat-binding")) return Response.json({ binding: null });
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetcher);
  render(<AccountPanel />);
  await screen.findByRole("heading", { name: "Alice" });

  fireEvent.change(screen.getByLabelText("当前密码"), { target: { value: "old-password" } });
  fireEvent.change(screen.getByLabelText("新密码"), {
    target: { value: "new-password-long-enough" },
  });
  fireEvent.click(screen.getByRole("button", { name: "更新密码" }));

  await waitFor(() => expect(screen.getByText("密码已更新。" )).toBeInTheDocument());
  expect(fetcher).toHaveBeenCalledWith("/api/gsm/auth/password/change", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      current_password: "old-password",
      new_password: "new-password-long-enough",
    }),
  });
});
