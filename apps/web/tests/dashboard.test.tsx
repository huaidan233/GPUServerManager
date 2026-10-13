import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, vi } from "vitest";

import { ContainerCreateWizard } from "../components/ContainerCreateWizard";
import { Dashboard } from "../components/Dashboard";
import { GpuCard } from "../components/GpuCard";
import { WorkspaceNav } from "../components/WorkspaceNav";
import { version as appVersion } from "../package.json";
import {
  mockDashboardData,
  mockGpus,
  mockImages,
  mockMountProfiles,
  mockPresets,
  mockServers,
} from "../lib/mockData";

const personalContainerData = {
  ...mockDashboardData,
  actor: { ...mockDashboardData.actor, memberId: "member-admin" },
  containers: mockDashboardData.containers.map(container => ({ ...container, ownerMemberId: "member-admin", user: "admin", sshCommand: "" })),
};

const navigation = vi.hoisted(() => ({
  replace: vi.fn((href: string) => window.history.replaceState(null, "", href)),
  refresh: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: navigation.replace, refresh: navigation.refresh }),
}));

beforeEach(() => {
  navigation.replace.mockClear();
  navigation.refresh.mockClear();
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.unstubAllGlobals();
});

test("administrator sidebar separates shared resources, management and personal pages", () => {
  render(<Dashboard data={mockDashboardData} />);

  const research = within(screen.getByRole("group", { name: "用户功能" }));
  const management = within(screen.getByRole("group", { name: "管理员功能" }));
  const personal = within(screen.getByRole("group", { name: "个人功能" }));
  expect(research.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
    "概览", "资源概览", "容器列表", "我的数据", "GPU 空闲提醒",
  ]);
  expect(management.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
    "成员管理", "审批", "审计与告警", "系统",
  ]);
  expect(personal.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
    "打印", "我的账户",
  ]);
  expect(screen.getByLabelText(`版本 v${appVersion}`)).toHaveTextContent(`v${appVersion}`);
});

test.each([
  ["user", "用户功能"],
  ["power_user", "用户功能"],
  ["viewer", "用户功能"],
  ["teacher", "导师功能"],
] as const)("%s only sees its own groups and rejects administrator deep links", (role, groupLabel) => {
  const fetcher = vi.fn();
  vi.stubGlobal("fetch", fetcher);
  render(<Dashboard data={{
    ...mockDashboardData,
    actor: { ...mockDashboardData.actor, role, status: "active" },
  }} initialWorkspace="system" />);

  expect(screen.getByRole("group", { name: groupLabel })).toBeInTheDocument();
  const settings = within(screen.getByRole("group", { name: "个人设置" }));
  expect(settings.getAllByRole("tab")).toHaveLength(1);
  expect(settings.getByRole("tab", { name: "我的账户" })).toBeInTheDocument();
  expect(screen.queryByRole("group", { name: "管理员功能" })).not.toBeInTheDocument();
  expect(screen.queryByRole("tab", { name: "成员管理" })).not.toBeInTheDocument();
  expect(screen.queryByRole("tab", { name: "系统" })).not.toBeInTheDocument();
  expect(screen.queryByRole("tab", { name: "审计与告警" })).not.toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "概览" })).toHaveAttribute("aria-selected", "true");
  expect(fetcher).not.toHaveBeenCalled();
});

test("workspace context identifies resource creation as a shared user feature", () => {
  render(<Dashboard data={mockDashboardData} initialWorkspace="resources" />);
  expect(screen.getByRole("tabpanel")).toHaveAttribute("data-workspace-group", "research");
  expect(screen.getByText("资源查看与容器创建")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("tab", { name: "打印" }));
  expect(screen.getByRole("tabpanel", { name: "打印", exact: true })).toHaveAttribute("data-workspace-group", "personal");
  expect(screen.getByText("日常打印与个人账户")).toBeInTheDocument();
  expect(screen.queryByText("资源查看与容器创建")).not.toBeInTheDocument();
  expect(screen.getByLabelText(`版本 v${appVersion}`)).toBeInTheDocument();

  fireEvent.click(screen.getByRole("tab", { name: "概览" }));
  expect(screen.getByRole("tabpanel", { name: "概览", exact: true })).toHaveAttribute("data-workspace-group", "research");
});

