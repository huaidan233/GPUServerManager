import type { ImageSummary } from "./mockData";

export const imageCategories = [
  ["python", "Python · 数据分析工作台"],
  ["python-base", "Python · 精简基础环境"],
  ["cuda", "CUDA · GPU 开发"],
  ["go", "Go · 服务与工具开发"],
  ["ubuntu", "Ubuntu · 通用基础环境"],
  ["legacy", "兼容保留 · 已暂停新建"],
  ["custom", "其他镜像"],
] as const;

export function imageCategory(image?: ImageSummary): string {
  if (image?.creationEnabled === false) return "legacy";
  return image?.environment ?? "custom";
}

export function preferredImage(images: ImageSummary[]) {
  return images.find(image => image.recommended && image.creationEnabled !== false)
    ?? images.find(image => image.creationEnabled !== false) ?? images[0];
}
