import type { GpuCardProps } from "../components/GpuCard";

export type ServerSummary = {
  id: string;
  name: string;
  gpuCount: number;
  cpuTotal?: number;
  memoryTotalGb?: number;
};

export type Preset = {
  name: string;
  cpu: number;
  memoryGb: number;
};

export type ImageSummary = {
  digest?: string;
  releaseDate?: string;
  minimumDriver?: string;
  securityStatus?: string;
  securityNotes?: string[];
  applications?: string[];
  cleanCuda?: boolean;
  recommended?: boolean;
  creationEnabled?: boolean;
  name: string;
  displayName: string;
  environment?: "python" | "go" | "cuda" | "ubuntu" | "python-base";
  runtimeVersion?: string;
  packages?: string[];
  description?: string;
  gpuReady?: boolean;
  serverIds?: string[];
};

export type GpuSummary = GpuCardProps & {
  serverId?: string;
  serverName?: string;
};

export type GpuState = "free" | "shareable" | "busy" | "exclusive" | "unknown";
export type MemoryTier = "lt_8" | "8_24" | "24_48" | "full_card";

export type GpuRecommendation = {
  serverId: string;
  gpuUuid: string;
  index: number;
  state: GpuState;
  safeToShare: boolean;
  memoryFreeGb: number;
  reasonCodes: string[];
};

export type QueueSummary = {
  id: string;
  userId: string;
  gpuCount: number;
  memoryTier: MemoryTier;
  estimatedDurationMinutes: number;
  compatibleServerIds: string[];
  status: string;
  createdAt: string;
  position: number;
};

export type ApprovalSummary = {
  id: string;
  requesterUserId: string;
  ownerUserId: string;
  status: string;
  triggerReasons: string[];
  createdAt: string;
  expiresAt: string;
  specification?: {
    name: string; server_id: string; image: string; preset: string;
    cpu_limit: number; memory_limit_gb: number; gpu_devices: string[];
    mount_profile_ids: string[]; ssh_enabled: boolean; auto_start?: boolean;
    memory_tier?: string; estimated_duration_minutes?: number; note?: string;
  };
  reviewerUserId?: string;
  reviewReason?: string;
  decidedAt?: string;
  containerId?: string;
};

export type HealthStatus = "ok" | "degraded" | "failed" | "unknown";

export type HealthSummary = {
  serverId: string;
  status: HealthStatus;
  warningCount: number;
  criticalCount: number;
  observedAt: string;
  stale: boolean;
};

export type HealthEventSummary = {
  id: string;
  code: string;
  scope: "infrastructure" | "container";
  serverId: string;
  containerId?: string;
  ownerUserId?: string;
  severity: string;
  status: "open" | "recovered";
  openedAt: string;
  lastSeenAt: string;
  evidence?: Record<string, unknown>;
};

export type BackupInventorySummary = {
  key: string;
  sizeBytes: number;
  modifiedAt: string;
};

export type BackupRunSummary = {
  id: string;
  key?: string;
  kind: string;
  status: "success" | "failed" | "running";
  sizeBytes?: number;
  integrityStatus?: "valid" | "invalid";
  finishedAt?: string;
  message?: string;
};

export type BackupDrillSummary = {
  id: string;
  status: "success" | "failed";
  checkedAt?: string;
};

export type BackupOperationsSummary = {
  backend: "local" | "oss";
  inventory: BackupInventorySummary[];
  runs: BackupRunSummary[];
  latestSuccess: BackupRunSummary | null;
  latestDrill: BackupDrillSummary | null;
};

export type MountProfile = {
  id: string;
  name: string;
  containerPath: string;
  defaultChecked: boolean;
};

export type MetricSummary = {
  label: "Servers" | "GPUs" | "Containers" | "Alerts";
  value: string;
  detail: string;
  icon: "server" | "cpu" | "boxes" | "bell";
};

export type ActorRole = "viewer" | "user" | "power_user" | "teacher" | "admin";

export type MemberKind = "student" | "staff";

export type ActorSummary = {
  userId: string;
  memberId?: string;
  displayName?: string;
  role: ActorRole;
  memberKind?: MemberKind;
  status?: string;
};

export type ContainerSummary = {
  id: string;
  name: string;
  user: string;
  server: string;
  cpu: number;
  memoryGb: number;
  gpus: string[];
  status: string;
  statusText: string;
  sshCommand?: string;
  ownerMemberId?: string;
  version: number;
  mounts?: Array<{
    hostPath: string;
    containerPath: string;
    mode: string;
  }>;
};

export const containerStatusLabels: Record<string, string> = {
  created: "待启动",
  paused: "已暂停",
  restarting: "重启中",
  reconfiguring: "配置更新中",
  configuration_unknown: "配置结果待确认",
  provisioning: "创建中",
  provisioning_unknown: "创建结果未知",
  running: "运行中",
  stopped: "已停止",
  failed: "失败",
  deleting: "删除中",
  deleted: "已删除",
  execution_unknown: "操作结果未知",
  unknown: "未知",
};

export function containerStatusLabel(status: string): string {
  return containerStatusLabels[status] ?? status;
}

export type DashboardModuleError = {
  module: string;
  message: string;
};

