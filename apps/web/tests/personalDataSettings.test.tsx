import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, expect, test, vi } from "vitest";
import { QuotaSettingsPanel } from "../components/PersonalQuotaPanel";
import { PersonalDataPanel } from "../components/PersonalDataPanel";
import { ImageVersionsPanel } from "../components/ImageVersionsPanel";

afterEach(() => vi.unstubAllGlobals());

test("superuser dialog searches across pages without rendering quota form", async () => {
  const members = Array.from({ length: 20 }, (_, i) => ({ id: `m${i}`, username: `member-${i}`, role: "user", status: "active", superuser: false }));
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ baseline: { cpu: 8, memory_gb: 8, gpus: 0 }, members })));
  render(<QuotaSettingsPanel section="superusers" />);
  await screen.findByLabelText("member-0超级用户");
  expect(screen.getAllByRole("checkbox")).toHaveLength(8);
  expect(screen.queryByLabelText("一级审批：管理员")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  expect(screen.getByLabelText("member-8超级用户")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("搜索成员"), { target: { value: "member-19" } });
  expect(screen.getAllByRole("checkbox")).toHaveLength(1);
  expect(screen.getByLabelText("member-19超级用户")).toBeInTheDocument();
});

test("image search finds an environment outside the first page", () => {
  render(<ImageVersionsPanel images={Array.from({ length: 15 }, (_, i) => ({ name: `python:${i}`, displayName: `Python ${i}` }))} onUpdated={() => {}} />);
  expect(screen.getAllByRole("row")).toHaveLength(7);
  fireEvent.change(screen.getByLabelText("搜索镜像"), { target: { value: "python:14" } });
  expect(screen.getByText("Python 14")).toBeInTheDocument();
  expect(screen.getAllByRole("row")).toHaveLength(2);
});

test("rename sends only a path inside the current personal directory and refreshes", async () => {
  let name = "datasets";
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = input.toString();
    if (path.endsWith("/rename")) { expect(JSON.parse(String(init?.body))).toEqual({ path: "datasets", new_name: "project-a" }); name = "project-a"; return Response.json({ renamed: true }); }
    if (path.endsWith("/list")) return Response.json({ entries: [{ name, kind: "directory", size: 0, modified_at: 0 }] });
    if (path.endsWith("/status")) return Response.json({ total_bytes: 100, available_bytes: 50, data_root: "/data/users/alice" });
    return Response.json({ configured: false, public_keys: [] });
  });
  vi.stubGlobal("fetch", fetcher);
  render(<PersonalDataPanel />);
  fireEvent.click(await screen.findByRole("button", { name: "重命名 datasets" }));
  fireEvent.change(screen.getByLabelText("新名称"), { target: { value: "project-a" } });
  fireEvent.click(screen.getByRole("button", { name: "保存名称" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "project-a/" })).toBeInTheDocument());
  expect(screen.queryByRole("dialog", { name: "重命名" })).toBeNull();
});
