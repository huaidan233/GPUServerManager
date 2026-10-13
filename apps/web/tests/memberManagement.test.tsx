import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, test, vi } from "vitest";

import { MemberManagementPanel } from "../components/MemberManagementPanel";
import { mockDashboardData } from "../lib/mockData";


const master = {
  id: "member-alice",
  username: "alice",
  display_name: "Alice Zhang",
  role: "user",
  status: "active",
  version: 3,
  current_program: {
    id: "program-alice",
    level: "master",
    enrollment_date: "2026-09-01",
    expected_graduation_date: "2029-07-01",
  },
  program_history: [],
  wechat_binding: null,
};


afterEach(() => {
  vi.unstubAllGlobals();
});


function openMemberAction(memberName: string, actionName: string) {
  fireEvent.click(screen.getByRole("button", { name: `打开 ${memberName}` }));
  fireEvent.click(screen.getByRole("button", { name: `${memberName} 的更多操作` }));
  fireEvent.click(screen.getByRole("button", { name: `${actionName} ${memberName}` }));
}

test("member container management is scoped, keeps the admin identity and refreshes after a start", async () => {
  const aliceContainer = { ...mockDashboardData.containers[0], status: "stopped", statusText: "已停止" };
  const fetched = vi.fn(async (url: RequestInfo | URL) => url.toString().endsWith("/start")
    ? Response.json({ container: { id: aliceContainer.id, status: "running" } })
    : Response.json([master, { ...master, id: "member-bob", username: "bob", display_name: "Bob Li" }]));
  vi.stubGlobal("fetch", fetched);
  const refreshed = vi.fn();
  render(<MemberManagementPanel containers={[aliceContainer, mockDashboardData.containers[1]]} actor={{ userId: "admin", memberId: "member-admin", role: "admin", status: "active" }} onContainersUpdated={refreshed} />);
  const trigger = await screen.findByRole("button", { name: "管理 Alice Zhang 的容器" });
  fireEvent.click(trigger);
  const dialog = within(screen.getByRole("dialog", { name: "Alice Zhang 的容器" }));
  expect(dialog.getByRole("button", { name: `管理 ${aliceContainer.name}` })).toBeVisible();
  expect(dialog.queryByRole("button", { name: `管理 ${mockDashboardData.containers[1].name}` })).not.toBeInTheDocument();
  expect(dialog.queryByText("SSH 密码")).not.toBeInTheDocument();
  expect(fetched.mock.calls.some(([url]) => url.toString().endsWith("ssh-password"))).toBe(false);
  fireEvent.click(dialog.getByRole("button", { name: `启动 ${aliceContainer.name}` }));
  expect(await dialog.findByRole("button", { name: `停止 ${aliceContainer.name}` })).toBeEnabled();
  expect(refreshed).toHaveBeenCalledTimes(1);
  expect(fetched).toHaveBeenCalledWith(`/api/gsm/containers/${aliceContainer.id}/start`, { method: "POST" });
  fireEvent.click(dialog.getByRole("button", { name: "关闭Alice Zhang 的容器" }));
  expect(trigger).toHaveFocus();
});

test("member details open an empty container view and a different member never inherits its rows", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json([master, { ...master, id: "member-empty", username: "empty", display_name: "Empty Member" }])));
  render(<MemberManagementPanel containers={mockDashboardData.containers} actor={{ userId: "admin", role: "admin" }} />);
  fireEvent.click(await screen.findByRole("button", { name: "管理 Alice Zhang 的容器" }));
  expect(screen.getByRole("button", { name: `管理 ${mockDashboardData.containers[0].name}` })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "关闭Alice Zhang 的容器" }));
  fireEvent.click(screen.getByRole("button", { name: "打开 Empty Member" }));
  fireEvent.click(screen.getByRole("button", { name: "管理容器" }));
  expect(screen.queryByRole("dialog", { name: "Empty Member 详情" })).not.toBeInTheDocument();
  const dialog = within(screen.getByRole("dialog", { name: "Empty Member 的容器" }));
  expect(dialog.getByText("该成员暂无容器。")).toBeVisible();
  expect(dialog.queryByRole("button", { name: `管理 ${mockDashboardData.containers[0].name}` })).not.toBeInTheDocument();
});


