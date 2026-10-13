"use client";

import { Clipboard, Download, KeyRound, TerminalSquare } from "lucide-react";
import { useState } from "react";

import { authenticatedFetch } from "../lib/authenticatedFetch";
import { AccessibleDialog } from "./AccessibleDialog";

type CertificateResult = {
  certificate: string;
  principal: string;
  valid_after: string;
  valid_before: string | null;
};

type Props = {
  containerId: string;
  sshCommand: string;
  onClose: () => void;
};

export function SSHConnectionDialog({ containerId, sshCommand, onClose }: Props) {
  const [publicKey, setPublicKey] = useState("");
  const [result, setResult] = useState<CertificateResult | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState("");
  const certificateName = `gsm_member-${encodeURIComponent(containerId)}-cert.pub`;
  const certificatePath = `~/.ssh/${certificateName}`;
  const certificateCommand = sshCommand.replace(/^ssh\b/, `ssh -i ~/.ssh/gsm_member -o CertificateFile=${certificatePath}`);

  async function issueCertificate() {
    setBusy(true);
    setError("");
    setCopied("");
    try {
      const response = await authenticatedFetch(
        `/api/gsm/containers/${encodeURIComponent(containerId)}/ssh-certificate`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ public_key: publicKey.trim() }),
        },
      );
      const payload = await safeJson(response);
      if (!response.ok) {
        throw new Error(publicDetail(payload, "SSH 证书签发失败，请检查公钥或稍后重试。"));
      }
      setResult(payload as CertificateResult);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "SSH 证书签发失败，请稍后重试。");
    } finally {
      setBusy(false);
    }
  }

  async function copyValue(label: string, value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(`${label}已复制`);
    } catch {
      setCopied(`${label}复制失败，请手动选择文本复制。`);
    }
  }

  function downloadCertificate() {
    if (!result) return;
    const blob = new Blob([`${result.certificate}\n`], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = certificateName;
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <AccessibleDialog title="配置 SSH 连接" onClose={onClose} destructiveBusy={busy}>
      {!result ? (
        <>
          <p>证书仅用于你在当前容器的 SSH 登录，永久有效，无需定期续签。私钥保存在本机，不会上传到平台。</p>
          <div className="ssh-setup-steps">
            <strong>第一次使用？先在本机生成密钥</strong>
            <code>ssh-keygen -t ed25519 -f ~/.ssh/gsm_member</code>
            <span>然后复制 <code>~/.ssh/gsm_member.pub</code> 的完整内容粘贴到下面。</span>
          </div>
          <label htmlFor="ssh-public-key">本地 SSH 公钥</label>
          <textarea
            id="ssh-public-key"
            rows={5}
            value={publicKey}
            onChange={(event) => setPublicKey(event.currentTarget.value)}
            placeholder="ssh-ed25519 AAAA..."
            spellCheck={false}
            autoComplete="off"
          />
          {error && <p className="create-error" role="alert">{error}</p>}
          <div className="dialog-actions">
            <button className="secondary-button" type="button" onClick={onClose} disabled={busy}>取消</button>
            <button className="primary-button" type="button" onClick={issueCertificate} disabled={busy || !publicKey.trim()}>
              <KeyRound size={16} aria-hidden="true" />{busy ? "签发中" : "签发永久证书"}
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="container-notice">{result.valid_before === null
            ? "永久有效，无需续签。此证书仅限当前成员与当前容器使用。"
            : `证书有效期至 ${formatDate(result.valid_before)}。这是旧版短期证书，到期后需重新签发。`}</p>
          <section className="ssh-result-section" aria-labelledby="ssh-command-heading">
            <h3 id="ssh-command-heading"><TerminalSquare size={16} aria-hidden="true" />连接命令</h3>
            <div className="container-copy-value">
              <code>{certificateCommand}</code>
              <button className="secondary-button" type="button" onClick={() => void copyValue("连接命令", certificateCommand)}>
                <Clipboard size={14} aria-hidden="true" />复制
              </button>
            </div>
          </section>
          <section className="ssh-result-section" aria-labelledby="ssh-cert-heading">
            <h3 id="ssh-cert-heading">证书文件</h3>
            <p className="form-help">将下载的文件保存为 <code>{certificatePath}</code>，上面的命令会同时加载该证书和私钥 <code>~/.ssh/gsm_member</code>。不同容器的证书分开保存，可以复用同一把本地密钥。</p>
            <div className="container-copy-value">
              <code>{result.certificate}</code>
              <button className="secondary-button" type="button" onClick={() => void copyValue("证书", result.certificate)}>
                <Clipboard size={14} aria-hidden="true" />复制
              </button>
            </div>
            <button className="secondary-button" type="button" onClick={downloadCertificate}>
              <Download size={14} aria-hidden="true" />下载证书
            </button>
          </section>
          {copied && <p className="form-help" role="status">{copied}</p>}
          <div className="dialog-actions">
            <button className="primary-button" type="button" onClick={onClose}>完成</button>
          </div>
        </>
      )}
    </AccessibleDialog>
  );
}

async function safeJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function publicDetail(payload: unknown, fallback: string): string {
  if (payload && typeof payload === "object" && "detail" in payload) {
    const detail = (payload as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.length <= 240) return detail;
  }
  return fallback;
}

function formatDate(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString("zh-CN", { hour12: false });
}
