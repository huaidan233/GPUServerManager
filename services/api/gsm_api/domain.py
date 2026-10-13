from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Role(StrEnum):
    VIEWER = "viewer"
    USER = "user"
    POWER_USER = "power_user"
    TEACHER = "teacher"
    ADMIN = "admin"


class RiskLevel(StrEnum):
    READ = "read"
    OWN_SAFE = "own_safe"
    OWN_RISKY = "own_risky"
    DESTRUCTIVE = "destructive"
    ADMIN_RISKY = "admin_risky"
    CRITICAL = "critical"


class MemoryTier(StrEnum):
    LT_8 = "lt_8"
    GB_8_24 = "8_24"
    GB_24_48 = "24_48"
    FULL_CARD = "full_card"


class ResourcePreset(BaseModel):
    name: str
    cpu: int
    memory_gb: int
    gpu: str
    purpose: str


class ImageRecord(BaseModel):
    name: str
    display_name: str
    tag: str = "latest"
    digest: str | None = None
    framework: str
    cuda_version: str | None = None
    ssh_ready: bool = False
    gpu_ready: bool = False
    trusted: bool = False
    domestic_mirror: str | None = None
    description: str = ""
    environment: str | None = None
    runtime_version: str | None = None
    packages: list[str] = Field(default_factory=list)
    server_ids: list[str] = Field(default_factory=list)
    release_date: str | None = None
    minimum_driver: str | None = None
    security_status: str = "unscanned"
    security_notes: list[str] = Field(default_factory=list)
    applications: list[str] = Field(default_factory=list)
    clean_cuda: bool = False
    recommended: bool = False
    creation_enabled: bool = True


class ServerRecord(BaseModel):
    id: str
    name: str
    status: str = "online"
    gpu_count: int = 0
    cpu_total: int = 0
    memory_total_gb: int = 0


class MountProfile(BaseModel):
    id: str
    name: str
    host_path_template: str
    container_path: str
    mode: str = "rw"
    available_server_ids: list[str] = Field(default_factory=list)


class AgentRecord(BaseModel):
    id: str
    name: str
    kind: str
    server_id: str | None = None
    base_url: str
    status: str = "online"
    capabilities: list[str] = Field(default_factory=list)


class ContainerRecord(BaseModel):
    id: str
    name: str
    user_id: str
    server_id: str
    image: str
    cpu_limit: int
    memory_limit_gb: int
    gpu_devices: list[str] = Field(default_factory=list)
    status: str = "stopped"
    metadata: dict[str, Any] = Field(default_factory=dict)
