"use client";

import {
  Bell,
  Boxes,
  Plus,
  ShieldCheck,
} from "lucide-react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState, useTransition } from "react";
import { version as appVersion } from "../package.json";

import { SystemSettingsPanel } from "./SystemSettingsPanel";
import { AccountPanel } from "./AccountPanel";
import { AccessibleDialog } from "./AccessibleDialog";
import { ActivityPanel } from "./ActivityPanel";
import { ContainerCreateWizard } from "./ContainerCreateWizard";
import type { EnvironmentRecipe } from "../lib/environmentRecipe";
import { ContainerOperationsTable } from "./ContainerOperationsTable";
import { ResourceUsageSummary } from "./ResourceUsageSummary";
import { HealthOperationsPanel } from "./HealthOperationsPanel";
import { ExecutionReconciliationPanel } from "./ExecutionReconciliationPanel";
import { PersonalQuotaPanel, QuotaRequestsPanel } from "./PersonalQuotaPanel";
import { GpuAvailabilityPanel } from "./GpuAvailabilityPanel";
import { NotificationSettingsPanel } from "./NotificationSettingsPanel";
import { PersonalDataPanel } from "./PersonalDataPanel";
import { PrintOperationsPanel } from "./PrintOperationsPanel";
import { MemberManagementPanel } from "./MemberManagementPanel";
import { RoleOverview } from "./RoleOverview";
import { TeacherWorkspace } from "./TeacherWorkspace";
import { TeacherUsagePanel } from "./TeacherUsagePanel";
import { WorkspaceNav } from "./WorkspaceNav";
import type { TeacherStudent, WorkspaceOverview } from "../lib/api";
import type { ActorRole, DashboardData } from "../lib/mockData";
import {
  effectiveWorkspaceRole,
  resolveWorkspace,
  workspaceItems,
  workspaceGroups,
  type WorkspaceId,
} from "../lib/workspaces";

type DashboardProps = {
  data: DashboardData;
  initialWorkspace?: WorkspaceId;
  overview?: WorkspaceOverview;
  teacherStudents?: TeacherStudent[];
};

