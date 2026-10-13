import { Boxes, Cpu, Users } from "lucide-react";

import type { TeacherStudent } from "../lib/api";
import type { ContainerSummary, HealthEventSummary } from "../lib/mockData";


type TeacherUsagePanelProps = {
  students: TeacherStudent[];
  containers: ContainerSummary[];
  healthEvents: HealthEventSummary[];
};

export function TeacherUsagePanel({ students, containers, healthEvents }: TeacherUsagePanelProps) {
  const memberIds = new Set(students.map((student) => student.id));
  const legacyNames = new Set(
    students.flatMap((student) => [student.username, student.display_name])
      .map((value) => value.toLocaleLowerCase()),
  );
  const visibleContainers = containers.filter((container) =>
    container.ownerMemberId
      ? memberIds.has(container.ownerMemberId)
      : legacyNames.has(container.user.toLocaleLowerCase()),
  );
  const studentById = new Map(students.map((student) => [student.id, student]));
  const relevantWarnings = healthEvents.filter((event) =>
    event.status === "open"
    && (
      (event.containerId && visibleContainers.some((container) => container.id === event.containerId))
      || (event.ownerUserId && legacyNames.has(event.ownerUserId.toLocaleLowerCase()))
    ),
  );
  const running = visibleContainers.filter((container) => container.status === "running").length;
  const gpuCount = visibleContainers.reduce((count, container) => count + container.gpus.length, 0);

  return (
    <section className="teacher-usage-workspace" aria-labelledby="teacher-usage-heading">
      <div className="workspace-heading">
        <div>
          <h2 id="teacher-usage-heading">学生资源使用情况</h2>
          <p>仅统计当前分配给你的学生。</p>
        </div>
      </div>

      <dl className="teacher-usage-summary">
        <div><Users size={18} aria-hidden="true" /><dt>学生</dt><dd>{students.length}</dd></div>
        <div><Boxes size={18} aria-hidden="true" /><dt>运行中容器</dt><dd>{running}</dd></div>
        <div><Cpu size={18} aria-hidden="true" /><dt>已分配 GPU</dt><dd>{gpuCount}</dd></div>
      </dl>

      {visibleContainers.length === 0 ? (
        <p className="empty-state">学生当前没有容器</p>
      ) : (
        <div className="table-scroll">
          <table aria-label="已分配学生资源使用列表">
            <thead><tr><th>学生</th><th>容器</th><th>服务器</th><th>GPU</th><th>状态</th></tr></thead>
            <tbody>
              {visibleContainers.map((container) => {
                const student = container.ownerMemberId
                  ? studentById.get(container.ownerMemberId)
                  : students.find((item) => legacyNames.has(container.user.toLocaleLowerCase()) && [item.username, item.display_name].some((value) => value.toLocaleLowerCase() === container.user.toLocaleLowerCase()));
                return (
                  <tr key={container.id}>
                    <td>{student?.display_name ?? container.user}</td>
                    <td><strong>{container.name}</strong></td>
                    <td>{container.server}</td>
                    <td>{container.gpus.join(", ") || "-"}</td>
                    <td><span className={`status-pill ${container.status}`}>{container.statusText}</span></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {relevantWarnings.length > 0 && (
        <div className="teacher-warning-list" aria-label="学生资源告警">
          <h3>需要关注</h3>
          {relevantWarnings.map((event) => <p key={event.id}>{event.code} · {event.severity}</p>)}
        </div>
      )}
    </section>
  );
}