test("keyboard navigation crosses group boundaries and preserves unrelated URL parameters", () => {
  window.history.replaceState(null, "", "/?server=server-a&workspace=system");
  const onSelect = vi.fn();
  const { rerender } = render(<WorkspaceNav role="admin" active="system" onSelect={onSelect} />);
  const system = screen.getByRole("tab", { name: "系统" });
  system.focus();
  fireEvent.keyDown(system, { key: "ArrowDown" });
  expect(onSelect).toHaveBeenLastCalledWith("print");
  expect(screen.getByRole("tab", { name: "打印" })).toHaveFocus();
  expect(navigation.replace).toHaveBeenLastCalledWith("/?server=server-a&workspace=print", { scroll: false });

  rerender(<WorkspaceNav role="admin" active="print" onSelect={onSelect} />);
  fireEvent.keyDown(screen.getByRole("tab", { name: "打印" }), { key: "ArrowUp" });
  expect(onSelect).toHaveBeenLastCalledWith("system");
  expect(system).toHaveFocus();

  fireEvent.keyDown(system, { key: "End" });
  expect(onSelect).toHaveBeenLastCalledWith("account");
  const account = screen.getByRole("tab", { name: "我的账户" });
  expect(account).toHaveFocus();
  fireEvent.keyDown(account, { key: "ArrowDown" });
  expect(onSelect).toHaveBeenLastCalledWith("overview");
  expect(screen.getByRole("tab", { name: "概览" })).toHaveFocus();
});

test("desktop workspace navigation supports directional keyboard selection", () => {
  render(<Dashboard data={mockDashboardData} initialWorkspace="resources" />);
  const resources = screen.getByRole("tab", { name: "容器列表" });
  resources.focus();
  fireEvent.keyDown(resources, { key: "ArrowUp" });
  expect(screen.getByRole("tab", { name: "资源概览" })).toHaveFocus();
  expect(screen.getByRole("tab", { name: "资源概览" })).toHaveAttribute("aria-selected", "true");
  expect(navigation.replace).toHaveBeenCalledWith("/?workspace=resource-overview", { scroll: false });
});

test("overview shortcuts select a workspace and keep its URL in sync", () => {
  render(<Dashboard data={mockDashboardData} initialWorkspace="overview" overview={{
    persona: "user", generated_at: "2026-09-08T10:00:00+08:00", tasks: {},
  }} />);
  fireEvent.click(screen.getByRole("button", { name: "进入资源工作区" }));
  expect(navigation.replace).toHaveBeenCalledWith("/?workspace=resources", { scroll: false });
  expect(screen.getByRole("tab", { name: "容器列表" })).toHaveAttribute("aria-selected", "true");
});

test("missing GPU observations explain the next step instead of an empty panel", () => {
  render(<Dashboard data={{ ...mockDashboardData, gpus: [] }} initialWorkspace="resources" />);
  expect(screen.getByText(/暂无 GPU 观测/)).toBeInTheDocument();
  expect(screen.getByText(/无观测不代表 GPU 空闲/)).toBeInTheDocument();
});

test.each(["admin", "user", "power_user"] as const)(
  "%s opens printing in its own sidebar workspace instead of resources",
  (role) => {
    render(<Dashboard data={{
      ...mockDashboardData,
      actor: { ...mockDashboardData.actor, role, status: "active" },
    }} initialWorkspace="resources" />);

    expect(screen.queryByRole("region", { name: "打印" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "打印" }));

    expect(screen.getByRole("region", { name: "打印" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "打印", level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "打印" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("heading", { name: "创建容器" })).not.toBeInTheDocument();
    expect(navigation.replace).toHaveBeenCalledWith("/?workspace=print", { scroll: false });

    fireEvent.click(screen.getByRole("tab", { name: "容器列表" }));
    expect(screen.queryByRole("region", { name: "打印" })).not.toBeInTheDocument();
  },
);

test("printing supports opening its workspace directly", () => {
  render(<Dashboard data={mockDashboardData} initialWorkspace="print" />);

  expect(screen.getByRole("tab", { name: "打印" })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("region", { name: "打印" })).toBeInTheDocument();
});

test.each(["viewer"] as const)(
  "%s cannot select an unauthorized print workspace",
  (role) => {
    render(<Dashboard data={{
      ...mockDashboardData,
      actor: { ...mockDashboardData.actor, role, status: "active" },
    }} initialWorkspace="print" />);

    expect(screen.queryByRole("tab", { name: "打印" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "打印" })).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "概览" })).toHaveAttribute("aria-selected", "true");
  },
);

