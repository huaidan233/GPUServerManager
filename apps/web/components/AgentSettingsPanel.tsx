"use client";

import {
  Bot,
  PlugZap,
  RefreshCw,
  RotateCw,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { type FormEvent, useCallback, useEffect, useState } from "react";

import { authenticatedFetch } from "../lib/authenticatedFetch";
import { AccessibleDialog } from "./AccessibleDialog";


type ProviderSlot = "primary" | "fallback";

export type PublicAgentSettings = {
  version: number;
  endpoint: string;
  model: string;
  primary_configured: boolean;
  primary_fingerprint: string | null;
  fallback_configured: boolean;
  fallback_fingerprint: string | null;
  active_slot: "primary" | "fallback" | "disabled";
  primary_status: string;
  fallback_status: string;
  updated_at: string | null;
  updated_by: string | null;
};

type CommandKind = "save" | "clear-primary" | "clear-fallback" | "test-primary" | "test-fallback";

type PendingCommand = {
  kind: CommandKind;
  url: string;
  init: RequestInit;
};

const slotLabels = {
  primary: "主密钥",
  fallback: "备用密钥",
} as const;

const statusLabels: Record<string, string> = {
  healthy: "正常",
  standby: "待命",
  cooldown: "冷却中",
  failed: "异常",
  unknown: "未检测",
};

export function AgentSettingsPanel() {
  const [settings, setSettings] = useState<PublicAgentSettings | null>(null);
  const [endpoint, setEndpoint] = useState("");
  const [model, setModel] = useState("");
  const [replacePrimary, setReplacePrimary] = useState(false);
  const [replaceFallback, setReplaceFallback] = useState(false);
  const [primaryKey, setPrimaryKey] = useState("");
  const [fallbackKey, setFallbackKey] = useState("");
  const [clearSlot, setClearSlot] = useState<ProviderSlot | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [pendingCommand, setPendingCommand] = useState<PendingCommand | null>(null);
  const [reauthenticating, setReauthenticating] = useState(false);
  const [reauthTotp, setReauthTotp] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const applySettings = useCallback((value: PublicAgentSettings) => {
    setSettings(value);
    setEndpoint(value.endpoint);
    setModel(value.model);
  }, []);

  const loadSettings = useCallback(async () => {
    setError("");
    try {
      const response = await fetch("/api/gsm/admin/agent-settings");
      const payload = await safeJson(response);
      if (!response.ok || !isPublicSettings(payload)) {
        throw new Error(publicDetail(payload, "无法加载 Agent 设置。"));
      }
      applySettings(payload);
    } catch (caught) {
      setError(errorMessage(caught, "无法加载 Agent 设置。"));
    }
  }, [applySettings]);

  useEffect(() => {
    void loadSettings();
  }, [loadSettings]);

  async function runCommand(command: PendingCommand, allowReauthentication = true) {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const response = await authenticatedFetch(command.url, command.init);
      const payload = await safeJson(response);
      const detail = publicDetail(payload, "操作失败，请稍后重试。");
      if (
        response.status === 403
        && allowReauthentication
        && detail === "recent administrator authentication required"
      ) {
        setPendingCommand(command);
        setClearSlot(null);
        setConfirmation("");
        setReauthenticating(true);
        return;
      }
      if (!response.ok) {
        if (response.status === 409 && command.kind !== "test-primary" && command.kind !== "test-fallback") {
          await loadSettings();
        }
        throw new Error(detail);
      }
      if (command.kind === "test-primary" || command.kind === "test-fallback") {
        const result = payload as { latency_ms?: unknown };
        const slot = command.kind === "test-primary" ? "primary" : "fallback";
        const latency = typeof result.latency_ms === "number" ? result.latency_ms : 0;
        setMessage(`${slotLabels[slot]}连接正常，${latency} ms`);
        return;
      }
      if (!isPublicSettings(payload)) {
        throw new Error("Central API 返回了无效设置。");
      }
      applySettings(payload);
      if (command.kind === "save") {
        setPrimaryKey("");
        setFallbackKey("");
        setReplacePrimary(false);
        setReplaceFallback(false);
        setMessage("Agent 设置已保存");
      } else {
        const slot = command.kind === "clear-primary" ? "primary" : "fallback";
        setClearSlot(null);
        setConfirmation("");
        setMessage(`${slotLabels[slot]}已清除`);
      }
    } catch (caught) {
      setError(errorMessage(caught, "操作失败，请稍后重试。"));
    } finally {
      setBusy(false);
    }
  }

  function submitSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!settings) return;
    const body: Record<string, unknown> = {
      expected_version: settings.version,
      endpoint: endpoint.trim(),
      model: model.trim(),
    };
    if (replacePrimary && primaryKey) body.primary_key = primaryKey;
    if (replaceFallback && fallbackKey) body.fallback_key = fallbackKey;
    void runCommand({
      kind: "save",
      url: "/api/gsm/admin/agent-settings",
      init: jsonRequest("PUT", body),
    });
  }

  function submitClear(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!settings || !clearSlot) return;
    const clearPrimary = clearSlot === "primary";
    void runCommand({
      kind: clearPrimary ? "clear-primary" : "clear-fallback",
      url: "/api/gsm/admin/agent-settings",
      init: jsonRequest("PUT", {
        expected_version: settings.version,
        endpoint: settings.endpoint,
        model: settings.model,
        [clearPrimary ? "clear_primary" : "clear_fallback"]: true,
        confirmation: requiredClearPhrase(clearSlot),
      }),
    });
  }

  function testProvider(slot: ProviderSlot) {
    void runCommand({
      kind: slot === "primary" ? "test-primary" : "test-fallback",
      url: "/api/gsm/admin/agent-settings/test",
      init: jsonRequest("POST", { slot }),
    });
  }

  async function refreshHealth() {
    if (!settings) return;
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/gsm/admin/agent-settings/health");
      const payload = await safeJson(response);
      if (!response.ok || !isHealth(payload)) {
        throw new Error(publicDetail(payload, "无法刷新连接状态。"));
      }
      setSettings({ ...settings, ...payload });
      setMessage("连接状态已刷新");
    } catch (caught) {
      setError(errorMessage(caught, "无法刷新连接状态。"));
    } finally {
      setBusy(false);
    }
  }

  async function submitReauthentication(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!pendingCommand) return;
    setBusy(true);
    setError("");
    try {
      const response = await authenticatedFetch(
        "/api/gsm/admin/agent-settings/reauthenticate",
        jsonRequest("POST", { totp_code: reauthTotp }),
      );
      const payload = await safeJson(response);
      if (!response.ok) {
        throw new Error(publicDetail(payload, "管理员认证失败。"));
      }
      const command = pendingCommand;
      setPendingCommand(null);
      setReauthenticating(false);
      setReauthTotp("");
      await runCommand(command, false);
    } catch (caught) {
      setError(errorMessage(caught, "管理员认证失败。"));
    } finally {
      setBusy(false);
    }
  }

  if (!settings) {
    return (
      <section className="agent-settings-workspace" aria-labelledby="agent-settings-heading">
        <div className="workspace-heading">
          <div className="workspace-title"><Bot size={21} aria-hidden="true" /><div><h2 id="agent-settings-heading">Agent 模型配置</h2><p>正在读取配置...</p></div></div>
        </div>
        {error && <p className="workspace-error" role="alert">{error}</p>}
      </section>
    );
  }

  return (
    <section className="agent-settings-workspace" aria-labelledby="agent-settings-heading">
      <div className="workspace-heading">
        <div className="workspace-title">
          <Bot size={21} aria-hidden="true" />
          <div><h2 id="agent-settings-heading">Agent 模型配置</h2><p>统一管理微信总 Agent 使用的主、备模型连接。</p></div>
        </div>
        <button className="secondary-button" type="button" disabled={busy} onClick={() => void refreshHealth()}>
          <RefreshCw size={16} aria-hidden="true" />刷新状态
        </button>
      </div>

      {error && <p className="workspace-error" role="alert">{error}</p>}
      {message && <p className="table-message" role="status">{message}</p>}

      <form className="agent-settings-form" onSubmit={submitSettings}>
        <div className="agent-provider-fields">
          <label>API 地址<input name="agent-endpoint" type="url" autoComplete="off" spellCheck={false} required value={endpoint} onChange={(event) => setEndpoint(event.currentTarget.value)} /></label>
          <label>模型<input name="agent-model" autoComplete="off" spellCheck={false} required value={model} onChange={(event) => setModel(event.currentTarget.value)} /></label>
        </div>

        <div className="agent-key-list">
          <KeySlot
            slot="primary"
            configured={settings.primary_configured}
            fingerprint={settings.primary_fingerprint}
            status={settings.primary_status}
            active={settings.active_slot === "primary"}
            replacing={replacePrimary}
            replacement={primaryKey}
            busy={busy}
            onToggleReplace={() => { setReplacePrimary((value) => !value); setPrimaryKey(""); }}
            onReplacement={setPrimaryKey}
            onClear={() => { setClearSlot("primary"); setConfirmation(""); }}
            onTest={() => testProvider("primary")}
          />
          <KeySlot
            slot="fallback"
            configured={settings.fallback_configured}
            fingerprint={settings.fallback_fingerprint}
            status={settings.fallback_status}
            active={settings.active_slot === "fallback"}
            replacing={replaceFallback}
            replacement={fallbackKey}
            busy={busy}
            onToggleReplace={() => { setReplaceFallback((value) => !value); setFallbackKey(""); }}
            onReplacement={setFallbackKey}
            onClear={() => { setClearSlot("fallback"); setConfirmation(""); }}
            onTest={() => testProvider("fallback")}
          />
        </div>

        <div className="agent-settings-actions">
          <span>{settings.updated_at ? `最近更新 ${formatDateTime(settings.updated_at)}` : "尚未保存"}</span>
          <button className="primary-button" type="submit" disabled={busy || !endpoint.trim() || !model.trim()}>
            <ShieldCheck size={16} aria-hidden="true" />{busy ? "处理中" : "保存 Agent 设置"}
          </button>
        </div>
      </form>

      {clearSlot && (
        <AccessibleDialog title={`清除${slotLabels[clearSlot]}`} onClose={() => { setClearSlot(null); setConfirmation(""); }} destructiveBusy={busy}>
          <form className="dialog-form" onSubmit={submitClear}>
            <p>清除后该槽位立即停止使用。此操作不会显示或导出原密钥。</p>
            <span className="required-phrase">{requiredClearPhrase(clearSlot)}</span>
            <label htmlFor="agent-clear-confirmation">输入确认短语</label>
            <input id="agent-clear-confirmation" name="agent-clear-confirmation" autoComplete="off" spellCheck={false} value={confirmation} onChange={(event) => setConfirmation(event.currentTarget.value)} />
            <div className="dialog-actions">
              <button className="secondary-button" type="button" disabled={busy} onClick={() => { setClearSlot(null); setConfirmation(""); }}>取消</button>
              <button className="danger-button" type="submit" disabled={busy || confirmation !== requiredClearPhrase(clearSlot)}>确认清除</button>
            </div>
          </form>
        </AccessibleDialog>
      )}

      {reauthenticating && (
        <AccessibleDialog title="管理员重新认证" onClose={() => { if (!busy) { setReauthenticating(false); setPendingCommand(null); } }} destructiveBusy={busy}>
          <form className="dialog-form" onSubmit={submitReauthentication}>
            <p>输入验证器中的 6 位动态验证码，验证后 10 分钟内可管理模型配置。</p>
            <label htmlFor="agent-reauth-totp">动态验证码</label>
            <input id="agent-reauth-totp" name="agent-reauth-totp" inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" maxLength={6} autoFocus required value={reauthTotp} onChange={(event) => setReauthTotp(event.currentTarget.value.replace(/\D/g, ""))} />
            <div className="dialog-actions">
              <button className="secondary-button" type="button" disabled={busy} onClick={() => { setReauthenticating(false); setPendingCommand(null); }}>取消</button>
              <button className="primary-button" type="submit" disabled={busy || !/^[0-9]{6}$/.test(reauthTotp)}>重新认证并继续</button>
            </div>
          </form>
        </AccessibleDialog>
      )}
    </section>
  );
}

