import { expect, test, vi } from "vitest";
const proxy = vi.hoisted(() => ({ proxyCentralRequest: vi.fn(), proxyJsonRequest: vi.fn() }));
vi.mock("../lib/apiProxy", () => proxy);
import { POST } from "../app/api/gsm/data/[...path]/route";

test("rename is forwarded through the web gateway and unrelated operations are rejected", async () => {
  proxy.proxyJsonRequest.mockResolvedValue(Response.json({ renamed: true }));
  const request = new Request("http://localhost/api/gsm/data/rename", { method: "POST", body: JSON.stringify({ path: "a", new_name: "b" }) });
  const response = await POST(request, { params: Promise.resolve({ path: ["rename"] }) });
  expect(response.status).toBe(200);
  expect(response.headers.get("Cache-Control")).toBe("no-store");
  expect(proxy.proxyJsonRequest).toHaveBeenCalledWith(request, "/data/rename", "POST", {});
  expect((await POST(request, { params: Promise.resolve({ path: ["remove-everything"] }) })).status).toBe(404);
});
