import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, test, vi } from "vitest";

import { LoginScreen } from "../components/LoginScreen";


afterEach(() => {
  vi.unstubAllGlobals();
});


test("logs in with optional administrator totp and uses a generic error", async () => {
  const fetcher = vi.fn(async () =>
    Response.json({ detail: "internal authentication detail" }, { status: 401 }));
  vi.stubGlobal("fetch", fetcher);
  render(<LoginScreen />);

  fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "admin" } });
  fireEvent.change(screen.getByLabelText("密码"), { target: { value: "password" } });
  fireEvent.change(screen.getByLabelText("动态验证码（管理员必填）"), {
    target: { value: "123456" },
  });
  fireEvent.click(screen.getByRole("button", { name: "登录" }));

  await waitFor(() =>
    expect(screen.getByRole("alert")).toHaveTextContent("账号、密码或验证码无效"),
  );
  expect(screen.queryByText("internal authentication detail")).not.toBeInTheDocument();
  expect(screen.getByRole("alert").textContent?.length).toBeLessThanOrEqual(32);
  expect(fetcher).toHaveBeenCalledWith("/api/gsm/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      username: "admin",
      password: "password",
      totp_code: "123456",
    }),
  });
});

test.each([
  [429, "尝试次数过多"],
  [502, "登录服务暂时不可用"],
  [503, "登录服务暂时不可用"],
  [403, "登录请求未被接受"],
])("does not call HTTP %s a password error", async (status, message) => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "private internal detail" }, { status })));
  render(<LoginScreen />);
  fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "admin" } });
  fireEvent.change(screen.getByLabelText("密码"), { target: { value: "password" } });
  fireEvent.click(screen.getByRole("button", { name: "登录" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(message));
  expect(screen.getByRole("alert")).not.toHaveTextContent("密码或验证码无效");
  expect(screen.queryByText("private internal detail")).not.toBeInTheDocument();
});

test("explains network failures without blaming credentials", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
  render(<LoginScreen />);
  fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "admin" } });
  fireEvent.change(screen.getByLabelText("密码"), { target: { value: "password" } });
  fireEvent.click(screen.getByRole("button", { name: "登录" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("无法连接登录服务"));
});
