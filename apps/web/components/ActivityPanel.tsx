"use client";

import { useEffect, useState } from "react";

import type { ActivityEntry } from "../lib/api";


const channelLabels: Record<string, string> = {
  web: "Web",
  wechat: "微信 Agent",
  personal_api: "个人 API",
  local_cli: "本地命令行",
  worker: "系统任务",
};

const actionLabels: Record<string, string> = {
  stop_container: "停止容器",
  start_container: "启动容器",
  delete_container: "删除容器",
  create_container: "创建容器",
  extend_member_program: "延期学籍",
  promote_member_to_doctor: "硕转博",
  correct_member_profile: "更正学籍",
};

type ActivityPanelProps = {
  initialEntries?: ActivityEntry[];
};

export function ActivityPanel({ initialEntries }: ActivityPanelProps) {
  const [entries, setEntries] = useState<ActivityEntry[] | null>(
    initialEntries ?? null,
  );
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (initialEntries) return;
    let active = true;
    fetch("/api/gsm/account/activity")
      .then(async (response) => {
        if (!response.ok) throw new Error("request failed");
        return (await response.json()) as ActivityEntry[];
      })
      .then((value) => {
        if (active) setEntries(value);
      })
      .catch(() => {
        if (active) setError("活动记录暂时不可用。");
      });
    return () => {
      active = false;
    };
  }, [initialEntries]);

  return (
    <section className="activity-workspace" aria-labelledby="activity-heading">
      <div className="workspace-heading">
        <h2 id="activity-heading">活动记录</h2>
      </div>
      {error && <p role="alert">{error}</p>}
      {entries === null && !error && <p role="status">正在加载...</p>}
      {entries?.length === 0 && <p className="empty-state">暂无活动记录</p>}
      {entries && entries.length > 0 && (
        <div className="table-scroll">
          <table aria-label="本人活动记录">
            <thead>
              <tr>
                <th>渠道</th>
                <th>操作</th>
                <th>目标</th>
                <th>结果</th>
                <th>时间</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr key={entry.id}>
                  <td>{channelLabels[entry.actor_channel] ?? entry.actor_channel}</td>
                  <td>{actionLabels[entry.action_type] ?? entry.action_type}</td>
                  <td>
                    <span>{entry.target_type}</span>
                    <code>{entry.target_id}</code>
                  </td>
                  <td>{entry.result}</td>
                  <td>{formatDateTime(entry.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function formatDateTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : new Intl.DateTimeFormat("zh-CN", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      }).format(parsed);
}
