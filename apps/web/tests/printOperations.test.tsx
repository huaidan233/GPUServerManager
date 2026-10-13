import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { PrintOperationsPanel } from "../components/PrintOperationsPanel";


const printers = [
  {
    id: "printer-a",
    display_name: "课题组打印机",
    status: "online",
    capabilities: {
      sides: ["simplex", "duplex_long_edge"],
      color_modes: ["monochrome"],
      papers: ["A4", "auto"],
      max_copies: 20,
    },
  },
  {
    id: "printer-offline",
    display_name: "离线打印机",
    status: "offline",
    capabilities: {
      sides: ["simplex"],
      color_modes: ["monochrome"],
      papers: ["A4"],
      max_copies: 1,
    },
  },
];


afterEach(() => {
  vi.unstubAllGlobals();
});


function response(payload: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  } as Response;
}


describe("PrintOperationsPanel", () => {
  test("uploads, configures and requires the exact personal confirmation", async () => {
    let jobs: Array<Record<string, unknown>> = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input.toString();
      if (url.endsWith("/printers")) return response(printers);
      if (url.endsWith("/jobs") && (!init || init.method === "GET")) return response(jobs);
      if (url.endsWith("/jobs") && init?.method === "POST") {
        jobs = [{
          id: "job-1",
          status: "draft",
          source_filename: "paper.pdf",
          source_page_count: 3,
          required_phrase: "确认打印 job-1",
          settings: null,
        }];
        return response(jobs[0], 201);
      }
      if (url.endsWith("/jobs/job-1") && init?.method === "PATCH") {
        const settings = JSON.parse(String(init.body));
        jobs[0] = { ...jobs[0], settings, required_phrase: "确认打印 job-1" };
        return response(jobs[0]);
      }
      if (url.endsWith("/jobs/job-1/confirm")) {
        jobs[0] = { ...jobs[0], status: "queued", required_phrase: undefined };
        return response(jobs[0]);
      }
      throw new Error(`unexpected request ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<PrintOperationsPanel actorRole="user" />);

    await screen.findByRole("option", { name: "课题组打印机" });
    expect((screen.getByRole("option", { name: "离线打印机" }) as HTMLOptionElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "彩色" }) as HTMLButtonElement).disabled).toBe(true);

    const file = new File(["%PDF-test"], "paper.pdf", { type: "application/pdf" });
    fireEvent.change(screen.getByLabelText("打印文件"), { target: { files: [file] } });
    fireEvent.click(screen.getByRole("button", { name: "上传为打印草稿" }));
    await screen.findByText("paper.pdf");

    fireEvent.click(screen.getByRole("button", { name: "保存打印设置" }));
    const phraseInput = await screen.findByLabelText("打印确认短语");
    const confirmButton = screen.getByRole("button", { name: "确认打印" });
    expect((confirmButton as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(phraseInput, { target: { value: "确认打印 job-1" } });
    expect((confirmButton as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(confirmButton);

    await screen.findByText("排队中");
    expect(fetchMock).toHaveBeenCalledWith("/api/gsm/print/jobs/job-1/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phrase: "确认打印 job-1" }),
    });
  });

  test("rejects unsupported type and shows failure and ambiguous states", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (input.toString().endsWith("/printers")) return response(printers);
      return response([
        { id: "failed-1", status: "failed", source_filename: "bad.pdf" },
        { id: "unknown-1", status: "delivery_unknown", source_filename: "maybe.pdf" },
      ]);
    }));
    render(<PrintOperationsPanel actorRole="user" />);

    const file = new File(["text"], "notes.txt", { type: "text/plain" });
    fireEvent.change(await screen.findByLabelText("打印文件"), { target: { files: [file] } });

    expect((await screen.findByRole("alert")).textContent).toContain("仅支持 PDF、JPG、PNG");
    expect(screen.getByText("失败")).not.toBeNull();
    expect(screen.getByText("结果未知")).not.toBeNull();
  });

  test("cancels and reprints from recent jobs", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input.toString();
      if (url.endsWith("/printers")) return response(printers);
      if (url.endsWith("/jobs") && (!init || init.method === "GET")) {
        return response([{ id: "job-1", status: "queued", source_filename: "paper.pdf" }]);
      }
      if (url.endsWith("/cancel")) return response({ id: "job-1", status: "cancelled" });
      if (url.endsWith("/reprint")) return response({
        id: "job-2",
        status: "draft",
        source_filename: "paper.pdf",
        required_phrase: "确认打印 job-2",
      }, 201);
      throw new Error(`unexpected request ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<PrintOperationsPanel actorRole="user" />);

    fireEvent.click(await screen.findByRole("button", { name: "取消打印 paper.pdf" }));
    await screen.findByText("已取消");
    fireEvent.click(screen.getByRole("button", { name: "重新打印 paper.pdf" }));
    await screen.findByLabelText("打印确认短语");

    expect(fetchMock).toHaveBeenCalledWith("/api/gsm/print/jobs/job-1/cancel", { method: "POST" });
    expect(fetchMock).toHaveBeenCalledWith("/api/gsm/print/jobs/job-1/reprint", { method: "POST" });
  });

  test("does not render for viewer role", () => {
    const { container } = render(<PrintOperationsPanel actorRole="viewer" />);

    expect(container.innerHTML).toBe("");
  });
});