test("teacher navigation exposes only teacher workspaces and syncs the URL", () => {
  render(
    <Dashboard
      data={{
        ...mockDashboardData,
        actor: {
          userId: "teacher",
          memberId: "member-teacher",
          displayName: "李老师",
          role: "teacher",
          status: "active",
        },
      }}
      initialWorkspace="overview"
    />,
  );

  expect(screen.getByRole("tab", { name: "学生" })).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "审批" })).toBeInTheDocument();
  expect(screen.queryByRole("tab", { name: "成员管理" })).not.toBeInTheDocument();
  expect(screen.queryByRole("tab", { name: "系统" })).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("tab", { name: "审批" }));

  expect(navigation.replace).toHaveBeenCalledWith("/?workspace=approvals", {
    scroll: false,
  });
  expect(window.location.search).toBe("?workspace=approvals");
});

test("pending-exit administrator navigation loses privileged workspaces", () => {
  render(
    <Dashboard
      data={{
        ...mockDashboardData,
        actor: {
          ...mockDashboardData.actor,
          status: "pending_exit",
        },
      }}
      initialWorkspace="system"
    />,
  );

  expect(screen.getByRole("tab", { name: "概览" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  expect(screen.queryByRole("tab", { name: "成员管理" })).not.toBeInTheDocument();
  expect(screen.queryByRole("tab", { name: "系统" })).not.toBeInTheDocument();
  expect(screen.queryByRole("group", { name: "管理员功能" })).not.toBeInTheDocument();
  expect(screen.getByRole("group", { name: "用户功能" })).toBeInTheDocument();
});

test("dashboard renders role overview and assigned teacher students", () => {
  const teacherData = {
    ...mockDashboardData,
    actor: {
      userId: "teacher",
      memberId: "member-teacher",
      displayName: "李老师",
      role: "teacher" as const,
      status: "active",
    },
  };
  const overview = {
    persona: "teacher" as const,
    generated_at: "2026-08-10T08:00:00+08:00",
    tasks: {
      assigned_expiring_students: { count: 1, items: [] },
    },
  };
  const teacherStudents = [{
    id: "member-alice",
    username: "alice",
    display_name: "Alice",
    role: "user" as const,
    member_kind: "student" as const,
    supervisor_member_id: "member-teacher",
    status: "active",
    version: 2,
    current_program: {
      level: "master",
      expected_graduation_date: "2029-07-01",
    },
    program_history: [],
  }];
  const { rerender } = render(
    <Dashboard
      data={teacherData}
      overview={overview}
      teacherStudents={teacherStudents}
      initialWorkspace="overview"
    />,
  );

  expect(screen.getByText("1 名学生临近毕业")).toBeInTheDocument();

  rerender(
    <Dashboard
      data={teacherData}
      overview={overview}
      teacherStudents={teacherStudents}
      initialWorkspace="students"
    />,
  );
  expect(screen.getByRole("table", { name: "已分配学生列表" })).toBeInTheDocument();
  expect(screen.getByText("Alice")).toBeInTheDocument();
});

test("header controls navigate and refresh while unsupported mount sync is absent", () => {
  render(<Dashboard data={mockDashboardData} />);

  fireEvent.click(screen.getByRole("button", { name: "打开审计与活动" }));
  expect(navigation.replace).toHaveBeenCalledWith("/?workspace=audit", { scroll: false });

  fireEvent.click(screen.getByRole("tab", { name: "容器列表" }));
  fireEvent.click(screen.getByRole("button", { name: "刷新资源" }));
  expect(navigation.refresh).toHaveBeenCalledTimes(1);
  expect(screen.getByText(/最近刷新/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "同步挂载" })).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "打开告警" }));
  expect(navigation.replace).toHaveBeenLastCalledWith("/?workspace=audit", { scroll: false });
});

test("renders occupied gpu with user and memory", () => {
  render(
    <GpuCard
      index={0}
      user="Alice"
      container="llama-debug"
      memoryUsedGb={21}
      memoryTotalGb={48}
      utilization={86}
      state="busy"
      processCount={2}
      declaredContainerCount={1}
      freshness="fresh"
    />,
  );

  expect(screen.getByText("GPU 0")).toBeInTheDocument();
  expect(screen.getByText("Alice")).toBeInTheDocument();
  expect(screen.getByText("21 / 48 GB")).toBeInTheDocument();
  expect(screen.getByText("繁忙")).toBeInTheDocument();
  expect(screen.getByText("进程 2")).toBeInTheDocument();
  expect(screen.getByText("声明容器 1")).toBeInTheDocument();

  const meter = screen.getByRole("meter", { name: "GPU 0 显存使用" });
  expect(meter).toHaveAttribute("aria-valuemin", "0");
  expect(meter).toHaveAttribute("aria-valuemax", "48");
  expect(meter).toHaveAttribute("aria-valuenow", "21");
  expect(meter).toHaveAttribute("aria-valuetext", "21 / 48 GB (44%)");
});

