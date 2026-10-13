"use client";
import { useState } from "react";
import type { ImageSummary } from "../lib/mockData";
import { authenticatedFetch } from "../lib/authenticatedFetch";
import { imageCategories, imageCategory } from "../lib/imageCategories";

export function ImageVersionsPanel({ images, onUpdated }: { images: ImageSummary[]; onUpdated: () => void }) {
  const [busy, setBusy] = useState(""); const [error, setError] = useState(""); const [reason, setReason] = useState("");
  const [category, setCategory] = useState("all");
  const [search, setSearch] = useState(""); const [page, setPage] = useState(0);
  const visibleImages = images.filter(image => (category === "all" || imageCategory(image) === category) && `${image.displayName} ${image.name}`.toLowerCase().includes(search.toLowerCase()));
  const maxPage = Math.max(0, Math.ceil(visibleImages.length / 6) - 1); const currentPage = Math.min(page, maxPage);
  async function change(image: ImageSummary, recommend: boolean) {
    setBusy(image.name); setError("");
    try {
      const response = await authenticatedFetch("/api/gsm/images/policy", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reference: image.name, creation_enabled: recommend || image.creationEnabled === false, recommended: recommend, reason }) });
      if (!response.ok) throw new Error("镜像策略保存失败，请刷新后重试。");
      onUpdated();
    } catch (e) { setError(e instanceof Error ? e.message : "保存失败。"); } finally { setBusy(""); }
  }
  return <section className="workspace-panel"><h2>镜像版本</h2><p className="form-help">推荐版本用于新建容器。选择旧版并设为推荐即可回退默认版本；升级已有实验时请先导出环境，复用配置并选择新镜像。</p>
    <label>镜像分类<select value={category} onChange={e => { setCategory(e.currentTarget.value); setPage(0); }}><option value="all">全部环境（{images.length}）</option>{imageCategories.map(([kind, label]) => { const count = images.filter(image => imageCategory(image) === kind).length; return count ? <option key={kind} value={kind}>{label}（{count}）</option> : null; })}</select></label>
    <p className="form-help">精简版适合自行管理依赖；数据分析版预装 NumPy、pandas、sklearn 和 JupyterLab。GPU 项目可选择 CUDA 开发环境。</p>
    <label className="settings-search">搜索镜像<input type="search" value={search} onChange={e => { setSearch(e.target.value); setPage(0); }} placeholder="环境名称、版本或镜像地址" /></label>
    <label>版本变更原因<input value={reason} maxLength={500} onChange={e => setReason(e.currentTarget.value)} placeholder="例如：课程兼容性验证后回退" /></label>
    {error && <p role="alert">{error}</p>}<div className="table-wrap"><table aria-label="镜像版本目录"><thead><tr><th>版本</th><th>兼容性与依赖</th><th>安全检查</th><th>状态与操作</th></tr></thead><tbody>{visibleImages.slice(currentPage * 6, currentPage * 6 + 6).map(image => <tr key={image.name}>
      <td><strong>{image.displayName}</strong><p><code>{image.name}</code></p><details><summary>固定镜像 ID</summary><code>{image.digest || "未验证"}</code></details></td>
      <td>{image.minimumDriver && <p>最低 NVIDIA 驱动：{image.minimumDriver}</p>}{image.cleanCuda && <p>纯净 CUDA，不含 PyTorch</p>}<details><summary>依赖清单</summary><p>{image.packages?.join("、") || "见镜像说明"}</p></details></td>
      <td>{image.securityStatus === "known-vulnerabilities" ? "存在已知风险，建议迁移" : image.securityStatus === "checked" ? "已检查固定依赖" : "未完成安全扫描"}{image.securityNotes?.map(note => <p key={note}>{note}</p>)}</td>
      <td><p>{image.creationEnabled === false ? "已暂停新建" : image.recommended ? "推荐" : "可用"}</p><button className="secondary-button" disabled={!!busy || !reason.trim() || !image.digest} onClick={() => void change(image, true)}>设为推荐</button><button className="secondary-button" disabled={!!busy || !reason.trim() || !image.digest} onClick={() => void change(image, false)}>{image.creationEnabled === false ? "允许新建" : "暂停新建"}</button></td>
    </tr>)}</tbody></table></div>{!visibleImages.length && <p className="empty-state">没有符合条件的镜像。</p>}
    <div className="settings-pagination"><span>共 {visibleImages.length} 个镜像 · 第 {currentPage + 1} / {maxPage + 1} 页</span><button className="secondary-button" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>上一页</button><button className="secondary-button" disabled={currentPage === maxPage} onClick={() => setPage(currentPage + 1)}>下一页</button></div></section>;
}
