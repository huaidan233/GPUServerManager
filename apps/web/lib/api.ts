import {
  mockDashboardData,
  type DashboardData,
  type DashboardModuleError,
  containerStatusLabel,
  type GpuState,
  type HealthEventSummary,
  type HealthStatus,
  type MemoryTier,
  type BackupOperationsSummary,
} from "./mockData";

type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

type LoadDashboardOptions = {
  apiUrl?: string;
  cookie?: string;
  origin?: string;
  fetcher?: Fetcher;
};

type LoadSessionOptions = {
  apiUrl?: string;
  cookie?: string;
  fetcher?: Fetcher;
};

export type MemberSession = {
  requires_password_change?: boolean;
  member: {
    id: string;
    username: string;
    display_name: string;
    role: DashboardData["actor"]["role"];
    status: string;
  };
  session: Record<string, boolean | string | null>;
};

export type WorkspaceTask<T extends Record<string, unknown> = Record<string, unknown>> = {
  count: number;
  items: T[];
};

export type WorkspaceOverview = {
  persona: "administrator" | "teacher" | "user";
  generated_at: string;
  tasks: Record<string, WorkspaceTask>;
};

export type ActivityEntry = {
  id: string;
  actor_member_id: string;
  actor_channel: string;
  action_type: string;
  target_type: string;
  target_id: string;
  result: string;
  created_at: string;
};

export type TeacherStudent = {
  id: string;
  username: string;
  display_name: string;
  role: DashboardData["actor"]["role"];
  member_kind: "student";
  supervisor_member_id: string;
  status: string;
  version: number;
  current_program: Record<string, unknown> | null;
  program_history: Array<Record<string, unknown>>;
};

type ApiServer = {
  id: string;
  name: string;
  status?: string;
  gpu_count: number;
  cpu_total?: number;
  memory_total_gb?: number;
};

type ApiPreset = {
  name: string;
  cpu: number;
  memory_gb: number;
};

type ApiImage = {
  digest?: string;
  release_date?: string;
  minimum_driver?: string;
  security_status?: string;
  security_notes?: string[];
  applications?: string[];
  clean_cuda?: boolean;
  recommended?: boolean;
  creation_enabled?: boolean;
  name: string;
  display_name: string;
  tag?: string;
  environment?: "python" | "go" | "cuda" | "ubuntu" | "python-base";
  runtime_version?: string;
  packages?: string[];
  description?: string;
  gpu_ready?: boolean;
  server_ids?: string[];
};

type ApiMountProfile = {
  id: string;
  name: string;
  container_path: string;
};

type ApiGpu = {
  index: number;
  memory_total_mb: number;
  memory_used_mb: number;
  utilization: number;
  governance_state?: GpuState;
  data_freshness?: string;
  processes?: Array<Record<string, unknown>>;
  declared_containers?: Array<{
    container_id: string;
    container_name?: string;
    user_id: string;
  }>;
  containers?: Array<{
    container_name: string;
    user_id: string;
  }>;
};

type ApiRecommendation = {
  server_id: string;
  gpu_uuid: string;
  index: number;
  state: GpuState;
  safe_to_share: boolean;
  memory_free_mb: number;
  reason_codes: string[];
};

type ApiQueue = {
  id: string;
  user_id: string;
  gpu_count: number;
  memory_tier: MemoryTier;
  estimated_duration_minutes: number;
  compatible_server_ids: string[];
  status: string;
  created_at: string;
};

type ApiApproval = {
  id: string;
  requester_user_id: string;
  owner_user_id: string;
  status: string;
  trigger_reasons: string[];
  created_at: string;
  expires_at: string;
  frozen_container_spec?: import("./mockData").ApprovalSummary["specification"];
  reviewer_user_id?: string;
  review_reason?: string;
  decided_at?: string;
  container_id?: string;
};

type ApiContainer = {
  id: string;
  name: string;
  user_id: string;
  server_id: string;
  cpu_limit: number;
  memory_limit_gb: number;
  gpu_devices?: string[];
  status: string;
  ssh_command?: string | null;
  owner_member_id?: string | null;
  version?: number;
  mounts?: Array<{
    host_path: string;
    container_path: string;
    mode: string;
  }>;
};

