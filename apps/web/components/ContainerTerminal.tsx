"use client";

import { useEffect, useRef, useState } from "react";
import type { Terminal } from "@xterm/xterm";

import { authenticatedFetch } from "../lib/authenticatedFetch";
import { containerJson, containerRequest, managementError } from "../lib/containerManagement";

type Props = { containerId: string; disabledReason?: string; autoConnect?: boolean };
type Output = { output: string; cursor: number; truncated: boolean; closed: boolean };

export function ContainerTerminal({ containerId, disabledReason, autoConnect = false }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const terminal = useRef<Terminal | null>(null);
  const disconnectButton = useRef<HTMLButtonElement>(null);
  const disconnect = useRef<() => void>(() => {});
  const [attempt, setAttempt] = useState(autoConnect ? 1 : 0);
  const [status, setStatus] = useState<"idle" | "connecting" | "connected" | "closed">("idle");
  const [error, setError] = useState("");

  useEffect(() => {
    if (!attempt || disabledReason) return;
    let disposed = false;
    let sessionId = "";
    let cursor = 0;
    let pollTimer: ReturnType<typeof setTimeout> | undefined;
    let inputTimer: ReturnType<typeof setTimeout> | undefined;
    let observer: ResizeObserver | undefined;
    let instance: Terminal | undefined;
    let pendingInput = "";
    let writing = false;
    let size: { columns: number; rows: number } | undefined;
    const path = `/api/gsm/containers/${encodeURIComponent(containerId)}/terminal`;

    function closeRemote(identifier: string) {
      void authenticatedFetch(`${path}/${identifier}`, { method: "DELETE", keepalive: true }).catch(() => {});
    }

    function stop() {
      if (disposed) return;
      disposed = true;
      clearTimeout(pollTimer);
      clearTimeout(inputTimer);
      observer?.disconnect();
      if (instance) instance.options.disableStdin = true;
      if (sessionId) closeRemote(sessionId);
      setStatus("closed");
    }
    disconnect.current = stop;

    function fail(reason: unknown) {
      if (disposed) return;
      setError(managementError(reason));
      stop();
    }

    async function flush() {
      if (disposed || !sessionId || writing || (!pendingInput && !size)) return;
      writing = true;
      const chunk = Array.from(pendingInput).slice(0, 2048).join("");
      pendingInput = pendingInput.slice(chunk.length);
      const dimensions = size;
      size = undefined;
      try {
        await containerRequest(`${path}/${sessionId}`, containerJson({ data: chunk, ...dimensions }));
      } catch (reason) {
        fail(reason);
      } finally {
        writing = false;
        if (!disposed && (pendingInput || size)) inputTimer = setTimeout(flush, 30);
      }
    }

    async function poll() {
      if (disposed) return;
      try {
        const result = await containerRequest<Output>(`${path}/${sessionId}?cursor=${cursor}`);
        if (disposed) return;
        if (result.truncated) instance?.writeln("\r\n[部分输出已超出缓冲区，仅显示最近内容]\r\n");
        if (result.output) {
          instance?.write(Uint8Array.from(atob(result.output), (character) => character.charCodeAt(0)));
        }
        cursor = result.cursor;
        if (result.closed) {
          stop();
          return;
        }
        pollTimer = setTimeout(poll, 300);
      } catch (reason) {
        fail(reason);
      }
    }

    async function connect() {
      try {
        const [{ Terminal }, { FitAddon }] = await Promise.all([import("@xterm/xterm"), import("@xterm/addon-fit")]);
        if (disposed || !host.current) return;
        instance = new Terminal({
          cursorBlink: true, fontSize: 14, fontFamily: '"Cascadia Code", Consolas, monospace',
          scrollback: 2000, screenReaderMode: true, disableStdin: true,
          theme: { background: "#172235", foreground: "#e2e7ef", cursor: "#a6b8fa" },
        });
        terminal.current = instance;
        const fit = new FitAddon();
        instance.loadAddon(fit);
        instance.open(host.current);
        fit.fit();
        const dimensions = () => ({ columns: Math.min(300, Math.max(20, instance!.cols)), rows: Math.min(100, Math.max(5, instance!.rows)) });
        instance.onData((data) => {
          if (disposed || !sessionId) return;
          if (pendingInput.length + data.length > 16384) {
            setError("粘贴内容过长或发送过快，请分段输入。未发送本次输入。");
            return;
          }
          pendingInput += data;
          clearTimeout(inputTimer);
          inputTimer = setTimeout(flush, 30);
        });
        instance.onResize(() => {
          size = dimensions();
          clearTimeout(inputTimer);
          inputTimer = setTimeout(flush, 100);
        });
        const opened = await containerRequest<{ session_id: string }>(path, containerJson(dimensions()));
        if (disposed) {
          closeRemote(opened.session_id);
          return;
        }
        sessionId = opened.session_id;
        instance.options.disableStdin = false;
        setStatus("connected");
        instance.focus();
        observer = new ResizeObserver(() => fit.fit());
        observer.observe(host.current!);
        void poll();
      } catch (reason) {
        fail(reason);
      }
    }

    window.addEventListener("pagehide", stop);
    void connect();
    return () => {
      stop();
      window.removeEventListener("pagehide", stop);
      instance?.dispose();
      terminal.current = null;
    };
  }, [attempt, containerId, disabledReason]);

  const active = status === "connecting" || status === "connected";
  return (
    <div className="container-terminal">
      <div className="container-detail-toolbar">
        <span role="status" className={`terminal-status ${status}`}>
          <span aria-hidden="true" />
          {{ idle: "尚未连接", connecting: "正在连接…", connected: "已连接 · /bin/sh", closed: "已断开" }[status]}
        </span>
        <div className="container-detail-actions">
          <button type="button" className="secondary-button" disabled={status !== "connected"} onClick={() => terminal.current?.clear()}>清屏</button>
          {active ? <button ref={disconnectButton} type="button" className="secondary-button" onClick={() => disconnect.current()}>断开连接</button>
            : <button type="button" className="primary-button" disabled={Boolean(disabledReason)} onClick={() => {
              setError(""); setStatus("connecting"); setAttempt((value) => value + 1);
            }}>{status === "closed" ? "重新连接" : "连接终端"}</button>}
        </div>
      </div>
      {disabledReason && <p className="container-notice">{disabledReason}</p>}
      {error && <p role="alert" className="create-error">{error}</p>}
      <div className="terminal-surface">
        {!attempt && <div className="terminal-placeholder"><strong>在容器内开始工作</strong><p>连接后可执行命令、安装依赖和调试程序。</p></div>}
        <div ref={host} className="terminal-host" aria-label="容器交互终端"
          onKeyDownCapture={(event) => {
            if (event.ctrlKey && event.shiftKey && event.key.toLowerCase() === "x") {
              event.preventDefault(); event.stopPropagation(); disconnectButton.current?.focus();
            }
          }}
          onKeyDown={(event) => { if (event.key === "Escape") event.stopPropagation(); }} />
      </div>
      <p className="form-help">以非 root 用户进入此容器，不开放宿主机。支持 Ctrl+C 和 Ctrl+Shift+V；Ctrl+Shift+X 将焦点移回操作栏。关闭面板或切换标签会断开，10 分钟无输入自动结束。长任务请使用独立的持久会话。</p>
    </div>
  );
}