test("occupied GPU can be configured for later but cannot start immediately", () => {
  render(
    <ContainerCreateWizard
      servers={mockServers}
      presets={mockPresets}
      images={mockImages}
      gpus={mockGpus}
      mountProfiles={mockMountProfiles}
    />,
  );

  fireEvent.click(screen.getByLabelText("手动选择 GPU"));
  fireEvent.click(screen.getByLabelText("GPU 0: Alice 使用中"));

  expect(screen.getByText("GPU 0 当前被 Alice / llama-debug 使用")).toBeInTheDocument();
  expect(screen.getByRole("checkbox", { name: "workspace 挂载到 /workspace" })).toBeChecked();

  const createButton = screen.getByRole("button", { name: "创建容器" });
  expect(createButton).toBeDisabled();
  expect(createButton).toHaveAccessibleDescription("这张 GPU 已被占用，请换卡或取消立即启动，等待空闲后再启动。");

  const shareConfirmation = screen.getByRole("checkbox", { name: "创建后立即启动" });
  expect(shareConfirmation).toBeChecked();

  fireEvent.click(shareConfirmation);

  expect(createButton).toBeEnabled();
});

test("submits create container payload through the web API proxy", async () => {
  const fetchMock = vi.fn(async () => {
    return {
      ok: true,
      json: async () => ({ container: { name: "api-debug" }, warnings: [] }),
    } as Response;
  });
  vi.stubGlobal("fetch", fetchMock);

  render(
    <ContainerCreateWizard
      servers={mockServers}
      presets={mockPresets}
      images={mockImages}
      gpus={mockGpus}
      mountProfiles={mockMountProfiles}
    />,
  );

  fireEvent.change(screen.getByLabelText("容器名称"), { target: { value: "api-debug" } });
  fireEvent.change(screen.getByLabelText("资源套餐"), { target: { value: "Debug" } });
  fireEvent.change(screen.getByLabelText("服务器"), { target: { value: "server2" } });
  fireEvent.click(screen.getByLabelText("手动选择 GPU"));
  fireEvent.click(screen.getByLabelText("GPU 2: 空闲"));
  if (screen.queryByLabelText("SSH 初始密码（至少 12 位）")) fireEvent.change(screen.getByLabelText("SSH 初始密码（至少 12 位）"), { target: { value: "creation-ssh-password" } });
  fireEvent.click(screen.getByRole("button", { name: "创建容器" }));

  await waitFor(() => expect(screen.getByText("容器 api-debug 已创建。")).toBeInTheDocument());
  expect(fetchMock).toHaveBeenCalledWith("/api/gsm/containers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: "api-debug",
      server_id: "server2",
      image: "gsm/pytorch-cuda-ssh:latest",
      preset: "Debug",
      cpu_limit: 4,
      memory_limit_gb: 8,
      gpu_devices: ["2"],
      mount_profile_ids: ["workspace-default", "scratch-default"],
      ssh_enabled: true,
      ssh_password: "creation-ssh-password",
      auto_start: true,
      note: "",
      memory_tier: "8_24",
      estimated_duration_minutes: 120,
    }),
  });
});

test("preset selection fills editable resources and editing switches to custom", () => {
  render(
    <ContainerCreateWizard
      servers={mockServers}
      presets={mockPresets}
      images={mockImages}
      gpus={mockGpus}
      mountProfiles={mockMountProfiles}
    />,
  );

  expect(screen.getByLabelText("CPU（核）")).toHaveValue(2);
  expect(screen.getByLabelText("内存（GiB）")).toHaveValue(8);
  fireEvent.change(screen.getByLabelText("资源套餐"), { target: { value: "Debug" } });
  expect(screen.getByLabelText("CPU（核）")).toHaveValue(4);
  expect(screen.getByLabelText("内存（GiB）")).toHaveValue(8);

  fireEvent.change(screen.getByLabelText("CPU（核）"), { target: { value: "6" } });
  expect(screen.getByLabelText("资源套餐")).toHaveValue("Custom");
  expect(screen.getByLabelText("内存（GiB）")).toHaveValue(8);
  fireEvent.change(screen.getByLabelText("资源套餐"), { target: { value: "Train" } });
  expect(screen.getByLabelText("CPU（核）")).toHaveValue(8);
  expect(screen.getByLabelText("内存（GiB）")).toHaveValue(32);
  fireEvent.change(screen.getByLabelText("内存（GiB）"), { target: { value: "24" } });
  expect(screen.getByLabelText("资源套餐")).toHaveValue("Custom");
  expect(screen.getByLabelText("CPU（核）")).toHaveValue(8);
});

