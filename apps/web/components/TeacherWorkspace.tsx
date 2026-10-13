"use client";
import { ApprovalDetails } from "./ApprovalDetails";

import {
  CalendarPlus,
  Check,
  GraduationCap,
  Pencil,
  X,
} from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";

import { authenticatedFetch } from "../lib/authenticatedFetch";
import type { TeacherStudent } from "../lib/api";
import type { ApprovalSummary } from "../lib/mockData";
import { AccessibleDialog } from "./AccessibleDialog";


type AcademicAction = "profile-correction" | "extend" | "promote-to-doctor";

type OpenAction = {
  kind: AcademicAction;
  student: TeacherStudent;
};

type AcademicProgram = {
  level?: string;
  enrollment_date?: string;
  expected_graduation_date?: string;
};

type TeacherWorkspaceProps = {
  initialStudents: TeacherStudent[];
  initialApprovals: ApprovalSummary[];
  view?: "all" | "students" | "approvals";
  onUpdated?: () => void;
  refreshing?: boolean;
};

export function TeacherWorkspace({
  initialStudents,
  initialApprovals,
  view = "all",
  onUpdated,
  refreshing = false,
}: TeacherWorkspaceProps) {
  const [students, setStudents] = useState(initialStudents);
  const [approvals, setApprovals] = useState(initialApprovals);
  const [action, setAction] = useState<OpenAction | null>(null);
  const [displayName, setDisplayName] = useState("");
  const [enrollmentDate, setEnrollmentDate] = useState("");
  const [graduationDate, setGraduationDate] = useState("");
  const [enrollmentYear, setEnrollmentYear] = useState("");
  const [effectiveDate, setEffectiveDate] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [reason, setReason] = useState("");
  const [approvalReasons, setApprovalReasons] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { setApprovals(initialApprovals); }, [initialApprovals]);
  useEffect(() => { setStudents(initialStudents); }, [initialStudents]);

  function openAction(kind: AcademicAction, student: TeacherStudent) {
    const current = currentProgram(student);
    const nextYear = new Date().getFullYear() + 1;
    setAction({ kind, student });
    setDisplayName(student.display_name);
    setEnrollmentDate(current.enrollment_date ?? "");
    setGraduationDate(current.expected_graduation_date ?? "");
    setEnrollmentYear(String(nextYear));
    setEffectiveDate(`${nextYear}-09-01`);
    setConfirmation("");
    setReason("");
    setError(null);
  }

  async function submitAcademicAction(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!action) return;
    setBusy(true);
    setError(null);
    const payload = academicPayload(action.kind, action.student.version, {
      displayName,
      enrollmentDate,
      graduationDate,
      enrollmentYear,
      effectiveDate,
      confirmation,
      reason,
    });
    try {
      const response = await authenticatedFetch(
        `/api/gsm/teacher/students/${encodeURIComponent(action.student.id)}/${action.kind}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      );
      if (!response.ok) throw new Error(await responseDetail(response));
      const updated = (await response.json()) as TeacherStudent;
      setStudents((current) =>
        current.map((student) => student.id === updated.id ? updated : student),
      );
      setAction(null);
      setMessage("学籍信息已更新。");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "学籍操作失败。");
    } finally {
      setBusy(false);
    }
  }

  async function decideApproval(
    approval: ApprovalSummary,
    decision: "approved" | "rejected",
  ) {
    const reviewReason = approvalReasons[approval.id]?.trim();
    if (!reviewReason) return;
    setBusy(true);
    setError(null);
    try {
      const response = await authenticatedFetch(
        `/api/gsm/approvals/${encodeURIComponent(approval.id)}/decision`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ decision, reason: reviewReason }),
        },
      );
      if (!response.ok) throw new Error(await responseDetail(response));
      const result = await response.json();
      setApprovals((current) => current.map((item) => item.id === approval.id ? { ...item, status: decision, reviewReason, reviewerUserId: result.reviewer_user_id, decidedAt: result.decided_at } : item));
      setMessage(decision === "approved" ? "申请已批准。" : "申请已拒绝。");
      onUpdated?.();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "审批失败。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="teacher-workspace">
      {message && <p role="status">{message}</p>}
      {error && <p role="alert">{error}</p>}
      {view !== "approvals" && <section aria-labelledby="assigned-students-heading">
        <div className="workspace-heading">
          <h2 id="assigned-students-heading">我的学生</h2>
          <span>{students.length} 人</span>
        </div>
        {students.length === 0 ? (
          <p className="empty-state">暂无已分配学生</p>
        ) : (
          <div className="table-scroll">
            <table aria-label="已分配学生列表">
              <thead>
                <tr>
                  <th>学生</th>
                  <th>培养阶段</th>
                  <th>预计毕业</th>
                  <th>状态</th>
                  <th>学籍操作</th>
                </tr>
              </thead>
              <tbody>
                {students.map((student) => {
                  const program = currentProgram(student);
                  return (
                    <tr key={student.id}>
                      <td>
                        <strong>{student.display_name}</strong>
                        <small>{student.username}</small>
                      </td>
                      <td>{program.level === "doctor" ? "博士" : "硕士"}</td>
                      <td>{program.expected_graduation_date ?? "-"}</td>
                      <td>{student.status}</td>
                      <td>
                        <div className="row-actions">
                          <button
                            className="secondary-button"
                            type="button"
                            aria-label={`更正 ${student.display_name} 的学籍`}
                            onClick={() => openAction("profile-correction", student)}
                          >
                            <Pencil size={15} aria-hidden="true" />
                            更正
                          </button>
                          <button
                            className="secondary-button"
                            type="button"
                            aria-label={`延期 ${student.display_name}`}
                            onClick={() => openAction("extend", student)}
                          >
                            <CalendarPlus size={15} aria-hidden="true" />
                            延期
                          </button>
                          {program.level === "master" && (
                            <button
                              className="secondary-button"
                              type="button"
                              aria-label={`${student.display_name} 硕转博`}
                              onClick={() => openAction("promote-to-doctor", student)}
                            >
                              <GraduationCap size={15} aria-hidden="true" />
                              硕转博
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>}

      {view !== "students" && <section aria-labelledby="teacher-approvals-heading">
        <div className="workspace-heading">
          <h2 id="teacher-approvals-heading">学生申请</h2>
          <span>{approvals.length} 项</span>
          {onUpdated && <button className="secondary-button" type="button" disabled={busy || refreshing} onClick={onUpdated}>{refreshing ? "刷新中…" : "刷新学生申请"}</button>}
        </div>
        {approvals.length === 0 ? (
          <p className="empty-state">暂无待审批申请</p>
        ) : (
          <div className="approval-list">
            {approvals.map((approval) => (
              <div className="approval-row" key={approval.id}>
                <div>
                  <strong>{approval.ownerUserId}</strong>
                  <code>{approval.id}</code>
                  <ApprovalDetails approval={approval} />
                </div>
                <span className="status-pill">{({ pending: "待审批", approved: "已批准", rejected: "已拒绝", consumed: "已创建", expired: "已过期", executing: "创建中" } as Record<string, string>)[approval.status] ?? approval.status}</span>
                {approval.status === "pending" && <><input
                  aria-label={`审批理由 ${approval.id}`}
                  value={approvalReasons[approval.id] ?? ""}
                  onChange={(event) => setApprovalReasons((current) => ({
                    ...current,
                    [approval.id]: event.target.value,
                  }))}
                  placeholder="审批理由"
                />
                <div className="row-actions">
                  <button
                    className="secondary-button"
                    type="button"
                    disabled={busy || !approvalReasons[approval.id]?.trim()}
                    aria-label={`批准 ${approval.id}`}
                    onClick={() => decideApproval(approval, "approved")}
                  >
                    <Check size={15} aria-hidden="true" />
                    批准
                  </button>
                  <button
                    className="secondary-button"
                    type="button"
                    disabled={busy || !approvalReasons[approval.id]?.trim()}
                    aria-label={`拒绝 ${approval.id}`}
                    onClick={() => decideApproval(approval, "rejected")}
                  >
                    <X size={15} aria-hidden="true" />
                    拒绝
                  </button>
                </div></>}
              </div>
            ))}
          </div>
        )}
      </section>}

      {action && (
        <AccessibleDialog
          title={actionTitle(action.kind, action.student.display_name)}
          className="academic-dialog"
          destructiveBusy={busy}
          onClose={() => setAction(null)}
        >
            <form className="academic-form" onSubmit={submitAcademicAction}>
              {action.kind === "profile-correction" && (
                <>
                  <label>
                    姓名
                    <input
                      value={displayName}
                      onChange={(event) => setDisplayName(event.target.value)}
                      required
                    />
                  </label>
                  <label>
                    入学日期
                    <input
                      type="date"
                      value={enrollmentDate}
                      onChange={(event) => setEnrollmentDate(event.target.value)}
                    />
                  </label>
                  <label>
                    预计毕业日期
                    <input
                      type="date"
                      value={graduationDate}
                      onChange={(event) => setGraduationDate(event.target.value)}
                    />
                  </label>
                </>
              )}
              {action.kind === "extend" && (
                <label>
                  新的预计毕业日期
                  <input
                    type="date"
                    value={graduationDate}
                    onChange={(event) => setGraduationDate(event.target.value)}
                    required
                  />
                </label>
              )}
              {action.kind === "promote-to-doctor" && (
                <>
                  <label>
                    博士入学年份
                    <input
                      type="number"
                      min="1900"
                      max="2200"
                      value={enrollmentYear}
                      onChange={(event) => setEnrollmentYear(event.target.value)}
                      required
                    />
                  </label>
                  <label>
                    生效日期
                    <input
                      type="date"
                      value={effectiveDate}
                      onChange={(event) => setEffectiveDate(event.target.value)}
                      required
                    />
                  </label>
                  <label>
                    确认短语
                    <input
                      value={confirmation}
                      onChange={(event) => setConfirmation(event.target.value)}
                      autoComplete="off"
                      required
                    />
                  </label>
                </>
              )}
              <label>
                {action.kind === "extend" ? "延期原因" : "变更原因"}
                <textarea
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                  required
                />
              </label>
              {error && <p role="alert">{error}</p>}
              <div className="dialog-actions">
                <button
                  className="secondary-button"
                  type="button"
                  disabled={busy}
                  onClick={() => setAction(null)}
                >
                  取消
                </button>
                <button className="primary-button" type="submit" disabled={busy}>
                  {submitLabel(action.kind)}
                </button>
              </div>
            </form>
        </AccessibleDialog>
      )}
    </div>
  );
}

function currentProgram(student: TeacherStudent): AcademicProgram {
  return (student.current_program ?? {}) as AcademicProgram;
}

function academicPayload(
  kind: AcademicAction,
  version: number,
  values: {
    displayName: string;
    enrollmentDate: string;
    graduationDate: string;
    enrollmentYear: string;
    effectiveDate: string;
    confirmation: string;
    reason: string;
  },
): Record<string, unknown> {
  if (kind === "extend") {
    return {
      expected_version: version,
      expected_graduation_date: values.graduationDate,
      reason: values.reason.trim(),
    };
  }
  if (kind === "promote-to-doctor") {
    return {
      expected_version: version,
      enrollment_year: Number(values.enrollmentYear),
      effective_date: values.effectiveDate,
      reason: values.reason.trim(),
      confirmation: values.confirmation,
    };
  }
  return {
    expected_version: version,
    display_name: values.displayName.trim(),
    enrollment_date: values.enrollmentDate || undefined,
    expected_graduation_date: values.graduationDate || undefined,
    reason: values.reason.trim(),
  };
}

function actionTitle(kind: AcademicAction, name: string): string {
  if (kind === "extend") return `延期 ${name}`;
  if (kind === "promote-to-doctor") return `${name} 硕转博`;
  return `更正 ${name} 的学籍`;
}

function submitLabel(kind: AcademicAction): string {
  if (kind === "extend") return "确认延期";
  if (kind === "promote-to-doctor") return "确认硕转博";
  return "保存更正";
}

async function responseDetail(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { detail?: string };
    return payload.detail ?? "操作失败。";
  } catch {
    return "操作失败。";
  }
}
