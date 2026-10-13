import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, expect, test, vi } from "vitest";
import { GpuHistoryChart, chartSegments } from "../components/GpuHistoryChart";
import { proxyGpuHistory } from "../lib/apiProxy";

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

test("curve gaps remain missing instead of being rendered as zero", () => {
  const points = [20, null, 0].map(utilization => ({ at: "", utilization, memory_used_mb: null, memory_total_mb: null }));
  expect(chartSegments(points, p => p.utilization)).toHaveLength(2);
});

test("history loads real series and requests the selected time range", async () => {
  const fetcher = vi.fn().mockResolvedValue(Response.json({ start: "2026-09-28T00:00:00Z", end: "2026-09-28T01:00:00Z", series: [{ gpu_uuid: "gpu1", index: 0, points: [{ at: "2026-09-28T00:00:00Z", utilization: 25, memory_used_mb: 512, memory_total_mb: 1024 }] }] }));
  vi.stubGlobal("fetch", fetcher);
  render(<GpuHistoryChart serverId="server1" serverName="实验室" />);
  expect(await screen.findByRole("img", { name: /GPU 0/ })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "24 小时" }));
  await waitFor(() => expect(fetcher).toHaveBeenLastCalledWith(expect.stringContaining("minutes=1440"), expect.anything()));
});

test("history proxy forwards authentication and rejects unsupported ranges", async () => {
  const fetcher = vi.fn().mockResolvedValue(Response.json({ series: [] }));
  const request = new Request("https://manager.test/api/gsm/servers/s1/gpu-history?minutes=360", { headers: { Cookie: "gsm_session=private" } });
  expect((await proxyGpuHistory("s1", request, { fetcher, apiUrl: "http://api:8000" })).status).toBe(200);
  expect(fetcher.mock.calls[0][0]).toBe("http://api:8000/api/servers/s1/gpu-history?minutes=360");
  expect(fetcher.mock.calls[0][1].headers.Cookie).toContain("gsm_session=private");
  expect((await proxyGpuHistory("s1", new Request("https://manager.test/?minutes=99"), { fetcher })).status).toBe(422);
  expect(fetcher).toHaveBeenCalledTimes(1);
});

test("keyboard inspection synchronizes GPUs and does not turn missing samples into zero", async () => {
  const series = [[0, 50, null, 100], [10, 30, null, 80]].map((values, index) => ({
    gpu_uuid: `gpu${index}`, index, points: values.map((utilization, minute) => ({
      at: `2026-09-28T00:0${minute}:00Z`, utilization,
      memory_used_mb: utilization === null ? null : 512, memory_total_mb: utilization === null ? null : 1024,
    })),
  }));
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ start: "2026-09-28T00:00:00Z", end: "2026-09-28T00:03:00Z", series })));
  render(<GpuHistoryChart serverId="server1" serverName="实验室" />);
  const first = await screen.findByRole("slider", { name: "GPU 0 历史采样时间" });
  const second = screen.getByRole("slider", { name: "GPU 1 历史采样时间" });
  fireEvent.focus(first);
  fireEvent.keyDown(first, { key: "Home" });
  expect(first).toHaveAttribute("aria-valuetext", expect.stringContaining("计算利用率 0%"));
  expect(second).toHaveAttribute("aria-valuenow", "0");
  fireEvent.keyDown(first, { key: "ArrowRight" });
  expect(first).toHaveAttribute("aria-valuetext", expect.stringContaining("计算利用率 50%"));
  expect(second).toHaveAttribute("aria-valuetext", expect.stringContaining("计算利用率 30%"));
  fireEvent.keyDown(first, { key: "ArrowRight" });
  expect(first).toHaveAttribute("aria-valuetext", expect.stringContaining("计算利用率 无采样"));
  expect(screen.getAllByText("此时刻无采样")).toHaveLength(2);
  fireEvent.keyDown(first, { key: "Escape" });
  expect(first).toHaveAttribute("aria-valuetext", expect.stringContaining("计算利用率 100%"));
  expect(screen.queryByText("此时刻无采样")).not.toBeInTheDocument();
});

test("a failed history request can be retried from the panel", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(new Response(null, { status: 503 }))
    .mockResolvedValueOnce(Response.json({ start: "2026-09-28T00:00:00Z", end: "2026-09-28T01:00:00Z", series: [] }));
  vi.stubGlobal("fetch", fetcher);
  render(<GpuHistoryChart serverId="server1" serverName="实验室" />);
  expect(await screen.findByRole("alert")).toHaveTextContent("请重试");
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  expect(await screen.findByText("暂无历史采样")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

test("GPU freshness uses raw sample time, survives client clock skew and ages during offline refreshes", async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2030-01-01T00:00:00Z"));
  const history = { start: "2026-09-27T00:16:00Z", end: "2026-09-28T00:16:00Z", series: [{
    gpu_uuid: "gpu1", index: 0, latest_observed_at: "2026-09-28T00:15:30Z",
    points: [{ at: "2026-09-28T00:08:00Z", utilization: 25, memory_used_mb: 512, memory_total_mb: 1024 }],
  }] };
  const fetcher = vi.fn().mockResolvedValueOnce(Response.json(history)).mockImplementation(() => Promise.reject(new Error("offline")));
  vi.stubGlobal("fetch", fetcher);
  render(<GpuHistoryChart serverId="fresh-gpu" serverName="GPU" />);
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(screen.getByText("最近采样")).toHaveAttribute("data-stale", "false");
  await act(async () => { await vi.advanceTimersByTimeAsync(180000); });
  expect(screen.getByText("历史采样")).toHaveAttribute("data-stale", "true");
  fetcher.mockResolvedValue(Response.json(history));
  await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
  expect(screen.getByText("最近采样")).toHaveAttribute("data-stale", "false");
});

test("latest raw readout remains paired with its timestamp when the current chart bucket has a gap", async () => {
  const raw = { at: "2026-09-28T00:15:30Z", utilization: 72, memory_used_mb: 340, memory_total_mb: 46068 };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ start: "2026-09-27T00:16:00Z", end: "2026-09-28T00:16:00Z", series: [{
    gpu_uuid: "gap-gpu", index: 0, latest_observed_at: raw.at, latest_sample: raw, points: [
      { ...raw, at: "2026-09-28T00:00:00Z", utilization: 25 },
      { ...raw, at: "2026-09-28T00:08:00Z", utilization: null, memory_used_mb: null },
    ],
  }] })));
  render(<GpuHistoryChart serverId="gap-gpu" serverName="GPU" />);
  const slider = await screen.findByRole("slider", { name: "GPU 0 历史采样时间" });
  expect(slider).toHaveAttribute("aria-valuetext", expect.stringContaining("计算利用率 72%"));
  expect(screen.getByText("最近采样").querySelector("time")).toHaveAttribute("dateTime", raw.at);
  fireEvent.keyDown(slider, { key: "Home" });
  expect(slider).toHaveAttribute("aria-valuetext", expect.stringContaining("计算利用率 25%"));
  fireEvent.keyDown(slider, { key: "End" });
  expect(slider).toHaveAttribute("aria-valuetext", expect.stringContaining("计算利用率 无采样"));
  fireEvent.keyDown(slider, { key: "Escape" });
  expect(slider).toHaveAttribute("aria-valuetext", expect.stringContaining("计算利用率 72%"));
});