test.each(["user", "power_user", "admin"] as const)(
  "%s can submit custom resources without inheriting a preset's limits",
  async (role) => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      json: async () => ({ container: { name: "debug" }, warnings: [] }),
    }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    render(<Dashboard data={{
      ...mockDashboardData,
      actor: { ...mockDashboardData.actor, role, status: "active" },
    }} initialWorkspace="resources" />);

    if (screen.queryByLabelText("SSH 初始密码（至少 12 位）")) fireEvent.change(screen.getByLabelText("SSH 初始密码（至少 12 位）"), { target: { value: "creation-ssh-password" } });
    fireEvent.click(screen.getByRole("button", { name: "创建容器" }));
    const dialog = within(screen.getByRole("dialog", { name: "创建容器" }));
    fireEvent.change(screen.getByLabelText("资源套餐"), { target: { value: "Custom" } });
    fireEvent.change(screen.getByLabelText("CPU（核）"), { target: { value: "6" } });
    fireEvent.change(screen.getByLabelText("内存（GiB）"), { target: { value: "24" } });
    if (screen.queryByLabelText("SSH 初始密码（至少 12 位）")) fireEvent.change(screen.getByLabelText("SSH 初始密码（至少 12 位）"), { target: { value: "creation-ssh-password" } });
    fireEvent.click(dialog.getByRole("button", { name: "创建容器" }));

    expect(await screen.findByText("容器 debug 已创建。")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "创建容器" })).not.toBeInTheDocument();
    expect(navigation.refresh).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith("/api/gsm/containers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: "debug",
        server_id: "server1",
        image: "gsm/pytorch-cuda-ssh:latest",
        preset: "Custom",
        cpu_limit: 6,
        memory_limit_gb: 24,
        gpu_devices: [],
        mount_profile_ids: ["workspace-default", "scratch-default"],
        ssh_enabled: true,
        ssh_password: "creation-ssh-password",
        auto_start: true,
        note: "",
        memory_tier: "8_24",
        estimated_duration_minutes: 120,
      }),
    });
  },
);

test.each([
  ["CPU（核）", ""],
  ["CPU（核）", "0"],
  ["CPU（核）", "1.5"],
  ["内存（GiB）", ""],
  ["内存（GiB）", "-1"],
  ["内存（GiB）", "2.5"],
])("rejects invalid custom resource %s=%s before sending a request", (label, value) => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  render(
    <ContainerCreateWizard
      servers={mockServers}
      presets={mockPresets}
      images={mockImages}
      gpus={mockGpus}
      mountProfiles={mockMountProfiles}
    />,
  );

  fireEvent.change(screen.getByLabelText(label), { target: { value } });
  if (screen.queryByLabelText("SSH 初始密码（至少 12 位）")) fireEvent.change(screen.getByLabelText("SSH 初始密码（至少 12 位）"), { target: { value: "creation-ssh-password" } });
  fireEvent.submit(screen.getByRole("button", { name: "创建容器" }).closest("form")!);
  expect(screen.getByRole("alert")).toHaveTextContent("CPU 核数和内存必须为正整数。");
  expect(fetchMock).not.toHaveBeenCalled();
});

test.each(["Large Train", "Custom"])("shows approval submission for oversized %s resources without claiming creation", async (preset) => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: true,
      status: 202,
      json: async () => ({
        requires_approval: true,
        approval: { id: "approval-large" },
      }),
    }) as unknown as Response),
  );

  render(
    <ContainerCreateWizard
      servers={mockServers}
      presets={mockPresets}
      images={mockImages}
      gpus={mockGpus}
      mountProfiles={mockMountProfiles}
    />,
  );

  fireEvent.change(screen.getByLabelText("资源套餐"), { target: { value: preset } });
  if (preset === "Custom") {
    fireEvent.change(screen.getByLabelText("内存（GiB）"), { target: { value: "48" } });
  }
  if (screen.queryByLabelText("SSH 初始密码（至少 12 位）")) fireEvent.change(screen.getByLabelText("SSH 初始密码（至少 12 位）"), { target: { value: "creation-ssh-password" } });
  fireEvent.click(screen.getByRole("button", { name: "创建容器" }));

  expect(await screen.findByText("已提交管理员审批：approval-large")).toBeInTheDocument();
  expect(screen.queryByText(/容器 .* 已创建/)).not.toBeInTheDocument();
});