export type DashboardData = {
  moduleErrors?: DashboardModuleError[];
  source: "api" | "mock";
  actor: ActorSummary;
  metrics: MetricSummary[];
  servers: ServerSummary[];
  presets: Preset[];
  images: ImageSummary[];
  gpus: GpuSummary[];
  mountProfiles: MountProfile[];
  containers: ContainerSummary[];
  recommendations: GpuRecommendation[];
  queue: QueueSummary[];
  approvals: ApprovalSummary[];
  health: HealthSummary[];
  healthEvents: HealthEventSummary[];
  backups: BackupOperationsSummary | null;
};

export const mockServers: ServerSummary[] = [
  { id: "server1", name: "Server1", gpuCount: 8 },
  { id: "server2", name: "Server2", gpuCount: 4 },
];

export const mockPresets: Preset[] = [
  { name: "Coding", cpu: 2, memoryGb: 8 },
  { name: "Debug", cpu: 4, memoryGb: 8 },
  { name: "Train", cpu: 8, memoryGb: 32 },
  { name: "Large Train", cpu: 16, memoryGb: 64 },
];

export const mockImages: ImageSummary[] = [
  { name: "gsm/pytorch-cuda-ssh:latest", displayName: "PyTorch CUDA SSH" },
  { name: "gsm/cuda-base-ssh:latest", displayName: "CUDA Base SSH" },
  { name: "gsm/coding-cpu-ssh:latest", displayName: "Coding CPU SSH" },
];

export const mockGpus: GpuSummary[] = [
  {
    serverId: "server1",
    serverName: "Server1",
    index: 0,
    user: "Alice",
    container: "llama-debug",
    memoryUsedGb: 21,
    memoryTotalGb: 48,
    utilization: 86,
    state: "busy",
    processCount: 1,
    declaredContainerCount: 1,
    freshness: "fresh",
  },
  {
    serverId: "server1",
    serverName: "Server1",
    index: 1,
    user: "Bob",
    container: "diffusion-train",
    memoryUsedGb: 38,
    memoryTotalGb: 48,
    utilization: 92,
    state: "busy",
    processCount: 1,
    declaredContainerCount: 1,
    freshness: "fresh",
  },
  {
    serverId: "server2",
    serverName: "Server2",
    index: 2,
    memoryUsedGb: 0,
    memoryTotalGb: 48,
    utilization: 0,
    state: "free",
    processCount: 0,
    declaredContainerCount: 0,
    freshness: "fresh",
  },
  {
    serverId: "server2",
    serverName: "Server2",
    index: 3,
    user: "Carol",
    container: "shared-debug",
    memoryUsedGb: 12,
    memoryTotalGb: 48,
    utilization: 41,
    sharedBy: ["Carol", "Dave"],
    state: "shareable",
    processCount: 1,
    declaredContainerCount: 2,
    freshness: "fresh",
  },
];

export const mockMountProfiles: MountProfile[] = [
  { id: "workspace-default", name: "workspace", containerPath: "/workspace", defaultChecked: true },
  { id: "scratch-default", name: "scratch", containerPath: "/scratch", defaultChecked: true },
];

export const mockContainers: ContainerSummary[] = [
  {
    id: "container-llama-debug",
    name: "llama-debug",
    user: "Alice",
    server: "Server1",
    cpu: 4,
    memoryGb: 16,
    gpus: ["0"],
    status: "running",
    statusText: "running",
    ownerMemberId: "member-alice",
    version: 1,
  },
  {
    id: "container-diffusion-train",
    name: "diffusion-train",
    user: "Bob",
    server: "Server1",
    cpu: 8,
    memoryGb: 32,
    gpus: ["1"],
    status: "running",
    statusText: "running",
    ownerMemberId: "member-bob",
    version: 1,
  },
  {
    id: "container-coding-cpu",
    name: "coding-cpu",
    user: "Carol",
    server: "Server2",
    cpu: 2,
    memoryGb: 8,
    gpus: [],
    status: "stopped",
    statusText: "已停止",
    ownerMemberId: "member-carol",
    version: 1,
  },
];

export const mockMetrics: MetricSummary[] = [
  { label: "Servers", value: "2", detail: "1 online, 1 standby", icon: "server" },
  { label: "GPUs", value: "4", detail: "3 active, 1 shared", icon: "cpu" },
  { label: "Containers", value: "14", detail: "9 running", icon: "boxes" },
  { label: "Alerts", value: "2", detail: "GPU sharing reminders", icon: "bell" },
];

export const mockDashboardData: DashboardData = {
  source: "mock",
  actor: { userId: "admin", displayName: "admin", role: "admin", status: "active" },
  metrics: mockMetrics,
  servers: mockServers,
  presets: mockPresets,
  images: mockImages,
  gpus: mockGpus,
  mountProfiles: mockMountProfiles,
  containers: mockContainers,
  recommendations: [
    {
      serverId: "server1",
      gpuUuid: "server1-gpu-2",
      index: 2,
      state: "free",
      safeToShare: true,
      memoryFreeGb: 48,
      reasonCodes: ["memory_available", "advisory_only"],
    },
  ],
  queue: [],
  approvals: [],
  health: [
    {
      serverId: "server1",
      status: "degraded",
      warningCount: 1,
      criticalCount: 0,
      observedAt: "2026-08-04T00:00:00Z",
      stale: false,
    },
  ],
  healthEvents: [],
  backups: {
    backend: "local",
    inventory: [],
    runs: [],
    latestSuccess: null,
    latestDrill: null,
  },
};
