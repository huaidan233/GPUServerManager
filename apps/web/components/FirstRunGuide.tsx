"use client";

import Link from "next/link";

export function FirstRunGuide({ isAdmin, serverCount }: { isAdmin: boolean; serverCount: number }) {
  if (!isAdmin || serverCount > 0) return null;

  return (
    <section className="panel" aria-labelledby="first-run-guide-heading">
      <h2 id="first-run-guide-heading">开始配置 GPU Server Manager</h2>
      <p>当前账号已登录，但还没有可用服务器。按下面步骤配置一次即可，后续操作会继续沿用当前登录会话。</p>
      <ol>
        <li>在“系统”工作区配置并测试 Agent。</li>
        <li>按部署文档启动第一台 Server Agent，并让它连接到 Central API。</li>
        <li>返回“资源”工作区，刷新页面确认 GPU 和容器列表。</li>
      </ol>
      <div className="dialog-actions">
        <Link className="secondary-button" href="/?workspace=system">打开系统设置</Link>
        <button className="primary-button" type="button" onClick={() => window.location.reload()}>重新检查服务器</button>
      </div>
    </section>
  );
}