type ApiActor = {
  id?: string;
  username?: string;
  display_name?: string;
  status?: string;
  user_id?: string;
  role: DashboardData["actor"]["role"];
};

type ApiHealth = {
  server_id: string;
  status: HealthStatus;
  warning_count: number;
  critical_count: number;
  observed_at: string;
  stale: boolean;
};

type ApiHealthEvent = {
  id: string;
  code: string;
  scope: "infrastructure" | "container";
  server_id: string;
  container_id?: string | null;
  owner_user_id?: string | null;
  severity: string;
  status: "open" | "recovered";
  opened_at: string;
  last_seen_at: string;
  evidence?: Record<string, unknown>;
};

type ApiBackupStatus = {
  backend: "local" | "oss";
  inventory: Array<{ key: string; size_bytes: number; modified_at: string }>;
  runs: Array<{
    id: string;
    key?: string;
    kind: string;
    status: "success" | "failed" | "running";
    size_bytes?: number;
    integrity_status?: "valid" | "invalid";
    finished_at?: string;
    message?: string;
  }>;
  latest_success?: ApiBackupStatus["runs"][number] | null;
  latest_drill?: { id: string; status: "success" | "failed"; checked_at?: string } | null;
};

export async function loadDashboardData(
  options: LoadDashboardOptions = {},
): Promise<DashboardData> {
  const apiUrl = normalizeApiUrl(
    options.apiUrl ?? process.env.GSM_WEB_API_URL ?? process.env.GSM_API_URL ?? "http://api:8000",
  );
  const fetcher = options.fetcher ?? fetch;
  const headers: Record<string, string> = options.cookie ? { Cookie: options.cookie } : {};
  const csrfToken = readCookie(options.cookie, "gsm_csrf");
  const mutationHeaders = {
    ...headers,
    ...(options.origin ? { Origin: options.origin } : {}),
    ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
    "Content-Type": "application/json",
  };

  try {
    const [
      actorResult,
      serversResult,
      presetsResult,
      imagesResult,
      mountProfilesResult,
      containersResult,
      recommendationResult,
      queueResult,
      approvalsResult,
      healthResult,
      healthEventResult,
    ] = await Promise.allSettled([
      fetchJson<ApiActor>(fetcher, `${apiUrl}/me`, headers),
      fetchJson<ApiServer[]>(fetcher, `${apiUrl}/servers`, headers),
      fetchJson<ApiPreset[]>(fetcher, `${apiUrl}/presets`, headers),
      fetchJson<ApiImage[]>(fetcher, `${apiUrl}/images`, headers),
      fetchJson<ApiMountProfile[]>(fetcher, `${apiUrl}/mount-profiles`, headers),
      fetchJson<ApiContainer[]>(fetcher, `${apiUrl}/containers`, headers),
      fetchJson<{ recommendations: ApiRecommendation[] }>(
        fetcher,
        `${apiUrl}/governance/recommendations`,
        mutationHeaders,
        {
          method: "POST",
          body: JSON.stringify({
            gpu_count: 1,
            memory_tier: "8_24",
            estimated_duration_minutes: 120,
          }),
        },
      ),
      fetchJson<ApiQueue[]>(fetcher, `${apiUrl}/governance/queue`, headers),
      fetchJson<ApiApproval[]>(fetcher, `${apiUrl}/governance/approvals`, headers),
      fetchJson<{ servers: ApiHealth[] }>(fetcher, `${apiUrl}/operations/health`, headers),
      fetchJson<{ events: ApiHealthEvent[] }>(fetcher, `${apiUrl}/operations/health/events`, headers),
    ]);
    if (actorResult.status === "rejected" || serversResult.status === "rejected") {
      throw new Error("Central API core resources are unavailable");
    }
    const actor = actorResult.value;
    const servers = serversResult.value;
    const moduleErrors: DashboardModuleError[] = [];
    const presets = settledModuleValue(presetsResult, [], "资源套餐", moduleErrors);
    const images = settledModuleValue(imagesResult, [], "镜像目录", moduleErrors);
    const mountProfiles = settledModuleValue(mountProfilesResult, [], "挂载配置", moduleErrors);
    const containers = settledModuleValue(containersResult, [], "容器列表", moduleErrors);
    const recommendationPayload = settledModuleValue(recommendationResult, { recommendations: [] }, "GPU 推荐", moduleErrors);
    const queue = settledModuleValue(queueResult, [], "候补队列", moduleErrors);
    const approvals = settledModuleValue(approvalsResult, [], "审批列表", moduleErrors);
    const healthResultValue = settledModuleValue(healthResult, { servers: [] }, "服务器健康", moduleErrors);
    const healthEventResultValue = settledModuleValue(healthEventResult, { events: [] }, "健康事件", moduleErrors);
    let backupResult: ApiBackupStatus | null = null;
    if (actor.role === "admin") {
      const backupSettled = await Promise.allSettled([fetchJson<ApiBackupStatus>(fetcher, `${apiUrl}/operations/backups`, headers)]);
      backupResult = settledModuleValue(backupSettled[0], null, "备份状态", moduleErrors);
    }
    const gpuResults = await Promise.allSettled(
      servers.map((server) => fetchJson<ApiGpu[]>(fetcher, `${apiUrl}/servers/${server.id}/gpus`, headers)),
    );
    const gpus = gpuResults.flatMap((result, index) => {
      if (result.status === "rejected") {
        moduleErrors.push({ module: `${servers[index].name} GPU`, message: "暂时无法加载" });
        return [];
      }
      return result.value.map((gpu) => ({ gpu, server: servers[index] }));
    });

    return {
      source: "api",
      actor: {
        userId: actor.username ?? actor.user_id ?? actor.id ?? "unknown",
        memberId: actor.id,
        displayName: actor.display_name,
        role: actor.role,
        status: actor.status,
      },
      metrics: buildMetrics(servers, gpus, containers),
      servers: servers.map((server) => ({
        id: server.id,
        name: server.name,
        gpuCount: server.gpu_count,
        cpuTotal: server.cpu_total,
        memoryTotalGb: server.memory_total_gb,
      })),
      presets: presets.map((preset) => ({
        name: preset.name,
        cpu: preset.cpu,
        memoryGb: preset.memory_gb,
      })),
      images: images.map((image) => ({
        digest: image.digest, releaseDate: image.release_date, minimumDriver: image.minimum_driver,
        securityStatus: image.security_status, securityNotes: image.security_notes, applications: image.applications,
        cleanCuda: image.clean_cuda, recommended: image.recommended, creationEnabled: image.creation_enabled,
        name: image.tag ? `${image.name}:${image.tag}` : image.name,
        displayName: image.display_name,
        environment: image.environment,
        runtimeVersion: image.runtime_version,
        packages: image.packages,
        description: image.description,
        gpuReady: image.gpu_ready,
        serverIds: image.server_ids,
      })),
      gpus: gpus.map(({ gpu, server }) => {
        const declarations = gpu.declared_containers ?? gpu.containers ?? [];
        const displayContainers = gpu.containers ?? declarations;
        const users = displayContainers.map((container) => container.user_id);
        const firstContainer = displayContainers[0];
        return {
          serverId: server.id,
          serverName: server.name,
          index: gpu.index,
          user: firstContainer?.user_id,
          container: firstContainer?.container_name,
          memoryUsedGb: Math.round(gpu.memory_used_mb / 1024 * 100) / 100,
          memoryTotalGb: Math.round(gpu.memory_total_mb / 1024 * 100) / 100,
          utilization: gpu.utilization,
          sharedBy: users,
          state: gpu.governance_state ?? "unknown",
          processCount: gpu.processes?.length ?? 0,
          declaredContainerCount: declarations.length,
          freshness: gpu.data_freshness ?? "unknown",
        };
      }),
      mountProfiles: mountProfiles.map((mountProfile, index) => ({
        id: mountProfile.id,
        name: mountProfile.name || mountProfile.id,
        containerPath: mountProfile.container_path,
        defaultChecked: index === 0 || mountProfile.container_path === "/workspace",
      })),
      containers: containers.map((container) => ({
        id: container.id,
        name: container.name,
        user: container.user_id,
        server: container.server_id,
        cpu: container.cpu_limit,
        memoryGb: container.memory_limit_gb,
        gpus: container.gpu_devices ?? [],
        status: container.status,
        statusText: containerStatusLabel(container.status),
        sshCommand: container.ssh_command ?? undefined,
        ownerMemberId: container.owner_member_id ?? undefined,
        version: container.version ?? 1,
        mounts: (container.mounts ?? []).map((mount) => ({
          hostPath: mount.host_path,
          containerPath: mount.container_path,
          mode: mount.mode,
        })),
      })),
      moduleErrors,
      recommendations: recommendationPayload.recommendations.map((item) => ({
        serverId: item.server_id,
        gpuUuid: item.gpu_uuid,
        index: item.index,
        state: item.state,
        safeToShare: item.safe_to_share,
        memoryFreeGb: item.memory_free_mb / 1024,
        reasonCodes: item.reason_codes,
      })),
      queue: queue.map((item, index) => ({
        id: item.id,
        userId: item.user_id,
        gpuCount: item.gpu_count,
        memoryTier: item.memory_tier,
        estimatedDurationMinutes: item.estimated_duration_minutes,
        compatibleServerIds: item.compatible_server_ids,
        status: item.status,
        createdAt: item.created_at,
        position: index + 1,
      })),
      approvals: approvals.map((item) => ({
        id: item.id,
        requesterUserId: item.requester_user_id,
        ownerUserId: item.owner_user_id,
        status: item.status,
        triggerReasons: item.trigger_reasons,
        createdAt: item.created_at,
        expiresAt: item.expires_at,
        specification: item.frozen_container_spec,
        reviewerUserId: item.reviewer_user_id,
        reviewReason: item.review_reason,
        decidedAt: item.decided_at,
        containerId: item.container_id,
      })),
      health: healthResultValue.servers.map((item) => ({
        serverId: item.server_id,
        status: item.status,
        warningCount: item.warning_count,
        criticalCount: item.critical_count,
        observedAt: item.observed_at,
        stale: item.stale,
      })),
      healthEvents: healthEventResultValue.events.map<HealthEventSummary>((item) => ({
        id: item.id,
        code: item.code,
        scope: item.scope,
        serverId: item.server_id,
        containerId: item.container_id ?? undefined,
        ownerUserId: item.owner_user_id ?? undefined,
        severity: item.severity,
        status: item.status,
        openedAt: item.opened_at,
        lastSeenAt: item.last_seen_at,
        evidence: item.evidence,
      })),
      backups: backupResult ? mapBackups(backupResult) : null,
    };
  } catch {
    if (process.env.NODE_ENV === "production") {
      throw new Error("Central API is unavailable");
    }
    return mockDashboardData;
  }
}

