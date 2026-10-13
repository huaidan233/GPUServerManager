"use client";

import { KeyRound, LogIn } from "lucide-react";
import { FormEvent, useState } from "react";

export function LoginScreen() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [totpCode, setTotpCode] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError("");

    try {
      const response = await fetch("/api/gsm/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username,
          password,
          ...(totpCode.trim() ? { totp_code: totpCode.trim() } : {}),
        }),
      });
      if (!response.ok) {
        setError(response.status === 429
          ? "尝试次数过多，请 15 分钟后再试。"
          : response.status >= 500
            ? "登录服务暂时不可用，请稍后重试。"
            : response.status === 401
              ? "账号、密码或验证码无效"
              : "登录请求未被接受，请刷新页面后重试。");
        return;
      }
      window.location.reload();
    } catch {
      setError("无法连接登录服务，请检查网络并刷新页面重试。");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-story" aria-label="课题组计算平台">
        <div className="auth-brand"><span className="rack-mark" aria-hidden="true"><i /><i /><i /></span><span>GPU Manager<small>课题组计算平台</small></span></div>
        <div className="auth-story-copy"><p className="eyebrow">研究的下一步，从这里开始</p><h2>把算力交给平台。<br />把时间留给研究。</h2><p>统一管理 GPU、实验容器与研究资源。<br />为课题组的每一次探索，准备好工作空间。</p></div>
        <div className="auth-resource-diagram" aria-hidden="true"><div><span>计算资源</span><i /><i /><i /><i /></div><div><span>实验环境</span><i /><i /><i /></div><div><span>团队协作</span><i /><i /></div></div>
        <p className="auth-story-footer">私有部署 · 成员身份验证 · 操作可追溯</p>
      </section>
      <section className="auth-panel" aria-labelledby="auth-heading">
        <header className="auth-heading">
          <span className="auth-mark" aria-hidden="true"><KeyRound size={22} /></span>
          <div>
            <p className="eyebrow">GPU Server Manager</p>
            <h1 id="auth-heading">成员登录</h1>
          </div>
        </header>
        <p className="auth-description">新成员使用用户名和初始密码登录，首次登录后设置自己的密码。</p>

        <form className="auth-form" onSubmit={submit}>
          <label htmlFor="auth-username">用户名</label>
          <input
            id="auth-username"
            type="text"
            autoComplete="username"
            required
            value={username}
            onChange={(event) => setUsername(event.currentTarget.value)}
          />

          <label htmlFor="auth-password">密码</label>
          <input
            id="auth-password"
            type="password"
            autoComplete="current-password"
            minLength={1}
            required
            value={password}
            onChange={(event) => setPassword(event.currentTarget.value)}
          />

          <label htmlFor="auth-totp">动态验证码（管理员必填）</label>
          <input
            id="auth-totp"
            type="text"
            inputMode="numeric"
            autoComplete="one-time-code"
            aria-describedby="auth-totp-help"
            value={totpCode}
            onChange={(event) => setTotpCode(event.currentTarget.value)}
          />
          <small id="auth-totp-help">管理员填写验证器当前的 6 位动态码；普通成员留空。</small>

          {error && <p className="auth-message auth-error" role="alert">{error}</p>}

          <div className="auth-actions">
            <button className="primary-button" type="submit" disabled={submitting}>
              <LogIn size={16} aria-hidden="true" />
              登录
            </button>
          </div>
        </form>
        <p className="auth-footnote">尚未获得账号？请联系课题组管理员。</p>
      </section>
    </main>
  );
}
