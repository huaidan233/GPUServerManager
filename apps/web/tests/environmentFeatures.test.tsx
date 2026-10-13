import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, expect, test, vi } from "vitest";
import { ContainerCreateWizard } from "../components/ContainerCreateWizard";
import { ContainerEnvironmentTools } from "../components/ContainerEnvironmentTools";
import { MemberManagementPanel } from "../components/MemberManagementPanel";
import { parseEnvironmentRecipe, type EnvironmentRecipe } from "../lib/environmentRecipe";
import { mockDashboardData } from "../lib/mockData";

afterEach(() => vi.unstubAllGlobals());
const recipe: EnvironmentRecipe = { format: "gsm-environment", version: 1,
  configuration: { name: "reused", server_id: "server1", image: mockDashboardData.images[0].name, cpu_limit: 3, memory_limit_gb: 12, gpu_count: 1, mount_profile_ids: ["private-path"] },
  startup_commands: ["python train.py"],
};

test("reused configuration requires a fresh password and deliberate GPU selection", () => {
  render(<ContainerCreateWizard {...mockDashboardData} initialRecipe={recipe} />);
  expect(screen.getByLabelText("CPU（核）")).toHaveValue(3);
  expect(screen.getByLabelText("SSH 初始密码（至少 12 位）")).toHaveValue("");
  expect(screen.getByLabelText("不使用 GPU")).toBeChecked();
  expect(screen.getByText("原配置使用 1 张 GPU。")).toBeVisible();
  expect(screen.getByText("python train.py")).toBeInTheDocument();
});

test("recipe parser rejects malformed values and strips credential-like extras", () => {
  expect(() => parseEnvironmentRecipe({ ...recipe, version: 99 })).toThrow();
  expect(() => parseEnvironmentRecipe({ ...recipe, configuration: { ...recipe.configuration, cpu_limit: -1 } })).toThrow();
  const result = parseEnvironmentRecipe({ ...recipe, ssh_password: "secret", configuration: { ...recipe.configuration, ssh_password: "secret" } });
  expect(JSON.stringify(result)).not.toContain("secret");
});

test("application launch uses the authenticated API and a separate popup", async () => {
  const replace = vi.fn(); const close = vi.fn();
  vi.spyOn(window, "open").mockReturnValue({ location: { replace }, close, opener: {} } as unknown as Window);
  const fetcher = vi.fn().mockImplementation(async (url: string) => Response.json(url.endsWith("python-environments") ? { status: "scanned", environments: [], truncated: false } : { url: "https://apps.test/apps/handoff#ticket" }));
  vi.stubGlobal("fetch", fetcher);
  render(<ContainerEnvironmentTools containerId="c1" running />);
  fireEvent.click(screen.getByRole("button", { name: "打开 JupyterLab" }));
  await waitFor(() => expect(replace).toHaveBeenCalledWith("https://apps.test/apps/handoff#ticket"));
  expect(JSON.parse(fetcher.mock.calls.find(call => call[0].endsWith("/applications"))![1].body)).toEqual({ application: "jupyter", async_start: true });
  expect(close).not.toHaveBeenCalled();
  vi.restoreAllMocks();
});

test("supervisor assignment and CSV preview retain teacher usernames", async () => {
  const teacher = { id: "t1", username: "teacher1", display_name: "李老师", role: "teacher", member_kind: "staff", status: "active", version: 1, current_program: null, program_history: [], wechat_binding: null };
  const student = { ...teacher, id: "s1", username: "student1", display_name: "小王", role: "user", member_kind: "student", current_program: { id: "p1", level: "master", enrollment_date: "2026-09-01", expected_graduation_date: "2029-07-01" } };
  const fetcher = vi.fn().mockImplementation(async (url: string) => Response.json(url.endsWith("/supervisor") ? { ...student, supervisor_member_id: "t1", version: 2 } : [teacher, student]));
  vi.stubGlobal("fetch", fetcher); render(<MemberManagementPanel containers={[]} />);
  fireEvent.click(await screen.findByRole("button", { name: "打开 小王" }));
  fireEvent.click(screen.getByRole("button", { name: "小王 的更多操作" }));
  fireEvent.click(screen.getByRole("button", { name: "分配导师 小王" }));
  fireEvent.change(screen.getByLabelText("导师", { exact: true }), { target: { value: "t1" } });
  fireEvent.change(screen.getByLabelText("分配原因"), { target: { value: "新生入组" } });
  fireEvent.click(screen.getByRole("button", { name: "保存导师" }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
  expect(JSON.parse(fetcher.mock.calls[1][1].body)).toMatchObject({ supervisor_member_id: "t1", expected_version: 1 });
  await waitFor(() => expect(screen.queryByLabelText("分配原因")).not.toBeInTheDocument());
  const file = new File(["username,display_name,member_kind,role,program_level,enrollment_year,supervisor_username\nstu02,学生二,student,user,master,2026,teacher1"], "members.csv", { type: "text/csv" });
  fireEvent.change(screen.getByLabelText("导入 CSV"), { target: { files: [file] } });
  expect(await screen.findByRole("cell", { name: "teacher1" })).toBeVisible();
});
