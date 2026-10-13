import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";

import { ContainerDetailsDialog } from "../components/ContainerDetailsDialog";
import { panelReadResponse } from "./containerPanelFixtures";

const container = {
  id: "container-restart",
  name: "restart-demo",
  user: "alice",
  server: "server1",
  cpu: 4,
  memoryGb: 16,
  gpus: [],
  status: "running",
  statusText: "运行中",
  version: 1,
};

afterEach(() => {
  vi.unstubAllGlobals();
});

test("does not start a workload when its stop outcome is unknown", async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const panel = panelReadResponse(input); if (panel) return panel;
    const url = String(input);
    if (url.endsWith("/details")) return Response.json({ status: "running", container: { status: "running" }, runtime_available: true, terminal_available: true });
    if (url.endsWith("/stop")) return Response.json({ confirmation: { id: "uncertain-stop", required_phrase: "STOP job" } }, { status: 202 });
    if (url.endsWith("/confirm")) return Response.json({ status: "execution_unknown" });
    throw new Error(`Unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<ContainerDetailsDialog container={container} canOperate onClose={() => undefined} />);
  await screen.findByText(/最近采样/);
  fireEvent.click(screen.getByRole("button", { name: "重启容器" }));
  fireEvent.click(screen.getByRole("button", { name: "确认重启" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("停止结果尚未确认");
  expect(fetchMock.mock.calls.filter(([url]) => !panelReadResponse(url)).map(([url]) => String(url))).toEqual([
    "/api/gsm/containers/container-restart/details", "/api/gsm/containers/container-restart/stop", "/api/gsm/confirmations/uncertain-stop/confirm",
  ]);
  expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/start"))).toBe(false);
});

test("restarts a running container after explicit confirmation and existing session", async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const panel = panelReadResponse(input); if (panel) return panel;
    const url = String(input);
    if (url.endsWith("/details")) return Response.json({
      status: "running",
      container: { image: "demo/image:latest", status: "running" },
      runtime_available: true,
      terminal_available: true,
      usage: null,
      ports: [],
      networks: [],
      mounts: [],
    });
    if (url.endsWith("/stop")) return Response.json({
      requires_confirmation: true,
      confirmation: { id: "restart-stop-confirm", required_phrase: "停止 container restart-demo" },
    }, { status: 202 });
    if (url.endsWith("/confirm")) return Response.json({ status: "executed", container: { status: "stopped" } });
    if (url.endsWith("/start")) return Response.json({ status: "running", container: { status: "running" } });
    throw new Error(`Unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<ContainerDetailsDialog container={container} canOperate onClose={() => undefined} />);
  await screen.findByText(/最近采样/);

  const restartButton = screen.getByRole("button", { name: "重启容器" });
  expect(restartButton).toBeEnabled();
  expect(screen.queryByText(/输入以下短语/)).not.toBeInTheDocument();
  fireEvent.click(restartButton);
  expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "确认重启" }));

  await waitFor(() => expect(screen.getByText("重启请求已提交，正在刷新容器状态。")).toBeInTheDocument());
  const mutations = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
  expect(mutations).toEqual([
    ["/api/gsm/containers/container-restart/stop", expect.objectContaining({ method: "POST" })],
    ["/api/gsm/confirmations/restart-stop-confirm/confirm", expect.objectContaining({ method: "POST", body: JSON.stringify({ phrase: "停止 container restart-demo" }) })],
    ["/api/gsm/containers/container-restart/start", expect.objectContaining({ method: "POST" })],
  ]);
});
