"use client";

import { FormEvent, useState } from "react";
import { authenticatedFetch } from "../lib/authenticatedFetch";

type Enrollment = { secret: string; recovery_codes: string[] };

export function FirstLoginScreen({ username, isAdmin }: { username: string; isAdmin: boolean }) {
  const [initialPassword, setInitialPassword] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [totp, setTotp] = useState("");
  const [enrollment, setEnrollment] = useState<Enrollment | null>(null);
  const [savedRecovery, setSavedRecovery] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [complete, setComplete] = useState(false);

  async function enroll() {
    setBusy(true); setError("");
    try {
      const response = await authenticatedFetch("/api/gsm/auth/initial-mfa", { method: "POST" });
      if (!response.ok) throw new Error("无法生成验证器设置，请重新登录后重试。");
      setEnrollment(await response.json() as Enrollment);
      setSavedRecovery(false);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "设置失败，请重试。"); }
    finally { setBusy(false); }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError("");
    if (password !== confirmation) { setError("两次新密码不一致。"); return; }
    if (password === initialPassword) { setError("新密码不能与初始密码相同。"); return; }
    setBusy(true);
    try {
      const response = await authenticatedFetch("/api/gsm/auth/password/change", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ current_password: initialPassword, new_password: password,
          ...(isAdmin ? { totp_code: totp } : {}) }),
      });
      if (!response.ok) throw new Error("设置失败，请检查初始密码、新密码和管理员验证码。");
      setInitialPassword(""); setPassword(""); setConfirmation(""); setEnrollment(null); setComplete(true);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "设置失败，请重试。"); }
    finally { setBusy(false); }
  }

  return <main className="auth-shell"><section className="auth-panel" aria-labelledby="first-login-heading">
    <h1 id="first-login-heading">{complete ? "账号已激活" : "首次登录：设置自己的密码"}</h1>
    {complete ? <><p>请使用新密码{isAdmin ? "和验证器动态码" : ""}登录。</p><button className="primary-button" onClick={() => window.location.reload()}>返回登录</button></> : <>
      <p>当前账号：{username}。完成设置后才能使用计算资源。</p>
      <form className="auth-form" onSubmit={submit}>
        <label htmlFor="first-initial">初始密码</label><input id="first-initial" type="password" autoComplete="current-password" required value={initialPassword} onChange={(event) => setInitialPassword(event.currentTarget.value)} />
        <label htmlFor="first-password">新密码（至少 12 位）</label><input id="first-password" type="password" autoComplete="new-password" required minLength={12} maxLength={128} value={password} onChange={(event) => setPassword(event.currentTarget.value)} />
        <label htmlFor="first-confirm">再次输入新密码</label><input id="first-confirm" type="password" autoComplete="new-password" required value={confirmation} onChange={(event) => setConfirmation(event.currentTarget.value)} />
        {isAdmin && <section aria-label="管理员双重验证">
          <h2>设置验证器</h2><p>管理员需要启用 2FA；设置密钥和恢复码由你自己保存。</p>
          {!enrollment ? <button type="button" className="secondary-button" disabled={busy} onClick={() => void enroll()}>生成验证器设置</button> : <>
            <p>在验证器中手动添加基于时间的账户（6 位、30 秒）：</p><code>{enrollment.secret}</code>
            <details><summary>恢复码（请保存）</summary>{enrollment.recovery_codes.map((code) => <p key={code}><code>{code}</code></p>)}</details>
            <label><input type="checkbox" checked={savedRecovery} onChange={(event) => setSavedRecovery(event.currentTarget.checked)} />我已保存验证器设置和恢复码</label>
          </>}
          <label htmlFor="first-totp">验证器当前的 6 位动态码</label><input id="first-totp" inputMode="numeric" autoComplete="one-time-code" required pattern="[0-9]{6}" value={totp} onChange={(event) => setTotp(event.currentTarget.value)} />
        </section>}
        {error && <p className="create-error" role="alert">{error}</p>}
        <button className="primary-button" type="submit" disabled={busy || (isAdmin && (!enrollment || !savedRecovery))}>{busy ? "正在保存…" : "激活并保存新密码"}</button>
      </form>
    </>}
  </section></main>;
}
