import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, expect, test, vi } from "vitest";
import { ContainerCreateWizard } from "../components/ContainerCreateWizard";
import { ImageVersionsPanel } from "../components/ImageVersionsPanel";
import { mockDashboardData, type ImageSummary } from "../lib/mockData";

afterEach(() => vi.unstubAllGlobals());
const images: ImageSummary[] = [
  { name: "gsm/python-science:py311", displayName: "Python 3.11 / Torch 2.5.1", environment: "python", runtimeVersion: "3.11.10", gpuReady: true, serverIds: ["server1"], packages: ["torch", "scikit-learn"] },
  { name: "gsm/python-science:py312", displayName: "Python 3.12 / Torch 2.5.1", environment: "python", runtimeVersion: "3.12.14", gpuReady: true, serverIds: ["server1"] },
  { name: "gsm/go-dev:1.26.8", displayName: "Go 1.26.8", environment: "go", runtimeVersion: "1.26.8", gpuReady: false, serverIds: ["server1"] },
  { name: "gsm/go-dev:1.27.1", displayName: "Go 1.27.1", environment: "go", runtimeVersion: "1.27.1", gpuReady: false, serverIds: ["server1"] },
];

test("version selector exposes prepared Python dependencies and sends the exact image", async () => {
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ container: { name: "ready", status: "running" } }, { status: 201 }));
  vi.stubGlobal("fetch", fetcher);
  render(<ContainerCreateWizard {...mockDashboardData} images={images} />);
  expect(screen.getByText("已安装：torch、scikit-learn")).toBeVisible();
  fireEvent.change(screen.getByLabelText("环境版本"), { target: { value: images[1].name } });
  fireEvent.change(screen.getByLabelText("SSH 初始密码（至少 12 位）"), { target: { value: "test-ssh-password" } });
  fireEvent.click(screen.getByRole("button", { name: "创建容器", exact: true }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledOnce());
  expect(JSON.parse(String(fetcher.mock.calls[0][1]?.body)).image).toBe(images[1].name);
});

test("Go version change clears previous GPU selection and disables GPU controls", () => {
  render(<ContainerCreateWizard {...mockDashboardData} images={images} />);
  fireEvent.click(screen.getByLabelText("自动选择空闲卡"));
  fireEvent.change(screen.getByLabelText("开发环境"), { target: { value: "go" } });
  expect(screen.getByLabelText("环境版本")).toHaveValue(images[2].name);
  expect(screen.getByLabelText("不使用 GPU")).toBeChecked();
  expect(screen.getByLabelText("自动选择空闲卡")).toBeDisabled();
  fireEvent.change(screen.getByLabelText("环境版本"), { target: { value: images[3].name } });
  expect(screen.getByLabelText("环境版本")).toHaveValue(images[3].name);
});

test("switching servers never silently submits an unavailable runtime", () => {
  render(<ContainerCreateWizard {...mockDashboardData} images={images} />);
  fireEvent.change(screen.getByLabelText("服务器"), { target: { value: "server2" } });
  expect(screen.getByRole("button", { name: "创建容器", exact: true })).toBeDisabled();
});

test("basic CUDA environment is selectable and keeps GPU enabled", () => {
  const basic: ImageSummary = { name: "gsm/cuda-devel:12.4.1", displayName: "CUDA 12.4.1", environment: "cuda", runtimeVersion: "12.4.1", gpuReady: true, serverIds: ["server1"], packages: ["nvcc"] };
  render(<ContainerCreateWizard {...mockDashboardData} images={[images[0], basic]} />);
  fireEvent.change(screen.getByLabelText("开发环境"), { target: { value: "cuda" } });
  expect(screen.getByLabelText("环境版本")).toHaveValue(basic.name);
  expect(screen.getByLabelText("自动选择空闲卡")).not.toBeDisabled();
  expect(screen.getByText("已安装：nvcc")).toBeVisible();
});

