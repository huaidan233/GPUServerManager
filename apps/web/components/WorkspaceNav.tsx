"use client";

import {
  Activity,
  BarChart3,
  Boxes,
  ClipboardCheck,
  GraduationCap,
  LayoutDashboard,
  ListOrdered,
  Printer,
  Settings,
  UserRound,
  Users,
} from "lucide-react";
import { useRouter } from "next/navigation";

import type { ActorRole } from "../lib/mockData";
import {
  workspaceGroups,
  type WorkspaceIcon,
  type WorkspaceId,
} from "../lib/workspaces";


const icons = {
  overview: LayoutDashboard,
  resources: Boxes,
  print: Printer,
  members: Users,
  students: GraduationCap,
  approvals: ClipboardCheck,
  queue: ListOrdered,
  usage: BarChart3,
  activity: Activity,
  system: Settings,
  account: UserRound,
} satisfies Record<WorkspaceIcon, typeof Activity>;

type WorkspaceNavProps = {
  role: ActorRole;
  active: WorkspaceId;
  onSelect: (workspace: WorkspaceId) => void;
};

export function WorkspaceNav({ role, active, onSelect }: WorkspaceNavProps) {
  const router = useRouter();
  const groups = workspaceGroups(role);
  const items = groups.flatMap((group) => group.items);

  function selectWorkspace(workspace: WorkspaceId) {
    const search = new URLSearchParams(window.location.search);
    search.set("workspace", workspace);
    const href = `/?${search.toString()}`;
    onSelect(workspace);
    router.replace(href, { scroll: false });
  }

  return (
    <nav aria-label="工作区">
      <div className="workspace-tabs" role="tablist" aria-label="工作区导航">
        {groups.map((group) => (
          <div
            className="workspace-nav-group"
            role="group"
            aria-labelledby={`workspace-group-${group.id}`}
            key={group.id}
          >
            <div className="workspace-group-heading">
              <span id={`workspace-group-${group.id}`}>{group.label}</span>
              {group.id === "management" && (
                <span className="workspace-group-badge" aria-hidden="true">ADMIN</span>
              )}
            </div>
            <div className="workspace-group-items">
              {group.items.map((item) => {
                const Icon = icons[item.icon];
                const selected = active === item.id;
                const index = items.findIndex((candidate) => candidate.id === item.id);
                return (
                  <button
                    id={`workspace-${item.id}-tab`}
                    key={item.id}
                    type="button"
                    role="tab"
                    aria-selected={selected}
                    aria-controls="workspace-panel"
                    tabIndex={selected ? 0 : -1}
                    onClick={() => selectWorkspace(item.id)}
                    onKeyDown={(event) => {
                      let target = index;
                      if (event.key === "ArrowDown" || event.key === "ArrowRight") target = (index + 1) % items.length;
                      else if (event.key === "ArrowUp" || event.key === "ArrowLeft") target = (index - 1 + items.length) % items.length;
                      else if (event.key === "Home") target = 0;
                      else if (event.key === "End") target = items.length - 1;
                      else return;
                      event.preventDefault();
                      selectWorkspace(items[target].id);
                      document.getElementById(`workspace-${items[target].id}-tab`)?.focus();
                    }}
                  >
                    <Icon size={16} aria-hidden="true" />
                    {item.label}
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </nav>
  );
}
