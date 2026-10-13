import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, test, vi } from "vitest";

import { ExecutionReconciliationPanel } from "../components/ExecutionReconciliationPanel";

const pending = {
  id: "confirm-stop",
  status: "execution_unknown",
  action_type: "stop_container",
  target: { type: "container", id: "container-1" },
  reason: "server_agent_response_unknown",
  unknown_at: "2026-08-31T02:00:00Z",
  action: {},
  evidence: null,
  suggested_outcome: null,
  inspected_at: null,
  resolved_outcome: null,
  resolution_note: null,
  resolved_at: null,
  supported: true,
};

const inspected = {
  ...pending,
  evidence: {
    observed_at: "2026-08-31T02:05:00Z",
    source: "server_agent.container_evidence",
    central_container: {
      present: true,
      id: "container-1",
      server_id: "server1",
      status: "running",
      version: 3,
      agent_container_id: "agent-1",
      gpu_devices: ["0"],
    },
    target: {
      present: true,
      status: "stopped",
      agent_container_id: "agent-1",
      reported_agent_container_id: "agent-1",
      identity_valid: true,
      exit_code: 0,
      oom_killed: false,
      health: null,
    },
  },
  suggested_outcome: "executed",
  inspected_at: "2026-08-31T02:05:00Z",
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ExecutionReconciliationPanel", () => {
  test("requires fresh evidence and an operator note before resolving", async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input.toString();
      if (url.endsWith("/operations/reconciliations") && !init?.method) {
        return Response.json({ reconciliations: [pending] });
      }
      if (url.endsWith("/confirm-stop/inspect")) {
        return Response.json({ reconciliation: inspected });
      }
      if (url.endsWith("/confirm-stop/resolve")) {
        return Response.json({
          reconciliation: { ...inspected, status: "reconciled_executed" },
          local_effect: { kind: "container_updated", container: { id: "container-1", status: "stopped" } },
        });
      }
      return Response.json({ detail: "unexpected request" }, { status: 500 });
    });
    vi.stubGlobal("fetch", fetcher);

    render(<ExecutionReconciliationPanel />);

    expect(await screen.findByText("停止容器")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "按证据结案" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "核验目标状态" }));

    expect((await screen.findAllByText("目标侧已执行")).length).toBeGreaterThan(0);
    expect(screen.getByText("server_agent.container_evidence")).toBeInTheDocument();
    expect(screen.getByText("身份匹配").nextSibling).toHaveTextContent("一致");

    fireEvent.click(screen.getByRole("button", { name: "按证据结案" }));
    const confirmButton = screen.getByRole("button", { name: "确认结案" });
    expect(confirmButton).toBeDisabled();
    fireEvent.change(screen.getByLabelText("结案备注"), {
      target: { value: "已核对 Agent 实时状态和容器身份" },
    });
    expect(confirmButton).toBeEnabled();
    fireEvent.click(confirmButton);

    await waitFor(() => expect(screen.getByText("没有需要核验的异常执行。")).toBeInTheDocument());
    const resolveCall = fetcher.mock.calls.find(([input]) => input.toString().endsWith("/confirm-stop/resolve"));
    expect(resolveCall?.[1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({
        outcome: "executed",
        note: "已核对 Agent 实时状态和容器身份",
      }),
    });
  });

  test("keeps resolution unavailable when evidence is inconclusive", async () => {
    const inconclusive = {
      ...inspected,
      evidence: {
        ...inspected.evidence,
        target: { present: true, status: "unknown", identity_valid: false },
      },
      suggested_outcome: null,
    };
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ reconciliations: [inconclusive] })));

    render(<ExecutionReconciliationPanel />);

    expect((await screen.findAllByText("证据不足")).length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "按证据结案" })).not.toBeInTheDocument();
  });

  test("shows resolve conflicts inside the dialog and refreshes stale evidence", async () => {
    const refreshed = {
      ...inspected,
      evidence: {
        ...inspected.evidence,
        target: { present: true, status: "running", identity_valid: true },
      },
      suggested_outcome: "not_executed",
      inspected_at: "2026-08-31T02:10:00Z",
    };
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input.toString();
      if (url.endsWith("/operations/reconciliations") && !init?.method) {
        return Response.json({ reconciliations: [inspected] });
      }
      if (url.endsWith("/confirm-stop/resolve")) {
        return Response.json({ detail: "目标证据已变化，请重新核验" }, { status: 409 });
      }
      if (url.endsWith("/confirm-stop/inspect")) {
        return Response.json({ reconciliation: refreshed });
      }
      return Response.json({ detail: "unexpected request" }, { status: 500 });
    });
    vi.stubGlobal("fetch", fetcher);

    render(<ExecutionReconciliationPanel />);

    fireEvent.click(await screen.findByRole("button", { name: "按证据结案" }));
    fireEvent.change(screen.getByLabelText("结案备注"), { target: { value: "已核验" } });
    fireEvent.click(screen.getByRole("button", { name: "确认结案" }));

    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("目标证据已变化，请重新核验");
    expect(within(dialog).getByText("目标侧未执行")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认结案" })).toBeEnabled();
    expect(fetcher.mock.calls.filter(([input]) => input.toString().endsWith("/confirm-stop/inspect"))).toHaveLength(1);
  });

  test("renders backup restore ticket evidence without container fields", async () => {
    const backup = {
      ...pending,
      id: "confirm-restore",
      action_type: "authorize_backup_restore",
      target: { type: "backup", id: "daily.tar.gz" },
      evidence: {
        observed_at: "2026-08-31T02:15:00Z",
        source: "central_store.restore_tickets",
        target: {
          present: true,
          confirmation_id: "confirm-restore",
          backup_key: "daily.tar.gz",
          match_count: 1,
          identity_valid: true,
          ticket_id: "restore-ticket-1",
          ticket_backup_key: "daily.tar.gz",
        },
      },
      suggested_outcome: "executed",
      inspected_at: "2026-08-31T02:15:00Z",
    };
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ reconciliations: [backup] })));

    render(<ExecutionReconciliationPanel />);

    expect(await screen.findByText("授权备份恢复")).toBeInTheDocument();
    expect(screen.getAllByText("恢复授权已生成").length).toBeGreaterThan(0);
    expect(screen.getByText("恢复授权票据").nextSibling).toHaveTextContent("存在");
    expect(screen.getByText("票据编号").nextSibling).toHaveTextContent("restore-ticket-1");
    expect(screen.getByText("备份身份匹配").nextSibling).toHaveTextContent("一致");
  });

  test("renders operation receipt identity and state without inventing target presence", async () => {
    const receipt = {
      ...pending,
      evidence: {
        observed_at: "2026-08-31T02:05:00Z",
        source: "server_agent.operation_receipt",
        central_container: {
          present: true,
          id: "container-1",
          server_id: "server1",
          status: "running",
          version: 1,
          agent_container_id: "agent-1",
          gpu_devices: ["0"],
        },
        target: {
          operation_id: "stop_container:container-1:1",
          action: "stop_container",
          target_id: "agent-1",
          receipt_present: true,
          binding_valid: true,
          state: "succeeded",
          outcome: "executed",
          result_code: "stopped",
        },
      },
      suggested_outcome: "executed",
      inspected_at: "2026-08-31T02:05:00Z",
    };
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ reconciliations: [receipt] })));

    render(<ExecutionReconciliationPanel />);

    expect(await screen.findByText("持久化回执")).toBeInTheDocument();
    expect(screen.getByText("Operation ID").nextSibling).toHaveTextContent("stop_container:container-1:1");
    expect(screen.getByText("持久化回执").nextSibling).toHaveTextContent("存在");
    expect(screen.getByText("回执绑定").nextSibling).toHaveTextContent("一致");
    expect(screen.getByText("回执状态").nextSibling).toHaveTextContent("已完成");
    expect(screen.getByText("回执结果").nextSibling).toHaveTextContent("已执行");
    expect(screen.queryByText("目标对象")).not.toBeInTheDocument();
  });
});
