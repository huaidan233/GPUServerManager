import type { ActorRole } from "./mockData";

export type WorkspaceId =
  | "overview"
  | "resources"
  | "resource-overview"
  | "data"
  | "print"
  | "members"
  | "students"
  | "approvals"
  | "queue"
  | "usage"
  | "activity"
  | "audit"
  | "system"
  | "account";

export type WorkspaceIcon =
  | "overview"
  | "resources"
  | "print"
  | "members"
  | "students"
  | "approvals"
  | "queue"
  | "usage"
  | "activity"
  | "system"
  | "account";

export type WorkspaceItem = {
  id: WorkspaceId;
  label: string;
  icon: WorkspaceIcon;
};

export type WorkspaceGroup = {
  id: "management" | "research" | "teaching" | "personal";
  label: string;
  description: string;
  items: WorkspaceItem[];
};

const administratorWorkspaces: WorkspaceItem[] = [
  { id: "members", label: "成员管理", icon: "members" },
  { id: "approvals", label: "审批", icon: "approvals" },
  { id: "audit", label: "审计与告警", icon: "activity" },
  { id: "system", label: "系统", icon: "system" },
];

const teacherWorkspaces: WorkspaceItem[] = [
  { id: "overview", label: "概览", icon: "overview" },
  { id: "resource-overview", label: "资源概览", icon: "usage" },
  { id: "resources", label: "容器列表", icon: "resources" },
  { id: "data", label: "我的数据", icon: "resources" },
  { id: "print", label: "打印", icon: "print" },
  { id: "students", label: "学生", icon: "students" },
  { id: "approvals", label: "审批", icon: "approvals" },
  { id: "usage", label: "使用情况", icon: "usage" },
  { id: "queue", label: "GPU 空闲提醒", icon: "queue" },
];

const memberWorkspaces: WorkspaceItem[] = [
  { id: "overview", label: "概览", icon: "overview" },
  { id: "resource-overview", label: "资源概览", icon: "usage" },
  { id: "resources", label: "容器列表", icon: "resources" },
  { id: "data", label: "我的数据", icon: "resources" },
  { id: "print", label: "打印", icon: "print" },
  { id: "queue", label: "GPU 空闲提醒", icon: "queue" },
  { id: "approvals", label: "额度申请", icon: "approvals" },
  { id: "activity", label: "活动", icon: "activity" },
];

const accountWorkspace: WorkspaceItem = {
  id: "account", label: "我的账户", icon: "account",
};

export function workspaceGroups(role: ActorRole): WorkspaceGroup[] {
  if (role === "admin") {
    return [
      {
        id: "research",
        label: "用户功能",
        description: "资源查看与容器创建",
        items: [
          { id: "overview", label: "概览", icon: "overview" },
          { id: "resource-overview", label: "资源概览", icon: "usage" },
          { id: "resources", label: "容器列表", icon: "resources" },
          { id: "data", label: "我的数据", icon: "resources" },
          { id: "queue", label: "GPU 空闲提醒", icon: "queue" },
        ],
      },
      {
        id: "management",
        label: "管理员功能",
        description: "全组成员、审批与系统管理",
        items: administratorWorkspaces,
      },
      {
        id: "personal",
        label: "个人功能",
        description: "日常打印与个人账户",
        items: [{ id: "print", label: "打印", icon: "print" }, accountWorkspace],
      },
    ];
  }
  const primaryGroup: WorkspaceGroup = role === "teacher"
    ? {
        id: "teaching",
        label: "导师功能",
        description: "学生培养、申请与资源使用",
        items: teacherWorkspaces,
      }
    : {
        id: "research",
        label: "用户功能",
        description: "个人资源与研究任务",
        items: role === "viewer"
          ? memberWorkspaces.filter((item) => item.id !== "print")
          : memberWorkspaces,
      };
  return [
    primaryGroup,
    {
      id: "personal",
      label: "个人设置",
      description: "身份信息与账户安全",
      items: [accountWorkspace],
    },
  ];
}

export function workspaceItems(role: ActorRole): WorkspaceItem[] {
  return workspaceGroups(role).flatMap((group) => group.items);
}

export function effectiveWorkspaceRole(
  role: ActorRole,
  status: string | null | undefined,
): ActorRole {
  return status && status !== "active" ? "viewer" : role;
}

export function resolveWorkspace(
  role: ActorRole,
  requested: string | null | undefined,
): WorkspaceId {
  const allowed = workspaceItems(role);
  return allowed.some((item) => item.id === requested)
    ? (requested as WorkspaceId)
    : "overview";
}
