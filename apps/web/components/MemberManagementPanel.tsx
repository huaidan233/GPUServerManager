"use client";

import {
  CalendarClock,
  ChevronRight,
  ShieldCheck,
  Upload,
  UserPlus,
} from "lucide-react";
import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import Papa from "papaparse";

import { AccessibleDialog } from "./AccessibleDialog";
import { ContainerOperationsTable } from "./ContainerOperationsTable";
import {
  MemberDetailDrawer,
  type MemberActionKind,
  type MemberDetail,
} from "./MemberDetailDrawer";
import { authenticatedFetch } from "../lib/authenticatedFetch";
import type { ActorSummary, ContainerSummary } from "../lib/mockData";
import { ownsContainer } from "../lib/containerOwnership";

type ProgramLevel = "master" | "doctor";

type Member = MemberDetail;
type Supervisor = Pick<Member, "id" | "username" | "display_name" | "status">;

type DialogKind =
  | "supervisor"
  | "invite"
  | "credentials"
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
  | "transfer"
  | "batch-preview";

type DialogState = { kind: DialogKind; memberId?: string };

type CommandKind = "member" | "invite" | "batch" | "code" | "transfer" | "list";

type PendingCommand = {
  url: string;
  init: RequestInit;
  kind: CommandKind;
  memberId?: string;
};

type OneTimeResult = {
  title: string;
  entries: Array<{ label: string; value: string }>;
};

type CsvInvitation = {
  username: string;
  display_name: string;
  member_kind: "student" | "staff";
  role: "viewer" | "user" | "power_user" | "teacher" | "admin";
  program_level?: ProgramLevel;
  enrollment_year?: number;
  supervisor_username?: string;
};

type MemberManagementPanelProps = {
  containers: ContainerSummary[];
  currentMemberId?: string;
  actor?: ActorSummary;
  onContainersUpdated?: () => void;
};

const TRANSFER_CONFIRMATION = "TRANSFER DATA OWNERSHIP";
const PROMOTION_CONFIRMATION = "PROMOTE TO DOCTOR";