test("renders dense filters, degree badges and graduation emphasis", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json([
    master,
    {
      ...master,
      id: "member-bob",
      username: "bob",
      display_name: "Bob Li",
      current_program: {
        ...master.current_program,
        id: "program-bob",
        level: "doctor",
        expected_graduation_date: "2030-07-01",
      },
    },
  ])));

  render(<MemberManagementPanel containers={[]} />);

  expect(await screen.findByRole("table", { name: "课题组成员列表" })).toBeInTheDocument();
  expect(screen.getByLabelText("成员状态筛选")).toBeInTheDocument();
  expect(screen.getByLabelText("培养层次筛选")).toBeInTheDocument();
  expect(await screen.findByText("硕士")).toBeInTheDocument();
  expect(screen.getByText("博士")).toBeInTheDocument();
  expect(screen.getByText("2029-07-01")).toHaveClass("graduation-date");
});


test("member row opens a detail drawer with one action menu", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json([master])));
  render(<MemberManagementPanel containers={[]} />);
  await screen.findByText("Alice Zhang");

  expect(screen.queryByRole("button", { name: "延期 Alice Zhang" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "打开 Alice Zhang" }));

  const drawer = screen.getByRole("dialog", { name: "Alice Zhang 详情" });
  expect(drawer).toBeInTheDocument();
  const menuTrigger = screen.getByRole("button", { name: "Alice Zhang 的更多操作" });
  expect(menuTrigger).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(menuTrigger);
  expect(menuTrigger).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByRole("button", { name: "延期 Alice Zhang" })).toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: /更多操作/ })).toHaveLength(1);
});


test("CSV import previews rows before one-time codes are created", async () => {
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    if (input.toString() === "/api/gsm/admin/members") return Response.json([master]);
    return Response.json({ members: [] });
  });
  vi.stubGlobal("fetch", fetcher);
  render(<MemberManagementPanel containers={[]} />);
  await screen.findByText("Alice Zhang");

  const csv = [
    "username,display_name,member_kind,role,program_level,enrollment_year",
    "new-master,New Master,student,user,master,2026",
  ].join("\n");
  const file = new File([csv], "members.csv", { type: "text/csv" });
  fireEvent.change(screen.getByLabelText("导入 CSV"), {
    target: { files: [file] },
  });

  expect(await screen.findByRole("heading", { name: "确认 1 条邀请" })).toBeInTheDocument();
  expect(screen.getByRole("cell", { name: "new-master" })).toBeInTheDocument();
  expect(fetcher).not.toHaveBeenCalledWith(
    "/api/gsm/admin/members/batch",
    expect.anything(),
  );

  fireEvent.click(screen.getByRole("button", { name: "创建 1 个成员" }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledWith(
    "/api/gsm/admin/members/batch",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        members: [{
          username: "new-master",
          display_name: "New Master",
          member_kind: "student",
          role: "user",
          program_level: "master",
          enrollment_year: 2026,
        }],
      }),
    },
  ));
});


test("previews academic defaults and provides an initial password without pairing codes", async () => {
  const fetcher = vi.fn()
    .mockResolvedValueOnce(Response.json([master]))
    .mockResolvedValueOnce(Response.json({
      member: { ...master, id: "member-new", username: "new-member", version: 1 },
      initial_password: "initial-password-once",
    }, { status: 201 }));
  vi.stubGlobal("fetch", fetcher);
  render(<MemberManagementPanel containers={[]} />);
  await screen.findByText("Alice Zhang");

  fireEvent.click(screen.getByRole("button", { name: "邀请成员" }));
  fireEvent.change(screen.getByLabelText("入学年份"), { target: { value: "2028" } });
  expect(screen.getByText("2028-09-01 至 2031-07-01")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "new-member" } });
  fireEvent.change(screen.getByLabelText("姓名"), { target: { value: "New Member" } });
  fireEvent.click(screen.getByRole("button", { name: "确认邀请" }));

  const result = await screen.findByRole("dialog", { name: "成员登录信息" });
  expect(within(result).getByText("initial-password-once")).toBeInTheDocument();
  expect(within(result).queryByText("微信配对码")).not.toBeInTheDocument();
  expect(fetcher).toHaveBeenLastCalledWith("/api/gsm/admin/members", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      username: "new-member",
      display_name: "New Member",
      role: "user",
      member_kind: "student",
      program_level: "master",
      enrollment_year: 2028,
    }),
  });
});


