import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";

import { ResourceGovernancePanel } from "../components/ResourceGovernancePanel";
import type { ApprovalSummary, GpuRecommendation, QueueSummary } from "../lib/mockData";

afterEach(() => {
  vi.unstubAllGlobals();
});

const recommendations: GpuRecommendation[] = [
  {
    serverId: "server1",
    gpuUuid: "GPU-a",
    index: 0,
    state: "shareable",
    safeToShare: true,
    memoryFreeGb: 32,
    reasonCodes: ["mounted_without_process", "advisory_only"],
  },
  {
    serverId: "server1",
    gpuUuid: "GPU-b",
    index: 1,
    state: "unknown",
    safeToShare: false,
    memoryFreeGb: 0,
    reasonCodes: ["stale_observation"],
  },
];

test("renders memory tiers and server-provided recommendation states", () => {
  render(
    <ResourceGovernancePanel
      actor={{ userId: "alice", role: "user" }}
      servers={[{ id: "server1", name: "Server1", gpuCount: 8 }]}
      initialRecommendations={recommendations}
      initialQueue={[]}
      initialApprovals={[]}
    />,
  );

  expect(screen.getByRole("button", { name: "8–24 GB" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  expect(screen.getByText("可安全共享")).toBeInTheDocument();
  expect(screen.getByText("未知")).toBeInTheDocument();
  expect(screen.getByText("已挂载但无计算进程")).toBeInTheDocument();
});

test("joins and withdraws from the advisory queue without reservation wording", async () => {
  const queued: QueueSummary = {
    id: "q1",
    userId: "alice",
    gpuCount: 1,
    memoryTier: "8_24",
    estimatedDurationMinutes: 120,
    compatibleServerIds: ["server1"],
    status: "waiting",
    createdAt: "2026-08-04T00:00:00Z",
    position: 1,
  };
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce({ ok: true, status: 201, json: async () => queued } as Response)
    .mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ ...queued, status: "withdrawn" }),
    } as Response);
  vi.stubGlobal("fetch", fetchMock);

  render(
    <ResourceGovernancePanel
      actor={{ userId: "alice", role: "user" }}
      servers={[{ id: "server1", name: "Server1", gpuCount: 8 }]}
      initialRecommendations={[]}
      initialQueue={[]}
      initialApprovals={[]}
    />,
  );

  fireEvent.click(screen.getByRole("button", { name: "加入候补" }));
  expect(await screen.findByText("候补第 1 位")).toBeInTheDocument();
  expect(screen.getByText("不锁定 GPU")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "退出候补 q1" }));

  await waitFor(() => expect(screen.queryByText("候补第 1 位")).not.toBeInTheDocument());
  expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/gsm/governance/queue", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      gpu_count: 1,
      memory_tier: "8_24",
      estimated_duration_minutes: 120,
      compatible_server_ids: ["server1"],
      workload_profile: "standard",
    }),
  });
  expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/gsm/governance/queue/q1", {
    method: "DELETE",
  });
});

test("admin can decide and execute approvals using only the approval id", async () => {
  const approvals: ApprovalSummary[] = [
    {
      id: "a-pending",
      requesterUserId: "alice",
      ownerUserId: "alice",
      status: "pending",
      triggerReasons: ["gpu_count"],
      createdAt: "2026-08-04T00:00:00Z",
      expiresAt: "2026-08-05T00:00:00Z",
    },
    {
      id: "a-approved",
      requesterUserId: "bob",
      ownerUserId: "bob",
      status: "approved",
      triggerReasons: ["memory_gb"],
      createdAt: "2026-08-04T00:00:00Z",
      expiresAt: "2026-08-05T00:00:00Z",
    },
  ];
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ ...approvals[0], status: "approved" }),
    } as Response)
    .mockResolvedValueOnce({
      ok: true,
      status: 201,
      json: async () => ({ container: { name: "approved-job" } }),
    } as Response);
  vi.stubGlobal("fetch", fetchMock);

  render(
    <ResourceGovernancePanel
      actor={{ userId: "admin", role: "admin" }}
      servers={[{ id: "server1", name: "Server1", gpuCount: 8 }]}
      initialRecommendations={[]}
      initialQueue={[]}
      initialApprovals={approvals}
    />,
  );

  fireEvent.change(screen.getByLabelText("审批理由 a-pending"), {
    target: { value: "资源充足" },
  });
  fireEvent.click(screen.getByRole("button", { name: "批准申请 a-pending" }));
  await waitFor(() => expect(screen.getByText("申请 a-pending 已批准")).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: "创建已批准容器 a-approved" }));
  await waitFor(() => expect(screen.getByText("容器 approved-job 已创建")).toBeInTheDocument());

  expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/gsm/approvals/a-pending/decision", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision: "approved", reason: "资源充足" }),
  });
  expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/gsm/approvals/a-approved/execute", {
    method: "POST",
  });
});
