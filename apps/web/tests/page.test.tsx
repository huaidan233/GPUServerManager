import { fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  cookie: "gsm_session=session; gsm_csrf=csrf",
  loadDashboardData: vi.fn(),
  loadMemberSession: vi.fn(),
  loadTeacherStudents: vi.fn(),
  loadWorkspaceOverview: vi.fn(),
  refresh: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: mocks.refresh }),
}));

vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({ toString: () => mocks.cookie })),
  headers: vi.fn(async () => new Headers({ host: "gsm.test", "x-forwarded-proto": "https" })),
}));

vi.mock("../lib/api", () => ({
  loadDashboardData: mocks.loadDashboardData,
  loadMemberSession: mocks.loadMemberSession,
  loadTeacherStudents: mocks.loadTeacherStudents,
  loadWorkspaceOverview: mocks.loadWorkspaceOverview,
}));

vi.mock("../components/Dashboard", () => ({
  Dashboard: ({
    data,
    initialWorkspace,
    overview,
    teacherStudents,
  }: {
    data: { actor: { userId: string } };
    initialWorkspace: string;
    overview?: { persona: string };
    teacherStudents?: unknown[];
  }) => (
    <>
      <div>dashboard:{data.actor.userId}:{initialWorkspace}</div>
      <div>overview:{overview?.persona ?? "none"}</div>
      <div>students:{teacherStudents?.length ?? 0}</div>
    </>
  ),
}));

vi.mock("../components/LoginScreen", () => ({
  LoginScreen: () => <div>login-screen</div>,
}));

import Page from "../app/page";

beforeEach(() => {
  mocks.loadDashboardData.mockReset();
  mocks.loadMemberSession.mockReset();
  mocks.loadTeacherStudents.mockReset();
  mocks.loadWorkspaceOverview.mockReset();
  mocks.refresh.mockReset();
  mocks.loadTeacherStudents.mockResolvedValue([]);
  mocks.loadWorkspaceOverview.mockResolvedValue({ persona: "user" });
});

afterEach(() => {
  vi.unstubAllEnvs();
});

test("renders login without loading dashboard data for a 401 session", async () => {
  mocks.loadMemberSession.mockResolvedValue(null);

  render(await Page({}));

  expect(screen.getByText("login-screen")).toBeInTheDocument();
  expect(mocks.loadMemberSession).toHaveBeenCalledWith({
    cookie: "gsm_session=session; gsm_csrf=csrf",
  });
  expect(mocks.loadDashboardData).not.toHaveBeenCalled();
  expect(mocks.loadWorkspaceOverview).not.toHaveBeenCalled();
});

test("loads authenticated dashboard data with the same incoming cookie", async () => {
  mocks.loadMemberSession.mockResolvedValue({
    member: { username: "alice", role: "user" },
  });
  mocks.loadDashboardData.mockResolvedValue({ actor: { userId: "alice" } });

  render(await Page({}));

  expect(screen.getByText("dashboard:alice:overview")).toBeInTheDocument();
  expect(mocks.loadDashboardData).toHaveBeenCalledWith({
    cookie: "gsm_session=session; gsm_csrf=csrf",
    origin: "https://gsm.test",
  });
  expect(mocks.loadWorkspaceOverview).toHaveBeenCalledWith({
    cookie: "gsm_session=session; gsm_csrf=csrf",
  });
  expect(mocks.loadTeacherStudents).not.toHaveBeenCalled();
  expect(screen.getByText("overview:user")).toBeInTheDocument();
});

test("renders a bounded service error and no mock administrator in production", async () => {
  vi.stubEnv("NODE_ENV", "production");
  mocks.loadMemberSession.mockResolvedValue({
    member: { username: "alice", role: "user" },
  });
  mocks.loadDashboardData.mockRejectedValue(new Error("database password and host details"));

  render(await Page({}));

  expect(screen.getByRole("alert")).toHaveTextContent("服务暂时不可用，请稍后重试。");
  expect(screen.queryByText(/database password|mock|admin/i)).not.toBeInTheDocument();
  expect(screen.getByRole("alert").textContent?.length).toBeLessThanOrEqual(32);
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  expect(mocks.refresh).toHaveBeenCalledTimes(1);
});

test("authorizes requested workspace against the live session role", async () => {
  mocks.loadMemberSession.mockResolvedValue({
    member: { username: "teacher", role: "teacher" },
  });
  mocks.loadDashboardData.mockResolvedValue({ actor: { userId: "teacher" } });
  mocks.loadWorkspaceOverview.mockResolvedValue({ persona: "teacher" });
  mocks.loadTeacherStudents.mockResolvedValue([{ id: "member-alice" }]);

  render(
    await Page({
      searchParams: Promise.resolve({ workspace: "students" }),
    }),
  );
  expect(screen.getByText("dashboard:teacher:students")).toBeInTheDocument();
  expect(screen.getByText("overview:teacher")).toBeInTheDocument();
  expect(screen.getByText("students:1")).toBeInTheDocument();
  expect(mocks.loadTeacherStudents).toHaveBeenCalledWith({
    cookie: "gsm_session=session; gsm_csrf=csrf",
  });

  render(
    await Page({
      searchParams: Promise.resolve({ workspace: "system" }),
    }),
  );
  expect(screen.getByText("dashboard:teacher:overview")).toBeInTheDocument();
});

test("pending-exit administrator cannot select an administrator workspace", async () => {
  mocks.loadMemberSession.mockResolvedValue({
    member: {
      username: "leaving-admin",
      role: "admin",
      status: "pending_exit",
    },
  });
  mocks.loadDashboardData.mockResolvedValue({
    actor: { userId: "leaving-admin" },
  });

  render(
    await Page({
      searchParams: Promise.resolve({ workspace: "system" }),
    }),
  );

  expect(screen.getByText("dashboard:leaving-admin:overview")).toBeInTheDocument();
});
