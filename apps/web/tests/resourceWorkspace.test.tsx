import { act, fireEvent, render, screen, within } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ContainerOperationsTable } from "../components/ContainerOperationsTable";
import { Dashboard } from "../components/Dashboard";
import { ResourceUsageSummary } from "../components/ResourceUsageSummary";
import { mockDashboardData, mockGpus } from "../lib/mockData";

const navigation = vi.hoisted(() => ({ replace: vi.fn(), refresh: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => navigation }));

const memberData = {
  ...mockDashboardData,
  actor: { ...mockDashboardData.actor, memberId: "member-alice", userId: "alice", role: "user" as const, status: "active" },
  containers: mockDashboardData.containers.map(container => ({ ...container, ownerMemberId: "member-alice", user: "alice", sshCommand: "" })),
};

beforeEach(() => {
  navigation.refresh.mockClear();
  navigation.replace.mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

test.each(["admin", "teacher", "user"] as const)("%s personal container list excludes other members and uses member ID before legacy username", role => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ password: null, state: "not_set" })));
  const own = { ...mockDashboardData.containers[0], id: "own", name: "my-research", ownerMemberId: "member-alice", user: "alice", sshCommand: "" };
  const legacy = { ...own, id: "legacy", name: "my-legacy", ownerMemberId: undefined, status: "stopped" };
  const other = { ...own, id: "other", name: "someone-else", ownerMemberId: "member-other" };
  render(<Dashboard data={{ ...mockDashboardData, actor: { userId: "alice", memberId: "member-alice", role }, containers: [other, legacy, own] }} initialWorkspace="resources" />);
  expect(screen.getByRole("heading", { name: "我的容器" })).toBeVisible();
  const table = within(screen.getByRole("table", { name: "容器列表" }));
  expect(table.getByRole("button", { name: "管理 my-research" })).toBeVisible();
  expect(table.getByRole("button", { name: "管理 my-legacy" })).toBeVisible();
  expect(table.queryByRole("button", { name: "管理 someone-else" })).not.toBeInTheDocument();
  expect(screen.getByText("1 个运行中 · 共 2 个 · 运行中的容器优先显示。")).toBeVisible();
});

test("administrator with no containers sees a personal empty state and can go to member management", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json([])));
  render(<Dashboard data={mockDashboardData} initialWorkspace="resources" />);
  expect(screen.getByText("你还没有容器")).toBeVisible();
  expect(screen.getByText("0 个运行中 · 共 0 个 · 运行中的容器优先显示。")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: /前往成员列表管理/ }));
  expect(navigation.replace).toHaveBeenCalledWith("/?workspace=members", { scroll: false });
  expect(await screen.findByRole("table", { name: "课题组成员列表" })).toBeVisible();
});

