import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, expect, test, vi } from "vitest";
import { FirstLoginScreen } from "../components/FirstLoginScreen";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test("requires a different matching password before activating the account", async () => {
  const fetcher = vi.fn(async () => Response.json({ status: "password_changed" }));
  vi.stubGlobal("fetch", fetcher);
  render(<FirstLoginScreen username="teacher-a" isAdmin={false} />);
  fireEvent.change(screen.getByLabelText("初始密码"), { target: { value: "initial-password" } });
  fireEvent.change(screen.getByLabelText("新密码（至少 12 位）"), { target: { value: "initial-password" } });
  fireEvent.change(screen.getByLabelText("再次输入新密码"), { target: { value: "initial-password" } });
  fireEvent.click(screen.getByRole("button", { name: "激活并保存新密码" }));
  expect(screen.getByRole("alert")).toHaveTextContent("新密码不能与初始密码相同");
  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText("新密码（至少 12 位）"), { target: { value: "my-new-password" } });
  fireEvent.change(screen.getByLabelText("再次输入新密码"), { target: { value: "my-new-password" } });
  fireEvent.click(screen.getByRole("button", { name: "激活并保存新密码" }));
  expect(await screen.findByRole("heading", { name: "账号已激活" })).toBeInTheDocument();
  await waitFor(() => expect(fetcher).toHaveBeenCalledWith("/api/gsm/auth/password/change", expect.objectContaining({
    body: JSON.stringify({ current_password: "initial-password", new_password: "my-new-password" }),
  })));
});

test("new administrator must save personal MFA enrollment before submitting", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ secret: "MYTESTSECRET", recovery_codes: ["one-recovery-code"] })));
  render(<FirstLoginScreen username="new-admin" isAdmin />);
  expect(screen.getByRole("button", { name: "激活并保存新密码" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "生成验证器设置" }));
  expect(await screen.findByText("MYTESTSECRET")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "激活并保存新密码" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox"));
  expect(screen.getByRole("button", { name: "激活并保存新密码" })).toBeEnabled();
});
