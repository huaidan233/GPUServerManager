"use client";

import { useState } from "react";
import { AccessibleDialog } from "./AccessibleDialog";
import type { ApprovalSummary, ServerSummary } from "../lib/mockData";

export function ApprovalDetails({ approval, servers = [] }: { approval: ApprovalSummary; servers?: ServerSummary[] }) {
  const [open, setOpen] = useState(false);
  const spec = approval.specification;
  return <>
    <button type="button" className="secondary-button" aria-label={`查看申请详情 ${approval.id}`} onClick={() => setOpen(true)}>查看详情</button>
    {open && <AccessibleDialog title="申请详情" className="approval-details-dialog" onClose={() => setOpen(false)}>
      <dl className="container-facts">
        <div><dt>申请编号</dt><dd>{approval.id}</dd></div>
        <div><dt>申请人</dt><dd>{approval.requesterUserId}</dd></div>
        <div><dt>容器所有者</dt><dd>{approval.ownerUserId}</dd></div>
        <div><dt>状态</dt><dd>{({ pending: "待审批", approved: "已批准", rejected: "已拒绝", consumed: "已创建", executing: "创建中", expired: "已过期" } as Record<string, string>)[approval.status] ?? approval.status}</dd></div>
        <div><dt>申请时间</dt><dd>{formatTime(approval.createdAt)}</dd></div>
        <div><dt>有效期至</dt><dd>{formatTime(approval.expiresAt)}</dd></div>
        <div><dt>审批触发项</dt><dd>{approval.triggerReasons.map((reason) => ({ gpu_count: "GPU 数量超出自主配额", memory_gb: "内存超出自主配额" }[reason] ?? reason)).join("、") || "未记录"}</dd></div>
      </dl>
      {spec ? <>
        <h3>申请的容器配置</h3>
        <dl className="container-facts">
          <div><dt>容器名称</dt><dd>{spec.name}</dd></div>
          <div><dt>服务器</dt><dd>{servers.find((server) => server.id === spec.server_id)?.name ?? spec.server_id}</dd></div>
          <div><dt>镜像</dt><dd><code>{spec.image}</code></dd></div>
          <div><dt>资源套餐</dt><dd>{spec.preset}</dd></div>
          <div><dt>CPU</dt><dd>{spec.cpu_limit} 核</dd></div>
          <div><dt>内存</dt><dd>{spec.memory_limit_gb} GiB</dd></div>
          <div><dt>GPU</dt><dd>{spec.gpu_devices.length ? spec.gpu_devices.map((id) => `GPU ${id}`).join("、") : "不使用 GPU"}</dd></div>
          <div><dt>挂载</dt><dd>{spec.mount_profile_ids.length ? spec.mount_profile_ids.join("、") : "无"}</dd></div>
          <div><dt>SSH</dt><dd>{spec.ssh_enabled ? "启用" : "关闭"}</dd></div>
          <div><dt>创建后启动</dt><dd>{spec.auto_start ? "是" : "否"}</dd></div>
          <div><dt>显存需求</dt><dd>{({ lt_8: "低于 8 GB", "8_24": "8–24 GB", "24_48": "24–48 GB", full_card: "整卡" } as Record<string, string>)[spec.memory_tier ?? ""] ?? "未记录"}</dd></div>
          <div><dt>预计使用时长</dt><dd>{spec.estimated_duration_minutes ? `${spec.estimated_duration_minutes} 分钟` : "未记录"}</dd></div>
          <div><dt>用途备注</dt><dd>{spec.note || "未填写"}</dd></div>
        </dl>
      </> : <p>此历史申请未记录容器配置。</p>}
      {(approval.reviewerUserId || approval.reviewReason) && <>
        <h3>审批记录</h3><dl className="container-facts">
          <div><dt>审批人</dt><dd>{approval.reviewerUserId || "未记录"}</dd></div>
          <div><dt>审批理由</dt><dd>{approval.reviewReason || "未填写"}</dd></div>
          <div><dt>审批时间</dt><dd>{formatTime(approval.decidedAt)}</dd></div>
        </dl>
      </>}
      {approval.containerId && <p>已创建容器：<code>{approval.containerId}</code></p>}
    </AccessibleDialog>}
  </>;
}

function formatTime(value?: string): string {
  if (!value) return "未记录";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}