export function MemberManagementPanel({ containers, currentMemberId, actor, onContainersUpdated }: MemberManagementPanelProps) {
  const teacherMode = actor?.role === "teacher";
  const apiBase = teacherMode ? "/api/gsm/teacher/students" : "/api/gsm/admin/members";
  const [supervisorDirectory, setSupervisorDirectory] = useState<Supervisor[]>([]);
  const [members, setMembers] = useState<Member[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [dialog, setDialog] = useState<DialogState | null>(null);
  const [oneTimeResult, setOneTimeResult] = useState<OneTimeResult | null>(null);
  const [csvPreview, setCsvPreview] = useState<CsvInvitation[]>([]);
  const [detailMemberId, setDetailMemberId] = useState<string | null>(null);
  const [containerMemberId, setContainerMemberId] = useState<string | null>(null);
  const detailTriggerRef = useRef<HTMLButtonElement>(null);

  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [roleFilter, setRoleFilter] = useState("");
  const [levelFilter, setLevelFilter] = useState("");
  const [yearFilter, setYearFilter] = useState("");
  const [supervisorFilter, setSupervisorFilter] = useState("");
  const [selectedSupervisor, setSelectedSupervisor] = useState("");
  const [inviteSupervisor, setInviteSupervisor] = useState("");
  const supervisors = teacherMode ? supervisorDirectory : members.filter((member) => ["invited", "active"].includes(member.status) && member.member_kind === "staff" && ["teacher", "admin"].includes(member.role));

  const currentYear = new Date().getFullYear();
  const [inviteUsername, setInviteUsername] = useState("");
  const [inviteName, setInviteName] = useState("");
  const [inviteRole, setInviteRole] = useState("user");
  const [invitePassword, setInvitePassword] = useState("");
  const [inviteLevel, setInviteLevel] = useState<ProgramLevel>("master");
  const [inviteYear, setInviteYear] = useState(currentYear);

  const [reason, setReason] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [extensionDate, setExtensionDate] = useState("");
  const [promotionYear, setPromotionYear] = useState(currentYear);
  const [promotionEffectiveDate, setPromotionEffectiveDate] = useState(`${currentYear}-09-01`);
  const [profileName, setProfileName] = useState("");
  const [profileLevel, setProfileLevel] = useState<ProgramLevel>("master");
  const [profileEnrollmentDate, setProfileEnrollmentDate] = useState("");
  const [profileGraduationDate, setProfileGraduationDate] = useState("");
  const [selectedRole, setSelectedRole] = useState("user");

  const [targetMemberId, setTargetMemberId] = useState("");
  const [selectedContainerIds, setSelectedContainerIds] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);

  const loadMembers = useCallback(async () => {
    const response = await fetch(apiBase);
    const payload = await safeJson(response);
    if (!response.ok || !Array.isArray(payload)) throw new Error("成员目录暂时无法加载。");
    setMembers(payload.filter(isMember));
    if (teacherMode) {
      const directoryResponse = await fetch("/api/gsm/teacher/supervisors");
      const directory = await safeJson(directoryResponse);
      if (!directoryResponse.ok || !Array.isArray(directory)) throw new Error("导师目录暂时无法加载。");
      setSupervisorDirectory(directory);
    }
  }, [apiBase, teacherMode]);

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        await loadMembers();
      } catch (caught) {
        if (active) setError(caught instanceof Error ? caught.message : "成员目录暂时无法加载。");
      } finally {
        if (active) setLoading(false);
      }
    }
    void load();
    return () => {
      active = false;
    };
  }, [loadMembers]);

  const selectedMember = dialog?.memberId
    ? members.find((member) => member.id === dialog.memberId) ?? null
    : null;
  const detailMember = detailMemberId
    ? members.find((member) => member.id === detailMemberId) ?? null
    : null;
  const containerMember = members.find(member => member.id === containerMemberId);
  const memberContainers = useMemo(() => containerMember
    ? containers.filter(container => ownsContainer(container, { memberId: containerMember.id, userId: containerMember.username }))
      .sort((first, second) => Number(second.status === "running") - Number(first.status === "running"))
    : [], [containers, containerMember]);

  const enrollmentYears = useMemo(() => Array.from(new Set(
    members
      .map((member) => member.current_program?.enrollment_date.slice(0, 4))
      .filter((year): year is string => Boolean(year)),
  )).sort(), [members]);

  const filteredMembers = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase();
    return members.filter((member) => {
      const program = member.current_program;
      return (!needle || `${member.display_name} ${member.username}`.toLocaleLowerCase().includes(needle))
        && (!statusFilter || member.status === statusFilter)
        && (!roleFilter || member.role === roleFilter)
        && (!levelFilter || program?.level === levelFilter)
        && (!supervisorFilter || (supervisorFilter === "unassigned" ? !!program && !member.supervisor_member_id : member.supervisor_member_id === supervisorFilter))
        && (!yearFilter || program?.enrollment_date.startsWith(yearFilter));
    });
  }, [members, search, statusFilter, roleFilter, levelFilter, yearFilter, supervisorFilter]);

  function ownedContainers(memberId: string): ContainerSummary[] {
    const member = members.find(candidate => candidate.id === memberId);
    return member ? containers.filter(container => ownsContainer(container, { memberId, userId: member.username })) : [];
  }

  function openDialog(kind: DialogKind, member?: Member) {
    setError("");
    setStatus("");
    setReason("");
    setConfirmation("");
    if (kind === "invite") {
      setInviteUsername("");
      setInviteName("");
      setInviteRole("user");
      setInvitePassword("");
      setInviteSupervisor(teacherMode ? actor?.userId ?? "" : "");
      setInviteLevel("master");
      setInviteYear(currentYear);
    }
    if (member) {
      const program = member.current_program;
      setExtensionDate(program?.expected_graduation_date ?? "");
      const defaultPromotionYear = program
        ? Number(program.expected_graduation_date.slice(0, 4)) - 1
        : currentYear;
      setPromotionYear(defaultPromotionYear);
      setPromotionEffectiveDate(`${defaultPromotionYear}-09-01`);
      setProfileName(member.display_name);
      setProfileLevel(program?.level ?? "master");
      setProfileEnrollmentDate(program?.enrollment_date ?? "");
      setProfileGraduationDate(program?.expected_graduation_date ?? "");
      setSelectedRole(member.role);
      setSelectedSupervisor(member.supervisor_member_id ?? "");
      if (kind === "transfer") {
        const inventory = ownedContainers(member.id);
        setSelectedContainerIds(inventory.map((container) => container.id));
        setTargetMemberId(eligibleTargets(member, members)[0]?.id ?? "");
      }
    }
    setDialog({ kind, memberId: member?.id });
  }

  function openMemberAction(kind: MemberActionKind, member: Member) {
    setDetailMemberId(null);
    openDialog(kind, member);
  }

  function closeDialog() {
    setDialog(null);
  }

  async function runCommand(command: PendingCommand) {
    setSubmitting(true);
    setError("");
    try {
      const response = await authenticatedFetch(command.url, command.init);
      const payload = await safeJson(response);
      const detail = publicDetail(payload, "操作失败，请稍后重试。");
      if (response.status === 409 && detail === "member still owns managed containers") {
        throw new Error("该成员仍有容器，需由管理员处理容器后再归档。");
      }
      if (response.status === 409 && detail === "member still has an active WeChat binding") {
        throw new Error("该成员仍绑定微信，请联系管理员解绑后再归档。");
      }
      if (response.status === 409) {
        setDialog(null);
        await loadMembers();
        setError("成员信息已发生变更或当前状态不支持此操作，目录已经刷新，请重新确认后操作。");
        return;
      }
      if (!response.ok) throw new Error(detail);
      applyCommandResult(payload, command);
      if (teacherMode) onContainersUpdated?.();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "操作失败，请稍后重试。");
    } finally {
      setSubmitting(false);
    }
  }

  function applyCommandResult(payload: unknown, command: PendingCommand) {
    if (command.kind === "list" && Array.isArray(payload)) {
      setMembers(payload.filter(isMember));
      setDialog(null);
      return;
    }
    const record = payload && typeof payload === "object" ? payload as Record<string, unknown> : {};
    if (command.kind === "batch") {
      const invitations = Array.isArray(record.members) ? record.members : [];
      const invitedMembers = invitations
        .map((item) => item && typeof item === "object" ? (item as Record<string, unknown>).member : null)
        .filter(isMember);
      setMembers((current) => [
        ...current,
        ...invitedMembers.filter((member) => !current.some((item) => item.id === member.id)),
      ]);
      const entries = invitations.flatMap((item) => {
        if (!item || typeof item !== "object") return [];
        const invitation = item as Record<string, unknown>;
        const member = isMember(invitation.member) ? invitation.member : null;
        if (!member) return [];
        return [
          ...(typeof invitation.initial_password === "string"
            ? [{ label: `${member.username} 初始密码`, value: invitation.initial_password }]
            : []),
        ];
      });
      setCsvPreview([]);
      if (entries.length > 0) {
        setOneTimeResult({ title: "批量成员初始密码", entries });
        setDialog({ kind: "credentials" });
      } else {
        setDialog(null);
        setStatus(`已创建 ${invitedMembers.length} 个成员。`);
      }
      return;
    }
    const wrappedMember = record.member;
    const member = isMember(wrappedMember) ? wrappedMember : (isMember(payload) ? payload : null);
    if (member) {
      setMembers((current) => {
        if (teacherMode && member.supervisor_member_id !== currentMemberId) {
          return current.filter((item) => item.id !== member.id);
        }
        const found = current.some((item) => item.id === member.id);
        return found
          ? current.map((item) => item.id === member.id ? member : item)
          : [...current, member];
      });
    }

    const credentials: OneTimeResult["entries"] = [];
    for (const [key, label] of [
      ["initial_password", "初始密码"],
      ["activation_code", "账号激活码"],
      ["password_reset_code", "密码重置码"],
    ] as const) {
      const value = record[key];
      if (typeof value === "string") credentials.push({ label, value });
    }

    if (credentials.length > 0) {
      setOneTimeResult({
        title: command.kind === "invite" ? "成员登录信息" : "密码重置信息",
        entries: credentials,
      });
      setDialog({ kind: "credentials" });
    } else {
      setDialog(null);
      setStatus(command.kind === "transfer" ? "容器所有权已更新。" : "成员信息已更新。");
    }
  }

  async function submitInvite(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await runCommand({
      url: apiBase,
      kind: "invite",
      init: jsonPost({
        username: inviteUsername,
        display_name: inviteName,
        role: inviteRole,
        member_kind: ["teacher", "admin"].includes(inviteRole) ? "staff" : "student",
        ...(["teacher", "admin"].includes(inviteRole) ? {} : {
          program_level: inviteLevel, enrollment_year: inviteYear,
          ...(inviteSupervisor ? { supervisor_username: inviteSupervisor } : {}),
        }),
        ...(invitePassword ? { initial_password: invitePassword } : {}),
      }),
    });
  }

  async function importCsv(file: File | undefined) {
    if (!file) return;
    setError("");
    setStatus("");
    try {
      const invitations = await parseInvitationCsv(file);
      setCsvPreview(invitations);
      setDialog({ kind: "batch-preview" });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "CSV 无法解析。");
    }
  }

  async function submitBatchInvite() {
    if (csvPreview.length === 0) return;
    await runCommand({
      url: "/api/gsm/admin/members/batch",
      kind: "batch",
      init: jsonPost({ members: csvPreview }),
    });
  }

  async function submitMemberCommand(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedMember || !dialog) return;
    const base = { expected_version: selectedMember.version };
    let action = dialog.kind;
    let body: Record<string, unknown>;

    if (dialog.kind === "profile-correction") {
      body = {
        ...base,
        display_name: profileName,
        ...(selectedMember.current_program ? { program_level: profileLevel,
          enrollment_date: profileEnrollmentDate, expected_graduation_date: profileGraduationDate } : {}),
        reason,
      };
    } else if (dialog.kind === "role") {
      body = {
        ...base,
        role: selectedRole,
        reason,
        ...(confirmation ? { confirmation } : {}),
      };
    } else if (dialog.kind === "supervisor") {
      body = { ...base, supervisor_member_id: selectedSupervisor || null, reason };
    } else if (dialog.kind === "extend") {
      body = { ...base, expected_graduation_date: extensionDate, reason };
    } else if (dialog.kind === "promote-to-doctor") {
      body = {
        ...base,
        enrollment_year: promotionYear,
        effective_date: promotionEffectiveDate,
        reason,
        confirmation,
      };
    } else if (dialog.kind === "transfer") {
      const versions = Object.fromEntries(
        ownedContainers(selectedMember.id)
          .filter((container) => selectedContainerIds.includes(container.id))
          .map((container) => [container.id, container.version]),
      );
      await runCommand({
        url: `/api/gsm/admin/members/${encodeURIComponent(selectedMember.id)}/containers/transfer`,
        memberId: selectedMember.id,
        kind: "transfer",
        init: jsonPost({
          ...base,
          target_member_id: targetMemberId,
          container_versions: versions,
          confirm_data_handoff: true,
          reason,
        }),
      });
      return;
    } else {
      body = {
        ...base,
        reason,
        ...(["disable", "archive"].includes(dialog.kind) && confirmation
          ? { confirmation }
          : {}),
      };
    }

    await runCommand({
      url: `${apiBase}/${encodeURIComponent(selectedMember.id)}/${action}`,
      memberId: selectedMember.id,
      kind: ["password-reset", "wechat-pairing-code"].includes(action) ? "code" : "member",
      init: jsonPost(body),
    });
  }

  const invitePreview = academicPreview(inviteLevel, inviteYear);

  return (
    <section className="member-workspace" aria-labelledby="member-management-heading">
      <header className="workspace-heading">
        <div className="workspace-title">
          <ShieldCheck size={22} aria-hidden="true" />
          <div>
            <p className="eyebrow">{teacherMode ? "教师工作区" : "管理员工作区"}</p>
            <h2 id="member-management-heading">{teacherMode ? "我的学生" : "成员管理"}</h2>
            <p>{teacherMode ? "管理名下学生的账号、培养信息与导师归属" : "成员身份、培养周期和资源归属"}</p>
          </div>
        </div>
        <div className="workspace-actions">
          {!teacherMode && <><a className="secondary-button" href="/templates/members.csv" download>下载 CSV 模板</a>
          <label className="secondary-button file-button">
            <Upload size={16} aria-hidden="true" />导入 CSV
            <input
              className="sr-only"
              type="file"
              accept=".csv,text/csv"
              aria-label="导入 CSV"
              onChange={(event) => {
                const file = event.currentTarget.files?.[0];
                event.currentTarget.value = "";
                void importCsv(file);
              }}
            />
          </label></>}
          <button className="primary-button" type="button" onClick={() => openDialog("invite")}>
            <UserPlus size={16} aria-hidden="true" />{teacherMode ? "新增学生" : "邀请成员"}
          </button>
        </div>
      </header>

      {error && <p className="workspace-error" role="alert">{error}</p>}
      {status && <p className="table-message" role="status">{status}</p>}

      <div className="member-filters" aria-label="成员筛选">
        {!teacherMode && <label>导师筛选<select value={supervisorFilter} onChange={(event) => setSupervisorFilter(event.currentTarget.value)}><option value="">全部导师</option><option value="unassigned">未分配导师</option>{members.filter((m) => m.member_kind === "staff").map((m) => <option key={m.id} value={m.id}>{m.display_name}</option>)}</select></label>}
        <label>搜索成员<input type="search" value={search} onChange={(event) => setSearch(event.currentTarget.value)} /></label>
        <label>成员状态筛选<select value={statusFilter} onChange={(event) => setStatusFilter(event.currentTarget.value)}>
          <option value="">全部状态</option>
          <option value="invited">待激活</option><option value="active">在组</option>
          <option value="pending_exit">待离组</option><option value="disabled">已停用</option>
          <option value="archived">已归档</option>
        </select></label>
        <label>成员角色筛选<select value={roleFilter} onChange={(event) => setRoleFilter(event.currentTarget.value)}>
          <option value="">全部角色</option><option value="viewer">只读</option>
          <option value="user">成员</option><option value="power_user">高级成员</option>
          {!teacherMode && <><option value="teacher">教师</option>
          <option value="admin">管理员</option></>}
        </select></label>
        <label>培养层次筛选<select value={levelFilter} onChange={(event) => setLevelFilter(event.currentTarget.value)}>
          <option value="">全部层次</option><option value="master">硕士培养</option><option value="doctor">博士培养</option>
        </select></label>
        <label>入学年份筛选<select value={yearFilter} onChange={(event) => setYearFilter(event.currentTarget.value)}>
          <option value="">全部年份</option>
          {enrollmentYears.map((year) => <option key={year} value={year}>{year}</option>)}
        </select></label>
      </div>

      <div className="table-wrap member-table-wrap">
        <table aria-label="课题组成员列表" className="member-table">
          <thead>
            <tr>
              <th>成员</th><th>培养层次</th><th>角色</th><th>入学</th><th>预期毕业</th>
              <th>剩余时间</th><th>状态</th><th>微信</th>{!teacherMode && <th>容器</th>}<th>操作</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={teacherMode ? 9 : 10} className="empty-state">正在加载成员目录...</td></tr>
            ) : filteredMembers.length === 0 ? (
              <tr><td colSpan={teacherMode ? 9 : 10} className="empty-state">没有符合条件的成员</td></tr>
            ) : filteredMembers.map((member) => {
              const program = member.current_program;
              const inventory = ownedContainers(member.id);
              return (
                <tr key={member.id} className={memberRowClass(member)}>
                  <td data-label="成员"><strong>{member.display_name}</strong><span className="member-username">@{member.username}</span></td>
                  <td data-label="培养层次">{program ? <DegreeBadge level={program.level} /> : "-"}</td>
                  <td data-label="角色">{roleLabel(member.role)}</td>
                  <td data-label="入学">{program?.enrollment_date ?? "-"}</td>
                  <td data-label="预期毕业" className="graduation-date">{program?.expected_graduation_date ?? "-"}</td>
                  <td data-label="剩余时间">{program ? remainingLabel(program.expected_graduation_date) : "-"}</td>
                  <td data-label="状态"><span className={`status-pill ${member.status}`}>{statusLabel(member.status)}</span></td>
                  <td data-label="微信">{member.wechat_binding ? "已绑定" : "未绑定"}</td>
                  {!teacherMode && <td data-label="容器"><button className="text-button" type="button" aria-label={`管理 ${member.display_name} 的容器`} onClick={(event) => { detailTriggerRef.current = event.currentTarget; setContainerMemberId(member.id); }}>{inventory.length} 个容器</button></td>}
                  <td data-label="操作">
                    <button
                      className="icon-button member-open-button"
                      type="button"
                      aria-label={`打开 ${member.display_name}`}
                      title="打开详情"
                      onClick={(event) => {
                        detailTriggerRef.current = event.currentTarget;
                        setDetailMemberId(member.id);
                      }}
                    >
                      <ChevronRight size={17} aria-hidden="true" />
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {detailMember && (
        <MemberDetailDrawer
          member={detailMember}
          containerCount={ownedContainers(detailMember.id).length}
          supervisorName={supervisors.find((member) => member.id === detailMember.supervisor_member_id)?.display_name}
          triggerRef={detailTriggerRef}
          onClose={() => setDetailMemberId(null)}
          onAction={(kind) => openMemberAction(kind, detailMember)}
          accountOnly={teacherMode}
          onManageContainers={teacherMode ? undefined : () => { setContainerMemberId(detailMember.id); setDetailMemberId(null); }}
        />
      )}

      {!teacherMode && containerMember && (
        <AccessibleDialog title={`${containerMember.display_name} 的容器`} className="member-containers-dialog" triggerRef={detailTriggerRef} onClose={() => setContainerMemberId(null)}>
          <div className="container-detail-toolbar">
            <p>@{containerMember.username} · 共 {memberContainers.length} 个容器</p>
            <button className="secondary-button" type="button" onClick={onContainersUpdated}>刷新容器</button>
          </div>
          <ContainerOperationsTable key={containerMember.id} containers={memberContainers} actor={actor}
            canOperate={actor?.role === "admin" && (!actor.status || actor.status === "active")}
            onOperationComplete={onContainersUpdated} />
          {memberContainers.length === 0 && <p className="empty-state">该成员暂无容器。</p>}
        </AccessibleDialog>
      )}

      {dialog?.kind === "invite" && (
        <Dialog title={teacherMode ? "新增学生" : "邀请成员"} onClose={closeDialog}>
          <form className="dialog-form" onSubmit={submitInvite}>
            <label htmlFor="invite-username">用户名</label>
            <input id="invite-username" required value={inviteUsername} onChange={(event) => setInviteUsername(event.currentTarget.value)} />
            <label htmlFor="invite-name">姓名</label>
            <input id="invite-name" required value={inviteName} onChange={(event) => setInviteName(event.currentTarget.value)} />
            <label htmlFor="invite-role">角色</label>
            <select id="invite-role" value={inviteRole} onChange={(event) => setInviteRole(event.currentTarget.value)}>
              <option value="viewer">只读</option><option value="user">成员</option><option value="power_user">高级成员</option>{!teacherMode && <><option value="teacher">教师</option><option value="admin">管理员</option></>}
            </select>
            <label htmlFor="invite-password">初始密码（留空自动生成）</label>
            <input id="invite-password" type="password" autoComplete="new-password" minLength={12} maxLength={128} value={invitePassword} onChange={(event) => setInvitePassword(event.currentTarget.value)} />
            <p className="form-help">将用户名和初始密码交给成员；首次登录必须设置自己的新密码。微信配对由成员登录后自行设置。</p>
            {["teacher", "admin"].includes(inviteRole) ? <p className="academic-preview">教职工账户长期有效，无需填写入学或毕业时间。</p> : <>
            <label htmlFor="invite-supervisor">导师（可选）</label><select id="invite-supervisor" aria-describedby="invite-supervisor-help" value={inviteSupervisor} onChange={(event) => setInviteSupervisor(event.currentTarget.value)}><option value="">{teacherMode ? "默认分配给我" : "暂不分配"}</option>{supervisors.map((m) => <option key={m.id} value={m.username}>{m.display_name} (@{m.username}){m.status === "invited" ? " · 待首次登录" : ""}</option>)}</select>
            <p id="invite-supervisor-help" className="form-help">{teacherMode ? "默认归自己管理；选择其他导师后，该学生仅由对应导师和管理员管理。" : "新建教师可直接分配为导师，教师完成首次登录改密后即可查看学生。"}</p>
            <label htmlFor="invite-level">培养层次</label>
            <select id="invite-level" value={inviteLevel} onChange={(event) => setInviteLevel(event.currentTarget.value as ProgramLevel)}>
              <option value="master">硕士</option><option value="doctor">博士</option>
            </select>
            <label htmlFor="invite-year">入学年份</label>
            <input id="invite-year" type="number" min={1900} max={2200} required value={inviteYear} onChange={(event) => setInviteYear(Number(event.currentTarget.value))} />
            <p className="academic-preview"><CalendarClock size={16} aria-hidden="true" />{invitePreview}</p>
            </>}
            <DialogActions submitting={submitting} submitLabel="确认邀请" onClose={closeDialog} />
          </form>
        </Dialog>
      )}

      {dialog?.kind === "batch-preview" && (
        <Dialog title={`确认 ${csvPreview.length} 条邀请`} onClose={closeDialog} wide>
          <p>确认后统一创建；任一条失败时整批不会写入。</p>
          <div className="table-wrap batch-preview-table">
            <table aria-label="CSV 邀请预览">
              <thead><tr><th>用户名</th><th>姓名</th><th>身份</th><th>角色</th><th>培养信息</th><th>导师用户名</th></tr></thead>
              <tbody>
                {csvPreview.map((invitation) => (
                  <tr key={invitation.username}>
                    <td>{invitation.username}</td>
                    <td>{invitation.display_name}</td>
                    <td>{invitation.member_kind === "staff" ? "教职工" : "学生"}</td>
                    <td>{roleLabel(invitation.role)}</td>
                    <td>{invitation.member_kind === "staff" ? "-" : `${invitation.program_level === "doctor" ? "博士" : "硕士"} · ${invitation.enrollment_year}`}</td>
                    <td>{invitation.supervisor_username || "未分配"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="dialog-actions">
            <button className="secondary-button" type="button" disabled={submitting} onClick={closeDialog}>取消</button>
            <button className="primary-button" type="button" disabled={submitting || csvPreview.length === 0} onClick={submitBatchInvite}>
              创建 {csvPreview.length} 个成员
            </button>
          </div>
        </Dialog>
      )}

      {dialog?.kind === "credentials" && oneTimeResult && (
        <Dialog title={oneTimeResult.title} onClose={() => { setOneTimeResult(null); closeDialog(); }}>
          <p className="form-help">将初始登录信息交给对应成员，首次登录必须改密。微信配对与验证器密钥由成员自行保存。</p>
          <dl className="credential-list">
            {oneTimeResult.entries.map((entry) => <div key={entry.label}><dt>{entry.label}</dt><dd>{entry.value}</dd></div>)}
          </dl>
          <div className="dialog-actions"><button className="primary-button" type="button" onClick={() => { setOneTimeResult(null); closeDialog(); }}>完成</button></div>
        </Dialog>
      )}

      {selectedMember && dialog?.kind === "supervisor" && <Dialog title={`分配导师 ${selectedMember.display_name}`} onClose={closeDialog}>
        <form className="dialog-form" onSubmit={submitMemberCommand}>
          <label htmlFor="member-supervisor">导师</label><select id="member-supervisor" aria-describedby="member-supervisor-help" value={selectedSupervisor} onChange={(event) => setSelectedSupervisor(event.currentTarget.value)}><option value="">取消分配</option>{supervisors.map((m) => <option key={m.id} value={m.id}>{m.display_name} (@{m.username}){m.status === "invited" ? " · 待首次登录" : ""}</option>)}</select>
          <p id="member-supervisor-help" className="form-help">可选择已启用或待首次登录的教师、教职工管理员。教师完成首次登录改密后即可查看学生；已停用、离组或归档的账号不可分配。</p>
          {teacherMode && <p className="form-help">转交其他导师或取消分配后，该学生会立即移出你的管理范围。</p>}
          <label htmlFor="supervisor-reason">分配原因</label><input id="supervisor-reason" required maxLength={500} value={reason} onChange={(event) => setReason(event.currentTarget.value)} />
          <DialogActions submitting={submitting} submitLabel="保存导师" disabled={!reason.trim()} onClose={closeDialog} />
        </form>
      </Dialog>}
      {selectedMember && dialog?.kind === "extend" && (
        <Dialog title={`延期 ${selectedMember.display_name}`} onClose={closeDialog}>
          <form className="dialog-form" onSubmit={submitMemberCommand}>
            <label htmlFor="extension-date">新的毕业日期</label>
            <input id="extension-date" type="date" required value={extensionDate} onChange={(event) => setExtensionDate(event.currentTarget.value)} />
            <label htmlFor="extension-reason">延期原因</label>
            <textarea id="extension-reason" required value={reason} onChange={(event) => setReason(event.currentTarget.value)} />
            <DialogActions submitting={submitting} submitLabel="确认延期" disabled={!reason.trim() || !extensionDate} onClose={closeDialog} />
          </form>
        </Dialog>
      )}

      {selectedMember && dialog?.kind === "promote-to-doctor" && (
        <Dialog title={`硕转博 ${selectedMember.display_name}`} onClose={closeDialog}>
          <form className="dialog-form" onSubmit={submitMemberCommand}>
            <label htmlFor="promotion-year">博士入学年份</label>
            <input id="promotion-year" type="number" min={1900} max={2200} value={promotionYear} onChange={(event) => {
              const year = Number(event.currentTarget.value);
              setPromotionYear(year);
              setPromotionEffectiveDate(`${year}-09-01`);
            }} />
            <label htmlFor="promotion-effective">转博生效日期</label>
            <input id="promotion-effective" type="date" value={promotionEffectiveDate} onChange={(event) => setPromotionEffectiveDate(event.currentTarget.value)} />
            <p className="academic-preview">预计博士毕业：{promotionYear + 4}-07-01</p>
            <label htmlFor="promotion-reason">转博原因</label>
            <textarea id="promotion-reason" required value={reason} onChange={(event) => setReason(event.currentTarget.value)} />
            <label htmlFor="promotion-confirmation">转博确认短语</label>
            <input id="promotion-confirmation" spellCheck={false} autoComplete="off" value={confirmation} onChange={(event) => setConfirmation(event.currentTarget.value)} />
            <code className="required-phrase">{PROMOTION_CONFIRMATION}</code>
            <DialogActions submitting={submitting} submitLabel="确认硕转博" disabled={!reason.trim() || confirmation !== PROMOTION_CONFIRMATION} onClose={closeDialog} />
          </form>
        </Dialog>
      )}

      {selectedMember && dialog?.kind === "profile-correction" && (
        <Dialog title={`修正资料 ${selectedMember.display_name}`} onClose={closeDialog}>
          <form className="dialog-form" onSubmit={submitMemberCommand}>
            <label htmlFor="profile-name">姓名</label><input id="profile-name" required value={profileName} onChange={(event) => setProfileName(event.currentTarget.value)} />
            {selectedMember.current_program && <><label htmlFor="profile-level">培养层次</label><select id="profile-level" value={profileLevel} onChange={(event) => setProfileLevel(event.currentTarget.value as ProgramLevel)}><option value="master">硕士</option><option value="doctor">博士</option></select>
            <label htmlFor="profile-enrollment">入学日期</label><input id="profile-enrollment" type="date" value={profileEnrollmentDate} onChange={(event) => setProfileEnrollmentDate(event.currentTarget.value)} />
            <label htmlFor="profile-graduation">预期毕业日期</label><input id="profile-graduation" type="date" value={profileGraduationDate} onChange={(event) => setProfileGraduationDate(event.currentTarget.value)} />
            </>}
            <label htmlFor="profile-reason">修正原因</label><textarea id="profile-reason" required value={reason} onChange={(event) => setReason(event.currentTarget.value)} />
            <DialogActions submitting={submitting} submitLabel="保存修正" disabled={!reason.trim()} onClose={closeDialog} />
          </form>
        </Dialog>
      )}

      {selectedMember && dialog?.kind === "role" && (
        <Dialog title={`调整角色 ${selectedMember.display_name}`} onClose={closeDialog}>
          <form className="dialog-form" onSubmit={submitMemberCommand}>
            <label htmlFor="member-role">新角色</label><select id="member-role" value={selectedRole} onChange={(event) => setSelectedRole(event.currentTarget.value)}>{selectedMember.current_program ? <><option value="viewer">只读</option><option value="user">成员</option><option value="power_user">高级成员</option></> : <option value="teacher">教师</option>}{!teacherMode && <option value="admin">管理员</option>}</select>
            <label htmlFor="role-reason">变更原因</label><textarea id="role-reason" required value={reason} onChange={(event) => setReason(event.currentTarget.value)} />
            {!teacherMode && <><label htmlFor="role-confirmation">角色变更确认短语（需要时）</label><input id="role-confirmation" spellCheck={false} value={confirmation} onChange={(event) => setConfirmation(event.currentTarget.value)} /></>}
            {selectedMember.id === currentMemberId && selectedMember.role === "admin" && selectedRole !== "admin" && (
              <code className="required-phrase">DEMOTE MY ADMIN ACCOUNT</code>
            )}
            <DialogActions
              submitting={submitting}
              submitLabel="确认角色变更"
              disabled={!reason.trim() || (
                selectedMember.id === currentMemberId
                && selectedMember.role === "admin"
                && selectedRole !== "admin"
                && confirmation !== "DEMOTE MY ADMIN ACCOUNT"
              )}
              onClose={closeDialog}
            />
          </form>
        </Dialog>
      )}

      {selectedMember && isReasonDialog(dialog?.kind) && (
        <ReasonCommandDialog
          kind={dialog.kind}
          member={selectedMember}
          reason={reason}
          confirmation={confirmation}
          submitting={submitting}
          setReason={setReason}
          setConfirmation={setConfirmation}
          onSubmit={submitMemberCommand}
          onClose={closeDialog}
          requireSelfConfirmation={dialog.kind === "disable" && selectedMember.id === currentMemberId}
        />
      )}

      {!teacherMode && selectedMember && dialog?.kind === "transfer" && (
        <Dialog title={`转移容器 ${selectedMember.display_name}`} onClose={closeDialog} wide>
          <form className="dialog-form" onSubmit={submitMemberCommand}>
            <div className="transfer-inventory">
              {ownedContainers(selectedMember.id).map((container) => (
                <label className="transfer-container" key={container.id}>
                  <input
                    type="checkbox"
                    checked={selectedContainerIds.includes(container.id)}
                    onChange={(event) => setSelectedContainerIds((current) => event.currentTarget.checked ? [...current, container.id] : current.filter((id) => id !== container.id))}
                  />
                  <span><strong>{container.name}</strong><small>{container.status} · GPU {container.gpus.join(", ") || "无"} · {container.sshCommand ?? "SSH 未发布"}</small>{container.mounts?.map((mount) => <small key={`${mount.hostPath}:${mount.containerPath}`}>{mount.hostPath} → {mount.containerPath} ({mount.mode})</small>)}</span>
                </label>
              ))}
            </div>
            <label htmlFor="transfer-target">新容器所有者</label>
            <select id="transfer-target" required value={targetMemberId} onChange={(event) => setTargetMemberId(event.currentTarget.value)}>
              <option value="">选择成员</option>
              {eligibleTargets(selectedMember, members).map((member) => <option key={member.id} value={member.id}>{member.display_name} (@{member.username})</option>)}
            </select>
            <label htmlFor="transfer-reason">转移原因</label><textarea id="transfer-reason" required value={reason} onChange={(event) => setReason(event.currentTarget.value)} />
            <label htmlFor="transfer-confirmation">数据移交确认短语</label><input id="transfer-confirmation" spellCheck={false} autoComplete="off" value={confirmation} onChange={(event) => setConfirmation(event.currentTarget.value)} />
            <code className="required-phrase">{TRANSFER_CONFIRMATION}</code>
            <DialogActions submitting={submitting} submitLabel="确认转移" disabled={!targetMemberId || !reason.trim() || selectedContainerIds.length === 0 || confirmation !== TRANSFER_CONFIRMATION} onClose={closeDialog} />
          </form>
        </Dialog>
      )}


    </section>
  );
}

function ReasonCommandDialog({ kind, member, reason, confirmation, submitting, setReason, setConfirmation, onSubmit, onClose, requireSelfConfirmation }: {
  kind: ReasonDialogKind;
  member: Member;
  reason: string;
  confirmation: string;
  submitting: boolean;
  setReason: (value: string) => void;
  setConfirmation: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onClose: () => void;
  requireSelfConfirmation: boolean;
}) {
  const config = reasonDialogConfig(kind, member, requireSelfConfirmation);
  const needsExact = config.exactConfirmation !== undefined;
  return (
    <Dialog title={`${config.title} ${member.display_name}`} onClose={onClose}>
      <form className="dialog-form" onSubmit={onSubmit}>
        <p>{config.consequence}</p>
        <label htmlFor="command-reason">{config.reasonLabel}</label>
        <textarea id="command-reason" required value={reason} onChange={(event) => setReason(event.currentTarget.value)} />
        {needsExact && <>
          <label htmlFor="command-confirmation">确认短语</label>
          <input id="command-confirmation" spellCheck={false} autoComplete="off" value={confirmation} onChange={(event) => setConfirmation(event.currentTarget.value)} />
          <code className="required-phrase">{config.exactConfirmation}</code>
        </>}
        <DialogActions submitting={submitting} submitLabel={config.submitLabel} disabled={!reason.trim() || (needsExact && confirmation !== config.exactConfirmation)} onClose={onClose} danger={config.danger} />
      </form>
    </Dialog>
  );
}

type ReasonDialogKind = "password-reset" | "revoke-sessions" | "wechat-pairing-code" | "wechat-unbind" | "disable" | "reactivate" | "archive";

function isReasonDialog(kind?: DialogKind): kind is ReasonDialogKind {
  return kind !== undefined && ["password-reset", "revoke-sessions", "wechat-pairing-code", "wechat-unbind", "disable", "reactivate", "archive"].includes(kind);
}

function reasonDialogConfig(kind: ReasonDialogKind, member: Member, requireSelfConfirmation: boolean) {
  const configs: Record<ReasonDialogKind, {
    title: string;
    reasonLabel: string;
    submitLabel: string;
    consequence: string;
    danger: boolean;
    exactConfirmation?: string;
  }> = {
    "password-reset": { title: "重置密码", reasonLabel: "重置原因", submitLabel: "生成初始密码", consequence: "现有密码和会话将失效。成员下次登录必须更改初始密码。", danger: false },
    "revoke-sessions": { title: "撤销会话", reasonLabel: "撤销原因", submitLabel: "撤销全部会话", consequence: "该成员需要重新登录。", danger: true },
    "wechat-pairing-code": { title: "生成微信配对码", reasonLabel: "生成原因", submitLabel: "生成配对码", consequence: "旧的未使用配对码将失效。", danger: false },
    "wechat-unbind": { title: "解绑微信", reasonLabel: "解绑原因", submitLabel: "确认解绑", consequence: "当前微信路由将立即失去操作权限。", danger: true },
    "disable": { title: "停用成员", reasonLabel: "停用原因", submitLabel: "确认停用", consequence: "Web 与微信访问将停用，并禁止签发新 SSH 证书；容器不会停止，已签发的永久 SSH 证书不随网页账号停用而撤销。如需中断直连，请联系管理员。", danger: true, exactConfirmation: requireSelfConfirmation ? "DISABLE MY ACCOUNT" : undefined },
    "reactivate": { title: "重新启用成员", reasonLabel: "启用原因", submitLabel: "确认启用", consequence: "成员将恢复在组身份，但旧会话不会恢复。", danger: false },
    "archive": { title: "归档成员", reasonLabel: "归档原因", submitLabel: "确认归档", consequence: "归档后成员历史不可再修改。", danger: true, exactConfirmation: `ARCHIVE ${member.username}` },
  };
  return configs[kind];
}

function Dialog({ title, children, onClose, wide = false }: { title: string; children: ReactNode; onClose: () => void; wide?: boolean }) {
  return (
    <AccessibleDialog
      title={title}
      onClose={onClose}
      className={wide ? "dialog-wide" : undefined}
    >
      {children}
    </AccessibleDialog>
  );
}

function DialogActions({ submitting, submitLabel, onClose, disabled = false, danger = false }: { submitting: boolean; submitLabel: string; onClose: () => void; disabled?: boolean; danger?: boolean }) {
  return <div className="dialog-actions"><button className="secondary-button" type="button" onClick={onClose}>取消</button><button className={danger ? "danger-button" : "primary-button"} type="submit" disabled={submitting || disabled}>{submitLabel}</button></div>;
}

function DegreeBadge({ level }: { level: ProgramLevel }) {
  return <span className={`degree-badge ${level}`}>{level === "doctor" ? "博士" : "硕士"}</span>;
}

function jsonPost(body: unknown): RequestInit {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

const csvHeaders = [
  "username",
  "display_name",
  "member_kind",
  "role",
  "program_level",
  "enrollment_year",
] as const;

async function parseInvitationCsv(file: File): Promise<CsvInvitation[]> {
  const result = await new Promise<Papa.ParseResult<Record<string, string>>>((resolve, reject) => {
    Papa.parse<Record<string, string>>(file, {
      header: true,
      skipEmptyLines: "greedy",
      transformHeader: (header) => header.trim(),
      complete: resolve,
      error: (error) => reject(new Error(error.message)),
    });
  });
  if (result.errors.length > 0) throw new Error("CSV 格式无效，请检查引号和分隔符。");
  const fields = result.meta.fields ?? [];
  if (
    ![csvHeaders.length, csvHeaders.length + 1].includes(fields.length)
    || csvHeaders.some((field, index) => fields[index] !== field)
    || (fields.length > csvHeaders.length && fields[6] !== "supervisor_username")
  ) {
    throw new Error(`CSV 表头必须为：${csvHeaders.join(",")}，可在末尾增加 supervisor_username（已启用或待首次登录导师的用户名）。`);
  }
  if (result.data.length === 0 || result.data.length > 100) {
    throw new Error("CSV 每次必须包含 1 至 100 名成员。");
  }

  const usernames = new Set<string>();
  return result.data.map((row, index) => {
    const line = index + 2;
    const username = row.username?.trim();
    const displayName = row.display_name?.trim();
    const memberKind = row.member_kind?.trim();
    const role = row.role?.trim();
    const programLevel = row.program_level?.trim();
    const enrollmentYear = row.enrollment_year?.trim();
    if (!username || !displayName) throw new Error(`第 ${line} 行缺少用户名或姓名。`);
    const normalized = username.toLocaleLowerCase();
    if (usernames.has(normalized)) throw new Error(`第 ${line} 行用户名重复。`);
    usernames.add(normalized);
    if (memberKind !== "student" && memberKind !== "staff") {
      throw new Error(`第 ${line} 行成员身份必须为 student 或 staff。`);
    }
    if (!isCsvRole(role)) throw new Error(`第 ${line} 行角色无效。`);
    const validCombination = memberKind === "staff"
      ? role === "teacher" || role === "admin"
      : role !== "teacher";
    if (!validCombination) throw new Error(`第 ${line} 行成员身份与角色不匹配。`);

    if (memberKind === "staff") {
      if (programLevel || enrollmentYear || row.supervisor_username?.trim()) throw new Error(`第 ${line} 行教职工不能填写培养信息或导师。`);
      return {
        username,
        display_name: displayName,
        member_kind: memberKind,
        role,
      };
    }
    if (programLevel !== "master" && programLevel !== "doctor") {
      throw new Error(`第 ${line} 行培养层次必须为 master 或 doctor。`);
    }
    const year = Number(enrollmentYear);
    if (!/^\d{4}$/.test(enrollmentYear) || year < 1900 || year > 2200) {
      throw new Error(`第 ${line} 行入学年份无效。`);
    }
    return {
      username,
      display_name: displayName,
      member_kind: memberKind,
      role,
      program_level: programLevel,
      enrollment_year: year,
      ...(row.supervisor_username?.trim() ? { supervisor_username: row.supervisor_username.trim() } : {}),
    };
  });
}

function isCsvRole(value: string): value is CsvInvitation["role"] {
  return ["viewer", "user", "power_user", "teacher", "admin"].includes(value);
}

async function safeJson(response: Response): Promise<unknown> {
  try { return await response.json(); } catch { return null; }
}

function publicDetail(payload: unknown, fallback: string): string {
  if (payload && typeof payload === "object" && "detail" in payload) {
    const detail = (payload as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.length <= 240) return detail;
  }
  return fallback;
}

function isMember(value: unknown): value is Member {
  if (!value || typeof value !== "object") return false;
  const record = value as Partial<Member>;
  return typeof record.id === "string" && typeof record.username === "string" && typeof record.version === "number";
}

function academicPreview(level: ProgramLevel, year: number): string {
  const duration = level === "doctor" ? 4 : 3;
  return `${year}-09-01 至 ${year + duration}-07-01`;
}

function eligibleTargets(source: Member, members: Member[]): Member[] {
  return members.filter((member) => member.id !== source.id && member.status === "active" && ["user", "power_user", "teacher", "admin"].includes(member.role));
}

function roleLabel(role: string): string {
  return ({ viewer: "只读", user: "成员", power_user: "高级成员", teacher: "教师", admin: "管理员" } as Record<string, string>)[role] ?? role;
}

function statusLabel(status: string): string {
  return ({ invited: "待激活", active: "在组", pending_exit: "待离组", disabled: "已停用", archived: "已归档" } as Record<string, string>)[status] ?? status;
}

function remainingLabel(dateValue: string): string {
  const target = new Date(`${dateValue}T00:00:00+08:00`).valueOf();
  const days = Math.ceil((target - Date.now()) / 86_400_000);
  if (days < 0) return `已超期 ${Math.abs(days)} 天`;
  if (days === 0) return "今天到期";
  if (days < 365) return `${days} 天`;
  return `${(days / 365).toFixed(1)} 年`;
}

function memberRowClass(member: Member): string {
  if (member.status === "pending_exit") return "member-row pending-exit";
  if (!member.current_program) return "member-row";
  const days = Math.ceil((new Date(`${member.current_program.expected_graduation_date}T00:00:00+08:00`).valueOf() - Date.now()) / 86_400_000);
  return days <= 90 ? "member-row expiring" : "member-row";
}
