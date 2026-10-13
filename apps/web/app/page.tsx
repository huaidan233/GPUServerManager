import { cookies, headers } from "next/headers";

import { Dashboard } from "../components/Dashboard";
import { DashboardModuleStatus } from "../components/DashboardModuleStatus";
import { FirstRunGuide } from "../components/FirstRunGuide";
import { LoginScreen } from "../components/LoginScreen";
import { FirstLoginScreen } from "../components/FirstLoginScreen";
import { ServiceError } from "../components/ServiceError";
import {
  loadDashboardData,
  loadMemberSession,
  loadTeacherStudents,
  loadWorkspaceOverview,
} from "../lib/api";
import {
  effectiveWorkspaceRole,
  resolveWorkspace,
} from "../lib/workspaces";

type PageProps = {
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
};

export default async function Page({ searchParams }: PageProps) {
  const cookie = (await cookies()).toString();
  let session;
  try {
    session = await loadMemberSession({ cookie });
  } catch {
    return <ServiceError />;
  }

  if (!session) {
    return <LoginScreen />;
  }
  if (session.requires_password_change) {
    return <FirstLoginScreen username={session.member.username} isAdmin={session.member.role === "admin"} />;
  }

  try {
    const incomingHeaders = await headers();
    const workspaceRole = effectiveWorkspaceRole(
      session.member.role,
      session.member.status,
    );
    const [dataResult, overviewResult, teacherStudentsResult] = await Promise.allSettled([
      loadDashboardData({
        cookie,
        origin: requestOrigin(incomingHeaders),
      }),
      loadWorkspaceOverview({ cookie }),
      workspaceRole === "teacher"
        ? loadTeacherStudents({ cookie })
        : Promise.resolve([]),
    ]);
    if (dataResult.status === "rejected") {
      throw dataResult.reason;
    }
    const data = dataResult.value;
    const moduleErrors = [...(data.moduleErrors ?? [])];
    const overview = overviewResult.status === "fulfilled" ? overviewResult.value : undefined;
    const teacherStudents = teacherStudentsResult.status === "fulfilled" ? teacherStudentsResult.value : [];
    if (overviewResult.status === "rejected") {
      moduleErrors.push({ module: "工作区概览", message: "暂时无法加载" });
    }
    if (teacherStudentsResult.status === "rejected") {
      moduleErrors.push({ module: "学生列表", message: "暂时无法加载" });
    }
    const params = searchParams ? await searchParams : {};
    const requestedWorkspace = Array.isArray(params.workspace)
      ? params.workspace[0]
      : params.workspace;
    const initialWorkspace = resolveWorkspace(
      workspaceRole,
      requestedWorkspace,
    );

    return (
      <>
      <FirstRunGuide isAdmin={session.member.role === "admin"} serverCount={data.servers?.length ?? 1} />
      <Dashboard
        data={{ ...data, moduleErrors }}
        initialWorkspace={initialWorkspace}
        overview={overview}
        teacherStudents={teacherStudents}
      />
      <DashboardModuleStatus errors={moduleErrors} />
      </>
    );
  } catch {
    return <ServiceError />;
  }
}

function requestOrigin(requestHeaders: Headers): string | undefined {
  const explicitOrigin = requestHeaders.get("origin");
  if (explicitOrigin) {
    return explicitOrigin;
  }
  const host = requestHeaders.get("x-forwarded-host") ?? requestHeaders.get("host");
  if (!host) {
    return undefined;
  }
  const protocol = requestHeaders.get("x-forwarded-proto")?.split(",", 1)[0].trim()
    ?? (process.env.NODE_ENV === "production" ? "https" : "http");
  return `${protocol}://${host}`;
}