test("uses the server-provided gpu state instead of declared container count", () => {
  render(
    <GpuCard
      index={3}
      user="Alice"
      container="mounted-only"
      memoryUsedGb={0}
      memoryTotalGb={48}
      utilization={0}
      state="shareable"
      processCount={0}
      declaredContainerCount={1}
      freshness="fresh"
    />,
  );

  expect(screen.getByText("可共享")).toBeInTheDocument();
  expect(screen.getByText("进程 0")).toBeInTheDocument();
  expect(screen.getByText("声明容器 1")).toBeInTheDocument();
});

test("shows create container API errors without clearing the form", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      return {
        ok: false,
        json: async () => ({ detail: "image is not in catalog" }),
      } as Response;
    }),
  );

  render(
    <ContainerCreateWizard
      servers={mockServers}
      presets={mockPresets}
      images={mockImages}
      gpus={mockGpus}
      mountProfiles={mockMountProfiles}
    />,
  );

  fireEvent.change(screen.getByLabelText("容器名称"), { target: { value: "bad-image" } });
  if (screen.queryByLabelText("SSH 初始密码（至少 12 位）")) fireEvent.change(screen.getByLabelText("SSH 初始密码（至少 12 位）"), { target: { value: "creation-ssh-password" } });
  fireEvent.click(screen.getByRole("button", { name: "创建容器" }));

  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("image is not in catalog"));
  expect(screen.getByLabelText("容器名称")).toHaveValue("bad-image");
});

test("pulls a custom image for the selected server through the web API proxy", async () => {
  const fetchMock = vi.fn(async () => {
    return {
      ok: true,
      json: async () => ({
        status: "pulled",
        image: { name: "docker.io/pytorch/pytorch", tag: "2.6.0-cuda12.4-cudnn9-runtime" },
      }),
    } as Response;
  });
  vi.stubGlobal("fetch", fetchMock);

  render(
    <ContainerCreateWizard
      servers={mockServers}
      presets={mockPresets}
      images={mockImages}
      gpus={mockGpus}
      mountProfiles={mockMountProfiles}
    />,
  );

  fireEvent.change(screen.getByLabelText("镜像地址"), {
    target: { value: "docker.io/pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime" },
  });
  fireEvent.click(screen.getByRole("button", { name: "拉取镜像" }));

  await waitFor(() =>
    expect(
      screen.getByText("镜像 docker.io/pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime 已加入目录。"),
    ).toBeInTheDocument(),
  );
  expect(screen.getByLabelText("镜像")).toHaveValue("docker.io/pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime");
  expect(fetchMock).toHaveBeenCalledWith("/api/gsm/images/pull", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      server_id: "server1",
      image: "docker.io/pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime",
    }),
  });
});

test("requests stop confirmation from the containers table and confirms it", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce({
      ok: true,
      status: 202,
      json: async () => ({
        requires_confirmation: true,
        confirmation: {
          id: "confirm-stop",
          required_phrase: "停止 container llama-debug",
          action_type: "stop_container",
        },
      }),
    } as Response)
    .mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ status: "executed", container: { id: "container-llama-debug", status: "stopped" } }),
    } as Response);
  vi.stubGlobal("fetch", fetchMock);

  render(<Dashboard data={personalContainerData} initialWorkspace="resources" />);

  fireEvent.click(screen.getByRole("button", { name: "停止 llama-debug" }));

  expect(await screen.findByRole("dialog", { name: "确认容器操作" })).toBeInTheDocument();
  expect(screen.getByText("停止 container llama-debug")).toBeInTheDocument();

  const confirmButton = screen.getByRole("button", { name: "确认执行" });
  expect(confirmButton).toBeDisabled();

  fireEvent.change(screen.getByLabelText("确认短语"), {
    target: { value: "停止 container llama-debug" },
  });
  fireEvent.click(confirmButton);

  await waitFor(() => expect(screen.getByText("操作已执行。")).toBeInTheDocument());
  expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/gsm/containers/container-llama-debug/stop", {
    method: "POST",
  });
  expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/gsm/confirmations/confirm-stop/confirm", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ phrase: "停止 container llama-debug" }),
  });
});

