"use client";

import { AlertTriangle, RotateCcw } from "lucide-react";
import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

import { authenticatedFetch } from "../lib/authenticatedFetch";
import { parseEnvironmentRecipe, type EnvironmentRecipe } from "../lib/environmentRecipe";
import { imageCategories, imageCategory, preferredImage } from "../lib/imageCategories";
import type {
  GpuSummary,
  ImageSummary,
  MountProfile,
  MemoryTier,
  Preset,
  ServerSummary,
} from "../lib/mockData";

type Props = {
  initialRecipe?: EnvironmentRecipe;
  servers: ServerSummary[];
  presets: Preset[];
  images: ImageSummary[];
  gpus: GpuSummary[];
  mountProfiles: MountProfile[];
  canCreate?: boolean;
  canPullImages?: boolean;
  onCancel?: () => void;
  onComplete?: (message: string) => void;
  onBusyChange?: (busy: boolean) => void;
};

export function ContainerCreateWizard({
  initialRecipe,
  servers,
  presets,
  images,
  gpus,
  mountProfiles,
  canCreate = true,
  canPullImages = true,
  onCancel,
  onComplete,
  onBusyChange,
}: Props) {
  const [recipe, setRecipe] = useState(initialRecipe);
  const [draftKey, setDraftKey] = useState(0);
  const [selectedServerId, setSelectedServerId] = useState(initialRecipe?.configuration.server_id ?? servers[0]?.id ?? "server1");
  const selectedServer = servers.find(server => server.id === selectedServerId);
  const [selectedPresetName, setSelectedPresetName] = useState(initialRecipe ? "Custom" : presets[0]?.name ?? "Custom");
  const [cpuLimit, setCpuLimit] = useState(String(initialRecipe?.configuration.cpu_limit ?? presets[0]?.cpu ?? 4));
  const [memoryLimitGb, setMemoryLimitGb] = useState(String(initialRecipe?.configuration.memory_limit_gb ?? presets[0]?.memoryGb ?? 16));
  const [catalogImages, setCatalogImages] = useState(images);
  const [customImage, setCustomImage] = useState("");
  const [gpuMode, setGpuMode] = useState<"none" | "auto" | "manual">("none");
  const [selectedGpus, setSelectedGpus] = useState<number[]>([]);
  const [memoryTier, setMemoryTier] = useState<MemoryTier>("8_24");
  const [estimatedDurationMinutes, setEstimatedDurationMinutes] = useState(120);
  const [autoStart, setAutoStart] = useState(true);
  const [selectedImage, setSelectedImage] = useState(initialRecipe?.configuration.image ?? preferredImage(images.filter(image => image.creationEnabled !== false
    && (!image.serverIds?.length || image.serverIds.includes(selectedServerId))))?.name ?? "");
  const currentImage = catalogImages.find((image) => image.name === selectedImage);
  const preparedImages = catalogImages.filter((image) => image.environment && (!image.serverIds?.length || image.serverIds.includes(selectedServerId)));
  const environmentKind = imageCategory(currentImage);
  const hasPreparedEnvironments = preparedImages.length > 0;
  const selectableImages = catalogImages.filter((image) => (!image.serverIds?.length || image.serverIds.includes(selectedServerId))
    && (!hasPreparedEnvironments || imageCategory(image) === environmentKind));
  const cpuOnlyEnvironment = !!currentImage?.environment && currentImage.gpuReady === false;
  function chooseImage(name: string) {
    setSelectedImage(name);
    const image = catalogImages.find((item) => item.name === name);
    if (image?.environment && image.gpuReady === false) { setGpuMode("none"); setSelectedGpus([]); }
  }
  const [createError, setCreateError] = useState("");
  const [sshPassword, setSshPassword] = useState("");
  const [createSuccess, setCreateSuccess] = useState("");
  const [isCreating, setIsCreating] = useState(false);
  const [pullError, setPullError] = useState("");
  const [pullSuccess, setPullSuccess] = useState("");
  const [isPulling, setIsPulling] = useState(false);
  const formRef = useRef<HTMLFormElement>(null);
  const selectedServerGpus = useMemo(() =>
    gpus.filter((gpu) => !gpu.serverId || gpu.serverId === selectedServerId),
    [gpus, selectedServerId],
  );

  useEffect(() => {
    onBusyChange?.(isCreating || isPulling);
  }, [isCreating, isPulling, onBusyChange]);

  const selectedOccupiedGpu = useMemo(() => {
    if (gpuMode !== "manual") {
      return undefined;
    }

    return selectedServerGpus.find(
      (gpu) =>
        selectedGpus.includes(gpu.index) &&
        (gpu.state === "busy" || gpu.state === "exclusive"),
    );
  }, [gpuMode, selectedGpus, selectedServerGpus]);
  const createDisabledReason =
    !canCreate
      ? "viewer 只能查看资源状态，不能创建容器。"
      : currentImage?.creationEnabled === false ? "此镜像版本已暂停新建，请选择其他版本。"
      : !servers.some(server => server.id === selectedServerId) ? "原服务器不可用，请重新选择服务器。"
      : (selectedServer?.cpuTotal !== undefined && Number(cpuLimit) > selectedServer.cpuTotal)
        || (selectedServer?.memoryTotalGb !== undefined && Number(memoryLimitGb) > selectedServer.memoryTotalGb)
        ? "单个容器的 CPU 或内存不能超过这台服务器的分配上限。"
      : !currentImage || (currentImage.serverIds?.length && !currentImage.serverIds.includes(selectedServerId))
        ? "请选择这台服务器已准备好的环境版本。"
      : autoStart && selectedOccupiedGpu
        ? "这张 GPU 已被占用，请换卡或取消立即启动，等待空闲后再启动。"
      : gpuMode === "auto" && !selectedServerGpus.some(gpu => gpu.state === "free")
        ? "暂无空闲 GPU，请选择无 GPU 或手动指定配置后暂不启动。"
        : undefined;


  function handleServerChange(serverId: string) {
    setSelectedServerId(serverId);
    if (!currentImage || currentImage.creationEnabled === false || (currentImage.serverIds?.length && !currentImage.serverIds.includes(serverId))) {
      chooseImage(preferredImage(catalogImages.filter((image) => image.creationEnabled !== false
        && (!image.serverIds?.length || image.serverIds.includes(serverId))))?.name ?? "");
    }
    setSelectedGpus([]);

  }

  function resetDraft() {
    setRecipe(undefined); setDraftKey(key => key + 1);
    formRef.current?.reset();
    setSelectedServerId(servers[0]?.id ?? "server1");
    setSelectedPresetName(presets[0]?.name ?? "Custom");
    setCpuLimit(String(presets[0]?.cpu ?? 4));
    setMemoryLimitGb(String(presets[0]?.memoryGb ?? 16));
    setSelectedImage(preferredImage(images.filter(image => image.creationEnabled !== false
      && (!image.serverIds?.length || image.serverIds.includes(servers[0]?.id ?? "server1"))))?.name ?? "");
    setCustomImage("");
    setGpuMode("none");
    setSelectedGpus([]);
    setMemoryTier("8_24");
    setEstimatedDurationMinutes(120);

    setCreateError("");
    setCreateSuccess("");
    setSshPassword("");
    setPullError("");
    setPullSuccess("");
  }

  async function importRecipe(file?: File) {
    if (!file) return;
    try {
      if (file.size > 1024 * 1024) throw new Error("环境文件不能超过 1 MiB。");
      const value = parseEnvironmentRecipe(JSON.parse(await file.text()));
      setRecipe(value); setDraftKey(key => key + 1); setSelectedServerId(value.configuration.server_id);
      setSelectedImage(value.configuration.image); setSelectedPresetName("Custom");
      setCpuLimit(String(value.configuration.cpu_limit)); setMemoryLimitGb(String(value.configuration.memory_limit_gb));
      setGpuMode("none"); setSelectedGpus([]); setSshPassword(""); setCreateError("");
    } catch (e) { setCreateError(e instanceof Error ? e.message : "无法读取环境文件。"); }
  }

  function toggleGpu(index: number) {
    setSelectedGpus((current) =>
      current.includes(index)
        ? current.filter((gpuIndex) => gpuIndex !== index)
        : [...current, index],
    );
  }

  async function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setCreateError("");
    setCreateSuccess("");

    const form = new FormData(event.currentTarget);
    if (createDisabledReason) { setCreateError(createDisabledReason); return; }
    const presetName = String(form.get("preset") ?? selectedPresetName);
    const requestedCpuLimit = Number(form.get("cpu-limit"));
    const requestedMemoryLimitGb = Number(form.get("memory-limit-gb"));
    if (sshPassword.length < 12 || sshPassword.length > 128 || /[\r\n\0]/.test(sshPassword)) {
      setCreateError("请设置 12–128 位的 SSH 初始密码。");
      return;
    }
    if (
      !Number.isSafeInteger(requestedCpuLimit) || requestedCpuLimit <= 0 ||
      !Number.isSafeInteger(requestedMemoryLimitGb) || requestedMemoryLimitGb <= 0
    ) {
      setCreateError("CPU 核数和内存必须为正整数。");
      return;
    }
    setIsCreating(true);
    const gpuDevices = gpuMode === "none" ? [] : gpuMode === "auto" ? autoSelectedGpuDevices() : selectedGpus.map(String);
    const payload = {
      name: String(form.get("container-name") || "debug"),
      server_id: String(form.get("server") ?? selectedServerId),
      image: selectedImage,
      preset: presetName,
      cpu_limit: requestedCpuLimit,
      memory_limit_gb: requestedMemoryLimitGb,
      gpu_devices: gpuDevices,
      mount_profile_ids: form.getAll("mounts").map(String),
      ssh_enabled: true,
      ssh_password: sshPassword,
      auto_start: autoStart,
      note: String(form.get("note") ?? "").trim(),
      memory_tier: memoryTier,
      estimated_duration_minutes: estimatedDurationMinutes,
    };

    try {
      const response = await authenticatedFetch("/api/gsm/containers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const result = await response.json();
      if (!response.ok) {
        setCreateError(String(result.detail ?? "创建容器失败"));
        return;
      }
      if (response.status === 202 && result.requires_approval) {
        setSshPassword("");
        const message = `已提交管理员审批：${result.approval.id}`;
        setCreateSuccess(message);
        onComplete?.(message);
        return;
      }
      const containerName = result.container?.name ?? payload.name;
      const message = `容器 ${containerName} 已创建${result.container?.status === "running" ? "并启动" : ""}。${result.container?.ssh_command ? `连接命令：${result.container.ssh_command}；使用创建时设置的 SSH 密码。` : ""}`;
      setSshPassword("");
      setCreateSuccess(message);
      onComplete?.(message);
    } catch {
      setCreateError("无法连接 Web API 代理。");
    } finally {
      setIsCreating(false);
    }
  }

  async function handlePullImage() {
    const image = customImage.trim();
    setPullError("");
    setPullSuccess("");
    if (!image) {
      setPullError("请输入镜像地址。");
      return;
    }
    setIsPulling(true);
    try {
      const response = await authenticatedFetch("/api/gsm/images/pull", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          server_id: selectedServerId,
          image,
        }),
      });
      const result = await response.json();
      if (!response.ok) {
        setPullError(String(result.detail ?? "拉取镜像失败。"));
        return;
      }
      setCatalogImages((current) =>
        current.some((item) => item.name === image)
          ? current
          : [...current, { name: image, displayName: image }],
      );
      setSelectedImage(image);
      setPullSuccess(`镜像 ${image} 已加入目录。`);
    } catch {
      setPullError("无法连接 Web API 代理。");
    } finally {
      setIsPulling(false);
    }
  }

  function autoSelectedGpuDevices(): string[] {
    const freeGpu = selectedServerGpus.find(
      (gpu) => gpu.state === "free",
    );
    return freeGpu ? [String(freeGpu.index)] : [];
  }

  return (
    <form ref={formRef} className="wizard" onSubmit={handleCreate}>
      <label className="secondary-button file-button">导入环境配置<input className="sr-only" type="file" accept=".json,application/json" aria-label="导入环境配置" onChange={event => { void importRecipe(event.currentTarget.files?.[0]); event.currentTarget.value = ""; }} /></label>
      {recipe && <div className="container-notice"><p>已载入环境配置。请重新选择 GPU、挂载目录并设置新 SSH 密码；创建时仍会检查配额和审批要求。</p>
        {recipe.configuration.gpu_count > 0 && <p>原配置使用 {recipe.configuration.gpu_count} 张 GPU。</p>}
        {recipe.image_digest && currentImage?.digest && recipe.image_digest !== currentImage.digest && <p>镜像 ID 与导出时不同，请确认使用当前版本。</p>}
        {!!recipe.startup_commands?.length && <details><summary>启动命令参考（不会自动执行）</summary><pre>{recipe.startup_commands.join("\n")}</pre></details>}
      </div>}
      <div className="wizard-row">
        <label htmlFor="container-name">容器名称</label>
        <input
          id="container-name"
          name="container-name" key={`name-${draftKey}`} defaultValue={recipe?.configuration.name ?? "debug"}
          type="text"
          autoComplete="off"
        />
      </div>

      <div className="wizard-row">
        <label htmlFor="server">服务器</label>
        <select
          id="server"
          name="server"
          value={selectedServerId}
          onChange={(event) => handleServerChange(event.currentTarget.value)}
        >
          {servers.map((server) => (
            <option key={server.id} value={server.id}>
              {server.name} · {server.gpuCount} GPU
            </option>
          ))}
        </select>
      </div>

      <div className="wizard-row">
        <label htmlFor="preset">资源套餐</label>
        <select
          id="preset"
          name="preset"
          value={selectedPresetName}
          onChange={(event) => {
            const preset = presets.find((item) => item.name === event.currentTarget.value);
            setSelectedPresetName(event.currentTarget.value);
            if (preset) {
              setCpuLimit(String(preset.cpu));
              setMemoryLimitGb(String(preset.memoryGb));
            }
          }}
        >
          {presets.map((preset) => (
            <option key={preset.name} value={preset.name}>
              {preset.name} · {preset.cpu}C / {preset.memoryGb}G
            </option>
          ))}
          <option value="Custom">自定义</option>
        </select>
      </div>

      {hasPreparedEnvironments && <div className="wizard-row">
        <label htmlFor="environment-kind">开发环境</label>
        <select id="environment-kind" value={environmentKind} onChange={(event) => {
          chooseImage(preferredImage(catalogImages.filter((image) => imageCategory(image) === event.currentTarget.value
            && (!image.serverIds?.length || image.serverIds.includes(selectedServerId))))?.name ?? "");
        }}>
          {imageCategories.map(([kind, label]) =>
            catalogImages.some((image) => imageCategory(image) === kind && (!image.serverIds?.length || image.serverIds.includes(selectedServerId)))
              ? <option key={kind} value={kind}>{label}</option> : null)}
        </select>
        <p className="form-help">环境已提前构建，创建后即可使用，无需等待安装依赖。</p>
      </div>}
      <div className="wizard-row">
        <label htmlFor="image">{hasPreparedEnvironments && environmentKind !== "custom" ? "环境版本" : "镜像"}</label>
        <select id="image" name="image" value={selectedImage} onChange={(event) => chooseImage(event.currentTarget.value)}>
          {selectableImages.map((image) => (
            <option key={image.name} value={image.name}>
              {image.displayName}{image.recommended ? " · 推荐" : ""}{image.creationEnabled === false ? " · 已暂停" : ""}
            </option>
          ))}
        </select>
        {currentImage?.environment && <div className="environment-description">
          <p className="form-help">{currentImage.description}</p>
          {currentImage.cleanCuda && <p className="form-help">纯净 CUDA，不包含 PyTorch 或科研框架。</p>}
          {currentImage.minimumDriver && <p className="form-help">最低 NVIDIA 驱动：{currentImage.minimumDriver}</p>}
          {currentImage.securityNotes?.map(note => <p key={note} className="form-help">{note}</p>)}
          {currentImage.digest && <details><summary>固定镜像版本</summary><code>{currentImage.digest}</code></details>}
          {!!currentImage.packages?.length && <p className="form-help">已安装：{currentImage.packages.join("、")}</p>}
          <p className="form-help">{currentImage.environment === "python" ? "可直接运行 python、pip 和 jupyter；项目需要不同依赖时，可在 /workspace 创建自己的虚拟环境。" : currentImage.environment === "go" ? "可直接运行 go build、go test，已提供 C/C++ 编译工具。" : currentImage.environment === "cuda" ? "包含 CUDA 编译器 nvcc，可编译 CUDA 扩展；Python 科研库需自行安装。" : currentImage.environment === "python-base" ? "仅提供 Python、pip 和基础编译工具，项目依赖由用户自行管理。" : "提供基础 Linux 用户态和常用编译工具，项目依赖由用户自行管理。"}</p>
        </div>}
      </div>

      <div className="wizard-row">
        <fieldset className="resource-limits" aria-describedby="resource-limits-help">
          <legend className="field-label">CPU 与内存</legend>
          <div className="resource-limit-inputs">
            <div className="wizard-row">
              <label htmlFor="cpu-limit">CPU（核）</label>
              <input
                id="cpu-limit"
                name="cpu-limit"
                type="number"
                min={1}
                max={selectedServer?.cpuTotal}
                step={1}
                required
                value={cpuLimit}
                onChange={(event) => {
                  setCpuLimit(event.currentTarget.value);
                  setSelectedPresetName("Custom");
                }}
              />
            </div>
            <div className="wizard-row">
              <label htmlFor="memory-limit-gb">内存（GiB）</label>
              <input
                id="memory-limit-gb"
                name="memory-limit-gb"
                type="number"
                min={1}
                max={selectedServer?.memoryTotalGb}
                step={1}
                required
                value={memoryLimitGb}
                onChange={(event) => {
                  setMemoryLimitGb(event.currentTarget.value);
                  setSelectedPresetName("Custom");
                }}
              />
            </div>
          </div>
          <p className="form-help" id="resource-limits-help">
            选择套餐可快速填入参数，修改后自动切换为自定义。启动时检查个人运行总额度，停止状态下可自由调整配置。
            {selectedServer?.cpuTotal !== undefined && selectedServer?.memoryTotalGb !== undefined && <>
              单个容器上限：{selectedServer.cpuTotal} 核 CPU、{selectedServer.memoryTotalGb} GiB 内存（已扣除系统预留）。多个容器的配额可以超售。
            </>}
          </p>
        </fieldset>
      </div>

      <div className="wizard-row">
        <label htmlFor="custom-image">镜像地址</label>
        <div className="inline-row">
          <input
            id="custom-image"
            name="custom-image"
            type="text"
            autoComplete="off"
            placeholder="docker.io/pytorch/pytorch:latest"
            value={customImage}
            onChange={(event) => setCustomImage(event.currentTarget.value)}
          />
          <button
            className="secondary-button"
            type="button"
            disabled={isPulling || !canPullImages}
            onClick={handlePullImage}
          >
            {isPulling ? "拉取中" : "拉取镜像"}
          </button>
        </div>
      </div>

      {!canPullImages && (
        <p className="form-help" role="status">
          只有 power user 或 admin 可以拉取自定义镜像。
        </p>
      )}

      {pullError && (
        <p className="form-help create-error" role="alert">
          {pullError}
        </p>
      )}

      {pullSuccess && (
        <p className="form-help create-success" role="status">
          {pullSuccess}
        </p>
      )}

      <fieldset className="wizard-row">
        <legend className="field-label">GPU</legend>
        <div className="choice-grid">
          <label>
            <input
              type="radio"
              name="gpu-mode"
              checked={gpuMode === "none"}
              onChange={() => setGpuMode("none")}
            />
            不使用 GPU
          </label>
          <label>
            <input
              type="radio"
              name="gpu-mode"
              checked={gpuMode === "auto"}
              disabled={cpuOnlyEnvironment}
              onChange={() => setGpuMode("auto")}
            />
            自动选择空闲卡
          </label>
          <label>
            <input
              type="radio"
              name="gpu-mode"
              aria-label="手动选择 GPU"
              checked={gpuMode === "manual"}
              disabled={cpuOnlyEnvironment}
              onChange={() => setGpuMode("manual")}
            />
            手动选择 GPU
          </label>
        </div>
      </fieldset>

      <fieldset className="wizard-row">
        <legend className="field-label">显存需求</legend>
        <div className="segmented-control" aria-label="显存需求档位">
          {([
            ["lt_8", "低于 8 GB"],
            ["8_24", "8–24 GB"],
            ["24_48", "24–48 GB"],
            ["full_card", "整卡"],
          ] as const).map(([value, label]) => (
            <button
              key={value}
              type="button"
              aria-pressed={memoryTier === value}
              onClick={() => setMemoryTier(value)}
            >
              {label}
            </button>
          ))}
        </div>
      </fieldset>

      <div className="wizard-row">
        <label htmlFor="estimated-duration">预计使用时长（分钟）</label>
        <input
          id="estimated-duration"
          name="estimated-duration"
          type="number"
          min={1}
          step={1}
          value={estimatedDurationMinutes}
          onChange={(event) =>
            setEstimatedDurationMinutes(Math.max(1, Number(event.currentTarget.value) || 1))
          }
        />
      </div>

      {gpuMode === "manual" && (
        <div className="choice-grid" aria-label="GPU 手动选择">
          {selectedServerGpus.map((gpu) => (
            <label key={gpu.index}>
              <input
                type="checkbox"
                name="gpus"
                value={gpu.index}
                aria-label={`GPU ${gpu.index}: ${gpuSelectionLabel(gpu)}`}
                checked={selectedGpus.includes(gpu.index)}
                onChange={() => toggleGpu(gpu.index)}
              />
              GPU {gpu.index}
            </label>
          ))}
        </div>
      )}

      {selectedOccupiedGpu && (
        <div className="warning-box" role="status">
          <AlertTriangle size={18} aria-hidden="true" />
          <span>
            GPU {selectedOccupiedGpu.index} 当前被 {selectedOccupiedGpu.user} / {selectedOccupiedGpu.container} 使用
          </span>
        </div>
      )}

      <label className="share-confirm"><input type="checkbox" checked={autoStart} onChange={event => setAutoStart(event.target.checked)} />创建后立即启动</label>
      <p className="form-help">取消勾选可先保存容器配置。只有启动时才占用个人额度；默认额度为 8 核 / 8 GiB / 0 张 GPU。</p>

      <div className="wizard-row">
        <span className="field-label">挂载</span>
        <div className="mount-list">
          {mountProfiles.map((mount) => (
            <label key={mount.id}>
              <input
                type="checkbox"
                name="mounts"
                value={mount.id}
                defaultChecked={mount.defaultChecked}
              />
              {mount.name} 挂载到 {mount.containerPath}
            </label>
          ))}
        </div>
      </div>

      <div className="wizard-row">
        <label htmlFor="initial-ssh-password">SSH 初始密码（至少 12 位）</label>
        <input id="initial-ssh-password" type="password" autoComplete="new-password" required minLength={12} maxLength={128}
          value={sshPassword} onChange={(event) => setSshPassword(event.currentTarget.value)} />
        <p className="form-help">创建后自动启动，使用此密码即可 SSH 连接。用户名为 researcher，连接命令会在创建完成后显示；也可使用 SSH 证书。</p>
      </div>

      <div className="wizard-row">
        <label htmlFor="note">备注</label>
        <input
          id="note"
          name="note"
          type="text"
          autoComplete="off"
          placeholder="例如：llama debug，预计今晚释放"
        />
      </div>

      {createDisabledReason && (
        <p className="form-help create-disabled-reason" id="create-disabled-reason">
          {createDisabledReason}
        </p>
      )}

      {createError && (
        <p className="form-help create-error" role="alert">
          {createError}
        </p>
      )}

      {createSuccess && (
        <p className="form-help create-success" role="status">
          {createSuccess}
        </p>
      )}

      <div className="dialog-actions">
        {onCancel ? (
          <button className="secondary-button" type="button" disabled={isCreating || isPulling} onClick={onCancel}>取消</button>
        ) : <button className="secondary-button" type="button" onClick={resetDraft}>
          <RotateCcw size={16} aria-hidden="true" />
          重置表单
        </button>}
        <button
          className="primary-button"
          type="submit"
          disabled={Boolean(createDisabledReason) || isCreating}
          aria-describedby={createDisabledReason ? "create-disabled-reason" : undefined}
        >
          {isCreating ? "创建中" : "创建容器"}
        </button>
      </div>

    </form>
  );
}

function gpuSelectionLabel(gpu: GpuSummary): string {
  if (gpu.state === "busy" || gpu.state === "exclusive") {
    return gpu.user ? `${gpu.user} 使用中` : "繁忙";
  }
  if (gpu.state === "shareable") {
    return "可共享";
  }
  if (gpu.state === "unknown") {
    return "未知";
  }
  return "空闲";
}