export function Dashboard({
  data,
  initialWorkspace,
  overview,
  teacherStudents = [],
}: DashboardProps) {
  const router = useRouter();
  const workspaceRole = effectiveWorkspaceRole(
    data.actor.role,
    data.actor.status,
  );
  const resolvedInitialWorkspace = resolveWorkspace(
    workspaceRole,
    initialWorkspace,
  );
  const [workspace, setWorkspace] = useState<WorkspaceId>(resolvedInitialWorkspace);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [refreshing, startRefresh] = useTransition();
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [createBusy, setCreateBusy] = useState(false);
  const [reuseRecipe, setReuseRecipe] = useState<EnvironmentRecipe | undefined>();
  const [creationMessage, setCreationMessage] = useState("");
  const createTriggerRef = useRef<HTMLButtonElement>(null);

  const active = !data.actor.status || data.actor.status === "active";
  const canCreateContainers = active && canWriteContainers(data.actor.role);
  const canOperateContainers = active && canWriteContainers(data.actor.role);
  const canPullImages = active && (
    data.actor.role === "power_user" || data.actor.role === "teacher" || data.actor.role === "admin"
  );
  const containers = useMemo(() => [...data.containers].sort((first, second) =>
    Number(second.status === "running") - Number(first.status === "running"),
  ), [data.containers]);
  const runningCount = containers.filter((container) => container.status === "running").length;
  const showCreateDialog = createDialogOpen && canCreateContainers
    && workspace === "resources";

  useEffect(() => {
    setWorkspace(resolvedInitialWorkspace);
    setCreateDialogOpen(false);
  }, [resolvedInitialWorkspace]);

  useEffect(() => { setLastUpdated(new Date()); }, [data]);

  useEffect(() => {
    if (!["resources", "resource-overview", "overview"].includes(workspace) || data.source !== "api") return;
    const refresh = () => {
      if (document.visibilityState === "visible" && !refreshing && !createBusy) {
        startRefresh(() => router.refresh());
      }
    };
    const timer = window.setInterval(refresh, 15000);
    document.addEventListener("visibilitychange", refresh);
    return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", refresh); };
  }, [workspace, data.source, refreshing, createBusy, router]);

  function navigateWorkspace(target: WorkspaceId) {
    const authorized = resolveWorkspace(workspaceRole, target);
    const search = new URLSearchParams(window.location.search);
    search.set("workspace", authorized);
    setWorkspace(authorized);
    setCreateDialogOpen(false);
    router.replace(`/?${search.toString()}`, { scroll: false });
  }

  function refreshResources() {
    startRefresh(() => { router.refresh(); });
  }

  function closeCreateDialog() {
    if (!createBusy) setCreateDialogOpen(false);
  }

  function completeContainerCreate(message: string) {
    setCreationMessage(message);
    setCreateBusy(false);
    setCreateDialogOpen(false);
    refreshResources();
  }

  const auditWorkspace: WorkspaceId = workspaceRole === "admin"
    ? "audit"
    : workspaceRole === "teacher" ? "usage" : "activity";
  const alertWorkspace: WorkspaceId = workspaceRole === "admin"
    ? "audit"
    : workspaceRole === "teacher" ? "usage" : "resources";
  const workspaceLabel = workspaceItems(workspaceRole).find((item) => item.id === workspace)?.label;
  const activeGroup = workspaceGroups(workspaceRole).find((group) =>
    group.items.some((item) => item.id === workspace),
  );
  const roleLabel = { admin: "管理员", teacher: "导师", power_user: "高级成员", user: "课题组成员", viewer: "只读成员" }[workspaceRole];

  return (
    <>
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <main id="main-content" className="dashboard-shell" tabIndex={-1}>
        <aside className="console-sidebar" aria-label="控制台导航">
          <Link className="console-brand" href="/?workspace=overview" aria-label="GPU Server Manager 概览">
            <span className="rack-mark" aria-hidden="true"><i /><i /><i /></span>
            <span><strong>GPU Manager</strong><small>课题组计算平台</small></span>
          </Link>
          <WorkspaceNav role={workspaceRole} active={workspace} onSelect={setWorkspace} />
          <div className="sidebar-footer">
            <div className="sidebar-footer-info">
              <ShieldCheck size={18} aria-hidden="true" />
              <div><strong>课题组内部使用</strong><span>资源操作均留有审计记录</span></div>
            </div>
            <div className="sidebar-version">
              <span>当前版本</span>
              <span className="version-badge" aria-label={`版本 v${appVersion}`}>v{appVersion}</span>
            </div>
          </div>
        </aside>
        <header className="topbar">
          <div>
            <p className="eyebrow">科研组私有云控制台 <span aria-hidden="true">/</span> {activeGroup?.label}</p>
            <h1>{workspaceLabel}</h1>
            <p className="workspace-context">{activeGroup?.description}</p>
          </div>
          <div className="topbar-actions">
            <button className="icon-button" type="button" aria-label="打开审计与活动" title="审计与活动" onClick={() => navigateWorkspace(auditWorkspace)}>
              <ShieldCheck size={18} aria-hidden="true" />
            </button>
            <button className="icon-button" type="button" aria-label="打开告警" title="告警" onClick={() => navigateWorkspace(alertWorkspace)}>
              <Bell size={18} aria-hidden="true" />
            </button>
            <span className={`connection-badge ${data.source === "api" ? "live" : "demo"}`}><i aria-hidden="true" />{data.source === "api" ? "已连接 API" : "演示数据"}</span>
            <button className="profile-button" type="button" onClick={() => navigateWorkspace("account")} aria-label="打开我的账户">
              <span className="profile-avatar" aria-hidden="true">{(data.actor.displayName ?? data.actor.userId).slice(0, 1).toUpperCase()}</span>
              <span><strong>{data.actor.displayName ?? data.actor.userId}</strong><small>{roleLabel}</small></span>
            </button>
          </div>
        </header>

        <div
          id="workspace-panel"
          className="workspace-panel"
          role="tabpanel"
          aria-labelledby={`workspace-${workspace}-tab`}
          data-workspace-group={activeGroup?.id}
        >
        {workspace === "overview" && overview && (
          <>
          <section className="overview-intro" aria-label="工作区概览">
            <div>
              <p className="eyebrow">你的研究工作空间</p>
              <h2>{workspaceRole === "admin" ? "让计算资源，有序运行。" : workspaceRole === "teacher" ? "研究进展，尽在掌握。" : "准备好，继续你的研究。"}</h2>
              <p>{workspaceRole === "admin" ? "成员、算力与运行状态，在同一处管理。" : workspaceRole === "teacher" ? "查看学生资源使用，处理待审批的研究申请。" : "查看可用算力、管理实验容器，或接着上次的工作。"}</p>
              <button className="primary-button" type="button" onClick={() => navigateWorkspace(workspaceRole === "teacher" ? "students" : "resources")}>
                <Boxes size={16} aria-hidden="true" />{workspaceRole === "teacher" ? "查看学生" : "进入资源工作区"}
              </button>
            </div>
            <dl className="overview-inventory">
              <div><dt>已配置服务器</dt><dd>{data.servers.length}<small> 台</small></dd></div>
              <div><dt>当前 GPU 观测</dt><dd>{data.gpus.length}<small> 张</small></dd></div>
              <div><dt>{workspaceRole === "admin" ? "全组容器" : "我的容器"}</dt><dd>{data.containers.length}<small> 个</small></dd></div>
            </dl>
          </section>
          <RoleOverview
            actor={data.actor}
            overview={overview}
            onNavigate={navigateWorkspace}
          />
          </>
        )}

        {workspace === "overview" && <HealthOperationsPanel actor={data.actor} health={data.health} events={data.healthEvents} containers={data.containers} />}
        {workspace === "resource-overview" && <>
        <PersonalQuotaPanel enabled={data.source === "api"} refreshKey={data} />

        <ResourceUsageSummary
          historyServers={data.source === "api" ? data.servers : []}
          gpus={data.gpus}
          serverCount={data.servers.length}
          runningCount={runningCount}
          refreshing={refreshing}
          updatedAt={lastUpdated ? formatRefreshTime(lastUpdated) : undefined}
          onRefresh={refreshResources}
        />

        </>}
        {workspace === "resources" && <>
        <PersonalQuotaPanel compact enabled={data.source === "api"} refreshKey={data} />
        <div className="container-usage-compact" aria-label="GPU 占用速览">{data.gpus.map(gpu => <span key={`${gpu.serverId}:${gpu.index}`}>GPU {gpu.index} · {gpu.freshness === "fresh" ? `${gpu.utilization}% · ${gpu.memoryUsedGb}/${gpu.memoryTotalGb} GiB` : "暂无有效观测"}</span>)}</div>
        {!data.gpus.length && <p className="form-help">暂无 GPU 观测；无观测不代表 GPU 空闲，请在资源概览检查采集状态。</p>}
        <div className="container-detail-toolbar"><Link href="/?workspace=resource-overview">查看资源曲线 →</Link><button className="secondary-button" disabled={refreshing} onClick={refreshResources}>刷新资源</button>{lastUpdated && <small>最近刷新 {formatRefreshTime(lastUpdated)}</small>}</div>
        <section className="panel resource-container-panel" aria-label="容器管理">
          <div className="panel-heading">
            <div>
              <h2>容器列表</h2>
              <p>{runningCount} 个运行中 · 共 {containers.length} 个 · 运行中的容器优先显示。</p>
            </div>
            <button
              ref={createTriggerRef}
              className="primary-button"
              type="button"
              disabled={!canCreateContainers}
              aria-describedby={!canCreateContainers ? "resource-create-unavailable" : undefined}
              onClick={() => {
                setCreationMessage("");
                setCreateBusy(false);
                setReuseRecipe(undefined); setCreateDialogOpen(true);
              }}
            >
              <Plus size={16} aria-hidden="true" />创建容器
            </button>
          </div>
          {!canCreateContainers && <p className="form-help" id="resource-create-unavailable">当前账户仅可查看资源，不能创建容器。</p>}
          {creationMessage && <div className="table-message" role="status">{creationMessage}
            {creationMessage.startsWith("已提交") && <Link className="secondary-button" href={`/?workspace=${workspaceRole === "admin" || workspaceRole === "teacher" ? "approvals" : "queue"}`}>查看申请进度</Link>}
          </div>}
          <ContainerOperationsTable
            containers={containers}
            actor={data.actor}
            canOperate={canOperateContainers}
            onOperationComplete={refreshResources}
            onReuse={recipe => { setReuseRecipe(recipe); setCreateDialogOpen(true); }}
          />
          {containers.length === 0 && <div className="resource-container-empty"><Boxes size={28} aria-hidden="true" /><strong>暂无可见容器</strong><p>{canCreateContainers ? "点击右上角“创建容器”，选择镜像和所需资源。" : "当前没有可查看的容器。"}</p></div>}
        </section>

        </>}

        {workspace === "data" && <PersonalDataPanel enabled={data.source === "api"} />}

        {workspace === "print" && (
          <PrintOperationsPanel
            actorRole={workspaceRole}
            autoLoad={data.source === "api"}
          />
        )}

        {workspace === "students" && workspaceRole === "teacher" && <TeacherWorkspace view="students" initialStudents={teacherStudents} initialApprovals={[]} onUpdated={refreshResources} refreshing={refreshing} />}
        {workspace === "approvals" && <QuotaRequestsPanel memberId={data.actor.memberId} enabled={data.source === "api"} />}

        {workspace === "usage" && workspaceRole === "teacher" && (
          <TeacherUsagePanel
            students={teacherStudents}
            containers={data.containers}
            healthEvents={data.healthEvents}
          />
        )}

        {workspace === "queue" && <GpuAvailabilityPanel servers={data.servers} enabled={data.source === "api"} />}

        {workspace === "audit" && workspaceRole === "admin" && (
          <div className="audit-workspace">
            <ExecutionReconciliationPanel />
            <ActivityPanel />

          </div>
        )}

        {workspace === "activity" && <ActivityPanel />}

        {workspace === "account" && <><AccountPanel /><NotificationSettingsPanel /></>}
        {workspace === "system" && data.actor.role === "admin" && <SystemSettingsPanel data={data} onUpdated={refreshResources} />}
        {workspace === "members" && data.actor.role === "admin" && (
          <MemberManagementPanel
            containers={data.containers}
            currentMemberId={data.actor.memberId}
          />
        )}
        </div>
      </main>
      {showCreateDialog && (
        <AccessibleDialog
          title="创建容器"
          className="create-container-dialog"
          onClose={closeCreateDialog}
          triggerRef={createTriggerRef}
          destructiveBusy={createBusy}
        >
          <p className="create-container-description">选择环境并配置资源。停止的容器不占个人额度；启动时检查运行总额。</p>
          <ContainerCreateWizard
            initialRecipe={reuseRecipe}
            servers={data.servers}
            presets={data.presets}
            images={data.images}
            gpus={data.gpus}
            mountProfiles={data.mountProfiles}
            canCreate={canCreateContainers}
            canPullImages={canPullImages}
            onCancel={closeCreateDialog}
            onComplete={completeContainerCreate}
            onBusyChange={setCreateBusy}
          />
        </AccessibleDialog>
      )}
    </>
  );
}

function canWriteContainers(role: ActorRole): boolean {
  return role === "user" || role === "power_user" || role === "teacher" || role === "admin";
}

function formatRefreshTime(value: Date): string {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(value);
}