test("teacher invitation has no academic dates or expiry", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(Response.json([master])).mockResolvedValueOnce(Response.json({
    member: { ...master, username: "teacher", role: "teacher", current_program: null }, initial_password: "teacher-initial-password",
  }));
  vi.stubGlobal("fetch", fetcher);
  render(<MemberManagementPanel containers={[]} />);
  await screen.findByText("Alice Zhang");
  fireEvent.click(screen.getByRole("button", { name: "邀请成员" }));
  fireEvent.change(screen.getByLabelText("角色"), { target: { value: "teacher" } });
  expect(screen.queryByLabelText("入学年份")).not.toBeInTheDocument();
  expect(screen.getByText(/教职工账户长期有效/)).toBeVisible();
  fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "teacher" } });
  fireEvent.change(screen.getByLabelText("姓名"), { target: { value: "Teacher" } });
  fireEvent.click(screen.getByRole("button", { name: "确认邀请" }));
  await screen.findByRole("dialog", { name: "成员登录信息" });
  expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({ username: "teacher", display_name: "Teacher", role: "teacher", member_kind: "staff" });
});

test("a teacher can be selected immediately after creation without reloading", async () => {
  const teacher = { ...master, id: "new-teacher", username: "new-teacher", display_name: "New Teacher",
    role: "teacher", member_kind: "staff", status: "invited", current_program: null };
  const fetcher = vi.fn()
    .mockResolvedValueOnce(Response.json([master]))
    .mockResolvedValueOnce(Response.json({ member: teacher, initial_password: "initial-staff-password" }, { status: 201 }))
    .mockResolvedValueOnce(Response.json({ member: { ...master, id: "new-student", username: "new-student", supervisor_member_id: teacher.id },
      initial_password: "initial-student-password" }, { status: 201 }));
  vi.stubGlobal("fetch", fetcher);
  render(<MemberManagementPanel containers={[]} />);
  await screen.findByText("Alice Zhang");
  fireEvent.click(screen.getByRole("button", { name: "邀请成员" }));
  fireEvent.change(screen.getByLabelText("角色"), { target: { value: "teacher" } });
  fireEvent.change(screen.getByLabelText("用户名"), { target: { value: teacher.username } });
  fireEvent.change(screen.getByLabelText("姓名"), { target: { value: teacher.display_name } });
  fireEvent.click(screen.getByRole("button", { name: "确认邀请" }));
  const credentials = await screen.findByRole("dialog", { name: "成员登录信息" });
  fireEvent.click(within(credentials).getByRole("button", { name: "完成" }));

  fireEvent.click(screen.getByRole("button", { name: "邀请成员" }));
  fireEvent.change(screen.getByLabelText("角色"), { target: { value: "user" } });
  const selector = screen.getByLabelText("导师（可选）");
  expect(within(selector).getByRole("option", { name: "New Teacher (@new-teacher) · 待首次登录" })).toBeEnabled();
  fireEvent.change(selector, { target: { value: teacher.username } });
  fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "new-student" } });
  fireEvent.change(screen.getByLabelText("姓名"), { target: { value: "New Student" } });
  fireEvent.click(screen.getByRole("button", { name: "确认邀请" }));
  await screen.findByRole("dialog", { name: "成员登录信息" });
  expect(JSON.parse(fetcher.mock.calls[2][1].body)).toMatchObject({ username: "new-student", supervisor_username: "new-teacher" });
});

test("both supervisor forms exclude unavailable teachers and allow invited teachers", async () => {
  const teachers = ["invited", "active", "disabled", "pending_exit", "archived"].map((status) => ({
    ...master, id: `teacher-${status}`, username: `teacher-${status}`, display_name: `Teacher ${status}`,
    member_kind: "staff", role: "teacher", status, current_program: null,
  }));
  const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => init?.method === "POST"
    ? Response.json({ ...master, supervisor_member_id: "teacher-invited", version: 4 })
    : Response.json([master, ...teachers]));
  vi.stubGlobal("fetch", fetcher);
  render(<MemberManagementPanel containers={[]} />);
  await screen.findByText("Alice Zhang");
  fireEvent.click(screen.getByRole("button", { name: "邀请成员" }));
  expect(within(screen.getByLabelText("导师（可选）")).getAllByRole("option")).toHaveLength(3);
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  openMemberAction("Alice Zhang", "分配导师");
  const selector = screen.getByLabelText("导师", { exact: true });
  expect(within(selector).getAllByRole("option")).toHaveLength(3);
  fireEvent.change(selector, { target: { value: "teacher-invited" } });
  fireEvent.change(screen.getByLabelText("分配原因"), { target: { value: "新生分组" } });
  fireEvent.click(screen.getByRole("button", { name: "保存导师" }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledWith("/api/gsm/admin/members/member-alice/supervisor",
    expect.objectContaining({ body: JSON.stringify({ expected_version: 3, supervisor_member_id: "teacher-invited", reason: "新生分组" }) })));
});

