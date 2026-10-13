"use client";

import {
  Archive,
  ArrowRightLeft,
  CalendarClock,
  GraduationCap,
  KeyRound,
  LogOut,
  MoreHorizontal,
  Pencil,
  ShieldCheck,
  Unlink,
  UserCheck,
  UserX,
} from "lucide-react";
import { type ReactNode, type RefObject, useState } from "react";

import { AccessibleDialog } from "./AccessibleDialog";


type ProgramDetail = {
  id: string;
  level: "master" | "doctor";
  enrollment_date: string;
  expected_graduation_date: string;
  actual_end_date?: string | null;
};

export type MemberDetail = {
  id: string;
  username: string;
  display_name: string;
  role: string;
  status: string;
  version: number;
  member_kind?: "student" | "staff";
  supervisor_member_id?: string | null;
  current_program: ProgramDetail | null;
  program_history: ProgramDetail[];
  wechat_binding: { route_key?: string; status?: string } | null;
};

export type MemberActionKind =
  | "supervisor"
  | "profile-correction"
  | "role"
  | "extend"
  | "promote-to-doctor"
  | "password-reset"
  | "revoke-sessions"
  | "wechat-pairing-code"
  | "wechat-unbind"
  | "disable"
  | "reactivate"
  | "archive"
  | "transfer";

type MemberDetailDrawerProps = {
  member: MemberDetail;
  containerCount: number;
  supervisorName?: string;
  triggerRef: RefObject<HTMLElement | null>;
  onAction: (kind: MemberActionKind) => void;
  onManageContainers: () => void;
  onClose: () => void;
};

export function MemberDetailDrawer({
  member,
  containerCount,
  supervisorName,
  triggerRef,
  onAction,
  onManageContainers,
  onClose,
}: MemberDetailDrawerProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const program = member.current_program;

  function action(kind: MemberActionKind) {
    setMenuOpen(false);
    onAction(kind);
  }

  return (
    <AccessibleDialog
      title={`${member.display_name} 详情`}
      triggerRef={triggerRef}
      onClose={onClose}
      className="member-drawer"
    >
      <dl className="member-detail-list">
        <div><dt>用户名</dt><dd>@{member.username}</dd></div>
        <div><dt>角色</dt><dd>{roleLabel(member.role)}</dd></div>
        {program && <div><dt>导师</dt><dd>{supervisorName || "未分配"}</dd></div>}
        <div><dt>状态</dt><dd>{statusLabel(member.status)}</dd></div>
        <div><dt>培养层次</dt><dd>{program ? levelLabel(program.level) : "-"}</dd></div>
        <div><dt>入学日期</dt><dd>{program?.enrollment_date ?? "-"}</dd></div>
        <div><dt>预计毕业</dt><dd>{program?.expected_graduation_date ?? "长期有效"}</dd></div>
        <div><dt>微信</dt><dd>{member.wechat_binding ? "已绑定" : "未绑定"}</dd></div>
        <div><dt>容器</dt><dd>{containerCount} 个容器 <button className="text-button" type="button" onClick={onManageContainers}>管理容器</button></dd></div>
      </dl>

      <div className="member-action-menu-wrap">
        <button
          className="secondary-button"
          type="button"
          aria-label={`${member.display_name} 的更多操作`}
          aria-expanded={menuOpen}
          aria-controls={`member-actions-${member.id}`}
          onClick={() => setMenuOpen((current) => !current)}
        >
          <MoreHorizontal size={16} aria-hidden="true" />
          更多操作
        </button>
        {menuOpen && (
          <div
            id={`member-actions-${member.id}`}
            className="member-action-menu"
            aria-label={`${member.display_name} 操作`}
          >
            <MenuAction label={`修正资料 ${member.display_name}`} icon={<Pencil size={15} />} onClick={() => action("profile-correction")} />
            <MenuAction label={`调整角色 ${member.display_name}`} icon={<ShieldCheck size={15} />} onClick={() => action("role")} />
            {program && member.status !== "archived" && <MenuAction label={`分配导师 ${member.display_name}`} icon={<GraduationCap size={15} />} onClick={() => action("supervisor")} />}
            {program && member.status !== "archived" && <MenuAction label={`延期 ${member.display_name}`} icon={<CalendarClock size={15} />} onClick={() => action("extend")} />}
            {program?.level === "master" && member.status !== "archived" && <MenuAction label={`硕转博 ${member.display_name}`} icon={<GraduationCap size={15} />} onClick={() => action("promote-to-doctor")} />}
            {member.status !== "archived" && <MenuAction label={`重置密码 ${member.display_name}`} icon={<KeyRound size={15} />} onClick={() => action("password-reset")} />}
            {member.status !== "archived" && <MenuAction label={`撤销会话 ${member.display_name}`} icon={<LogOut size={15} />} onClick={() => action("revoke-sessions")} />}
            {member.wechat_binding && <MenuAction label={`解绑微信 ${member.display_name}`} icon={<Unlink size={15} />} onClick={() => action("wechat-unbind")} danger />}
            {["invited", "active", "pending_exit"].includes(member.status) && <MenuAction label={`停用 ${member.display_name}`} icon={<UserX size={15} />} onClick={() => action("disable")} danger />}
            {member.status === "disabled" && <MenuAction label={`重新启用 ${member.display_name}`} icon={<UserCheck size={15} />} onClick={() => action("reactivate")} />}
            {member.status === "disabled" && <MenuAction label={`归档 ${member.display_name}`} icon={<Archive size={15} />} onClick={() => action("archive")} danger />}
            <MenuAction label={`转移容器 ${member.display_name}`} icon={<ArrowRightLeft size={15} />} onClick={() => action("transfer")} disabled={containerCount === 0 || member.status === "archived"} />
          </div>
        )}
      </div>
    </AccessibleDialog>
  );
}

function MenuAction({
  label,
  icon,
  onClick,
  disabled = false,
  danger = false,
}: {
  label: string;
  icon: ReactNode;
  onClick: () => void;
  disabled?: boolean;
  danger?: boolean;
}) {
  return (
    <button
      className={danger ? "danger-action" : ""}
      type="button"
      disabled={disabled}
      onClick={onClick}
    >
      {icon}
      <span>{label}</span>
    </button>
  );
}

function levelLabel(level: ProgramDetail["level"]): string {
  return level === "doctor" ? "博士" : "硕士";
}

function roleLabel(role: string): string {
  return ({ viewer: "只读", user: "成员", power_user: "高级成员", teacher: "老师", admin: "管理员" } as Record<string, string>)[role] ?? role;
}

function statusLabel(status: string): string {
  return ({ invited: "待激活", active: "在组", pending_exit: "待离组", disabled: "已停用", archived: "已归档" } as Record<string, string>)[status] ?? status;
}
