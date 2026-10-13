import { expect, test } from "vitest";

import {
  effectiveWorkspaceRole,
  resolveWorkspace,
  workspaceGroups,
  workspaceItems,
  type WorkspaceId,
} from "../lib/workspaces";

const allWorkspaces: WorkspaceId[] = [
  "overview", "resource-overview", "resources", "data", "print", "members", "students", "approvals",
  "queue", "usage", "activity", "audit", "system", "account",
];

test.each([
  ["admin", ["overview", "resource-overview", "resources", "data", "queue", "members", "approvals", "audit", "system", "print", "account"]],
  ["teacher", ["overview", "resource-overview", "resources", "data", "print", "students", "approvals", "usage", "queue", "account"]],
  ["user", ["overview", "resource-overview", "resources", "data", "print", "queue", "approvals", "activity", "account"]],
  ["power_user", ["overview", "resource-overview", "resources", "data", "print", "queue", "approvals", "activity", "account"]],
  ["viewer", ["overview", "resource-overview", "resources", "data", "queue", "approvals", "activity", "account"]],
] as const)("%s uses the same unique allowed pages for navigation and route resolution", (role, expected) => {
  const groups = workspaceGroups(role);
  const items = workspaceItems(role);
  expect(groups.flatMap((group) => group.items)).toEqual(items);
  expect(items.map((item) => item.id)).toEqual(expected);
  expect(new Set(items.map((item) => item.id)).size).toBe(items.length);
  expect(new Set(groups.map((group) => group.id)).size).toBe(groups.length);

  for (const requested of allWorkspaces) {
    const allowed = expected.some((workspace) => workspace === requested);
    expect(resolveWorkspace(role, requested)).toBe(allowed ? requested : "overview");
  }
  expect(resolveWorkspace(role, "unknown")).toBe("overview");
  expect(resolveWorkspace(role, undefined)).toBe("overview");
});

test.each(["admin", "user", "power_user"] as const)(
  "%s finds container creation in the shared user group, not administration",
  (role) => {
    const groups = workspaceGroups(role);
    expect(groups.find((group) => group.id === "research")?.items.map((item) => item.id))
      .toContain("resources");
    expect(groups.find((group) => group.id === "management")?.items.map((item) => item.id) ?? [])
      .not.toContain("resources");
  },
);

test.each(["pending_exit", "disabled", "pending"])(
  "an administrator with %s status cannot regain management pages through a group or route",
  (status) => {
    const role = effectiveWorkspaceRole("admin", status);
    expect(role).toBe("viewer");
    expect(workspaceGroups(role).map((group) => group.id)).toEqual(["research", "personal"]);
    for (const requested of ["system", "members", "audit", "print"]) {
      expect(resolveWorkspace(role, requested)).toBe("overview");
    }
  },
);
