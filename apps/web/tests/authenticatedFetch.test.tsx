import { afterEach, expect, test, vi } from "vitest";

import { authenticatedFetch } from "../lib/authenticatedFetch";

afterEach(() => {
  document.cookie = "gsm_csrf=; Max-Age=0; Path=/";
  vi.unstubAllGlobals();
});

test("adds the browser csrf cookie to same-origin mutations", async () => {
  document.cookie = "gsm_csrf=csrf%20token; Path=/";
  const fetcher = vi.fn(async () => Response.json({ status: "ok" }));
  vi.stubGlobal("fetch", fetcher);

  await authenticatedFetch("/api/gsm/containers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });

  const [, init] = fetcher.mock.calls[0];
  expect(new Headers(init?.headers).get("X-CSRF-Token")).toBe("csrf token");
  expect(new Headers(init?.headers).get("Content-Type")).toBe("application/json");
});

test("preserves Request headers when adding the csrf token", async () => {
  document.cookie = "gsm_csrf=csrf-token; Path=/";
  const fetcher = vi.fn(async () => Response.json({ status: "ok" }));
  vi.stubGlobal("fetch", fetcher);
  const request = new Request(`${window.location.origin}/api/gsm/containers`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Request-ID": "request-123",
    },
    body: "{}",
  });

  await authenticatedFetch(request);

  const [, init] = fetcher.mock.calls[0];
  const headers = new Headers(init?.headers);
  expect(headers.get("X-CSRF-Token")).toBe("csrf-token");
  expect(headers.get("Content-Type")).toBe("application/json");
  expect(headers.get("X-Request-ID")).toBe("request-123");
});

test("keeps the original request shape when no csrf cookie exists", async () => {
  const fetcher = vi.fn(async () => Response.json({ status: "ok" }));
  vi.stubGlobal("fetch", fetcher);
  const init = { method: "POST", body: "{}" };

  await authenticatedFetch("/api/gsm/containers", init);

  expect(fetcher).toHaveBeenCalledWith("/api/gsm/containers", init);
});

test("does not send the csrf cookie to a different origin", async () => {
  document.cookie = "gsm_csrf=csrf-token; Path=/";
  const fetcher = vi.fn(async () => Response.json({ status: "ok" }));
  vi.stubGlobal("fetch", fetcher);
  const init = { method: "DELETE" };

  await authenticatedFetch("https://central.invalid/api/containers/c1", init);

  expect(fetcher).toHaveBeenCalledWith("https://central.invalid/api/containers/c1", init);
});