type KeySlotProps = {
  slot: ProviderSlot;
  configured: boolean;
  fingerprint: string | null;
  status: string;
  active: boolean;
  replacing: boolean;
  replacement: string;
  busy: boolean;
  onToggleReplace: () => void;
  onReplacement: (value: string) => void;
  onClear: () => void;
  onTest: () => void;
};

function KeySlot({ slot, configured, fingerprint, status, active, replacing, replacement, busy, onToggleReplace, onReplacement, onClear, onTest }: KeySlotProps) {
  const label = slotLabels[slot];
  return (
    <section className="agent-key-row" aria-labelledby={`agent-${slot}-heading`}>
      <div className="agent-key-summary">
        <div>
          <h3 id={`agent-${slot}-heading`}>{label}</h3>
          <p>{configured && fingerprint ? `指纹 ${fingerprint}` : "未配置"}</p>
        </div>
        <div className="identity-badges">
          {active && <span className="role-badge">当前使用</span>}
          <span className={`status-pill agent-${status}`}>{statusLabels[status] ?? status}</span>
        </div>
      </div>
      {replacing && (
        <label className="agent-key-input">
          {`新的${label}`}
          <input name={`agent-${slot}-key`} type="password" autoComplete="new-password" spellCheck={false} value={replacement} onChange={(event) => onReplacement(event.currentTarget.value)} />
        </label>
      )}
      <div className="agent-key-actions">
        <button className="secondary-button" type="button" disabled={busy} onClick={onToggleReplace}><RotateCw size={15} aria-hidden="true" />{replacing ? "取消替换" : `替换${label}`}</button>
        <button className="secondary-button" type="button" disabled={busy || !configured} onClick={onTest}><PlugZap size={15} aria-hidden="true" />{`测试${label}`}</button>
        <button className="danger-button" type="button" disabled={busy || !configured} onClick={onClear}><Trash2 size={15} aria-hidden="true" />{`清除${label}`}</button>
      </div>
    </section>
  );
}