test("requires a reason for extension and exact doctoral promotion confirmation", async () => {
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    if (input.toString() === "/api/gsm/admin/members") return Response.json([master]);
    return Response.json({ ...master, version: 4 });
  });
  vi.stubGlobal("fetch", fetcher);
  render(<MemberManagementPanel containers={[]} />);
  await screen.findByText("Alice Zhang");

  openMemberAction("Alice Zhang", "延期");
  fireEvent.change(screen.getByLabelText("新的毕业日期"), { target: { value: "2030-07-01" } });
  expect(screen.getByRole("button", { name: "确认延期" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("延期原因"), { target: { value: "实验延期" } });
  fireEvent.click(screen.getByRole("button", { name: "确认延期" }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledWith(
    "/api/gsm/admin/members/member-alice/extend",
    expect.objectContaining({ method: "POST" }),
  ));

  openMemberAction("Alice Zhang", "硕转博");
  expect(screen.getByText("预计博士毕业：2032-07-01")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("转博确认短语"), { target: { value: "PROMOTE" } });
  expect(screen.getByRole("button", { name: "确认硕转博" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("转博原因"), { target: { value: "培养方案调整" } });
  fireEvent.change(screen.getByLabelText("转博确认短语"), {
    target: { value: "PROMOTE TO DOCTOR" },
  });
  expect(screen.getByRole("button", { name: "确认硕转博" })).toBeEnabled();
});


test("member invitation completes using the existing admin session without another verification", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(Response.json([master]))
    .mockResolvedValueOnce(Response.json({ member: { ...master, id: "new-member" }, initial_password: "new-initial-password" }, { status: 201 }));
  vi.stubGlobal("fetch", fetcher);
  render(<MemberManagementPanel containers={[]} />);
  await screen.findByText("Alice Zhang");
  fireEvent.click(screen.getByRole("button", { name: "邀请成员" }));
  fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "new-member" } });
  fireEvent.change(screen.getByLabelText("姓名"), { target: { value: "New Member" } });
  fireEvent.click(screen.getByRole("button", { name: "确认邀请" }));
  expect(await screen.findByText("new-initial-password")).toBeInTheDocument();
  expect(screen.queryByLabelText("动态验证码")).not.toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledTimes(2);
});


