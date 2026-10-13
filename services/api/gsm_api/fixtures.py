from gsm_api.domain import AgentRecord, ImageRecord, MountProfile, ResourcePreset, ServerRecord


PRESETS = [
    ResourcePreset(name="Coding", cpu=2, memory_gb=8, gpu="none", purpose="写代码、Codex、VS Code Remote"),
    ResourcePreset(name="Debug", cpu=4, memory_gb=8, gpu="none", purpose="小实验、调试；基础个人额度内可启动"),
    ResourcePreset(name="Train", cpu=8, memory_gb=32, gpu="optional-1-2", purpose="常规训练"),
    ResourcePreset(name="Large Train", cpu=16, memory_gb=64, gpu="optional-many", purpose="大训练"),
]

IMAGES = [
    ImageRecord(
        name="gsm/pytorch-cuda-ssh",
        display_name="PyTorch CUDA SSH",
        tag="latest",
        framework="pytorch",
        cuda_version="12.x",
        ssh_ready=True,
        gpu_ready=True,
        trusted=True,
        domestic_mirror="configurable",
        description="预装 PyTorch、CUDA runtime、sshd 和标准挂载入口。",
    ),
    ImageRecord(
        name="gsm/cuda-base-ssh",
        display_name="CUDA Base SSH",
        tag="latest",
        framework="cuda",
        cuda_version="12.x",
        ssh_ready=True,
        gpu_ready=True,
        trusted=True,
        domestic_mirror="configurable",
        description="CUDA runtime + sshd 基础镜像。",
    ),
    ImageRecord(
        name="gsm/coding-cpu-ssh",
        display_name="Coding CPU SSH",
        tag="latest",
        framework="base",
        ssh_ready=True,
        gpu_ready=False,
        trusted=True,
        domestic_mirror="configurable",
        description="无 GPU 开发容器。",
    ),
]

SERVERS = [
    ServerRecord(
        id="server1",
        name="Server1",
        status="online",
        gpu_count=8,
        cpu_total=128,
        memory_total_gb=512,
    )
]

MOUNT_PROFILES = [
    MountProfile(
        id="workspace-default",
        name="Workspace",
        host_path_template="/data/users/{username}",
        container_path="/workspace",
        mode="rw",
        available_server_ids=["server1"],
    ),
    MountProfile(
        id="datasets-readonly",
        name="Datasets",
        host_path_template="/data/datasets",
        container_path="/datasets",
        mode="ro",
        available_server_ids=["server1"],
    ),
    MountProfile(
        id="models-readonly",
        name="Models",
        host_path_template="/data/models",
        container_path="/models",
        mode="ro",
        available_server_ids=["server1"],
    ),
    MountProfile(
        id="scratch-default",
        name="Scratch",
        host_path_template="/scratch/users/{username}",
        container_path="/scratch",
        mode="rw",
        available_server_ids=["server1"],
    ),
]

AGENTS = [
    AgentRecord(
        id="server1-agent",
        name="Server1 Agent",
        kind="server-agent",
        server_id="server1",
        base_url="http://server-agent:8000",
        status="online",
        capabilities=[
            "containers.create",
            "containers.start",
            "containers.stop",
            "containers.delete",
            "images.pull",
            "gpus.read",
        ],
    )
]
