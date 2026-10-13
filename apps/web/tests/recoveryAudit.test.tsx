import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, expect, test, vi } from "vitest";
import { ContainerRecoveryPanel } from "../components/ContainerRecoveryPanel";
import { ImageVersionsPanel } from "../components/ImageVersionsPanel";

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });
const handlers = () => ({ onLogs: vi.fn(), onConfigure: vi.fn(), onUpdated: vi.fn() });

test("denied recovery shows a usable error and unlocks actions without claiming success", async () => {
  const fetcher = vi.fn().mockResolvedValue(Response.json({ detail: "private detail" }, { status: 403 }));
  vi.stubGlobal("fetch", fetcher); const callbacks = handlers();
  render(<ContainerRecoveryPanel containerId="c1" running={false} canStart {...callbacks}/>);
  fireEvent.click(screen.getByRole("button", { name: "检查并启动容器" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("没有操作权限");
  expect(screen.getByRole("button", { name: "检查并启动容器" })).toBeEnabled();
  expect(callbacks.onUpdated).not.toHaveBeenCalled();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});

test("recovery and diagnostic buttons prevent duplicate requests while recovery is pending", async () => {
  let complete!: (response: Response) => void;
  const fetcher = vi.fn().mockImplementation(() => new Promise<Response>(resolve => { complete = resolve; }));
  vi.stubGlobal("fetch", fetcher);
  render(<ContainerRecoveryPanel containerId="c1" running={false} canStart {...handlers()}/>);
  fireEvent.click(screen.getByRole("button", { name: "检查并启动容器" }));
  expect(screen.getByRole("button", { name: "正在启动…" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "下载诊断记录" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "正在启动…" }));
  expect(fetcher).toHaveBeenCalledOnce();
  complete(Response.json({ container: { status: "running" } }));
  expect(await screen.findByRole("status")).toHaveTextContent("checkpoint");
});

test("diagnostic export failure never creates a file or retains a successful status", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ detail: "gone" }, { status: 404 })));
  const createObjectURL = vi.fn();
  const OriginalURL = URL;
  vi.stubGlobal("URL", class extends OriginalURL { static createObjectURL = createObjectURL; static revokeObjectURL = vi.fn(); });
  render(<ContainerRecoveryPanel containerId="c1" running={false} canStart {...handlers()}/>);
  fireEvent.click(screen.getByRole("button", { name: "下载诊断记录" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("容器或终端会话已不存在");
  expect(createObjectURL).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "下载诊断记录" })).toBeEnabled();
});

test("running containers show the previous stop without a second recovery action", () => {
  render(<ContainerRecoveryPanel containerId="c1" running canStart {...handlers()}
    report={{ events: [], last_stop: { at: null, code: "oom", title: "OOM 终止", description: "依据 Docker", confidence: "confirmed", exit_code: 137, oom_killed: true } }}/>);
  expect(screen.getByText("上次停止：OOM 终止")).toBeVisible();
  expect(screen.queryByRole("button", { name: "检查并启动容器" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "检查资源配置" })).not.toBeInTheDocument();
});

test("resuming a paused image does not silently mark it recommended", async () => {
  const fetcher = vi.fn().mockResolvedValue(Response.json({}));
  vi.stubGlobal("fetch", fetcher); const updated = vi.fn();
  render(<ImageVersionsPanel images={[{ name: "gsm/python:3.11", displayName: "Python 3.11", environment: "python-base", digest: "sha256:" + "a".repeat(64), creationEnabled: false, recommended: false }]} onUpdated={updated}/>);
  fireEvent.change(screen.getByLabelText("版本变更原因"), { target: { value: "Validated old course requirements" } });
  fireEvent.click(screen.getByRole("button", { name: "允许新建" }));
  await waitFor(() => expect(updated).toHaveBeenCalledOnce());
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toMatchObject({ creation_enabled: true, recommended: false });
});

test("pausing a recommended image clears recommendation and preserves the selected reference", async () => {
  const fetcher = vi.fn().mockResolvedValue(Response.json({}));
  vi.stubGlobal("fetch", fetcher); const updated = vi.fn();
  render(<ImageVersionsPanel images={[{ name: "gsm/python:3.13", displayName: "Python 3.13", environment: "python-base", digest: "sha256:" + "b".repeat(64), creationEnabled: true, recommended: true }]} onUpdated={updated}/>);
  fireEvent.change(screen.getByLabelText("版本变更原因"), { target: { value: "Course compatibility review" } });
  const row = screen.getByText("gsm/python:3.13").closest("tr")!;
  fireEvent.click(within(row).getByRole("button", { name: "暂停新建" }));
  await waitFor(() => expect(updated).toHaveBeenCalledOnce());
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toMatchObject({ reference: "gsm/python:3.13", creation_enabled: false, recommended: false });
});