test("reloads the directory on an optimistic version conflict", async () => {
  const fetcher = vi.fn()
    .mockResolvedValueOnce(Response.json([master]))
    .mockResolvedValueOnce(Response.json({ detail: "stale member version" }, { status: 409 }))
    .mockResolvedValueOnce(Response.json([{ ...master, version: 4 }]));
  vi.stubGlobal("fetch", fetcher);
  render(<MemberManagementPanel containers={[]} />);
  await screen.findByText("Alice Zhang");

  openMemberAction("Alice Zhang", "延期");
  fireEvent.change(screen.getByLabelText("新的毕业日期"), { target: { value: "2030-07-01" } });
  fireEvent.change(screen.getByLabelText("延期原因"), { target: { value: "实验延期" } });
  fireEvent.click(screen.getByRole("button", { name: "确认延期" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("成员信息已发生变更或当前状态不支持此操作");
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(3));
});


test("requires exact confirmation for a versioned container handoff", async () => {
  const target = {
    ...master,
    id: "member-bob",
    username: "bob",
    display_name: "Bob Li",
    version: 2,
  };
  const container = {
    id: "container-alice",
    name: "alice-train",
    user: "alice",
    ownerMemberId: master.id,
    version: 5,
    server: "server1",
    cpu: 8,
    memoryGb: 32,
    gpus: ["0"],
    status: "running",
    statusText: "running",
    sshCommand: "ssh researcher@server1 -p 22001",
    mounts: [{ hostPath: "/data/alice", containerPath: "/workspace", mode: "rw" }],
  };
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    if (input.toString() === "/api/gsm/admin/members") return Response.json([master, target]);
    return Response.json([{ ...container, owner_member_id: target.id, version: 6 }]);
  });
  vi.stubGlobal("fetch", fetcher);
  render(<MemberManagementPanel containers={[container]} />);
  await screen.findByText("Alice Zhang");

  openMemberAction("Alice Zhang", "转移容器");
  expect(screen.getByText("/data/alice → /workspace (rw)")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("新容器所有者"), { target: { value: target.id } });
  fireEvent.change(screen.getByLabelText("转移原因"), { target: { value: "课题交接" } });
  fireEvent.change(screen.getByLabelText("数据移交确认短语"), {
    target: { value: "TRANSFER" },
  });
  expect(screen.getByRole("button", { name: "确认转移" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("数据移交确认短语"), {
    target: { value: "TRANSFER DATA OWNERSHIP" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认转移" }));

  await waitFor(() => expect(fetcher).toHaveBeenCalledWith(
    "/api/gsm/admin/members/member-alice/containers/transfer",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        expected_version: 3,
        target_member_id: "member-bob",
        container_versions: { "container-alice": 5 },
        confirm_data_handoff: true,
        reason: "课题交接",
      }),
    },
  ));
});


test("loads the directory using the existing administrator session", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(Response.json([master]));
  vi.stubGlobal("fetch", fetcher);
  render(<MemberManagementPanel containers={[]} />);

  expect(await screen.findByText("Alice Zhang")).toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher).toHaveBeenCalledWith("/api/gsm/admin/members");
  expect(screen.queryByRole("dialog", { name: "管理员重新认证" })).not.toBeInTheDocument();
});


test("requires exact confirmation for self demotion and self disable", async () => {
  const admin = { ...master, id: "member-admin", username: "admin", role: "admin" };
  vi.stubGlobal("fetch", vi.fn(async () => Response.json([admin])));
  render(<MemberManagementPanel containers={[]} currentMemberId="member-admin" />);
  await screen.findByText("Alice Zhang");

  openMemberAction("Alice Zhang", "调整角色");
  fireEvent.change(screen.getByLabelText("新角色"), { target: { value: "user" } });
  fireEvent.change(screen.getByLabelText("变更原因"), { target: { value: "交接管理员" } });
  fireEvent.change(screen.getByLabelText("角色变更确认短语（需要时）"), {
    target: { value: "DEMOTE" },
  });
  expect(screen.getByRole("button", { name: "确认角色变更" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("角色变更确认短语（需要时）"), {
    target: { value: "DEMOTE MY ADMIN ACCOUNT" },
  });
  expect(screen.getByRole("button", { name: "确认角色变更" })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "取消" }));

  openMemberAction("Alice Zhang", "停用");
  fireEvent.change(screen.getByLabelText("停用原因"), { target: { value: "离组交接" } });
  expect(screen.getByRole("button", { name: "确认停用" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("确认短语"), {
    target: { value: "DISABLE MY ACCOUNT" },
  });
  expect(screen.getByRole("button", { name: "确认停用" })).toBeEnabled();
});


const teacherActor = { userId: "teacher", memberId: "member-teacher", role: "teacher" as const, status: "active" };
const teacherStudent = { ...master, member_kind: "student", supervisor_member_id: "member-teacher" };
const teacherDirectory = [
  { id: "member-teacher", username: "teacher", display_name: "Professor Li", status: "active" },
  { id: "other-teacher", username: "other", display_name: "Professor Wang", status: "invited" },
];

test("teacher student management hides containers and privileged role choices", async () => {
  const fetched = vi.fn(async (url: RequestInfo | URL) => Response.json(url.toString().endsWith("supervisors") ? teacherDirectory : [teacherStudent]));
  vi.stubGlobal("fetch", fetched);
  render(<MemberManagementPanel containers={mockDashboardData.containers} currentMemberId="member-teacher" actor={teacherActor} />);
  await screen.findByText("Alice Zhang");
  expect(screen.getByRole("heading", { name: "我的学生" })).toBeVisible();
  expect(screen.queryByRole("columnheader", { name: "容器" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /管理.*容器/ })).not.toBeInTheDocument();
  expect(screen.queryByLabelText("导入 CSV")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "打开 Alice Zhang" }));
  fireEvent.click(screen.getByRole("button", { name: "Alice Zhang 的更多操作" }));
  expect(screen.queryByRole("button", { name: /容器|解绑微信|撤销会话/ })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "调整角色 Alice Zhang" }));
  expect(within(screen.getByLabelText("新角色")).getAllByRole("option").map(o => o.getAttribute("value"))).toEqual(["viewer", "user", "power_user"]);
  expect(fetched.mock.calls.every(([url]) => url.toString().startsWith("/api/gsm/teacher/"))).toBe(true);
});