function jsonRequest(method: string, body: unknown): RequestInit {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

function requiredClearPhrase(slot: ProviderSlot): string {
  return slot === "primary" ? "CLEAR PRIMARY KEY" : "CLEAR FALLBACK KEY";
}

function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

async function safeJson(response: Response): Promise<unknown> {
  try { return await response.json(); } catch { return null; }
}

function publicDetail(payload: unknown, fallback: string): string {
  if (payload && typeof payload === "object" && "detail" in payload) {
    const detail = (payload as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.length <= 240) return detail;
  }
  return fallback;
}

function errorMessage(value: unknown, fallback: string): string {
  return value instanceof Error && value.message ? value.message : fallback;
}

function isPublicSettings(value: unknown): value is PublicAgentSettings {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<PublicAgentSettings>;
  return typeof candidate.version === "number"
    && typeof candidate.endpoint === "string"
    && typeof candidate.model === "string"
    && typeof candidate.primary_configured === "boolean"
    && typeof candidate.fallback_configured === "boolean"
    && typeof candidate.active_slot === "string"
    && typeof candidate.primary_status === "string"
    && typeof candidate.fallback_status === "string";
}

function isHealth(value: unknown): value is Pick<PublicAgentSettings, "active_slot" | "primary_status" | "fallback_status"> {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Record<string, unknown>;
  return typeof candidate.active_slot === "string"
    && typeof candidate.primary_status === "string"
    && typeof candidate.fallback_status === "string";
}
