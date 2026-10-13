"use client";

import { Copy, Check } from "lucide-react";
import { useState } from "react";

export function CopyCommand({ command, name }: { command: string; name: string }) {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");
  async function copy() {
    setError("");
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
    } catch {
      setCopied(false);
      setError("复制失败，请选中命令手动复制。");
    }
  }
  return <div className="ssh-copy-command">
    <code translate="no">{command}</code>
    <button className="secondary-button" type="button" aria-label={`复制 ${name} 的 SSH 命令`} onClick={copy}>
      {copied ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
      <span role="status">{copied ? "已复制" : "复制"}</span>
    </button>
    {error && <span className="form-help create-error" role="alert">{error}</span>}
  </div>;
}