test("teacher creates a student with own supervision and one-time password", async () => {
  const fetched = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") return Response.json({ member: { ...teacherStudent, id: "new-student", username: "new", display_name: "New Student" }, initial_password: "generated-initial-password" });
    return Response.json(url.toString().endsWith("supervisors") ? teacherDirectory : [teacherStudent]);
  });
  vi.stubGlobal("fetch", fetched);
  render(<MemberManagementPanel containers={[]} currentMemberId="member-teacher" actor={teacherActor} />);
  await screen.findByText("Alice Zhang");
  fireEvent.click(screen.getByRole("button", { name: "新增学生" }));
  expect(screen.getByLabelText("导师（可选）")).toHaveValue("teacher");
  expect(within(screen.getByLabelText("角色")).getAllByRole("option")).toHaveLength(3);
  fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "new" } });
  fireEvent.change(screen.getByLabelText("姓名"), { target: { value: "New Student" } });
  fireEvent.click(screen.getByRole("button", { name: "确认邀请" }));
  expect(await screen.findByText("generated-initial-password")).toBeVisible();
  const posted = fetched.mock.calls.find(([, init]) => init?.method === "POST")!;
  expect(posted[0]).toBe("/api/gsm/teacher/students");
  expect(JSON.parse(posted[1]!.body as string)).toMatchObject({ role: "user", member_kind: "student", supervisor_username: "teacher" });
});

test("teacher reassignment removes the student from the local directory immediately", async () => {
  const fetched = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") return Response.json({ ...teacherStudent, supervisor_member_id: "other-teacher", version: 4 });
    return Response.json(url.toString().endsWith("supervisors") ? teacherDirectory : [teacherStudent]);
  });
  vi.stubGlobal("fetch", fetched);
  render(<MemberManagementPanel containers={[]} currentMemberId="member-teacher" actor={teacherActor} />);
  await screen.findByText("Alice Zhang");
  openMemberAction("Alice Zhang", "分配导师");
  fireEvent.change(screen.getByLabelText("导师"), { target: { value: "other-teacher" } });
  fireEvent.change(screen.getByLabelText("分配原因"), { target: { value: "research group transfer" } });
  fireEvent.click(screen.getByRole("button", { name: "保存导师" }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "打开 Alice Zhang" })).not.toBeInTheDocument());
  expect(fetched).toHaveBeenCalledWith("/api/gsm/teacher/students/member-alice/supervisor", expect.objectContaining({ method: "POST" }));
});

test.each([
  ["重置密码", "重置原因", "生成初始密码", "password-reset"],
  ["停用", "停用原因", "确认停用", "disable"],
  ["调整角色", "变更原因", "确认角色变更", "role"],
])("teacher %s uses scoped account API", async (action, reason, submit, suffix) => {
  const fetched = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") return Response.json(suffix === "password-reset" ? { member: teacherStudent, initial_password: "new-initial-password" } : teacherStudent);
    return Response.json(url.toString().endsWith("supervisors") ? teacherDirectory : [teacherStudent]);
  });
  vi.stubGlobal("fetch", fetched);
  render(<MemberManagementPanel containers={[]} currentMemberId="member-teacher" actor={teacherActor} />);
  await screen.findByText("Alice Zhang");
  openMemberAction("Alice Zhang", action);
  fireEvent.change(screen.getByLabelText(reason), { target: { value: "account maintenance" } });
  fireEvent.click(screen.getByRole("button", { name: submit }));
  await waitFor(() => expect(fetched).toHaveBeenCalledWith(`/api/gsm/teacher/students/member-alice/${suffix}`, expect.objectContaining({ method: "POST" })));
});
