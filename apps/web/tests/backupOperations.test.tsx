import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { BackupOperationsPanel } from "../components/BackupOperationsPanel";


const backupStatus = {
  backend: "local" as const,
  inventory: [
    {
      key: "gsm-daily.tar.gz",
      sizeBytes: 12 * 1024 * 1024,
      modifiedAt: "2026-08-04T18:00:00Z",
    },
  ],
  runs: [
    {
      id: "run-ok",
      key: "gsm-daily.tar.gz",
      kind: "daily",
      status: "success",
      integrityStatus: "valid",
      sizeBytes: 12 * 1024 * 1024,
      finishedAt: "2026-08-04T18:00:00Z",
    },
    {
      id: "run-failed",
      kind: "daily",
      status: "failed",
      finishedAt: "2026-08-03T18:00:00Z",
      message: "disk full",
    },
  ],
  latestSuccess: {
    id: "run-ok",
    key: "gsm-daily.tar.gz",
    kind: "daily",
    status: "success",
    sizeBytes: 12 * 1024 * 1024,
    finishedAt: "2026-08-04T18:00:00Z",
  },
  latestDrill: {
    id: "drill-1",
    status: "success",
    checkedAt: "2026-08-01T18:00:00Z",
  },
};


describe("BackupOperationsPanel", () => {
  test("shows admin backup status, integrity, drill, failure, and local risk", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (input.toString().endsWith("/verify")) {
          return { ok: true, json: async () => ({ status: "valid" }) } as Response;
        }
        throw new Error(`unexpected request ${input}`);
      }),
    );
    render(
      <BackupOperationsPanel actorRole="admin" initialStatus={backupStatus} />,
    );

    expect(screen.getByText("仅本机，不能抵御整机磁盘故障")).not.toBeNull();
    expect(screen.getByText(/12\.0 MB/)).not.toBeNull();
    expect(screen.getByText(/2026-08-01/)).not.toBeNull();
    expect(screen.getByText("失败")).not.toBeNull();
    expect(screen.getByText("完整性检查通过")).not.toBeNull();
    expect(screen.getByRole("button", { name: "申请恢复 gsm-daily.tar.gz" })).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "校验 gsm-daily.tar.gz" }));
    await waitFor(() => expect(screen.getByText("完整性检查通过")).not.toBeNull());
    expect(screen.queryByText(/encryption|secret|token/i)).toBeNull();
  });

  test("renders no backend inventory or restore controls for non-admins", () => {
    const { container } = render(
      <BackupOperationsPanel actorRole="user" initialStatus={backupStatus} />,
    );

    expect(container.innerHTML).toBe("");
    expect(screen.queryByText("gsm-daily.tar.gz")).toBeNull();
    expect(screen.queryByRole("button", { name: /申请恢复/ })).toBeNull();
  });

  test("requires the exact restore phrase and returns a service-stopped CLI command", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url.endsWith("/restore-request")) {
        return {
          ok: true,
          json: async () => ({
            confirmation: {
              id: "confirm-restore",
              required_phrase: "确认恢复 backup gsm-daily.tar.gz",
            },
          }),
        } as Response;
      }
      if (url.endsWith("/confirmations/confirm-restore/confirm")) {
        return {
          ok: true,
          json: async () => ({
            status: "authorized",
            command: "gsm-backup restore gsm-daily.tar.gz --ticket rt1 --service-stopped",
          }),
        } as Response;
      }
      throw new Error(`unexpected request ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(
      <BackupOperationsPanel actorRole="admin" initialStatus={backupStatus} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "申请恢复 gsm-daily.tar.gz" }));
    const input = await screen.findByLabelText("确认短语");
    const confirm = screen.getByRole("button", { name: "确认申请恢复" });
    expect((confirm as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(input, { target: { value: "确认恢复 backup gsm-daily.tar.gz" } });
    expect((confirm as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(confirm);

    await waitFor(() =>
      expect(screen.getByText(/gsm-backup restore .* --service-stopped/)).not.toBeNull(),
    );
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/gsm/confirmations/confirm-restore/confirm",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phrase: "确认恢复 backup gsm-daily.tar.gz" }),
      },
    );
  });
});
