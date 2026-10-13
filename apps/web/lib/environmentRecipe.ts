export type EnvironmentRecipe = {
  format: "gsm-environment"; version: 1; image_digest?: string;
  configuration: { name: string; image: string; server_id: string; cpu_limit: number; memory_limit_gb: number; gpu_count: number; mount_profile_ids: string[] };
  dependencies?: { status: string; python_packages?: string[]; runtime_versions?: Record<string, string> };
  startup_commands?: string[];
};

export function parseEnvironmentRecipe(value: unknown): EnvironmentRecipe {
  if (!value || typeof value !== "object") throw new Error("环境文件格式无效。");
  const recipe = value as EnvironmentRecipe;
  const c = recipe.configuration;
  if (recipe.format !== "gsm-environment" || recipe.version !== 1 || !c
      || ![c.name, c.image, c.server_id].every(v => typeof v === "string" && v.length > 0 && v.length <= 512)
      || ![c.cpu_limit, c.memory_limit_gb].every(v => Number.isSafeInteger(v) && v > 0 && v <= 1048576)
      || !Number.isSafeInteger(c.gpu_count) || c.gpu_count < 0 || c.gpu_count > 128
      || !Array.isArray(c.mount_profile_ids) || c.mount_profile_ids.length > 50 || !c.mount_profile_ids.every(v => typeof v === "string" && v.length <= 128)
      || (recipe.startup_commands && (!Array.isArray(recipe.startup_commands) || recipe.startup_commands.length > 50 || !recipe.startup_commands.every(v => typeof v === "string" && v.length <= 4096)))) {
    throw new Error("环境文件格式或版本不受支持。");
  }
  // Only allow explicitly selected fields into a new creation draft.
  return { format: "gsm-environment", version: 1, image_digest: recipe.image_digest,
    configuration: { name: c.name, image: c.image, server_id: c.server_id, cpu_limit: c.cpu_limit,
      memory_limit_gb: c.memory_limit_gb, gpu_count: c.gpu_count, mount_profile_ids: c.mount_profile_ids },
    startup_commands: recipe.startup_commands ?? [], dependencies: recipe.dependencies };
}

export function downloadRecipe(recipe: EnvironmentRecipe) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(recipe, null, 2)], { type: "application/json" }));
  const link = document.createElement("a"); link.href = url; link.download = "gsm-environment.json";
  link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