test("starts a stopped container from the containers table", async () => {
  const fetchMock = vi.fn(async () => {
    return {
      ok: true,
      json: async () => ({ container: { id: "container-coding-cpu", status: "running" } }),
    } as Response;
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<Dashboard data={personalContainerData} initialWorkspace="resources" />);

  fireEvent.click(screen.getByRole("button", { name: "启动 coding-cpu" }));

  await waitFor(() => expect(screen.getByText("容器 coding-cpu 已启动。")).toBeInTheDocument());
  expect(fetchMock).toHaveBeenCalledWith("/api/gsm/containers/container-coding-cpu/start", {
    method: "POST",
  });
});

test("requests delete confirmation and removes the container after confirming", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce({
      ok: true,
      status: 202,
      json: async () => ({
        requires_confirmation: true,
        confirmation: {
          id: "confirm-delete",
          required_phrase: "删除 container diffusion-train",
          action_type: "delete_container",
        },
      }),
    } as Response)
    .mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        status: "executed",
        action_type: "delete_container",
        container: { id: "container-diffusion-train", name: "diffusion-train", status: "running" },
      }),
    } as Response);
  vi.stubGlobal("fetch", fetchMock);

  render(<Dashboard data={personalContainerData} initialWorkspace="resources" />);

  fireEvent.click(screen.getByRole("button", { name: "删除 diffusion-train" }));
  expect(await screen.findByText("删除 container diffusion-train")).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("确认短语"), {
    target: { value: "删除 container diffusion-train" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认执行" }));

  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "删除 diffusion-train" })).not.toBeInTheDocument(),
  );
  expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/gsm/containers/container-diffusion-train", {
    method: "DELETE",
  });
});

test("resets the create form instead of offering a non-functional draft deletion", () => {
  render(
    <ContainerCreateWizard
      servers={mockServers}
      presets={mockPresets}
      images={mockImages}
      gpus={mockGpus}
      mountProfiles={mockMountProfiles}
    />,
  );

  fireEvent.change(screen.getByLabelText("容器名称"), { target: { value: "custom-name" } });
  fireEvent.change(screen.getByLabelText("镜像地址"), { target: { value: "custom:image" } });
  fireEvent.click(screen.getByLabelText("手动选择 GPU"));
  fireEvent.click(screen.getByLabelText("GPU 0: Alice 使用中"));
  fireEvent.click(screen.getByRole("button", { name: "重置表单" }));

  expect(screen.getByLabelText("容器名称")).toHaveValue("debug");
  expect(screen.getByLabelText("镜像地址")).toHaveValue("");
  expect(screen.getByLabelText("不使用 GPU")).toBeChecked();
  expect(screen.queryByLabelText("GPU 0: Alice 使用中")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "重置表单" })).toBeInTheDocument();
});

test("provides form names and disables note autocomplete", () => {
  render(
    <ContainerCreateWizard
      servers={mockServers}
      presets={mockPresets}
      images={mockImages}
      gpus={mockGpus}
      mountProfiles={mockMountProfiles}
    />,
  );

  expect(screen.getByLabelText("服务器")).toHaveAttribute("name", "server");
  expect(screen.getByLabelText("资源套餐")).toHaveAttribute("name", "preset");
  expect(screen.getByLabelText("镜像")).toHaveAttribute("name", "image");

  fireEvent.click(screen.getByLabelText("手动选择 GPU"));

  expect(screen.getByLabelText("GPU 0: Alice 使用中")).toHaveAttribute("name", "gpus");
  expect(screen.getByLabelText("GPU 0: Alice 使用中")).toHaveAttribute("value", "0");
  expect(screen.getByRole("checkbox", { name: "workspace 挂载到 /workspace" })).toHaveAttribute(
    "name",
    "mounts",
  );

  expect(screen.getByLabelText("备注")).toHaveAttribute("name", "note");
  expect(screen.getByLabelText("备注")).toHaveAttribute("autocomplete", "off");
});

test("renders skip link and an accessible containers table name", () => {
  render(<Dashboard data={mockDashboardData} initialWorkspace="resources" />);

  expect(screen.getByRole("link", { name: "跳到主要内容" })).toHaveAttribute(
    "href",
    "#main-content",
  );
  expect(screen.getByRole("main")).toHaveAttribute("id", "main-content");
  expect(screen.getByRole("table", { name: "容器列表" })).toBeInTheDocument();
});

test("viewer role sees identity and cannot mutate containers or images", () => {
  render(
    <Dashboard initialWorkspace="resources"
      data={{
        ...mockDashboardData,
        actor: { userId: "viewer", memberId: "member-admin", role: "viewer" },
        containers: personalContainerData.containers,
      }}
    />,
  );

  expect(screen.getByRole("button", { name: "打开我的账户" })).toHaveTextContent("viewer");
  expect(screen.getByText("只读成员")).toBeInTheDocument();
  expect(screen.getByText("当前账户仅可查看资源，不能创建容器。")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "创建容器" })).toBeDisabled();
  expect(screen.queryByRole("button", { name: "拉取镜像" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "停止 llama-debug" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "删除 llama-debug" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "启动 coding-cpu" })).toBeDisabled();
  expect(screen.queryByRole("tab", { name: "成员管理" })).not.toBeInTheDocument();
});

