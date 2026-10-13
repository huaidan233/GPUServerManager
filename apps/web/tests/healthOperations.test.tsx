import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { HealthOperationsPanel } from "../components/HealthOperationsPanel";
import type { ContainerSummary, HealthEventSummary, HealthSummary } from "../lib/mockData";

const health: HealthSummary[] = [
  {
    serverId: "server1",
    status: "degraded",
    warningCount: 1,
    criticalCount: 0,
    observedAt: "2026-08-04T00:00:00Z",
    stale: true,
  },
  {
    serverId: "server2",
    status: "unknown",
    warningCount: 0,
    criticalCount: 0,
    observedAt: "2026-08-03T00:00:00Z",
    stale: true,
  },
];

const events: HealthEventSummary[] = [
  {
    id: "event-recovered",
    code: "docker_unavailable",
    scope: "infrastructure",
    serverId: "server1",
    containerId: undefined,
    ownerUserId: undefined,
    severity: "critical",
    status: "recovered",
    openedAt: "2026-08-03T23:00:00Z",
    lastSeenAt: "2026-08-03T23:30:00Z",
    evidence: { error_type: "RuntimeError" },
  },
];

const containers: ContainerSummary[] = [
  {
    id: "c1",
    name: "llama-debug",
    user: "alice",
    server: "server1",
    cpu: 4,
    memoryGb: 16,
    gpus: ["0"],
    status: "running",
    statusText: "running",
  },
];

function renderPanel(actorRole: "admin" | "user" = "admin") {
  return render(
    <HealthOperationsPanel
      actor={{ userId: actorRole === "admin" ? "admin" : "alice", role: actorRole }}
      health={health}
      events={events}
      containers={containers}
    />,
  );
}

describe("HealthOperationsPanel", () => {
  test("renders severity, stale state, and recovery marker without raw logs", () => {
    renderPanel();

    expect(screen.getByRole("tab", { name: "健康" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByText("降级")).not.toBeNull();
    expect(screen.getByText("未知")).not.toBeNull();
    expect(screen.getAllByText("数据过期").length).toBe(2);
    expect(screen.getByText("已恢复")).not.toBeNull();
    expect(screen.queryByText(/RuntimeError/)).toBeNull();
    expect(screen.getByRole("button", { name: "查看原始证据 event-recovered" })).not.toBeNull();
  });

  test("does not expose raw evidence controls to a non-admin", () => {
    renderPanel("user");

    expect(screen.queryByRole("button", { name: /查看原始证据/ })).toBeNull();
  });

  test("loads an owner diagnostic report and shows source and confidence", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => ({
          id: "report-1",
          source: "rules",
          findings: [
            { code: "cuda_oom", cause: "GPU 显存不足", confidence: 0.98, suggestions: [] },
          ],
        }),
      }) as Response),
    );
    renderPanel("user");

    fireEvent.click(screen.getByRole("tab", { name: "诊断" }));
    fireEvent.click(screen.getByRole("button", { name: "诊断 llama-debug" }));

    await waitFor(() => expect(screen.getByText("GPU 显存不足")).not.toBeNull());
    expect(screen.getByText("规则")).not.toBeNull();
    expect(screen.getByText("98%")).not.toBeNull();
    expect(screen.getByText("report-1")).not.toBeNull();
    expect(screen.queryByText(/full_logs|raw logs/i)).toBeNull();
  });
});