export async function loadMemberSession(
  options: LoadSessionOptions = {},
): Promise<MemberSession | null> {
  const apiUrl = normalizeApiUrl(
    options.apiUrl ?? process.env.GSM_WEB_API_URL ?? process.env.GSM_API_URL ?? "http://api:8000",
  );
  const headers: Record<string, string> = options.cookie ? { Cookie: options.cookie } : {};
  const response = await (options.fetcher ?? fetch)(`${apiUrl}/auth/session`, {
    method: "GET",
    headers,
    cache: "no-store",
  });
  if (response.status === 401) {
    return null;
  }
  if (!response.ok) {
    throw new Error("Central API is unavailable");
  }
  try {
    return (await response.json()) as MemberSession;
  } catch {
    throw new Error("Central API is unavailable");
  }
}

export async function loadWorkspaceOverview(
  options: LoadSessionOptions = {},
): Promise<WorkspaceOverview> {
  return loadSessionResource("/workspace/overview", options);
}

export async function loadTeacherStudents(
  options: LoadSessionOptions = {},
): Promise<TeacherStudent[]> {
  return loadSessionResource("/teacher/students", options);
}

export function mapBackups(value: ApiBackupStatus): BackupOperationsSummary {
  return {
    backend: value.backend,
    inventory: value.inventory.map((item) => ({
      key: item.key,
      sizeBytes: item.size_bytes,
      modifiedAt: item.modified_at,
    })),
    runs: value.runs.map((item) => ({
      id: item.id,
      key: item.key,
      kind: item.kind,
      status: item.status,
      sizeBytes: item.size_bytes,
      integrityStatus: item.integrity_status,
      finishedAt: item.finished_at,
      message: item.message,
    })),
    latestSuccess: value.latest_success
      ? {
          id: value.latest_success.id,
          key: value.latest_success.key,
          kind: value.latest_success.kind,
          status: value.latest_success.status,
          sizeBytes: value.latest_success.size_bytes,
          integrityStatus: value.latest_success.integrity_status,
          finishedAt: value.latest_success.finished_at,
          message: value.latest_success.message,
        }
      : null,
    latestDrill: value.latest_drill
      ? {
          id: value.latest_drill.id,
          status: value.latest_drill.status,
          checkedAt: value.latest_drill.checked_at,
        }
      : null,
  };
}

