import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, test, vi } from "vitest";

import { ActivityPanel } from "../components/ActivityPanel";
import { RoleOverview } from "../components/RoleOverview";
import { TeacherWorkspace } from "../components/TeacherWorkspace";
import type {
  ActivityEntry,
  TeacherStudent,
  WorkspaceOverview,
} from "../lib/api";


afterEach(() => {
  vi.unstubAllGlobals();
});

const adminOverview: WorkspaceOverview = {
  persona: "administrator",
  generated_at: "2026-08-10T08:00:00+08:00",
  tasks: {
    unassigned_students: {
      count: 2,
      items: [],
    },
    pending_approvals: {
      count: 1,
      items: [],
    },
  },
};

const assignedStudent: TeacherStudent = {
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
};

test("administrator overview prioritizes actionable exceptions", () => {
  render(
    <RoleOverview
      actor={{ userId: "admin", role: "admin", status: "active" }}
      overview={adminOverview}
      onNavigate={vi.fn()}
    />,
  );

  expect(screen.getByRole("heading", { name: "需要关注" })).toBeInTheDocument();
  expect(screen.getByText("2 名未分配导师学生")).toBeInTheDocument();
  expect(screen.getByText("1 项待审批申请")).toBeInTheDocument();
});

test("teacher workspace exposes academic actions without security commands", () => {
  render(
    <TeacherWorkspace
      initialStudents={[assignedStudent]}
      initialApprovals={[]}
    />,
  );

  expect(screen.getByText("Alice")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "延期 Alice" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "更正 Alice 的学籍" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /重置密码/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /禁用/ })).not.toBeInTheDocument();
});

test("teacher academic dialog closes with Escape and restores trigger focus", async () => {
  render(
    <TeacherWorkspace
      initialStudents={[assignedStudent]}
      initialApprovals={[]}
    />,
  );
  const trigger = screen.getByRole("button", { name: "延期 Alice" });

  trigger.focus();
  fireEvent.click(trigger);
  expect(screen.getByRole("dialog", { name: "延期 Alice" })).toBeInTheDocument();
  fireEvent.keyDown(document, { key: "Escape" });

  await waitFor(() => expect(screen.queryByRole("dialog", { name: "延期 Alice" })).not.toBeInTheDocument());
  expect(trigger).toHaveFocus();
});

test("teacher student and approval views do not duplicate each other", () => {
  const { rerender } = render(
    <TeacherWorkspace
      view="students"
      initialStudents={[assignedStudent]}
      initialApprovals={[]}
    />,
  );

  expect(screen.getByRole("heading", { name: "我的学生" })).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "学生申请" })).not.toBeInTheDocument();

  rerender(
    <TeacherWorkspace
      view="approvals"
      initialStudents={[assignedStudent]}
      initialApprovals={[]}
    />,
  );
  expect(screen.getByRole("heading", { name: "学生申请" })).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "我的学生" })).not.toBeInTheDocument();
});

test("teacher extends an assigned student through the fixed teacher route", async () => {
  const fetcher = vi.fn(async () =>
    Response.json({
      ...assignedStudent,
      version: 3,
      current_program: {
        ...assignedStudent.current_program,
        expected_graduation_date: "2030-07-01",
      },
    }),
  );
  vi.stubGlobal("fetch", fetcher);
  render(
    <TeacherWorkspace
      initialStudents={[assignedStudent]}
      initialApprovals={[]}
    />,
  );

  fireEvent.click(screen.getByRole("button", { name: "延期 Alice" }));
  fireEvent.change(screen.getByLabelText("新的预计毕业日期"), {
    target: { value: "2030-07-01" },
  });
  fireEvent.change(screen.getByLabelText("延期原因"), {
    target: { value: "课题周期调整" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认延期" }));

  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
  expect(fetcher).toHaveBeenCalledWith(
    "/api/gsm/teacher/students/member-alice/extend",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        expected_version: 2,
        expected_graduation_date: "2030-07-01",
        reason: "课题周期调整",
      }),
    },
  );
  expect(await screen.findByText("学籍信息已更新。")).toBeInTheDocument();
});

test("activity renders human-readable channel and action attribution", () => {
  const entries: ActivityEntry[] = [
    {
      id: "audit-1",
      actor_member_id: "member-alice",
      actor_channel: "wechat",
      action_type: "stop_container",
      target_type: "container",
      target_id: "container-job",
      result: "executed",
      created_at: "2026-08-10T08:00:00+08:00",
    },
  ];

  render(<ActivityPanel initialEntries={entries} />);

  expect(screen.getByText("微信 Agent")).toBeInTheDocument();
  expect(screen.getByText("停止容器")).toBeInTheDocument();
  expect(screen.getByText("container-job")).toBeInTheDocument();
});
