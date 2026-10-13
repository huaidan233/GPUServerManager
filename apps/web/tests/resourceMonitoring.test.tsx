import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, expect, test, vi } from "vitest";
import { ResourceMonitor, resourceSegments } from "../components/ResourceMonitor";
import { proxyResourceHistory } from "../lib/resourceMonitoringProxy";

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

const points = [0, null, 2].map((cpu_used_cores, index) => ({ at: `2026-09-30T00:0${index}:00Z`, cpu_percent: cpu_used_cores == null ? null : cpu_used_cores * 100, cpu_used_cores, cpu_limit_cores: 4,
  memory_used_bytes: cpu_used_cores == null ? null : 1024**3, memory_total_bytes: 8*1024**3, cpu_total_cores: null, disk_read_bps: null, disk_write_bps: null }));
const history = { scope: "container", start: points[0].at, end: points[2].at, stale: false, latest: { ...points[2], observed_at: points[2].at }, points, disks: [], quota: { cpu_limit_cores: 4, memory_limit_bytes: 8*1024**3 } };

test("resource curves preserve missing samples and zero", () => {
  expect(resourceSegments([1, null, 0], 480, 100)).toHaveLength(2);
  expect(resourceSegments([null, null], 480, 100)).toHaveLength(0);
});

test("container monitoring keeps real usage separate from quotas and synchronizes keyboard time inspection", async () => {
  const fetcher = vi.fn().mockResolvedValue(Response.json(history));
  vi.stubGlobal("fetch", fetcher);
  render(<ResourceMonitor containerId="container1" />);
  const cpu = await screen.findByRole("slider", { name: "CPU 历史采样时间" });
  expect(screen.getByText("分配 4 核")).toBeInTheDocument();
  expect(screen.getByText("分配 8 GiB")).toBeInTheDocument();
  expect(cpu).toHaveAttribute("aria-valuetext", expect.stringContaining("2 核"));
  fireEvent.keyDown(cpu, { key: "Home" });
  expect(cpu).toHaveAttribute("aria-valuetext", expect.stringContaining("0 核"));
  fireEvent.keyDown(cpu, { key: "ArrowRight" });
  expect(cpu).toHaveAttribute("aria-valuetext", expect.stringContaining("无采样"));
  expect(screen.getByRole("slider", { name: "内存 历史采样时间" })).toHaveAttribute("aria-valuenow", "1");
  fireEvent.click(screen.getByRole("button", { name: "24 小时" }));
  await waitFor(() => expect(fetcher).toHaveBeenLastCalledWith(expect.stringContaining("minutes=1440"), expect.anything()));
});

test("failed loads retry and an empty history never says idle", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(new Response(null, { status: 503 })).mockResolvedValueOnce(Response.json({ ...history, latest: null }));
  vi.stubGlobal("fetch", fetcher);
  render(<ResourceMonitor serverId="s1" serverName="实验室" />);
  expect(await screen.findByRole("alert")).toHaveTextContent("请重试");
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  expect(await screen.findByText(/暂无历史采样/)).toBeInTheDocument();
  expect(screen.queryByRole("slider")).not.toBeInTheDocument();
});

test("retained samples become stale while refresh requests fail and recover with a new sample", async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2030-01-01T00:00:00Z")); // Client clock need not match server clock.
  const fetcher = vi.fn().mockResolvedValueOnce(Response.json(history)).mockImplementation(() => Promise.reject(new Error("offline")));
  vi.stubGlobal("fetch", fetcher);
  render(<ResourceMonitor containerId="stale-test" />);
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(screen.queryByText(/采样已过期/)).not.toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(210000); });
  expect(screen.getByText(/采样已过期/)).toBeInTheDocument();
  expect(screen.getByText(/不能代表此刻的资源占用/)).toBeInTheDocument();
  expect(screen.getByRole("slider", { name: "CPU 历史采样时间" })).toBeInTheDocument();
  fetcher.mockResolvedValue(Response.json(history));
  await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
  expect(screen.queryByText(/采样已过期/)).not.toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

test("resource proxy forwards authenticated cookies, disables caching and rejects unsafe ranges", async () => {
  const fetcher = vi.fn().mockResolvedValue(Response.json(history));
  const request = new Request("https://gsm.test/api/gsm/containers/c1/resource-history?minutes=360", { headers: { Cookie: "gsm_session=private" } });
  const response = await proxyResourceHistory("containers", "c1", request, { fetcher, apiUrl: "http://api:8000" });
  expect(response.status).toBe(200);
  expect(response.headers.get("Cache-Control")).toBe("no-store");
  expect(fetcher.mock.calls[0][0]).toBe("http://api:8000/api/containers/c1/resource-history?minutes=360");
  expect(fetcher.mock.calls[0][1].headers.Cookie).toBe("gsm_session=private");
  expect((await proxyResourceHistory("servers", "s1", new Request("https://gsm.test/?minutes=99"), { fetcher })).status).toBe(422);
  expect(fetcher).toHaveBeenCalledTimes(1);
});
