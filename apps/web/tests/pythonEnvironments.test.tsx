import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, expect, test, vi } from "vitest";
import { ContainerEnvironmentTools } from "../components/ContainerEnvironmentTools";
import { launchContainerApplication } from "../lib/applicationLaunch";
import { proxyApplicationJob, proxyPythonEnvironments } from "../lib/pythonEnvironmentProxy";

const python = "/workspace/.venv/bin/python";
const discovery = { status: "scanned", truncated: false, environments: [{ path: python, kind: "venv", version: "3.12.14", torch_version: "2.8.0+cu128", jupyter_version: null, default: true }] };
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.useRealTimers(); });

test("automatically discovers environments and checks torch only on explicit click", async () => {
  const fetcher = vi.fn().mockImplementation(async (url: string) => Response.json(url.includes("/check?") ? { status: "checked", path: python, python_version: "3.12.14", torch_status: "available", torch_version: "2.8.0+cu128", cuda_build: "12.8", cuda_available: false, device_count: 0 } : discovery));
  vi.stubGlobal("fetch", fetcher);
  render(<ContainerEnvironmentTools containerId="c1" running />);
  expect(await screen.findByRole("option", { name: /Python 3.12.14 · venv/ })).toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledTimes(1);
  fireEvent.change(screen.getByLabelText("Jupyter 使用的 Python 环境"), { target: { value: python } });
  fireEvent.click(screen.getByRole("button", { name: "检查 torch / CUDA" }));
  expect(await screen.findByText("torch 可用 · 当前使用 CPU")).toBeVisible();
  expect(screen.getByText(/未检测到可用 GPU/)).toBeVisible();
  expect(fetcher.mock.calls[1][0]).toContain("python_path=%2Fworkspace%2F.venv%2Fbin%2Fpython");
});

test("selected interpreter is launched asynchronously with real progress before handoff", async () => {
  const replace = vi.fn(); const close = vi.fn();
  vi.spyOn(window, "open").mockReturnValue({ location: { replace }, close, opener: null, closed: false } as unknown as Window);
  const fetcher = vi.fn().mockImplementation(async (url: string, init: RequestInit) => {
    if (url.endsWith("python-environments")) return Response.json(discovery);
    if (url.includes("/jobs/")) return Response.json({ job_id: "a".repeat(32), application: "jupyter", phase: "ready", status: "ready" });
    const body = JSON.parse(String(init.body));
    return Response.json(body.async_start ? { job_id: "a".repeat(32), application: "jupyter", phase: "preparing", status: "running" } : { url: "https://apps.test/apps/handoff#one-use" });
  });
  vi.stubGlobal("fetch", fetcher);
  render(<ContainerEnvironmentTools containerId="c1" running />);
  await screen.findByRole("option", { name: /Python 3.12.14 · venv/ });
  fireEvent.change(screen.getByLabelText("Jupyter 使用的 Python 环境"), { target: { value: python } });
  fireEvent.click(screen.getByRole("button", { name: "打开 JupyterLab" }));
  expect(await screen.findByText("准备 JupyterLab")).toBeVisible();
  expect(replace).not.toHaveBeenCalled();
  await waitFor(() => expect(replace).toHaveBeenCalledWith("https://apps.test/apps/handoff#one-use"), { timeout: 3500 });
  const launches = fetcher.mock.calls.filter(call => call[0].endsWith("/applications")).map(call => JSON.parse(String(call[1].body)));
  expect(launches).toEqual([{ application: "jupyter", python_path: python, async_start: true }, { application: "jupyter", python_path: python }]);
});

test("failed preparation displays fixed actionable reason and never asks for handoff", async () => {
  const fetcher = vi.fn().mockImplementation(async () => Response.json({ job_id: "a".repeat(32), application: "jupyter", phase: "failed", status: "failed", error_code: "app_start_interrupted" }));
  vi.stubGlobal("fetch", fetcher);
  await expect(launchContainerApplication("/api/gsm/containers/c1", { application: "jupyter" }, new AbortController().signal, vi.fn())).rejects.toThrow("服务器代理重启");
  expect(fetcher).toHaveBeenCalledTimes(1);
});

test("stopped container never discovers or probes and manual path remains available", () => {
  const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
  render(<ContainerEnvironmentTools containerId="c1" running={false} />);
  expect(screen.getByRole("button", { name: "检查 torch / CUDA" })).toBeDisabled();
  expect(fetcher).not.toHaveBeenCalled();
});