test("loads account and member workspaces only after their tabs are selected", async () => {
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    if (input.toString() === "/api/gsm/admin/members") return Response.json([]);
    if (input.toString() === "/api/gsm/account") {
      return Response.json({
        id: "member-admin",
        username: "admin",
        display_name: "Administrator",
        role: "admin",
        status: "active",
        current_program: null,
      });
    }
    if (input.toString().endsWith("wechat-binding")) return Response.json({ binding: null });
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetcher);
  render(<Dashboard data={mockDashboardData} />);

  expect(screen.getByRole("tab", { name: "概览" })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("tab", { name: "容器列表" })).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "我的账户" })).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "成员管理" })).toBeInTheDocument();
  expect(fetcher).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("tab", { name: "我的账户" }));
  expect(await screen.findByRole("heading", { name: "Administrator" })).toBeInTheDocument();
  expect(fetcher).not.toHaveBeenCalledWith("/api/gsm/admin/members");

  fireEvent.click(screen.getByRole("tab", { name: "成员管理" }));
  expect(await screen.findByRole("table", { name: "课题组成员列表" })).toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledWith("/api/gsm/admin/members");
});

test("administrator system workspace loads the masked Agent settings panel", async () => {
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    if (input.toString().endsWith("quota/settings")) return Response.json({ baseline: { cpu: 8, memory_gb: 8, gpus: 0 }, members: [] });
    if (input.toString().endsWith("print-admin/agents")) return Response.json({ agents: [], printers: [] });
    return Response.json({
    version: 1,
    endpoint: "https://provider.example/v1",
    model: "gpt-test",
    primary_configured: true,
    primary_fingerprint: "1eb90d4a288b",
    fallback_configured: true,
    fallback_fingerprint: "a288df15c5fc",
    active_slot: "primary",
    primary_status: "healthy",
    fallback_status: "standby",
    updated_at: "2026-08-10T08:00:00+00:00",
    updated_by: "member-admin",
  }); });
  vi.stubGlobal("fetch", fetcher);

  render(<Dashboard data={mockDashboardData} initialWorkspace="overview" />);
  expect(fetcher).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("tab", { name: "系统" }));

  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "管理微信 Agent" }));
  expect(await screen.findByRole("heading", { name: "Agent 模型配置" })).toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledWith("/api/gsm/admin/agent-settings");
});

test("administrator approvals workspace renders governance decisions", () => {
  render(<Dashboard data={mockDashboardData} initialWorkspace="approvals" />);

  expect(screen.getByRole("heading", { name: "申请临时额度" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "额度申请记录" })).toBeInTheDocument();
});

test("administrator audit workspace renders activity and health evidence", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json([])));
  render(<Dashboard data={mockDashboardData} initialWorkspace="audit" />);

  expect(screen.getByRole("heading", { name: "活动记录" })).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "服务状态" })).not.toBeInTheDocument();
});

test("ordinary member queue workspace renders queue controls", () => {
  render(
    <Dashboard
      data={{
        ...mockDashboardData,
        actor: {
          userId: "alice",
          memberId: "member-alice",
          displayName: "Alice",
          role: "user",
          status: "active",
        },
      }}
      initialWorkspace="queue"
    />,
  );

  expect(screen.getByRole("heading", { name: "GPU 空闲提醒", level: 2 })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "订阅空闲提醒" })).toBeInTheDocument();
});

test("teacher usage workspace contains only assigned student containers", () => {
  render(
    <Dashboard
      data={{
        ...mockDashboardData,
        actor: {
          userId: "teacher",
          memberId: "member-teacher",
          displayName: "李老师",
          role: "teacher",
          status: "active",
        },
      }}
      teacherStudents={[{
        id: "member-alice",
        username: "alice",
        display_name: "Alice",
        role: "user",
        member_kind: "student",
        supervisor_member_id: "member-teacher",
        status: "active",
        version: 2,
        current_program: {
          id: "program-alice",
          level: "master",
          enrollment_date: "2026-09-01",
          expected_graduation_date: "2029-07-01",
        },
        program_history: [],
      }]}
      initialWorkspace="usage"
    />,
  );

  expect(screen.getByRole("heading", { name: "学生资源使用情况" })).toBeInTheDocument();
  expect(screen.getByText("llama-debug")).toBeInTheDocument();
  expect(screen.queryByText("diffusion-train")).not.toBeInTheDocument();
});
