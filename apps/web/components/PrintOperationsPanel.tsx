"use client";

import { Printer, RefreshCw, Upload, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { authenticatedFetch } from "../lib/authenticatedFetch";
import type { ActorRole } from "../lib/mockData";

type PrinterRecord = {
  id: string;
  display_name: string;
  status: "online" | "offline" | "paused" | "out_of_paper";
  capabilities: {
    sides: string[];
    color_modes: string[];
    papers: string[];
    max_copies: number;
  };
};

type PrintJob = {
  id: string;
  status: string;
  source_filename: string;
  source_page_count?: number;
  settings?: Record<string, unknown> | null;
  required_phrase?: string;
};

type Props = { actorRole: ActorRole; autoLoad?: boolean };

const statusLabels: Record<string, string> = {
  draft: "草稿",
  queued: "排队中",
  leased: "已分配",
  spooling: "发送中",
  submitted: "打印中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  delivery_unknown: "结果未知",
};

async function request(path: string, init?: RequestInit) {
  const response = await authenticatedFetch(path, init);
  const payload = await response.json();
  if (!response.ok) throw new Error(String(payload.detail ?? "打印操作失败"));
  return payload;
}

export function PrintOperationsPanel({ actorRole, autoLoad = true }: Props) {
  const [printers, setPrinters] = useState<PrinterRecord[]>([]);
  const [jobs, setJobs] = useState<PrintJob[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [printerId, setPrinterId] = useState("");
  const [copies, setCopies] = useState(1);
  const [pageRanges, setPageRanges] = useState("");
  const [sides, setSides] = useState("simplex");
  const [colorMode, setColorMode] = useState("monochrome");
  const [paper, setPaper] = useState("A4");
  const [activeDraft, setActiveDraft] = useState<PrintJob | null>(null);
  const [pending, setPending] = useState<PrintJob | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  const selectedPrinter = useMemo(
    () => printers.find((item) => item.id === printerId),
    [printers, printerId],
  );

  const refresh = useCallback(async () => {
    setError("");
    try {
      const [printerResult, jobResult] = await Promise.all([
        request("/api/gsm/print/printers"),
        request("/api/gsm/print/jobs"),
      ]);
      if (!Array.isArray(printerResult) || !Array.isArray(jobResult)) {
        throw new Error("打印服务返回了无效数据");
      }
      setPrinters(printerResult as PrinterRecord[]);
      setJobs(jobResult as PrintJob[]);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "读取打印状态失败");
    }
  }, []);

  useEffect(() => {
    if (actorRole === "viewer" || !autoLoad) return;
    void refresh();
  }, [actorRole, autoLoad, refresh]);

  useEffect(() => {
    if (!printerId) {
      const available = printers.find((item) => item.status === "online");
      if (available) setPrinterId(available.id);
    }
  }, [printerId, printers]);

  if (actorRole === "viewer") return null;

  function chooseFile(selected: File | undefined) {
    setError("");
    if (!selected) {
      setFile(null);
      return;
    }
    const supported = ["application/pdf", "image/jpeg", "image/png"];
    if (!supported.includes(selected.type)) {
      setFile(null);
      setError("仅支持 PDF、JPG、PNG");
      return;
    }
    if (selected.size > 100 * 1024 * 1024) {
      setFile(null);
      setError("文件不能超过 100 MiB");
      return;
    }
    setFile(selected);
  }

  async function uploadDraft() {
    if (!file) return;
    setBusy("upload");
    setError("");
    try {
      const form = new FormData();
      form.set("file", file);
      const created = await request("/api/gsm/print/jobs", { method: "POST", body: form }) as PrintJob;
      setJobs((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      setActiveDraft(created);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "上传失败");
    } finally {
      setBusy("");
    }
  }

  async function saveSettings() {
    if (!activeDraft || !selectedPrinter) return;
    setBusy("settings");
    setError("");
    try {
      const updated = await request(`/api/gsm/print/jobs/${activeDraft.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          printer_id: printerId,
          copies,
          page_ranges: pageRanges,
          sides,
          color_mode: colorMode,
          paper,
        }),
      }) as PrintJob;
      setJobs((current) => current.map((item) => item.id === updated.id ? updated : item));
      setActiveDraft(updated);
      setPending(updated);
      setConfirmation("");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "保存设置失败");
    } finally {
      setBusy("");
    }
  }

  async function confirmPrint() {
    if (!pending?.required_phrase || confirmation !== pending.required_phrase) return;
    setBusy("confirm");
    try {
      const updated = await request(`/api/gsm/print/jobs/${pending.id}/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phrase: confirmation }),
      }) as PrintJob;
      setJobs((current) => current.map((item) => item.id === updated.id ? updated : item));
      setPending(null);
      setActiveDraft(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "确认失败");
    } finally {
      setBusy("");
    }
  }

  async function cancel(job: PrintJob) {
    const updated = await request(`/api/gsm/print/jobs/${job.id}/cancel`, { method: "POST" }) as PrintJob;
    setJobs((current) => current.map((item) => item.id === updated.id ? { ...item, ...updated } : item));
    if (activeDraft?.id === job.id) setActiveDraft(null);
  }

  async function reprint(job: PrintJob) {
    const draft = await request(`/api/gsm/print/jobs/${job.id}/reprint`, { method: "POST" }) as PrintJob;
    setJobs((current) => [draft, ...current]);
    setActiveDraft(draft);
    setPending(draft);
    setConfirmation("");
  }

  return (
    <section className="panel operations-section print-operations" aria-labelledby="print-operations-heading">
      <div className="panel-heading operations-heading">
        <div>
          <h2 id="print-operations-heading">打印</h2>
          <p>PDF、JPG、PNG · 最多 100 MiB / 1,000 页 / 20 份</p>
        </div>
        <button className="icon-button" type="button" aria-label="刷新打印状态" title="刷新" onClick={refresh}>
          <RefreshCw size={17} aria-hidden="true" />
        </button>
      </div>

      {error && <p className="form-help create-error" role="alert">{error}</p>}

      <div className="print-controls">
        <label className="print-file-field">
          <span className="field-label">打印文件</span>
          <input aria-label="打印文件" type="file" accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png" onChange={(event) => chooseFile(event.target.files?.[0])} />
        </label>
        <label>
          <span className="field-label">打印机</span>
          <select value={printerId} onChange={(event) => setPrinterId(event.target.value)}>
            <option value="">选择打印机</option>
            {printers.map((item) => <option key={item.id} value={item.id} disabled={item.status !== "online"}>{item.display_name}</option>)}
          </select>
        </label>
        <label>
          <span className="field-label">份数</span>
          <input type="number" min={1} max={Math.min(20, selectedPrinter?.capabilities.max_copies ?? 20)} value={copies} onChange={(event) => setCopies(Number(event.target.value))} />
        </label>
        <label>
          <span className="field-label">页码</span>
          <input type="text" placeholder="全部页" value={pageRanges} onChange={(event) => setPageRanges(event.target.value)} />
        </label>
        <button className="secondary-button" type="button" disabled={!file || busy === "upload"} onClick={uploadDraft}>
          <Upload size={16} aria-hidden="true" />上传为打印草稿
        </button>
      </div>

      <div className="print-settings-grid">
        <SettingButtons label="单双面" value={sides} onChange={setSides} options={[
          ["simplex", "单面"], ["duplex_long_edge", "长边双面"], ["duplex_short_edge", "短边双面"],
        ]} allowed={selectedPrinter?.capabilities.sides ?? []} />
        <SettingButtons label="颜色" value={colorMode} onChange={setColorMode} options={[["monochrome", "黑白"], ["color", "彩色"]]} allowed={selectedPrinter?.capabilities.color_modes ?? []} />
        <SettingButtons label="纸张" value={paper} onChange={setPaper} options={[["A4", "A4"], ["auto", "自动"]]} allowed={selectedPrinter?.capabilities.papers ?? []} />
        <button className="primary-button" type="button" disabled={!activeDraft || !selectedPrinter || busy === "settings"} onClick={saveSettings}>
          <Printer size={16} aria-hidden="true" />保存打印设置
        </button>
      </div>

      <div className="table-wrap">
        <table aria-label="最近打印任务">
          <thead><tr><th>文件</th><th>状态</th><th>任务</th><th>操作</th></tr></thead>
          <tbody>
            {jobs.length === 0 ? <tr><td colSpan={4}>暂无打印任务</td></tr> : jobs.map((job) => (
              <tr key={job.id}>
                <td>{job.source_filename}</td>
                <td><span className={`print-status print-${job.status}`}>{statusLabels[job.status] ?? job.status}</span></td>
                <td className="mono-cell">{job.id}</td>
                <td className="table-actions">
                  {["draft", "queued", "leased"].includes(job.status) && <button className="icon-button" type="button" aria-label={`取消打印 ${job.source_filename}`} title="取消" onClick={() => void cancel(job)}><X size={15} /></button>}
                  {["completed", "failed", "cancelled", "delivery_unknown"].includes(job.status) && <button className="icon-button" type="button" aria-label={`重新打印 ${job.source_filename}`} title="重新打印" onClick={() => void reprint(job)}><RefreshCw size={15} /></button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {pending?.required_phrase && (
        <div className="dialog-backdrop">
          <div className="dialog" role="dialog" aria-modal="true" aria-label="确认打印任务">
            <div className="dialog-heading"><strong>确认打印</strong><button className="icon-button" type="button" aria-label="关闭打印确认" onClick={() => setPending(null)}><X size={16} /></button></div>
            <p>{pending.required_phrase}</p>
            <label htmlFor="print-confirmation">打印确认短语</label>
            <input id="print-confirmation" aria-label="打印确认短语" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} spellCheck={false} />
            <div className="dialog-actions"><button className="secondary-button" type="button" onClick={() => setPending(null)}>取消</button><button className="primary-button" type="button" disabled={confirmation !== pending.required_phrase || busy === "confirm"} onClick={confirmPrint}>确认打印</button></div>
          </div>
        </div>
      )}
    </section>
  );
}

function SettingButtons({ label, value, onChange, options, allowed }: { label: string; value: string; onChange: (value: string) => void; options: string[][]; allowed: string[] }) {
  return <fieldset className="print-setting"><legend>{label}</legend><div className="segmented-control">{options.map(([option, text]) => <button key={option} type="button" aria-pressed={value === option} disabled={!allowed.includes(option)} onClick={() => onChange(option)}>{text}</button>)}</div></fieldset>;
}
