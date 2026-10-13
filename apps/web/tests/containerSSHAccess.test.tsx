import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, expect, test, vi } from "vitest";
import { ContainerSSHAccess } from "../components/ContainerSSHAccess";
import { RoleOverview } from "../components/RoleOverview";

afterEach(() => vi.unstubAllGlobals());
const props = { containerId: "c1", name: "my-job", command: "ssh researcher@203.0.113.10 -p 23002", isOwner: true };

test("password is directly above SSH, copies separately and refreshes after a reset", async () => {
  const copy = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: copy } });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(Response.json({ password: "initial-ssh-password", state: "saved" }))
    .mockResolvedValueOnce(Response.json({ password: "new-ssh-password", state: "saved" })));
  const { rerender } = render(<ContainerSSHAccess {...props} />);
  const password = await screen.findByText("initial-ssh-password");
  expect(password.compareDocumentPosition(screen.getByText(props.command)) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "复制 my-job 的 SSH 密码" }));
  await screen.findByText("密码已复制");
  expect(copy).toHaveBeenCalledWith("initial-ssh-password");
  rerender(<ContainerSSHAccess {...props} revision={1} />);
  await screen.findByText("new-ssh-password");
  expect(screen.queryByText("initial-ssh-password")).not.toBeInTheDocument();
});

test("nonowner never fetches or displays credentials", () => {
  const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
  render(<ContainerSSHAccess {...props} isOwner={false} />);
  expect(fetcher).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: /SSH 密码/ })).not.toBeInTheDocument();
  expect(screen.getByText(props.command)).toBeVisible();
});

test("legacy credentials explain the missing password, provide settings and do not change it", async () => {
  const fetcher = vi.fn().mockResolvedValue(Response.json({ password: null, state: "legacy" }));
  vi.stubGlobal("fetch", fetcher);
  const settings = vi.fn();
  render(<ContainerSSHAccess {...props} onSettings={settings} />);
  await screen.findByText(/旧密码未保存，仍可正常使用/);
  fireEvent.click(screen.getByRole("button", { name: "设置 SSH 密码" }));
  expect(settings).toHaveBeenCalledOnce();
  expect(fetcher).toHaveBeenCalledOnce();
  expect(fetcher.mock.calls[0][1].method).toBeUndefined();
});

test("credential loading error provides a working retry", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValueOnce(new Error("网络中断"))
    .mockResolvedValueOnce(Response.json({ password: "retry-ssh-password", state: "saved" })));
  render(<ContainerSSHAccess {...props} />);
  fireEvent.click(await screen.findByRole("button", { name: "重试读取密码" }));
  await screen.findByText("retry-ssh-password");
  await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
});

test.each(["user", "administrator"] as const)("%s overview routes approvals to the corresponding workspace", (persona) => {
  const navigate = vi.fn();
  render(<RoleOverview actor={{ userId: "test", role: persona === "user" ? "user" : "admin" }}
    overview={{ persona, generated_at: "2026-09-21T00:00:00Z", tasks: { pending_approvals: { count: 2, items: [] } } }} onNavigate={navigate} />);
  fireEvent.click(screen.getByRole("button", { name: persona === "user" ? "查看2 项我的申请待审批" : "查看2 项待审批申请" }));
  expect(navigate).toHaveBeenCalledWith("approvals");
});