function normalizeApiUrl(apiUrl: string): string {
  const trimmed = apiUrl.replace(/\/+$/, "");
  return trimmed.endsWith("/api") ? trimmed : `${trimmed}/api`;
}

function readCookie(cookieHeader: string | undefined, name: string): string | undefined {
  const prefix = `${name}=`;
  const raw = cookieHeader
    ?.split(";")
    .map((item) => item.trim())
    .find((item) => item.startsWith(prefix))
    ?.slice(prefix.length);
  if (!raw) {
    return undefined;
  }
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

async function fetchJson<T>(
  fetcher: Fetcher,
  url: string,
  headers: Record<string, string>,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetcher(url, {
    ...init,
    headers,
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(`Central API request failed: ${response.status}`);
  }
  return (await response.json()) as T;
}

function settledModuleValue<T>(
  result: PromiseSettledResult<T>,
  fallback: T,
  module: string,
  errors: DashboardModuleError[],
): T {
  if (result.status === "fulfilled") return result.value;
  errors.push({ module, message: "暂时无法加载" });
  return fallback;
}

async function loadSessionResource<T>(
 path: string,
  options: LoadSessionOptions,
): Promise<T> {
  const apiUrl = normalizeApiUrl(
    options.apiUrl ?? process.env.GSM_WEB_API_URL ?? process.env.GSM_API_URL ?? "http://api:8000",
  );
  const headers: Record<string, string> = options.cookie ? { Cookie: options.cookie } : {};
  return fetchJson<T>(options.fetcher ?? fetch, `${apiUrl}${path}`, headers);
}

function buildMetrics(
  servers: ApiServer[],
  gpus: Array<{ gpu: ApiGpu; server: ApiServer }>,
  containers: ApiContainer[],
) {
  const onlineServers = servers.filter((server) => server.status !== "offline").length;
  const activeGpus = gpus.filter(
    ({ gpu }) =>
      (gpu.processes ?? []).length > 0 || ["busy", "exclusive"].includes(gpu.governance_state ?? ""),
  ).length;
  const sharedGpus = gpus.filter(({ gpu }) => (gpu.processes ?? []).length > 1).length;
  const runningContainers = containers.filter((container) => container.status === "running").length;

  return [
    {
      label: "Servers" as const,
      value: String(servers.length),
      detail: `${onlineServers} online`,
      icon: "server" as const,
    },
    {
      label: "GPUs" as const,
      value: String(gpus.length),
      detail: `${activeGpus} active, ${sharedGpus} shared`,
      icon: "cpu" as const,
    },
    {
      label: "Containers" as const,
      value: String(containers.length),
      detail: `${runningContainers} running`,
      icon: "boxes" as const,
    },
    {
      label: "Alerts" as const,
      value: String(sharedGpus),
      detail: "GPU sharing reminders",
      icon: "bell" as const,
    },
  ];
}
