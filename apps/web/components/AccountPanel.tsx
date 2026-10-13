"use client";

import {
  GraduationCap,
  KeyRound,
  Link2,
  LogOut,
  MessageCircle,
  Monitor,
  RefreshCw,
  ShieldCheck,
  Unlink,
  UserRound,
} from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";

import { AccessibleDialog } from "./AccessibleDialog";
import { authenticatedFetch } from "../lib/authenticatedFetch";


type Program = {
  id: string;
  level: "master" | "doctor";
  enrollment_date: string;
  expected_graduation_date: string;
  actual_end_date?: string | null;
};

type Account = {
  id: string;
  username: string;
  display_name: string;
  role: string;
  status: string;
  current_program: Program | null;
};

type WechatBinding = {
  route_key: string;
  status: string;
};

type Session = {
  id: string;
  created_at?: string | null;
  last_seen_at?: string | null;
  idle_expires_at?: string | null;
  absolute_expires_at?: string | null;
  revoked_at?: string | null;
  browser_family?: string | null;
  os_family?: string | null;
  is_current?: boolean;
};

type AccountAction = "revoke-sessions" | "wechat-pair" | "wechat-unbind";

type AccountPanelProps = {
  onLoggedOut?: () => void;
};

export function AccountPanel({ onLoggedOut }: AccountPanelProps) {
  const [account, setAccount] = useState<Account | null>(null);
  const [programs, setPrograms] = useState<Program[]>([]);
  const [binding, setBinding] = useState<WechatBinding | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [passwordStatus, setPasswordStatus] = useState("");
  const [savingPassword, setSavingPassword] = useState(false);
  const [action, setAction] = useState<AccountAction | null>(null);
  const [confirmationPassword, setConfirmationPassword] = useState("");
  const [actionBusy, setActionBusy] = useState(false);
  const [pairingCode, setPairingCode] = useState<string | null>(null);
  const [refreshingBinding, setRefreshingBinding] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const [profile, history, wechat, sessionList] = await Promise.all([
          fetchJson<Account>("/api/gsm/account"),
          fetchJson<Program[]>("/api/gsm/account/programs"),
          fetchJson<{ binding: WechatBinding | null }>("/api/gsm/account/wechat-binding"),
          fetchJson<Session[]>("/api/gsm/account/sessions"),
        ]);
        if (!active) return;
        setAccount(profile);
        setPrograms(history);
        setBinding(wechat.binding);
        setSessions(sessionList);
      } catch {
        if (active) setError("账户信息暂时无法加载。");
      } finally {
        if (active) setLoading(false);
      }
    }
    void load();
    return () => {
      active = false;
    };
  }, []);

  function openAction(nextAction: AccountAction) {
    setAction(nextAction);
    setConfirmationPassword("");
    setError("");
    setStatus("");
  }

  async function changePassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSavingPassword(true);
    setError("");
    setPasswordStatus("");
    try {
      const response = await authenticatedFetch("/api/gsm/auth/password/change", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
      });
      if (!response.ok) throw new Error(await responseDetail(response, "密码更新失败。"));
      setCurrentPassword("");
      setNewPassword("");
      setPasswordStatus("密码已更新。");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "密码更新失败。");
    } finally {
      setSavingPassword(false);
    }
  }

  async function submitConfirmedAction(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!action) return;
    setActionBusy(true);
    setError("");
    try {
      const response = await authenticatedFetch(actionEndpoint(action), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: confirmationPassword }),
      });
      if (!response.ok) throw new Error(await responseDetail(response, "操作失败，请重试。"));
      const payload = await safeJson(response);
      if (action === "revoke-sessions") {
        const count = numberField(payload, "revoked_count");
        setSessions((current) => current.filter((session) => session.is_current || session.revoked_at));
        setStatus(`已撤销 ${count} 个其他会话。`);
      } else if (action === "wechat-pair") {
        const code = stringField(payload, "wechat_pairing_code");
        if (!code) throw new Error("配对码响应无效，请重试。");
        setPairingCode(code);
      } else {
        setBinding(null);
        setStatus("微信已解绑。");
      }
      setAction(null);
      setConfirmationPassword("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "操作失败，请重试。");
    } finally {
      setActionBusy(false);
    }
  }

  async function refreshWechatBinding() {
    setRefreshingBinding(true);
    setError("");
    try {
      const value = await fetchJson<{ binding: WechatBinding | null }>(
        "/api/gsm/account/wechat-binding",
      );
      setBinding(value.binding);
    } catch {
      setError("微信状态暂时无法刷新。");
    } finally {
      setRefreshingBinding(false);
    }
  }

  async function logout() {
    setLoggingOut(true);
    setError("");
    try {
      const response = await authenticatedFetch("/api/gsm/auth/logout", { method: "POST" });
      if (!response.ok) throw new Error("退出失败，请重试。");
      if (onLoggedOut) onLoggedOut();
      else window.location.reload();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "退出失败，请重试。");
      setLoggingOut(false);
    }
  }

  if (loading) {
    return <p className="workspace-loading" role="status">正在加载账户信息...</p>;
  }
  if (!account) {
    return <p className="workspace-error" role="alert">{error || "账户信息暂时无法加载。"}</p>;
  }

  const hasOtherActiveSession = sessions.some(
    (session) => !session.is_current && !session.revoked_at,
  );

  return (
    <section className="account-workspace" aria-labelledby="account-heading">
      <header className="workspace-heading">
        <div className="workspace-title">
          <UserRound size={22} aria-hidden="true" />
          <div>
            <p className="eyebrow">我的账户</p>
            <h2 id="account-heading">{account.display_name}</h2>
            <p>@{account.username}</p>
          </div>
        </div>
        <div className="account-header-actions">
          <div className="identity-badges">
            <span className="role-badge"><ShieldCheck size={14} aria-hidden="true" />{roleLabel(account.role)}</span>
            <span className={`status-pill ${account.status}`}>{statusLabel(account.status)}</span>
          </div>
          <button className="secondary-button" type="button" disabled={loggingOut} onClick={logout}>
            <LogOut size={16} aria-hidden="true" />退出登录
          </button>
        </div>
      </header>

      {error && <p className="workspace-error" role="alert">{error}</p>}
      {status && <p className="table-message" role="status">{status}</p>}

      <div className="account-columns">
        <section className="workspace-section" aria-labelledby="program-heading">
          <div className="section-title-row">
            <GraduationCap size={18} aria-hidden="true" />
            <h3 id="program-heading">培养信息</h3>
          </div>
          {account.current_program ? (
            <dl className="detail-list">
              <div><dt>培养层次</dt><dd><DegreeBadge level={account.current_program.level} /></dd></div>
              <div><dt>入学日期</dt><dd>{account.current_program.enrollment_date}</dd></div>
              <div><dt>预期毕业</dt><dd className="graduation-date">{account.current_program.expected_graduation_date}</dd></div>
            </dl>
          ) : (
            <p className="empty-state">暂无培养记录</p>
          )}

          {programs.filter((program) => program.id !== account.current_program?.id).length > 0 && (
            <div className="table-wrap compact-table">
              <table aria-label="培养经历">
                <thead><tr><th>层次</th><th>入学</th><th>预期毕业</th><th>实际结束</th></tr></thead>
                <tbody>
                  {programs.filter((program) => program.id !== account.current_program?.id).map((program) => (
                    <tr key={program.id}>
                      <td><DegreeBadge level={program.level} /></td>
                      <td>{program.enrollment_date}</td>
                      <td>{program.expected_graduation_date}</td>
                      <td>{program.actual_end_date ?? "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <section className="workspace-section" aria-labelledby="binding-heading">
          <div className="section-title-row">
            <MessageCircle size={18} aria-hidden="true" />
            <h3 id="binding-heading">微信身份</h3>
          </div>
          <p className={binding ? "binding-state bound" : "binding-state"}>
            {binding ? "微信已绑定" : "微信未绑定"}
          </p>
          {binding && <p className="mono-cell">{binding.route_key}</p>}
          <div className="inline-actions">
            {binding ? (
              <button className="secondary-button danger-action" type="button" onClick={() => openAction("wechat-unbind")}>
                <Unlink size={16} aria-hidden="true" />解绑微信
              </button>
            ) : (
              <button className="secondary-button" type="button" onClick={() => openAction("wechat-pair")}>
                <Link2 size={16} aria-hidden="true" />生成微信配对码
              </button>
            )}
            <button className="icon-button" type="button" aria-label="刷新微信状态" title="刷新微信状态" disabled={refreshingBinding} onClick={refreshWechatBinding}>
              <RefreshCw size={16} aria-hidden="true" />
            </button>
          </div>
        </section>
      </div>

      <section className="workspace-section" aria-labelledby="sessions-heading">
        <div className="section-title-row section-title-actions">
          <div>
            <Monitor size={18} aria-hidden="true" />
            <h3 id="sessions-heading">登录会话</h3>
          </div>
          <button className="secondary-button" type="button" disabled={!hasOtherActiveSession} onClick={() => openAction("revoke-sessions")}>
            撤销其他会话
          </button>
        </div>
        <div className="table-wrap compact-table">
          <table aria-label="我的登录会话">
            <thead><tr><th>设备</th><th>创建时间</th><th>最近活动</th><th>状态</th></tr></thead>
            <tbody>
              {sessions.length === 0 ? (
                <tr><td colSpan={4} className="empty-state">暂无会话记录</td></tr>
              ) : sessions.map((session) => (
                <tr key={session.id}>
                  <td>
                    <strong>{sessionDevice(session)}</strong>
                    <code className="session-id">{session.id}</code>
                  </td>
                  <td>{formatTimestamp(session.created_at)}</td>
                  <td>{formatTimestamp(session.last_seen_at)}</td>
                  <td>{session.is_current ? "当前会话" : session.revoked_at ? "已撤销" : "有效"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="workspace-section password-section" aria-labelledby="password-heading">
        <div className="section-title-row">
          <KeyRound size={18} aria-hidden="true" />
          <h3 id="password-heading">更新密码</h3>
        </div>
        <form className="password-form" onSubmit={changePassword}>
          <label htmlFor="current-password">当前密码</label>
          <input id="current-password" type="password" autoComplete="current-password" required value={currentPassword} onChange={(event) => setCurrentPassword(event.currentTarget.value)} />
          <label htmlFor="new-password">新密码</label>
          <input id="new-password" type="password" autoComplete="new-password" minLength={12} required value={newPassword} onChange={(event) => setNewPassword(event.currentTarget.value)} />
          <button className="primary-button" type="submit" disabled={savingPassword}>
            <KeyRound size={16} aria-hidden="true" />更新密码
          </button>
          {passwordStatus && <p className="table-message" role="status">{passwordStatus}</p>}
        </form>
      </section>

      {action && (
        <AccessibleDialog
          title={actionTitle(action)}
          onClose={() => setAction(null)}
          destructiveBusy={actionBusy}
        >
          <form className="dialog-form" onSubmit={submitConfirmedAction}>
            <p>{actionDescription(action)}</p>
            <label htmlFor="account-confirm-password">确认当前密码</label>
            <input id="account-confirm-password" type="password" autoComplete="current-password" required value={confirmationPassword} onChange={(event) => setConfirmationPassword(event.currentTarget.value)} />
            <div className="dialog-actions">
              <button className="secondary-button" type="button" disabled={actionBusy} onClick={() => setAction(null)}>取消</button>
              <button className={action === "wechat-unbind" ? "danger-button" : "primary-button"} type="submit" disabled={actionBusy || !confirmationPassword}>
                {actionSubmitLabel(action)}
              </button>
            </div>
          </form>
        </AccessibleDialog>
      )}

      {pairingCode && (
        <AccessibleDialog title="一次性微信配对码" onClose={() => setPairingCode(null)}>
          <p className="warning-text">关闭后不再显示</p>
          <code className="one-time-code">{pairingCode}</code>
          <div className="dialog-actions">
            <button className="primary-button" type="button" onClick={() => setPairingCode(null)}>已妥善记录</button>
          </div>
        </AccessibleDialog>
      )}
    </section>
  );
}

function DegreeBadge({ level }: { level: Program["level"] }) {
  return <span className={`degree-badge ${level}`}>{level === "doctor" ? "博士" : "硕士"}</span>;
}

function actionEndpoint(action: AccountAction): string {
  if (action === "revoke-sessions") return "/api/gsm/auth/sessions/revoke-others";
  if (action === "wechat-pair") return "/api/gsm/account/wechat-pairing-code";
  return "/api/gsm/account/wechat-unbind";
}

function actionTitle(action: AccountAction): string {
  if (action === "revoke-sessions") return "撤销其他会话";
  if (action === "wechat-pair") return "生成微信配对码";
  return "解绑微信";
}

function actionDescription(action: AccountAction): string {
  if (action === "revoke-sessions") return "当前会话将保留，其他有效登录会话将立即失效。";
  if (action === "wechat-pair") return "配对码仅显示一次，请只在课题组总管理 Agent 中使用。";
  return "解绑后，当前微信将立即失去通过总管理 Agent 操作的权限。";
}

function actionSubmitLabel(action: AccountAction): string {
  if (action === "revoke-sessions") return "确认撤销";
  if (action === "wechat-pair") return "确认生成";
  return "确认解绑";
}

async function fetchJson<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) throw new Error("request failed");
  return (await response.json()) as T;
}

async function safeJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

async function responseDetail(response: Response, fallback: string): Promise<string> {
  return publicDetail(await safeJson(response), fallback);
}

function publicDetail(payload: unknown, fallback: string): string {
  if (payload && typeof payload === "object" && "detail" in payload) {
    const detail = (payload as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.length <= 240) return detail;
  }
  return fallback;
}

function stringField(payload: unknown, field: string): string | null {
  if (!payload || typeof payload !== "object") return null;
  const value = (payload as Record<string, unknown>)[field];
  return typeof value === "string" ? value : null;
}

function numberField(payload: unknown, field: string): number {
  if (!payload || typeof payload !== "object") return 0;
  const value = (payload as Record<string, unknown>)[field];
  return typeof value === "number" ? value : 0;
}

function sessionDevice(session: Session): string {
  return `${session.browser_family ?? "其他浏览器"} · ${session.os_family ?? "其他系统"}`;
}

function roleLabel(role: string): string {
  return ({ viewer: "只读", user: "成员", power_user: "高级成员", teacher: "老师", admin: "管理员" } as Record<string, string>)[role] ?? role;
}

function statusLabel(status: string): string {
  return ({ invited: "待激活", active: "在组", pending_exit: "待离组", disabled: "已停用", archived: "已归档" } as Record<string, string>)[status] ?? status;
}

function formatTimestamp(value?: string | null): string {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString("zh-CN", { hour12: false });
}
