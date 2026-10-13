import { afterEach, expect, test, vi } from "vitest";
import { GET, POST } from "../app/api/gsm/admin/updates/[action]/route";

afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

test("update proxy allows only the fixed actions and forwards session CSRF instead of host credentials", async () => {
  vi.stubEnv("GSM_WEB_API_URL", "http://central:8000");
  const fetcher = vi.fn(async () => Response.json({ accepted: true }));
  vi.stubGlobal("fetch", fetcher);
  for (const action of ["prepare", "apply", "rollback"]) {
    const response = await POST(new Request(`https://gsm.test/api/gsm/admin/updates/${action}`, {
      method: "POST", headers: { "Content-Type": "application/json", Cookie: "gsm_session=session; gsm_csrf=csrf",
        Origin: "https://gsm.test", "X-CSRF-Token": "csrf" }, body: JSON.stringify({ request_id: "a".repeat(32), totp_code: "123456" }),
    }), { params: Promise.resolve({ action }) });
    expect(response.headers.get("cache-control")).toBe("no-store");
  }
  expect(fetcher.mock.calls.map(([url]) => url)).toEqual(["http://central:8000/api/admin/updates/prepare", "http://central:8000/api/admin/updates/apply", "http://central:8000/api/admin/updates/rollback"]);
  const headers = new Headers(fetcher.mock.calls[0][1]?.headers);
  expect(headers.get("X-CSRF-Token")).toBe("csrf");
  expect(headers.get("Cookie")).toContain("gsm_session=session");
  expect(headers.get("Authorization")).toBeNull();
  for (const action of ["status", "recover", "../../internal", "exec"]) {
    expect((await POST(new Request("https://gsm.test/"), { params: Promise.resolve({ action }) })).status).toBe(404);
  }
  expect((await GET(new Request("https://gsm.test/"), { params: Promise.resolve({ action: "prepare" }) })).status).toBe(404);
  expect(fetcher).toHaveBeenCalledTimes(3);
});
