"use client";

import { useState } from "react";
import { ArrowUpRight, Boxes, Bot, DatabaseBackup, Printer, RefreshCw, ShieldCheck, SlidersHorizontal } from "lucide-react";
import type { DashboardData } from "../lib/mockData";
import { AccessibleDialog } from "./AccessibleDialog";
import { QuotaSettingsPanel } from "./PersonalQuotaPanel";
import { ImageVersionsPanel } from "./ImageVersionsPanel";
import { AgentSettingsPanel } from "./AgentSettingsPanel";
import { BackupOperationsPanel } from "./BackupOperationsPanel";
import { PrintDeploymentPanel } from "./PrintDeploymentPanel";
import { UpdateSettingsPanel } from "./UpdateSettingsPanel";
import "./SystemSettings.css";

const entries = [
  { id: "quota", title: "配额与审批", description: "基础额度、统一审批人及跳过规则", icon: SlidersHorizontal, group: "资源与权限" },
  { id: "superusers", title: "超级用户", description: "管理可不限个人额度的成员", icon: ShieldCheck, group: "资源与权限" },
  { id: "images", title: "镜像与环境", description: "按分类查找、推荐版本和暂停新建", icon: Boxes, group: "资源与权限" },
  { id: "agent", title: "微信 Agent", description: "主备模型、连接地址和密钥", icon: Bot, group: "服务与维护" },
  { id: "printer", title: "打印机接入", description: "注册 Windows 打印电脑、下载安装包", icon: Printer, group: "服务与维护" },
  { id: "backups", title: "控制面备份", description: "校验、恢复演练及恢复授权", icon: DatabaseBackup, group: "服务与维护" },
  { id: "updates", title: "版本与更新", description: "查看发布版本、更新通道与升级说明", icon: RefreshCw, group: "服务与维护" },
] as const;

export function SystemSettingsPanel({ data, onUpdated }: { data: DashboardData; onUpdated: () => void }) {
  const [selected, setSelected] = useState<string | null>(null);
  const entry = entries.find(item => item.id === selected);
  return <div className="settings-hub">
    <div className="settings-intro"><div><span className="settings-eyebrow">管理控制台</span><h2>系统设置</h2><p>选择一项进行配置。资源权限与服务接入集中管理。</p></div><span className="settings-admin-label"><ShieldCheck size={15} />仅管理员可配置</span></div>
    <div className="settings-groups">{["资源与权限", "服务与维护"].map(group => <section className="settings-group" key={group} aria-label={group}>
      <h3>{group}</h3>{entries.filter(item => item.group === group).map(item => <button key={item.id} className="settings-entry" aria-label={`管理${item.title}`} onClick={() => setSelected(item.id)}>
        <span className="settings-entry-icon"><item.icon size={21} strokeWidth={1.6} /></span><span className="settings-entry-copy"><strong>{item.title}</strong><span>{item.description}</span>
          {item.id === "images" && <small>{data.images.length} 个环境</small>}{item.id === "backups" && <small>保留最近 10 个</small>}</span><ArrowUpRight size={17} className="settings-entry-arrow" />
      </button>)}
    </section>)}</div>
    <div className="settings-storage-note"><DatabaseBackup size={19} /><div><strong>数据与恢复</strong><p>个人文件各自隔离，保存在数据盘。容器快照保留最近 3 个，30 天后清理，至少留 1 个；正在使用和恢复中的快照会受到保护。</p><p>运行环境容量预警见「概览 → 服务状态」。容量保护启用后，已用达到 80% 或可用不足 100 GiB 时提醒；达到 90% 或可用不足 50 GiB 时暂停新建、镜像下载和快照生成，已有容器继续运行。</p></div></div>
    {entry && <AccessibleDialog title={entry.title} className="settings-dialog" onClose={() => setSelected(null)}>
      <div className="settings-dialog-content">
        {selected === "quota" && <QuotaSettingsPanel section="rules" />}
        {selected === "superusers" && <QuotaSettingsPanel section="superusers" />}
        {selected === "images" && <ImageVersionsPanel images={data.images} onUpdated={onUpdated} />}
        {selected === "agent" && <AgentSettingsPanel />}
        {selected === "printer" && <PrintDeploymentPanel />}
        {selected === "updates" && <UpdateSettingsPanel />}
        {selected === "backups" && <BackupOperationsPanel actorRole="admin" initialStatus={data.backups} onUpdated={onUpdated} />}
      </div>
    </AccessibleDialog>}
  </div>;
}
