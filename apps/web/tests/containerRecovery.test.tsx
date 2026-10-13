import { fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, expect, test, vi } from "vitest";
import { ContainerRecoveryPanel } from "../components/ContainerRecoveryPanel";

afterEach(() => vi.unstubAllGlobals());

test("recovery preserves uncertainty and uses the normal checked start API", async () => {
  const fetcher = vi.fn().mockResolvedValue(Response.json({ container: { status: "running" } }));
  const updated = vi.fn(); vi.stubGlobal("fetch", fetcher);
  render(<ContainerRecoveryPanel containerId="c1" running={false} canStart={true} onLogs={vi.fn()} onConfigure={vi.fn()} onUpdated={updated}
    report={{ events: [], last_stop: { at: null, code: "nonzero_exit", confidence: "unknown", title: "主进程退出（137）", description: "不能单凭退出码判断 OOM", exit_code: 137, oom_killed: false } }} />);
  expect(screen.getByText("原因待确认")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "检查并启动容器" }));
  expect(await screen.findByRole("status")).toHaveTextContent("checkpoint");
  expect(fetcher).toHaveBeenCalledWith("/api/gsm/containers/c1/start", expect.objectContaining({ method: "POST" }));
  expect(updated).toHaveBeenCalledOnce();
});

test("view-only actors can inspect the reason but cannot recover the container", () => {
  render(<ContainerRecoveryPanel containerId="c1" running={false} canStart={false} onLogs={vi.fn()} onConfigure={vi.fn()} onUpdated={vi.fn()} />);
  expect(screen.getByRole("button", { name: "检查并启动容器" })).toBeDisabled();
});
