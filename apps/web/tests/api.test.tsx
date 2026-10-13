import { afterEach, describe, expect, test, vi } from "vitest";

import { loadDashboardData, loadMemberSession } from "../lib/api";

afterEach(() => {
  vi.unstubAllEnvs();
});

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as Response;
}

describe("loadDashboardData", () => {
  test("loads and maps Central API dashboard data", async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input.toString();
      expect(init?.headers).toMatchObject({
        Cookie: "gsm_session=session; gsm_csrf=csrf",
      });
      expect(init?.headers).not.toHaveProperty("Authorization");

      if (url.endsWith("/api/servers")) {
        return jsonResponse([
          { id: "server1", name: "Server1", status: "online", gpu_count: 2 },
          { id: "server2", name: "Server2", status: "online", gpu_count: 1 },
        ]);
      }
      if (url.endsWith("/api/me")) {
        return jsonResponse({
          id: "member-admin",
          username: "admin",
          display_name: "Lab Admin",
          role: "admin",
          status: "active",
          current_program: null,
        });
      }
      if (url.endsWith("/api/servers/server1/gpus")) {
        return jsonResponse([
          {
            index: 0,
            memory_total_mb: 49152,
            memory_used_mb: 12288,
            utilization: 75,
            governance_state: "shareable",
            data_freshness: "fresh",
            processes: [{ pid: 1234 }],
            declared_containers: [
              { container_id: "c1", user_id: "alice" },
              { container_id: "c2", user_id: "bob" },
            ],
            containers: [
              {
                container_name: "alice-train",
                user_id: "alice",
              },
              {
                container_name: "bob-debug",
                user_id: "bob",
              },
            ],
          },
        ]);
      }
      if (url.endsWith("/api/servers/server2/gpus")) {
        return jsonResponse([
          {
            index: 0,
            memory_total_mb: 24576,
            memory_used_mb: 0,
            utilization: 0,
            governance_state: "free",
            data_freshness: "fresh",
            processes: [],
            declared_containers: [],
            containers: [],
          },
        ]);
      }
      if (url.endsWith("/api/presets")) {
        return jsonResponse([{ name: "Debug", cpu: 4, memory_gb: 16 }]);
      }
      if (url.endsWith("/api/images")) {
        return jsonResponse([
          { name: "gsm/pytorch-cuda-ssh", display_name: "PyTorch CUDA SSH", tag: "latest" },
        ]);
      }
      if (url.endsWith("/api/mount-profiles")) {
        return jsonResponse([
          { id: "workspace-default", name: "Workspace", container_path: "/workspace" },
        ]);
      }
      if (url.endsWith("/api/containers")) {
        return jsonResponse([
          {
            id: "c1",
            name: "alice-train",
            user_id: "alice",
            server_id: "server1",
            cpu_limit: 8,
            memory_limit_gb: 32,
            gpu_devices: ["0"],
            status: "running",
            ssh_command: "ssh researcher@Server1 -p 22001",
            owner_member_id: "member-alice",
            version: 7,
            mounts: [
              { host_path: "/data/alice", container_path: "/workspace", mode: "rw" },
            ],
          },
        ]);
      }
      if (url.endsWith("/api/governance/recommendations")) {
        expect(init?.method).toBe("POST");
        expect(init?.headers).toMatchObject({
          Origin: "https://gsm.test",
          "X-CSRF-Token": "csrf",
        });
        return jsonResponse({
          recommendations: [
            {
              server_id: "server1",
              gpu_uuid: "GPU-a",
              index: 0,
              state: "shareable",
              safe_to_share: true,
              memory_free_mb: 32768,
              reason_codes: ["mounted_without_process", "advisory_only"],
            },
          ],
        });
      }
      if (url.endsWith("/api/governance/queue")) {
        return jsonResponse([
          {
            id: "q1",
            user_id: "alice",
            gpu_count: 1,
            memory_tier: "8_24",
            estimated_duration_minutes: 120,
            compatible_server_ids: ["server1"],
            status: "waiting",
            created_at: "2026-08-04T00:00:00Z",
          },
        ]);
      }
      if (url.endsWith("/api/governance/approvals")) {
        return jsonResponse([
          {
            id: "a1",
            requester_user_id: "alice",
            owner_user_id: "alice",
            status: "pending",
            trigger_reasons: ["gpu_count"],
            created_at: "2026-08-04T00:00:00Z",
            expires_at: "2026-08-05T00:00:00Z",
          },
        ]);
      }
      if (url.endsWith("/api/operations/health")) {
        return jsonResponse({
          servers: [
            {
              server_id: "server1",
              status: "degraded",
              warning_count: 1,
              critical_count: 0,
              observed_at: "2026-08-04T00:00:00Z",
              stale: false,
            },
          ],
        });
      }
      if (url.endsWith("/api/operations/health/events")) {
        return jsonResponse({ events: [] });
      }
      if (url.endsWith("/api/operations/backups")) {
        return jsonResponse({
          backend: "local",
          inventory: [
            { key: "daily.tar.gz", size_bytes: 1024, modified_at: "2026-08-04T18:00:00Z" },
          ],
          runs: [
            {
              id: "backup-run-1",
              key: "daily.tar.gz",
              kind: "daily",
              status: "success",
              size_bytes: 1024,
              integrity_status: "valid",
              finished_at: "2026-08-04T18:00:00Z",
            },
          ],
          latest_success: {
            id: "backup-run-1",
            key: "daily.tar.gz",
            kind: "daily",
            status: "success",
            size_bytes: 1024,
            integrity_status: "valid",
            finished_at: "2026-08-04T18:00:00Z",
          },
          latest_drill: null,
        });
      }

      throw new Error(`unexpected URL ${url}`);
    });

    const data = await loadDashboardData({
      apiUrl: "http://api:8000",
      cookie: "gsm_session=session; gsm_csrf=csrf",
      origin: "https://gsm.test",
      fetcher,
    });

    expect(fetcher).toHaveBeenCalledTimes(14);
    expect(data.source).toBe("api");
    expect(data.actor).toEqual({
      userId: "admin",
      memberId: "member-admin",
      displayName: "Lab Admin",
      role: "admin",
      status: "active",
    });
    expect(data.metrics).toEqual([
      { label: "Servers", value: "2", detail: "2 online", icon: "server" },
      { label: "GPUs", value: "2", detail: "1 active, 0 shared", icon: "cpu" },
      { label: "Containers", value: "1", detail: "1 running", icon: "boxes" },
      { label: "Alerts", value: "0", detail: "GPU sharing reminders", icon: "bell" },
    ]);
    expect(data.gpus[0]).toMatchObject({
      serverId: "server1",
      serverName: "Server1",
      index: 0,
      user: "alice",
      container: "alice-train",
      memoryUsedGb: 12,
      memoryTotalGb: 48,
      utilization: 75,
      sharedBy: ["alice", "bob"],
      state: "shareable",
      processCount: 1,
      declaredContainerCount: 2,
      freshness: "fresh",
    });
    expect(data.gpus[1]).toMatchObject({
      serverId: "server2",
      serverName: "Server2",
      index: 0,
      state: "free",
      memoryTotalGb: 24,
    });
    expect(data.containers[0]).toMatchObject({
      name: "alice-train",
      user: "alice",
      statusText: "运行中",
      server: "server1",
      sshCommand: "ssh researcher@Server1 -p 22001",
      ownerMemberId: "member-alice",
      version: 7,
      mounts: [
        { hostPath: "/data/alice", containerPath: "/workspace", mode: "rw" },
      ],
    });
    expect(data.recommendations[0]).toMatchObject({
      gpuUuid: "GPU-a",
      state: "shareable",
      memoryFreeGb: 32,
    });
    expect(data.queue[0]).toMatchObject({ id: "q1", userId: "alice", position: 1 });
    expect(data.approvals[0]).toMatchObject({ id: "a1", status: "pending" });
    expect(data.health[0]).toMatchObject({ serverId: "server1", status: "degraded" });
    expect(data.backups).toMatchObject({
      backend: "local",
      runs: [{ id: "backup-run-1", integrityStatus: "valid" }],
      latestSuccess: { id: "backup-run-1", integrityStatus: "valid" },
    });
  });

  test("keeps core dashboard data when optional modules fail", async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url.endsWith("/api/me")) return jsonResponse({ id: "m1", username: "alice", role: "user", status: "active" });
      if (url.endsWith("/api/servers")) return jsonResponse([{ id: "server1", name: "Server1", gpu_count: 1 }, { id: "server2", name: "Server2", gpu_count: 1 }]);
      if (url.endsWith("/api/servers/server1/gpus")) return jsonResponse([{ index: 0, memory_total_mb: 24576, memory_used_mb: 0, utilization: 0, governance_state: "free", processes: [], containers: [] }]);
      if (url.endsWith("/api/servers/server2/gpus")) throw new Error("server2 unavailable");
      if (url.endsWith("/api/images")) throw new Error("image catalog unavailable");
      if (url.endsWith("/api/presets")) return jsonResponse([]);
      if (url.endsWith("/api/mount-profiles")) return jsonResponse([]);
      if (url.endsWith("/api/containers")) return jsonResponse([]);
      if (url.endsWith("/api/governance/recommendations")) return jsonResponse({ recommendations: [] });
      if (url.endsWith("/api/governance/queue")) return jsonResponse([]);
      if (url.endsWith("/api/governance/approvals")) return jsonResponse([]);
      if (url.endsWith("/api/operations/health")) return jsonResponse({ servers: [] });
      if (url.endsWith("/api/operations/health/events")) return jsonResponse({ events: [] });
      throw new Error(`unexpected URL ${url}`);
    });

    const data = await loadDashboardData({ apiUrl: "http://api:8000", cookie: "gsm_session=session", fetcher });

    expect(data.source).toBe("api");
    expect(data.actor.userId).toBe("alice");
    expect(data.gpus).toHaveLength(1);
    expect(data.images).toEqual([]);
    expect(data.containers).toEqual([]);
    expect(data.moduleErrors).toEqual(expect.arrayContaining([
      { module: "镜像目录", message: "暂时无法加载" },
      { module: "Server2 GPU", message: "暂时无法加载" },
    ]));
  });

  test("falls back to mock dashboard data when Central API is unavailable", async () => {
    const data = await loadDashboardData({
      apiUrl: "http://api:8000",
      cookie: "gsm_session=session; gsm_csrf=csrf",
      fetcher: async () => {
        throw new Error("API unavailable");
      },
    });

    expect(data.source).toBe("mock");
    expect(data.servers.length).toBeGreaterThan(0);
    expect(data.gpus.length).toBeGreaterThan(0);
  });

  test("does not expose mock administrator data in production", async () => {
    vi.stubEnv("NODE_ENV", "production");

    await expect(loadDashboardData({
      apiUrl: "http://api:8000",
      cookie: "gsm_session=session; gsm_csrf=csrf",
      fetcher: async () => {
        throw new Error("sensitive Central API failure");
      },
    })).rejects.toThrow("Central API is unavailable");
  });
});

describe("loadMemberSession", () => {
  test("checks the Central session with the incoming cookie", async () => {
    const fetcher = vi.fn(async () => Response.json({
      member: { id: "m1", username: "alice", display_name: "Alice", role: "user", status: "active" },
      session: { id: "s1" },
    }));

    const session = await loadMemberSession({
      apiUrl: "http://api:8000",
      cookie: "gsm_session=session; gsm_csrf=csrf",
      fetcher,
    });

    expect(session?.member.username).toBe("alice");
    expect(fetcher).toHaveBeenCalledWith("http://api:8000/api/auth/session", {
      method: "GET",
      headers: { Cookie: "gsm_session=session; gsm_csrf=csrf" },
      cache: "no-store",
    });
  });

  test("returns null only for an unauthenticated session", async () => {
    await expect(loadMemberSession({
      fetcher: async () => Response.json({ detail: "authentication required" }, { status: 401 }),
    })).resolves.toBeNull();

    await expect(loadMemberSession({
      fetcher: async () => Response.json({ detail: "database unavailable" }, { status: 503 }),
    })).rejects.toThrow("Central API is unavailable");
  });
});