test.each([
  ["user", "active"], ["power_user", "active"], ["teacher", "active"], ["viewer", "active"],
  ["admin", "pending_exit"], ["admin", "disabled"],
] as const)("%s (%s) cannot see the administrator container shortcut or enter member management directly", (role, status) => {
  const fetcher = vi.fn(async () => Response.json([]));
  vi.stubGlobal("fetch", fetcher);
  const data = { ...mockDashboardData, actor: { ...mockDashboardData.actor, role, status }, containers: [] };
  const { rerender } = render(<Dashboard data={data} initialWorkspace="resources" />);
  expect(screen.queryByRole("button", { name: /前往成员列表管理/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("tab", { name: "成员管理" })).not.toBeInTheDocument();
  rerender(<Dashboard data={data} initialWorkspace="members" />);
  expect(screen.getByRole("tab", { name: "概览" })).toHaveAttribute("aria-selected", "true");
  expect(screen.queryByRole("table", { name: "课题组成员列表" })).not.toBeInTheDocument();
  expect(fetcher).not.toHaveBeenCalledWith("/api/gsm/admin/members");
});

test("live GPU resource page refreshes every fifteen seconds", () => {
  vi.useFakeTimers();
  render(<Dashboard data={{ ...memberData, source: "api", servers: [] }} initialWorkspace="resources" />);
  act(() => vi.advanceTimersByTime(15000));
  expect(navigation.refresh).toHaveBeenCalledTimes(1);
  act(() => vi.advanceTimersByTime(15000));
  expect(navigation.refresh).toHaveBeenCalledTimes(2);
});

test("container list shows compact personal quota and moves diagnostics to overview", () => {
  const reversed = [...memberData.containers].reverse();
  render(<Dashboard data={{ ...memberData, containers: reversed }} initialWorkspace="resources" />);
  const summary = screen.getByRole("region", { name: "个人运行额度" });
  const containers = screen.getByRole("region", { name: "容器管理" });

  expect(summary.compareDocumentPosition(containers) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(screen.queryByRole("region", { name: "服务状态" })).not.toBeInTheDocument();
  expect(summary).toHaveClass("is-compact");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("CPU（核）")).not.toBeInTheDocument();
  const rows = within(containers).getAllByRole("row");
  expect(rows.slice(1).map((row) => within(row).getAllByRole("cell")[0].textContent)).toEqual([
    ...reversed.filter((container) => container.status === "running"),
    ...reversed.filter((container) => container.status !== "running"),
  ].map((container) => container.name));
  expect(screen.getByRole("button", { name: "创建容器" })).toBeEnabled();
});

test("creation opens in a focus-trapped dialog and cancel restores the page", () => {
  render(<Dashboard data={memberData} initialWorkspace="resources" />);
  const trigger = screen.getByRole("button", { name: "创建容器" });
  fireEvent.click(trigger);

  const dialog = within(screen.getByRole("dialog", { name: "创建容器" }));
  const close = dialog.getByRole("button", { name: "关闭创建容器" });
  const submit = dialog.getByRole("button", { name: "创建容器" });
  expect(close).toHaveFocus();
  expect(document.body.style.overflow).toBe("hidden");
  expect(dialog.queryByRole("button", { name: "删除草稿容器" })).not.toBeInTheDocument();
  expect(dialog.getByRole("button", { name: "拉取镜像" })).toBeDisabled();
  fireEvent.keyDown(close, { key: "Tab", shiftKey: true });
  expect(submit).toHaveFocus();
  fireEvent.keyDown(submit, { key: "Tab" });
  expect(close).toHaveFocus();
  fireEvent.keyDown(close, { key: "Escape" });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
  expect(document.body.style.overflow).not.toBe("hidden");

  fireEvent.click(trigger);
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "取消" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});

test("losing creation permission closes the modal and unlocks the page", () => {
  const { rerender } = render(<Dashboard data={memberData} initialWorkspace="resources" />);
  if (screen.queryByLabelText("SSH 初始密码（至少 12 位）")) fireEvent.change(screen.getByLabelText("SSH 初始密码（至少 12 位）"), { target: { value: "creation-ssh-password" } });
  fireEvent.click(screen.getByRole("button", { name: "创建容器" }));
  expect(document.body.style.overflow).toBe("hidden");
  rerender(<Dashboard data={{ ...memberData, actor: { ...memberData.actor, status: "pending_exit" } }} initialWorkspace="resources" />);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(document.body.style.overflow).not.toBe("hidden");
  expect(screen.getByRole("button", { name: "创建容器" })).toBeDisabled();
});

test("an in-flight creation cannot be dismissed and completion refreshes the list", async () => {
  let resolveRequest: (response: Response) => void = () => { throw new Error("request not started"); };
  const fetchMock = vi.fn(() => new Promise<Response>((resolve) => { resolveRequest = resolve; }));
  vi.stubGlobal("fetch", fetchMock);
  render(<Dashboard data={memberData} initialWorkspace="resources" />);
  if (screen.queryByLabelText("SSH 初始密码（至少 12 位）")) fireEvent.change(screen.getByLabelText("SSH 初始密码（至少 12 位）"), { target: { value: "creation-ssh-password" } });
  fireEvent.click(screen.getByRole("button", { name: "创建容器" }));
  const dialog = within(screen.getByRole("dialog"));
  if (screen.queryByLabelText("SSH 初始密码（至少 12 位）")) fireEvent.change(screen.getByLabelText("SSH 初始密码（至少 12 位）"), { target: { value: "creation-ssh-password" } });
  fireEvent.click(dialog.getByRole("button", { name: "创建容器" }));

  expect(dialog.getByRole("button", { name: "关闭创建容器" })).toBeDisabled();
  expect(dialog.getByRole("button", { name: "取消" })).toBeDisabled();
  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledTimes(1);

  await act(async () => { resolveRequest(Response.json({ container: { name: "new-container" } }, { status: 201 })); });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.getByText("容器 new-container 已创建。")).toBeInTheDocument();
  expect(navigation.refresh).toHaveBeenCalledTimes(1);
});

test("approval submission closes the dialog without reporting a created container", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ requires_approval: true, approval: { id: "approval-custom" } }, { status: 202 })));
  render(<Dashboard data={memberData} initialWorkspace="resources" />);
  if (screen.queryByLabelText("SSH 初始密码（至少 12 位）")) fireEvent.change(screen.getByLabelText("SSH 初始密码（至少 12 位）"), { target: { value: "creation-ssh-password" } });
  fireEvent.click(screen.getByRole("button", { name: "创建容器" }));
  const dialog = within(screen.getByRole("dialog"));
  fireEvent.change(dialog.getByLabelText("内存（GiB）"), { target: { value: "48" } });
  if (screen.queryByLabelText("SSH 初始密码（至少 12 位）")) fireEvent.change(screen.getByLabelText("SSH 初始密码（至少 12 位）"), { target: { value: "creation-ssh-password" } });
  fireEvent.click(dialog.getByRole("button", { name: "创建容器" }));

  expect(await screen.findByText("已提交管理员审批：approval-custom")).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.queryByText(/容器 .* 已创建/)).not.toBeInTheDocument();
});

