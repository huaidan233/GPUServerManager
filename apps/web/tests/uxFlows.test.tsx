import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, expect, test, vi } from "vitest";
import { ResourceGovernancePanel } from "../components/ResourceGovernancePanel";
import { ContainerOperationsTable } from "../components/ContainerOperationsTable";
import { AccessibleDialog } from "../components/AccessibleDialog";
import { Dashboard } from "../components/Dashboard";
import { mockDashboardData, type ApprovalSummary } from "../lib/mockData";
import { QuotaRequestsPanel } from "../components/PersonalQuotaPanel";
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), replace: vi.fn() }) }));
afterEach(() => vi.unstubAllGlobals());
const approval: ApprovalSummary = { id: "owned", requesterUserId: "alice", ownerUserId: "alice", status: "pending", triggerReasons: ["memory_gb"], createdAt: "2026-09-21T03:00:00Z", expiresAt: "2026-09-22T03:00:00Z" };
const props = { actor: { userId: "alice", role: "user" as const }, servers: mockDashboardData.servers, initialRecommendations: [], initialQueue: [] };

test("member sees refreshed approval, executes it once and retains its history", async () => {
  let finish: (r: Response) => void = () => undefined;
  const fetcher = vi.fn(() => new Promise<Response>(resolve => { finish = resolve; }));
  vi.stubGlobal("fetch", fetcher);
  const { rerender } = render(<ResourceGovernancePanel {...props} initialApprovals={[approval]} mode="queue" />);
  expect(screen.getByText("待审批")).toBeVisible();
  expect(screen.queryByRole("button", { name: "批准申请 owned" })).not.toBeInTheDocument();
  rerender(<ResourceGovernancePanel {...props} initialApprovals={[{ ...approval, status: "approved" }]} mode="queue" />);
  const create = screen.getByRole("button", { name: "创建已批准容器 owned" });
  fireEvent.click(create);
  expect(create).toBeDisabled();
  fireEvent.click(create);
  expect(fetcher).toHaveBeenCalledTimes(1);
  finish(Response.json({ container: { id: "created-1", name: "approved-job", status: "running", ssh_command: "ssh researcher@203.0.113.10 -p 23002" } }, { status: 201 }));
  await screen.findByText("已创建");
  expect(screen.getByRole("status")).toHaveTextContent("已创建并启动");
  fireEvent.click(screen.getByRole("button", { name: "查看申请详情 owned" }));
  expect(screen.getByText("created-1")).toBeVisible();
});

test("designated teacher can review their own application only at the second stage", async () => {
  const base = { user_id: "alice", purpose: "研究训练用途", limits: { cpu: 16, memory_gb: 16, gpus: 1 },
    starts_at: "2026-10-12T00:00:00Z", ends_at: "2026-10-15T00:00:00Z", administrator_id: "admin", teacher_id: "teacher", reviews: [] };
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => input.toString().endsWith("quota/me")
    ? Response.json({ baseline: base.limits, limits: base.limits, used: { cpu: 0, memory_gb: 0, gpus: 0 }, remaining: base.limits, grants: [] })
    : Response.json([{ ...base, id: "owned", status: "pending_teacher" }, { ...base, id: "early", status: "pending_admin" }, { ...base, id: "done", status: "approved" }])));
  render(<QuotaRequestsPanel memberId="teacher" />);
  const buttons = await screen.findAllByRole("button", { name: "通过本级审批" });
  expect(buttons).toHaveLength(1);
  expect(buttons[0]).toBeDisabled();
  fireEvent.change(screen.getByLabelText("审批说明"), { target: { value: "同意研究用途" } });
  expect(buttons[0]).toBeEnabled();
  expect(screen.queryByLabelText("一级审批：管理员")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("二级审批：教师")).not.toBeInTheDocument();
});

test("SSH command copies from the list and failed copying explains the fallback", async () => {
  const copy = vi.fn().mockResolvedValueOnce(undefined).mockRejectedValueOnce(new Error("denied"));
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: copy } });
  render(<ContainerOperationsTable containers={[{ ...mockDashboardData.containers[0], sshCommand: "ssh researcher@203.0.113.10 -p 23001" }]} />);
  const button = screen.getByRole("button", { name: /复制 .* 的 SSH 命令/ });
  fireEvent.click(button);
  await screen.findByText("已复制");
  expect(copy).toHaveBeenCalledWith("ssh researcher@203.0.113.10 -p 23001");
  fireEvent.click(button);
  await screen.findByText("复制失败，请选中命令手动复制。");
});

test("stop errors remain in the confirmation dialog and keyboard Escape restores focus", async () => {
  const c = mockDashboardData.containers[0];
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(Response.json({ confirmation: { id: "confirm-1", required_phrase: "STOP job" } }, { status: 202 }))
    .mockResolvedValueOnce(Response.json({ detail: "状态发生变化，请刷新后重试。" }, { status: 409 })));
  render(<ContainerOperationsTable containers={[c]} />);
  const trigger = screen.getByRole("button", { name: `停止 ${c.name}` });
  trigger.focus(); fireEvent.click(trigger);
  const dialog = within(await screen.findByRole("dialog", { name: "确认容器操作" }));
  fireEvent.change(dialog.getByLabelText("确认短语"), { target: { value: "STOP job" } });
  fireEvent.submit(dialog.getByLabelText("确认短语").closest("form")!);
  expect(await dialog.findByRole("alert")).toHaveTextContent("状态发生变化");
  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});

test("nested dialogs only allow the topmost dialog to handle Escape", () => {
  const outer = vi.fn(), inner = vi.fn();
  render(<><AccessibleDialog title="外层" onClose={outer}><button>外层操作</button></AccessibleDialog>
    <AccessibleDialog title="内层" onClose={inner}><button>内层操作</button></AccessibleDialog></>);
  fireEvent.keyDown(document, { key: "Escape" });
  expect(inner).toHaveBeenCalledOnce(); expect(outer).not.toHaveBeenCalled();
});

test("unknown deletion outcome keeps the workload visible and reports uncertainty", async () => {
  const c = mockDashboardData.containers[0];
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(Response.json({ confirmation: { id: "unknown", required_phrase: "DELETE job" } }, { status: 202 }))
    .mockResolvedValueOnce(Response.json({ status: "execution_unknown", operation_id: "operation-1" })));
  render(<ContainerOperationsTable containers={[c]} />);
  fireEvent.click(screen.getByRole("button", { name: `删除 ${c.name}` }));
  fireEvent.change(await screen.findByLabelText("确认短语"), { target: { value: "DELETE job" } });
  fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("操作结果未知"));
  expect(screen.getByRole("button", { name: `管理 ${c.name}` })).toBeInTheDocument();
});