test("environment proxy forwards auth, strips unknown queries and rejects traversal", async () => {
  const fetcher = vi.fn().mockImplementation(async () => Response.json(discovery));
  const request = new Request("http://web/api?python_path=%2Fworkspace%2F.venv%2Fbin%2Fpython&token=secret", { headers: { Cookie: "gsm_session=session" } });
  const response = await proxyPythonEnvironments("c1", request, true, { apiUrl: "http://api:8000", fetcher });
  expect(response.headers.get("cache-control")).toBe("no-store");
  expect(fetcher.mock.calls[0][0]).toBe("http://api:8000/api/containers/c1/python-environments/check?python_path=%2Fworkspace%2F.venv%2Fbin%2Fpython");
  expect(fetcher.mock.calls[0][1].headers.Cookie).toBe("gsm_session=session");
  expect((await proxyPythonEnvironments("c1", new Request("http://web/?python_path=/tmp/../python"), true, { fetcher })).status).toBe(422);
  expect((await proxyApplicationJob("c1", "jupyter", "../other", request, { fetcher })).status).toBe(404);
  expect(fetcher).toHaveBeenCalledTimes(1);
});

test("closing during preparation stops polling, closes waiting tab and permits reopening", async () => {
  const firstTab = { location: { replace: vi.fn() }, close: vi.fn(), closed: false, opener: null };
  const secondTab = { location: { replace: vi.fn() }, close: vi.fn(), closed: false, opener: null };
  vi.spyOn(window, "open").mockReturnValueOnce(firstTab as unknown as Window).mockReturnValueOnce(secondTab as unknown as Window);
  let launchCount = 0;
  const fetcher = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.endsWith("python-environments")) return Response.json(discovery);
    const payload = JSON.parse(String(init?.body));
    if (!payload.async_start) return Response.json({ url: "https://apps.test/apps/handoff#retry" });
    launchCount++;
    return Response.json({ job_id: "a".repeat(32), application: "jupyter", phase: launchCount === 1 ? "preparing" : "ready", status: launchCount === 1 ? "running" : "ready" });
  });
  vi.stubGlobal("fetch", fetcher);
  const first = render(<ContainerEnvironmentTools containerId="c1" running />);
  fireEvent.click(screen.getByRole("button", { name: "打开 JupyterLab" }));
  await screen.findByText("准备 JupyterLab");
  first.unmount();
  await waitFor(() => expect(firstTab.close).toHaveBeenCalledOnce());
  expect(firstTab.location.replace).not.toHaveBeenCalled();
  render(<ContainerEnvironmentTools containerId="c1" running />);
  fireEvent.click(screen.getByRole("button", { name: "打开 JupyterLab" }));
  await waitFor(() => expect(secondTab.location.replace).toHaveBeenCalledWith("https://apps.test/apps/handoff#retry"));
  expect(fetcher.mock.calls.some(([url]) => url.includes("/jobs/") || url.endsWith("/stop"))).toBe(false);
});

test("switching container resets the previous custom interpreter", async () => {
  vi.spyOn(window, "open").mockReturnValue(null);
  const fetcher = vi.fn().mockImplementation(async (url: string) => Response.json(url.endsWith("python-environments") ? discovery : { url: "https://apps.test/apps/handoff#other" }));
  vi.stubGlobal("fetch", fetcher);
  const view = render(<ContainerEnvironmentTools containerId="c1" running />);
  await screen.findByRole("option", { name: /Python 3.12.14 · venv/ });
  fireEvent.change(screen.getByLabelText("Jupyter 使用的 Python 环境"), { target: { value: python } });
  view.rerender(<ContainerEnvironmentTools containerId="c2" running />);
  expect(screen.getByLabelText("Jupyter 使用的 Python 环境")).toHaveValue("auto");
  fireEvent.click(screen.getByRole("button", { name: "打开 JupyterLab" }));
  await screen.findByRole("link", { name: /点击继续打开应用/ });
  const call = fetcher.mock.calls.find(([url]) => url === "/api/gsm/containers/c2/applications");
  expect(JSON.parse(String(call?.[1]?.body))).toEqual({ application: "jupyter", async_start: true });
});