test("creation errors keep custom values in the dialog for correction", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "镜像不可用" }, { status: 400 })));
  render(<Dashboard data={memberData} initialWorkspace="resources" />);
  if (screen.queryByLabelText("SSH 初始密码（至少 12 位）")) fireEvent.change(screen.getByLabelText("SSH 初始密码（至少 12 位）"), { target: { value: "creation-ssh-password" } });
  fireEvent.click(screen.getByRole("button", { name: "创建容器" }));
  const dialog = within(screen.getByRole("dialog"));
  fireEvent.change(dialog.getByLabelText("CPU（核）"), { target: { value: "6" } });
  if (screen.queryByLabelText("SSH 初始密码（至少 12 位）")) fireEvent.change(screen.getByLabelText("SSH 初始密码（至少 12 位）"), { target: { value: "creation-ssh-password" } });
  fireEvent.click(dialog.getByRole("button", { name: "创建容器" }));

  expect(await dialog.findByRole("alert")).toHaveTextContent("镜像不可用");
  expect(dialog.getByLabelText("CPU（核）")).toHaveValue(6);
  expect(dialog.getByLabelText("资源套餐")).toHaveValue("Custom");
  expect(dialog.getByRole("button", { name: "取消" })).toBeEnabled();
  expect(navigation.refresh).not.toHaveBeenCalled();
});

test("stale GPU observations are not presented as available capacity", () => {
  render(<ResourceUsageSummary gpus={[{ ...mockGpus[0], state: "free", freshness: "stale" }]} serverCount={1} runningCount={0} refreshing={false} onRefresh={vi.fn()} />);
  const strip = within(screen.getByRole("list", { name: "GPU 占用速览" }));
  expect(strip.getByText("未知")).toBeInTheDocument();
  expect(strip.getByText("暂无有效观测")).toBeInTheDocument();
  expect(strip.queryByText("空闲")).not.toBeInTheDocument();
});

test("container list accepts refreshed server props after creation", () => {
  const { rerender } = render(<ContainerOperationsTable containers={[]} />);
  expect(screen.queryByText("new-custom")).not.toBeInTheDocument();
  rerender(<ContainerOperationsTable containers={[{ ...mockDashboardData.containers[0], id: "new-custom", name: "new-custom", cpu: 6, memoryGb: 24 }]} />);
  expect(screen.getByText("new-custom")).toBeInTheDocument();
  expect(screen.getByText("6C / 24G")).toBeInTheDocument();
});
