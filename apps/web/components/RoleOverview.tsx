import { ChevronRight } from "lucide-react";

import type { WorkspaceOverview } from "../lib/api";
import type { ActorSummary } from "../lib/mockData";
import type { WorkspaceId } from "../lib/workspaces";


type TaskDisplay = {
  label: (count: number) => string;
  workspace: WorkspaceId;
};

const taskDisplays: Record<string, TaskDisplay> = {
  pending_approvals: {
    label: (count) => `${count} 项待审批申请`,
    workspace: "approvals",
  },
  unassigned_students: {
    label: (count) => `${count} 名未分配导师学生`,
    workspace: "members",
  },
  expiring_students: {
    label: (count) => `${count} 名临近毕业学生`,
    workspace: "members",
  },
  active_health_events: {
    label: (count) => `${count} 条活跃告警`,
    workspace: "audit",
  },
  backup_failures: {
    label: (count) => `${count} 次失败备份`,
    workspace: "system",
  },
  recent_high_risk_operations: {
    label: (count) => `${count} 条高风险操作`,
    workspace: "audit",
  },
  assigned_expiring_students: {
    label: (count) => `${count} 名学生临近毕业`,
    workspace: "students",
  },
  assigned_pending_approvals: {
    label: (count) => `${count} 项待我复审的额度申请`,
    workspace: "approvals",
  },
  assigned_active_containers: {
    label: (count) => `${count} 个学生活跃容器`,
    workspace: "usage",
  },
  relevant_health_events: {
    label: (count) => `${count} 条相关告警`,
    workspace: "usage",
  },
  owned_containers: {
    label: (count) => `${count} 个自有容器`,
    workspace: "resources",
  },
  queue_requests: {
    label: (count) => `${count} 项 GPU 空闲订阅`,
    workspace: "queue",
  },
  active_warnings: {
    label: (count) => `${count} 条资源告警`,
    workspace: "resources",
  },
};

type RoleOverviewProps = {
  actor: ActorSummary;
  overview: WorkspaceOverview;
  onNavigate: (workspace: WorkspaceId) => void;
};

export function RoleOverview({ actor, overview, onNavigate }: RoleOverviewProps) {
  const tasks = Object.entries(overview.tasks)
    .map(([key, task]) => ({ key, task, display: key === "pending_approvals" && overview.persona === "user"
      ? { label: (count: number) => `${count} 项我的申请待审批`, workspace: "approvals" as const }
      : taskDisplays[key] }))
    .filter((item): item is typeof item & { display: TaskDisplay } => Boolean(item.display));

  return (
    <section
      className="role-overview"
      aria-labelledby="role-overview-heading"
      data-role={actor.role}
    >
      <div className="workspace-heading">
        <h2 id="role-overview-heading">需要关注</h2>
        <time dateTime={overview.generated_at}>
          {formatTime(overview.generated_at)}
        </time>
      </div>
      {tasks.length === 0 ? (
        <p className="empty-state">当前无待办</p>
      ) : (
        <div className="task-list">
          {tasks.map(({ key, task, display }) => (
            <div className="task-row" key={key}>
              <div className="task-card-content"><span>{display.label(task.count)}</span><strong aria-hidden="true">{task.count.toString().padStart(2, "0")}</strong><small>{task.count > 0 ? "查看详情并处理" : "暂无相关事项"}</small></div>
              <button
                className="icon-button"
                type="button"
                aria-label={`查看${display.label(task.count)}`}
                title="查看"
                onClick={() => onNavigate(display.workspace)}
              >
                <ChevronRight size={17} aria-hidden="true" />
              </button>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function formatTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? ""
    : new Intl.DateTimeFormat("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      }).format(parsed);
}