test("paused Python images appear only in the compatibility category and cannot create containers", () => {
  const paused = { ...images[1], creationEnabled: false, recommended: true };
  render(<ContainerCreateWizard {...mockDashboardData} images={[images[0], paused]} />);
  expect(screen.getByLabelText("环境版本")).toHaveValue(images[0].name);
  expect(screen.queryByRole("option", { name: /Python 3.12/ })).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("开发环境"), { target: { value: "legacy" } });
  expect(screen.getByLabelText("环境版本")).toHaveValue(paused.name);
  expect(screen.getByRole("option", { name: /Python 3.12.*已暂停/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "创建容器", exact: true })).toBeDisabled();
  expect(screen.getByText("此镜像版本已暂停新建，请选择其他版本。")).toBeVisible();
});

test("switching environment category selects its enabled recommended version", () => {
  const catalog = [images[0], images[2], { ...images[3], recommended: true }];
  render(<ContainerCreateWizard {...mockDashboardData} images={catalog} />);
  // Start from Python explicitly; the globally recommended Go may be the default.
  fireEvent.change(screen.getByLabelText("开发环境"), { target: { value: "python" } });
  fireEvent.change(screen.getByLabelText("开发环境"), { target: { value: "go" } });
  expect(screen.getByLabelText("环境版本")).toHaveValue(images[3].name);
});

test("switching servers skips paused recommendations and repairs an explicitly selected legacy image", () => {
  const paused = { ...images[1], serverIds: ["server1", "server2"], creationEnabled: false, recommended: true };
  const enabled = { ...images[2], serverIds: ["server2"] };
  render(<ContainerCreateWizard {...mockDashboardData} images={[images[0], paused, enabled]} />);
  fireEvent.change(screen.getByLabelText("开发环境"), { target: { value: "legacy" } });
  expect(screen.getByLabelText("环境版本")).toHaveValue(paused.name);
  fireEvent.change(screen.getByLabelText("服务器"), { target: { value: "server2" } });
  expect(screen.getByLabelText("环境版本")).toHaveValue(enabled.name);
  expect(screen.getByLabelText("开发环境")).toHaveValue("go");
  expect(screen.getByRole("button", { name: "创建容器", exact: true })).toBeEnabled();
});

test("a server with only paused images never auto-selects one for a new container", () => {
  const paused = { ...images[1], serverIds: ["server2"], creationEnabled: false, recommended: true };
  render(<ContainerCreateWizard {...mockDashboardData} images={[images[0], paused]} />);
  fireEvent.change(screen.getByLabelText("服务器"), { target: { value: "server2" } });
  expect(screen.getByRole("button", { name: "创建容器", exact: true })).toBeDisabled();
  expect((screen.getByLabelText("镜像", { exact: true }) as HTMLSelectElement).selectedOptions).toHaveLength(0);
});

test("initial recommendation is available on the initially selected server", () => {
  const elsewhere = { ...images[3], serverIds: ["server2"], recommended: true };
  render(<ContainerCreateWizard {...mockDashboardData} images={[elsewhere, images[0]]} />);
  expect(screen.getByLabelText("环境版本")).toHaveValue(images[0].name);
  expect(screen.getByRole("button", { name: "创建容器", exact: true })).toBeEnabled();
});

test("image policy catalog groups paused versions under compatibility rather than Python", () => {
  const paused = { ...images[1], creationEnabled: false, recommended: true };
  render(<ImageVersionsPanel images={[images[0], paused]} onUpdated={() => undefined} />);
  fireEvent.change(screen.getByLabelText("镜像分类"), { target: { value: "python" } });
  expect(screen.getByText(images[0].name)).toBeVisible();
  expect(screen.queryByText(paused.name)).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("镜像分类"), { target: { value: "legacy" } });
  expect(screen.getByText(paused.name)).toBeVisible();
  expect(screen.queryByText(images[0].name)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "允许新建" })).toBeDisabled();
});
