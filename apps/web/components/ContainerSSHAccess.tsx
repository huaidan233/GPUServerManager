"use client";

import { useEffect, useState } from "react";
import { containerRequest, managementError } from "../lib/containerManagement";
import { CopyCommand } from "./CopyCommand";

type Credential = { password: string | null; state: "saved" | "legacy" | "not_set" };

export function ContainerSSHAccess({ containerId, name, command, isOwner, revision = 0, onConnect, onSettings, canConnect }: {
  containerId: string; name: string; command: string; isOwner: boolean; revision?: number;
  onConnect?: () => void; onSettings?: () => void; canConnect?: boolean;
}) {
  const [credential, setCredential] = useState<Credential | null>(null);
  const [error, setError] = useState("");
  const [copyMessage, setCopyMessage] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!isOwner) return;
    let cancelled = false;
    void containerRequest<Credential>(`/api/gsm/containers/${encodeURIComponent(containerId)}/ssh-password`)
      .then((value) => { if (!cancelled) { setCredential(value); setError(""); } })
      .catch((reason) => { if (!cancelled) { setCredential(null); setError(managementError(reason)); } });
    return () => { cancelled = true; };
  }, [containerId, isOwner, revision, retry]);

  return <div className="container-ssh-access">
    {isOwner && <div className="ssh-password-display">
      {error ? <><span role="alert">{error}</span><button className="secondary-button" type="button" onClick={() => setRetry((value) => value + 1)}>重试读取密码</button></>
        : credential?.password ? <>
          <span>SSH 密码</span><code translate="no">{credential.password}</code>
          <button className="secondary-button" type="button" aria-label={`复制 ${name} 的 SSH 密码`} onClick={async () => {
            try { await navigator.clipboard.writeText(credential.password!); setCopyMessage("密码已复制"); }
            catch { setCopyMessage("复制失败，请选中密码手动复制。"); }
          }}>复制密码</button><span className="form-help" role="status">{copyMessage}</span>
        </> : <span className="form-help">{!credential ? "正在读取 SSH 密码…" : credential.state === "legacy"
          ? "旧密码未保存，仍可正常使用；重设后会显示在这里。" : "尚未设置 SSH 密码，可设置密码或使用证书连接。"}</span>}
      {credential && !credential.password && onSettings && <button className="secondary-button" type="button" onClick={onSettings}>设置 SSH 密码</button>}
    </div>}
    <CopyCommand command={command} name={name} />
    {onConnect && <button className="secondary-button" type="button" disabled={!canConnect} onClick={onConnect}
      title={canConnect ? "无需输入密码，在网页中连接容器" : "请先启动容器"}>一键连接网页终端</button>}
  </div>;
}
