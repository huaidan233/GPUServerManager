import { afterEach, expect, test, vi } from "vitest";

import { POST as activate } from "../app/api/gsm/auth/activate/route";
import { POST as login } from "../app/api/gsm/auth/login/route";
import { POST as logout } from "../app/api/gsm/auth/logout/route";
import { POST as changePassword } from "../app/api/gsm/auth/password/change/route";
import { POST as reauthenticate } from "../app/api/gsm/auth/reauthenticate/route";
import { GET as session } from "../app/api/gsm/auth/session/route";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

test("auth BFF routes forward to their matching Central endpoints", async () => {
  vi.stubEnv("GSM_WEB_API_URL", "http://central:8000");
  const fetcher = vi.fn(async () => Response.json({ status: "ok" }));
  vi.stubGlobal("fetch", fetcher);
  const jsonRequest = () => new Request("https://gsm.test/api/gsm/auth", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value: "test" }),
  });
  const mutationRequest = () => new Request("https://gsm.test/api/gsm/auth", {
    method: "POST",
    headers: {
      Cookie: "gsm_session=session; gsm_csrf=csrf",
      Origin: "https://gsm.test",
      "X-CSRF-Token": "csrf",
    },
  });

  await activate(jsonRequest());
  await login(jsonRequest());
  await session(new Request("https://gsm.test/api/gsm/auth", {
    headers: { Cookie: "gsm_session=session; gsm_csrf=csrf" },
  }));
  await logout(mutationRequest());
  await changePassword(jsonRequest());
  await reauthenticate(jsonRequest());

  expect(fetcher.mock.calls.map(([url, init]) => [url, init?.method])).toEqual([
    ["http://central:8000/api/auth/activate", "POST"],
    ["http://central:8000/api/auth/login", "POST"],
    ["http://central:8000/api/auth/session", "GET"],
    ["http://central:8000/api/auth/logout", "POST"],
    ["http://central:8000/api/auth/password/change", "POST"],
    ["http://central:8000/api/auth/reauthenticate", "POST"],
  ]);
});
