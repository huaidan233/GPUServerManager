import { fireEvent, render, screen, within } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { expect, test, vi } from "vitest";
import { ResourceGovernancePanel } from "../components/ResourceGovernancePanel";
import { TeacherWorkspace } from "../components/TeacherWorkspace";
import { mockDashboardData, type ApprovalSummary } from "../lib/mockData";

const approval: ApprovalSummary = {
  id: "approval-details", requesterUserId: "alice", ownerUserId: "alice", status: "pending",
  triggerReasons: ["memory_gb"], createdAt: "2026-09-21T00:00:00Z", expiresAt: "2026-09-22T00:00:00Z",
  specification: { name: "model-train", server_id: "server1", image: "gsm/pytorch-cuda-ssh:latest", preset: "Custom",
    cpu_limit: 8, memory_limit_gb: 48, gpu_devices: ["1"], mount_profile_ids: ["workspace-default"],
    ssh_enabled: true, auto_start: true, estimated_duration_minutes: 240, note: "Complete experiment B for the lab meeting" },
};

test("administrator can inspect exact requested resources and later review decision details", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "approved", reviewer_user_id: "admin", review_reason: "Resources available", decided_at: "2026-09-21T01:00:00Z" })));
  render(<ResourceGovernancePanel actor={{ ...mockDashboardData.actor, role: "admin" }} servers={mockDashboardData.servers}
    initialRecommendations={[]} initialQueue={[]} initialApprovals={[approval]} mode="approvals" />);
  fireEvent.click(screen.getByRole("button", { name: "查看申请详情 approval-details" }));
  const dialog = within(screen.getByRole("dialog", { name: "申请详情" }));
  expect(dialog.getByText("8 核")).toBeVisible(); expect(dialog.getByText("48 GiB")).toBeVisible();
  expect(dialog.getByText("GPU 1")).toBeVisible(); expect(dialog.getByText(approval.specification!.image)).toBeVisible();
  expect(dialog.getByText(approval.specification!.note!)).toBeVisible();
  expect(dialog.getByText("240 分钟")).toBeVisible();
  fireEvent.click(dialog.getByRole("button", { name: "关闭申请详情" }));
  fireEvent.change(screen.getByLabelText("审批理由 approval-details"), { target: { value: "Resources available" } });
  fireEvent.click(screen.getByRole("button", { name: "批准申请 approval-details" }));
  await screen.findByText("申请 approval-details 已批准");
  fireEvent.click(screen.getByRole("button", { name: "查看申请详情 approval-details" }));
  expect(screen.getByText("Resources available")).toBeVisible();
  vi.unstubAllGlobals();
});

test("teacher sees the same requested configuration before deciding an assigned student's request", () => {
  render(<TeacherWorkspace initialStudents={[]} initialApprovals={[approval]} view="approvals" />);
  fireEvent.click(screen.getByRole("button", { name: "查看申请详情 approval-details" }));
  expect(screen.getByText("48 GiB")).toBeVisible();
  expect(screen.getByText(approval.specification!.note!)).toBeVisible();
});
