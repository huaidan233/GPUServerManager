import { afterEach, expect, test, vi } from "vitest";

import { GET as account } from "../app/api/gsm/account/route";
import { GET as accountPrograms } from "../app/api/gsm/account/programs/route";
import { GET as accountSessions } from "../app/api/gsm/account/sessions/route";
import { GET as accountWechat } from "../app/api/gsm/account/wechat-binding/route";
import { GET as memberDetail } from "../app/api/gsm/admin/members/[memberId]/route";
import { POST as memberAction } from "../app/api/gsm/admin/members/[memberId]/[action]/route";
import { POST as transferContainers } from "../app/api/gsm/admin/members/[memberId]/containers/transfer/route";
import { GET as membersGet, POST as membersPost } from "../app/api/gsm/admin/members/route";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

test("member BFF routes expose only fixed Central member endpoints", async () => {
  vi.stubEnv("GSM_WEB_API_URL", "http://central:8000");
  const fetcher = vi.fn(async () => Response.json({ status: "ok" }));
  vi.stubGlobal("fetch", fetcher);
  const getRequest = new Request("https://gsm.test/api/gsm/account?member_id=forged");
  const postRequest = () => new Request("https://gsm.test/api/gsm/admin/members", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason: "approved" }),
  });

  await account(getRequest);
  await accountPrograms(getRequest);
  await accountWechat(getRequest);
  await accountSessions(getRequest);
  await membersGet(getRequest);
  await membersPost(postRequest());
  await memberDetail(getRequest, { params: Promise.resolve({ memberId: "member-alice" }) });
  await memberAction(postRequest(), {
    params: Promise.resolve({ memberId: "member-alice", action: "extend" }),
  });
  await memberAction(postRequest(), {
    params: Promise.resolve({ memberId: "member-alice", action: "supervisor" }),
  });
  await transferContainers(postRequest(), {
    params: Promise.resolve({ memberId: "member-alice" }),
  });

  expect(fetcher.mock.calls.map(([url, init]) => [url, init?.method])).toEqual([
    ["http://central:8000/api/me", "GET"],
    ["http://central:8000/api/me/programs", "GET"],
    ["http://central:8000/api/me/wechat-binding", "GET"],
    ["http://central:8000/api/me/sessions", "GET"],
    ["http://central:8000/api/admin/members", "GET"],
    ["http://central:8000/api/admin/members", "POST"],
    ["http://central:8000/api/admin/members/member-alice", "GET"],
    ["http://central:8000/api/admin/members/member-alice/extend", "POST"],
    ["http://central:8000/api/admin/members/member-alice/supervisor", "POST"],
    ["http://central:8000/api/admin/members/member-alice/containers/transfer", "POST"],
  ]);
});

test("dynamic member route rejects non-whitelisted commands", async () => {
  const fetcher = vi.fn();
  vi.stubGlobal("fetch", fetcher);
  const response = await memberAction(
    new Request("https://gsm.test/api/gsm/admin/members/member-alice/owner", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role: "admin" }),
    }),
    { params: Promise.resolve({ memberId: "member-alice", action: "owner" }) },
  );

  expect(response.status).toBe(404);
  expect(fetcher).not.toHaveBeenCalled();
});
