import { createHmac } from "node:crypto";

import { describe, expect, test, vi } from "vitest";

import {
  proxyApprovalDecision,
  proxyAccount,
  proxyAccountPrograms,
  proxyAccountActivity,
  proxyAccountSessions,
  proxyAccountWechatPairingCode,
  proxyAccountWechatUnbind,
  proxyAccountWechatBinding,
  proxyAdminMemberAction,
  proxyAdminMemberBatch,
  proxyAdminMemberDetail,
  proxyAdminMembers,
  proxyAdminAgentSettings,
  proxyAdminAgentSettingsHealth,
  proxyAdminAgentSettingsTest,
  proxyAdminTransferContainers,
  proxyAuthActivate,
  proxyBackupManifest,
  proxyConfirmAction,
  proxyCreateBackup,
  proxyCreateContainer,
  proxyCreateDiagnostic,
  proxyDeleteContainer,
  proxyDrillBackup,
  proxyExecuteApproval,
  proxyGetContainer,
  proxyIssueSshCertificate,
  proxyGetDiagnostic,
  proxyGovernanceRecommendations,
  proxyHealthEvents,
  proxyHealthSummary,
  proxyInspectExecutionReconciliation,
  proxyJoinGovernanceQueue,
  proxyListBackups,
  proxyListGovernanceQueue,
  proxyListExecutionReconciliations,
  proxyPullImage,
  proxyRequestBackupRestore,
  proxyResolveExecutionReconciliation,
  proxyStartContainer,
  proxyStopContainer,
  proxyVerifyBackup,
  proxyWithdrawGovernanceQueue,
  proxyCreatePrintJob,
  proxyAuthLogin,
  proxyAuthLogout,
  proxyAuthPasswordChange,
  proxyAuthReauthenticate,
  proxyAuthRevokeOtherSessions,
  proxyAuthSession,
  proxyTeacherStudents,
  proxyTeacherStudentInvite,
  proxyTeacherSupervisors,
  proxyTeacherStudentAction,
  proxyWorkspaceOverview,
} from "../lib/apiProxy";

test("forwards SSH certificate requests through the member session", async () => {
  const fetcher = vi.fn(async () => Response.json({ certificate: "ssh-ed25519-cert" }));
  const request = new Request("https://gsm.test/api/gsm/containers/c1/ssh-certificate", {
    method: "POST",
    headers: {
      Cookie: "gsm_session=session; gsm_csrf=csrf",
      "Content-Type": "application/json",
      "X-CSRF-Token": "csrf",
    },
    body: JSON.stringify({ public_key: "ssh-ed25519 AAAA" }),
  });

  const response = await proxyIssueSshCertificate("c1", request, {
    apiUrl: "http://api:8000",
    fetcher,
  });

  expect(response.status).toBe(200);
  expect(fetcher).toHaveBeenCalledWith("http://api:8000/api/containers/c1/ssh-certificate", expect.objectContaining({
    method: "POST",
    body: JSON.stringify({ public_key: "ssh-ed25519 AAAA" }),
  }));
});

describe("administrator Agent settings proxy", () => {
  test("forwards only session-bound settings requests to explicit Central paths", async () => {
    const fetcher = vi.fn(async () => Response.json({ status: "ok" }));
    const options = { apiUrl: "http://api:8000", fetcher };
    const getRequest = new Request("https://gsm.test/api/gsm/admin/agent-settings", {
      headers: { Cookie: "gsm_session=session" },
    });
    const mutationRequest = () => new Request(
      "https://gsm.test/api/gsm/admin/agent-settings",
      {
        method: "POST",
        headers: {
          Cookie: "gsm_session=session; gsm_csrf=csrf",
          Origin: "https://gsm.test",
          "X-CSRF-Token": "csrf",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ slot: "primary" }),
      },
    );

    await proxyAdminAgentSettings(getRequest, "GET", options);
    await proxyAdminAgentSettings(mutationRequest(), "PUT", options);
    await proxyAdminAgentSettingsHealth(getRequest, options);
    await proxyAdminAgentSettingsTest(mutationRequest(), options);

    expect(fetcher.mock.calls.map(([url, init]) => [url, init?.method])).toEqual([
      ["http://api:8000/api/admin/agent-settings", "GET"],
      ["http://api:8000/api/admin/agent-settings", "PUT"],
      ["http://api:8000/api/admin/agent-settings/health", "GET"],
      ["http://api:8000/api/admin/agent-settings/test", "POST"],
    ]);
    for (const [, init] of fetcher.mock.calls) {
      expect(new Headers(init?.headers).get("authorization")).toBeNull();
    }
  });
});

function requestWithBody(body: unknown): Request {
  return new Request("http://localhost/api/gsm/containers", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

function emptyRequest(method = "POST"): Request {
  return new Request("http://localhost/api/gsm/containers/c1", { method });
}

describe("proxyCreateContainer", () => {
  test("preserves login device metadata without forwarding browser authorization", async () => {
    const fetcher = vi.fn(async () => Response.json({ status: "ok" }));
    const userAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/140.0.0.0";
    const request = new Request("https://gsm.test/api/gsm/auth/login", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "User-Agent": userAgent,
        Authorization: "Bearer browser-supplied-token",
      },
      body: JSON.stringify({ username: "alice", password: "password" }),
    });

    await proxyAuthLogin(request, { apiUrl: "http://api:8000", fetcher });

    const headers = new Headers(fetcher.mock.calls[0][1]?.headers);
    expect(headers.get("user-agent")).toBe(userAgent);
    expect(headers.get("authorization")).toBeNull();
  });

  test("forwards member session cookies and csrf without a shared token", async () => {
    const fetcher = vi.fn(async () => Response.json({ status: "ok" }));
    const request = new Request("https://gsm.test/api/gsm/containers", {
      method: "POST",
      headers: {
        Cookie: "gsm_session=session; gsm_csrf=csrf",
        Origin: "https://gsm.test",
        "X-CSRF-Token": "csrf",
        "Content-Type": "application/json",
        Authorization: "Bearer browser-supplied-token",
        "X-Untrusted-Identity": "admin",
      },
      body: JSON.stringify({ name: "job" }),
    });

    await proxyCreateContainer(request, { apiUrl: "http://api:8000", fetcher });

    expect(fetcher).toHaveBeenCalledWith("http://api:8000/api/containers", {
      method: "POST",
      headers: {
        Cookie: "gsm_session=session; gsm_csrf=csrf",
        Origin: "https://gsm.test",
        "X-CSRF-Token": "csrf",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ name: "job" }),
    });
    expect(fetcher.mock.calls[0][1]?.headers).not.toHaveProperty("Authorization");
  });

  test("rejects the development bearer hook in production", async () => {
    vi.stubEnv("NODE_ENV", "production");
    await expect(
      proxyCreateContainer(requestWithBody({ name: "job" }), {
        apiUrl: "http://api:8000",
        developmentToken: "dev-token",
        fetcher: vi.fn(),
      }),
    ).rejects.toThrow("development token is unavailable in production");
    vi.unstubAllEnvs();
  });

  test("supports an explicit development bearer hook", async () => {
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => {
      return {
        ok: true,
        status: 201,
        json: async () => ({ container: { id: "c1", name: "debug" }, warnings: [] }),
      } as Response;
    });

    const response = await proxyCreateContainer(requestWithBody({ name: "debug" }), {
      apiUrl: "http://api:8000",
      developmentToken: "server-token",
      fetcher,
    });

    expect(response.status).toBe(201);
    expect(await response.json()).toEqual({
      container: { id: "c1", name: "debug" },
      warnings: [],
    });
    expect(fetcher).toHaveBeenCalledWith("http://api:8000/api/containers", {
      method: "POST",
      headers: {
        Authorization: "Bearer server-token",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ name: "debug" }),
    });
  });

  test("maps Central API failures to JSON errors", async () => {
    const response = await proxyCreateContainer(requestWithBody({ name: "bad" }), {
      apiUrl: "http://api:8000",
      developmentToken: "server-token",
      fetcher: async () =>
        ({
          ok: false,
          status: 403,
          json: async () => ({ detail: "role cannot create containers" }),
        }) as Response,
    });

    expect(response.status).toBe(403);
    expect(await response.json()).toEqual({ detail: "role cannot create containers" });
  });

  test("gets one container with only the member session", async () => {
    const fetcher = vi.fn(async () => Response.json({ id: "c/1", status: "running" }));
    const request = new Request("https://gsm.test/api/gsm/containers/c%2F1", {
      headers: {
        Cookie: "gsm_session=session; gsm_csrf=csrf",
        Authorization: "Bearer forged-browser-token",
      },
    });

    const response = await proxyGetContainer("c/1", request, {
      apiUrl: "http://api:8000",
      fetcher,
    });

    expect(response.status).toBe(200);
    expect(fetcher).toHaveBeenCalledWith("http://api:8000/api/containers/c%2F1", {
      method: "GET",
      headers: { Cookie: "gsm_session=session; gsm_csrf=csrf" },
    });
    expect(fetcher.mock.calls[0][1]?.headers).not.toHaveProperty("Authorization");
  });

  test("forwards start container requests without exposing the token", async () => {
    const fetcher = vi.fn(async () => {
      return {
        ok: true,
        status: 200,
        json: async () => ({ container: { id: "c1", status: "running" } }),
      } as Response;
    });

    const response = await proxyStartContainer("c1", emptyRequest(), {
      apiUrl: "http://api:8000/api",
      developmentToken: "server-token",
      fetcher,
    });

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ container: { id: "c1", status: "running" } });
    expect(fetcher).toHaveBeenCalledWith("http://api:8000/api/containers/c1/start", {
      method: "POST",
      headers: {
        Authorization: "Bearer server-token",
      },
    });
  });

  test("forwards stop container requests and preserves confirmation payloads", async () => {
    const fetcher = vi.fn(async () => {
      return {
        ok: true,
        status: 202,
        json: async () => ({
          requires_confirmation: true,
          confirmation: { id: "confirm-stop", required_phrase: "停止 container llama-debug" },
        }),
      } as Response;
    });

    const response = await proxyStopContainer("c1", emptyRequest(), {
      apiUrl: "http://api:8000",
      developmentToken: "server-token",
      fetcher,
    });

    expect(response.status).toBe(202);
    expect(await response.json()).toEqual({
      requires_confirmation: true,
      confirmation: { id: "confirm-stop", required_phrase: "停止 container llama-debug" },
    });
    expect(fetcher).toHaveBeenCalledWith("http://api:8000/api/containers/c1/stop", {
      method: "POST",
      headers: {
        Authorization: "Bearer server-token",
      },
    });
  });

  test("forwards delete container requests through the server-side token", async () => {
    const fetcher = vi.fn(async () => {
      return {
        ok: true,
        status: 202,
        json: async () => ({
          requires_confirmation: true,
          confirmation: { id: "confirm-delete", required_phrase: "删除 container old-job" },
        }),
      } as Response;
    });

    const response = await proxyDeleteContainer("c1", emptyRequest("DELETE"), {
      apiUrl: "http://api:8000",
      developmentToken: "server-token",
      fetcher,
    });

    expect(response.status).toBe(202);
    expect(await response.json()).toEqual({
      requires_confirmation: true,
      confirmation: { id: "confirm-delete", required_phrase: "删除 container old-job" },
    });
    expect(fetcher).toHaveBeenCalledWith("http://api:8000/api/containers/c1", {
      method: "DELETE",
      headers: {
        Authorization: "Bearer server-token",
      },
    });
  });

  test("forwards confirmation phrase requests", async () => {
    const fetcher = vi.fn(async () => {
      return {
        ok: true,
        status: 200,
        json: async () => ({ status: "executed", container: { id: "c1", status: "stopped" } }),
      } as Response;
    });
    const request = requestWithBody({ phrase: "停止 container llama-debug" });

    const response = await proxyConfirmAction("confirm-stop", request, {
      apiUrl: "http://api:8000",
      developmentToken: "server-token",
      fetcher,
    });

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      status: "executed",
      container: { id: "c1", status: "stopped" },
    });
    expect(fetcher).toHaveBeenCalledWith("http://api:8000/api/confirmations/confirm-stop/confirm", {
      method: "POST",
      headers: {
        Authorization: "Bearer server-token",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ phrase: "停止 container llama-debug" }),
    });
  });

  test("forwards image pull requests to Central API", async () => {
    const fetcher = vi.fn(async () => {
      return {
        ok: true,
        status: 201,
        json: async () => ({
          status: "pulled",
          image: { name: "docker.io/pytorch/pytorch", tag: "2.6.0-cuda12.4-cudnn9-runtime" },
        }),
      } as Response;
    });
    const request = requestWithBody({
      server_id: "server1",
      image: "docker.io/pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime",
    });

    const response = await proxyPullImage(request, {
      apiUrl: "http://api:8000",
      developmentToken: "server-token",
      fetcher,
    });

    expect(response.status).toBe(201);
    expect(await response.json()).toEqual({
      status: "pulled",
      image: { name: "docker.io/pytorch/pytorch", tag: "2.6.0-cuda12.4-cudnn9-runtime" },
    });
    expect(fetcher).toHaveBeenCalledWith("http://api:8000/api/images/pull", {
      method: "POST",
      headers: {
        Authorization: "Bearer server-token",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        server_id: "server1",
        image: "docker.io/pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime",
      }),
    });
  });

  test("forwards recommendation and queue requests without exposing credentials", async () => {
    const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => ({
      ok: true,
      status: init?.method === "POST" ? 201 : 200,
      json: async () => ({ status: "ok" }),
    }) as Response);
    const options = { apiUrl: "http://api:8000", developmentToken: "server-token", fetcher };

    await proxyGovernanceRecommendations(
      requestWithBody({ memory_tier: "8_24", estimated_duration_minutes: 120 }),
      options,
    );
    await proxyListGovernanceQueue(emptyRequest("GET"), options);
    await proxyJoinGovernanceQueue(requestWithBody({ memory_tier: "8_24" }), options);
    await proxyWithdrawGovernanceQueue("q1", emptyRequest("DELETE"), options);

    expect(fetcher).toHaveBeenNthCalledWith(1, "http://api:8000/api/governance/recommendations", {
      method: "POST",
      headers: {
        Authorization: "Bearer server-token",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ memory_tier: "8_24", estimated_duration_minutes: 120 }),
    });
    expect(fetcher).toHaveBeenNthCalledWith(2, "http://api:8000/api/governance/queue", {
      method: "GET",
      headers: { Authorization: "Bearer server-token" },
    });
    expect(fetcher).toHaveBeenNthCalledWith(3, "http://api:8000/api/governance/queue", {
      method: "POST",
      headers: {
        Authorization: "Bearer server-token",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ memory_tier: "8_24" }),
    });
    expect(fetcher).toHaveBeenNthCalledWith(4, "http://api:8000/api/governance/queue/q1", {
      method: "DELETE",
      headers: { Authorization: "Bearer server-token" },
    });
  });

  test("forwards approval decisions and executes only the frozen approval id", async () => {
    const fetcher = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ status: "approved" }),
    }) as Response);
    const options = { apiUrl: "http://api:8000/api", developmentToken: "server-token", fetcher };

    await proxyApprovalDecision(
      "a1",
      requestWithBody({ decision: "approved", reason: "课题需要" }),
      options,
    );
    await proxyExecuteApproval("a1", emptyRequest(), options);

    expect(fetcher).toHaveBeenNthCalledWith(1, "http://api:8000/api/governance/approvals/a1/decision", {
      method: "POST",
      headers: {
        Authorization: "Bearer server-token",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ decision: "approved", reason: "课题需要" }),
    });
    expect(fetcher).toHaveBeenNthCalledWith(2, "http://api:8000/api/governance/approvals/a1/execute", {
      method: "POST",
      headers: { Authorization: "Bearer server-token" },
    });
  });

  test("forwards health and diagnostic requests with server-side credentials", async () => {
    const fetcher = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ status: "ok" }),
    }) as Response);
    const options = { apiUrl: "http://api:8000", developmentToken: "server-token", fetcher };

    await proxyHealthSummary(emptyRequest("GET"), options);
    await proxyHealthEvents(emptyRequest("GET"), options);
    await proxyCreateDiagnostic("c1", emptyRequest(), options);
    await proxyGetDiagnostic("d1", emptyRequest("GET"), options);

    expect(fetcher).toHaveBeenNthCalledWith(1, "http://api:8000/api/operations/health", {
      method: "GET",
      headers: { Authorization: "Bearer server-token" },
    });
    expect(fetcher).toHaveBeenNthCalledWith(2, "http://api:8000/api/operations/health/events", {
      method: "GET",
      headers: { Authorization: "Bearer server-token" },
    });
    expect(fetcher).toHaveBeenNthCalledWith(3, "http://api:8000/api/containers/c1/diagnostics", {
      method: "POST",
      headers: { Authorization: "Bearer server-token" },
    });
    expect(fetcher).toHaveBeenNthCalledWith(4, "http://api:8000/api/diagnostics/d1", {
      method: "GET",
      headers: { Authorization: "Bearer server-token" },
    });
  });

  test("forwards backup operations without exposing admin credentials", async () => {
    const fetcher = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ status: "ok" }),
    }) as Response);
    const options = { apiUrl: "http://api:8000", developmentToken: "server-token", fetcher };

    await proxyListBackups(emptyRequest("GET"), options);
    await proxyCreateBackup(requestWithBody({ kind: "manual" }), options);
    await proxyBackupManifest("daily.tar.gz", emptyRequest("GET"), options);
    await proxyVerifyBackup("daily.tar.gz", emptyRequest(), options);
    await proxyDrillBackup("daily.tar.gz", emptyRequest(), options);
    await proxyRequestBackupRestore("daily.tar.gz", emptyRequest(), options);

    expect(fetcher).toHaveBeenNthCalledWith(1, "http://api:8000/api/operations/backups", {
      method: "GET",
      headers: { Authorization: "Bearer server-token" },
    });
    expect(fetcher).toHaveBeenNthCalledWith(2, "http://api:8000/api/operations/backups", {
      method: "POST",
      headers: {
        Authorization: "Bearer server-token",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ kind: "manual" }),
    });
    expect(fetcher).toHaveBeenNthCalledWith(
      6,
      "http://api:8000/api/operations/backups/daily.tar.gz/restore-request",
      { method: "POST", headers: { Authorization: "Bearer server-token" } },
    );
  });
});

describe("authentication proxy", () => {
  test("signs the proxy-observed client source for Central API rate limiting", async () => {
    const fetcher = vi.fn(async () => Response.json({ detail: "invalid credentials" }, { status: 401 }));
    const request = new Request("https://gsm.test/api/gsm/auth/login", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-GSM-Edge-Client-Source": "203.0.113.25",
        "X-GSM-Edge-Secret": "edge-secret",
      },
      body: JSON.stringify({ username: "alice", password: "wrong" }),
    });

    await proxyAuthLogin(request, {
      apiUrl: "http://api:8000",
      fetcher,
      trustedProxySecret: "proxy-secret",
      edgeProxySecret: "edge-secret",
    });

    const headers = new Headers(fetcher.mock.calls[0][1]?.headers);
    const timestamp = headers.get("x-gsm-client-timestamp")!;
    const expected = createHmac("sha256", "proxy-secret")
      .update(`${timestamp}.203.0.113.25`, "utf8")
      .digest("hex");
    expect(headers.get("x-gsm-client-source")).toBe("203.0.113.25");
    expect(headers.get("x-gsm-client-signature")).toBe(`sha256=${expected}`);
  });

  test("maps execution reconciliation requests and encodes the confirmation id", async () => {
    const fetcher = vi.fn(async () => Response.json({ status: "ok" }));
    const options = { apiUrl: "http://api:8000", fetcher };
    const request = emptyRequest("GET");

    await proxyListExecutionReconciliations(request, options);
    await proxyInspectExecutionReconciliation("confirm/1", emptyRequest(), options);
    await proxyResolveExecutionReconciliation(
      "confirm/1",
      requestWithBody({ outcome: "executed", note: "目标状态已经核验" }),
      options,
    );

    expect(fetcher.mock.calls.map(([url, init]) => [url, init?.method])).toEqual([
      ["http://api:8000/api/operations/reconciliations", "GET"],
      ["http://api:8000/api/operations/reconciliations/confirm%2F1/inspect", "POST"],
      ["http://api:8000/api/operations/reconciliations/confirm%2F1/resolve", "POST"],
    ]);
    expect(fetcher.mock.calls[2][1]?.body).toBe(JSON.stringify({
      outcome: "executed",
      note: "目标状态已经核验",
    }));
  });

  test("encodes every dynamic operation identifier before building a Central path", async () => {
    const fetcher = vi.fn(async () => Response.json({ status: "ok" }));
    const options = { apiUrl: "http://api:8000", fetcher };
    const identifier = "scope/item?revision=1";

    await proxyStartContainer(identifier, emptyRequest(), options);
    await proxyStopContainer(identifier, emptyRequest(), options);
    await proxyDeleteContainer(identifier, emptyRequest("DELETE"), options);
    await proxyConfirmAction(identifier, requestWithBody({ phrase: "confirm" }), options);
    await proxyWithdrawGovernanceQueue(identifier, emptyRequest("DELETE"), options);
    await proxyApprovalDecision(identifier, requestWithBody({ decision: "approved" }), options);
    await proxyExecuteApproval(identifier, emptyRequest(), options);
    await proxyCreateDiagnostic(identifier, emptyRequest(), options);
    await proxyGetDiagnostic(identifier, emptyRequest("GET"), options);

    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      "http://api:8000/api/containers/scope%2Fitem%3Frevision%3D1/start",
      "http://api:8000/api/containers/scope%2Fitem%3Frevision%3D1/stop",
      "http://api:8000/api/containers/scope%2Fitem%3Frevision%3D1",
      "http://api:8000/api/confirmations/scope%2Fitem%3Frevision%3D1/confirm",
      "http://api:8000/api/governance/queue/scope%2Fitem%3Frevision%3D1",
      "http://api:8000/api/governance/approvals/scope%2Fitem%3Frevision%3D1/decision",
      "http://api:8000/api/governance/approvals/scope%2Fitem%3Frevision%3D1/execute",
      "http://api:8000/api/containers/scope%2Fitem%3Frevision%3D1/diagnostics",
      "http://api:8000/api/diagnostics/scope%2Fitem%3Frevision%3D1",
    ]);
  });

  test("enforces the JSON size limit against bytes actually read", async () => {
    vi.stubEnv("GSM_WEB_JSON_MAX_BYTES", "5");
    const fetcher = vi.fn();
    const request = new Request("http://localhost/api/gsm/containers", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Content-Length": "2",
      },
      body: JSON.stringify({ name: "larger-than-declared" }),
    });

    const response = await proxyCreateContainer(request, { fetcher });

    expect(response.status).toBe(413);
    expect(await response.json()).toEqual({ detail: "JSON request exceeds the size limit" });
    expect(fetcher).not.toHaveBeenCalled();
    vi.unstubAllEnvs();
  });

  test("does not sign a browser-forged edge source without the edge secret", async () => {
    const fetcher = vi.fn(async () => Response.json({ detail: "invalid credentials" }, { status: 401 }));
    const request = new Request("https://gsm.test/api/gsm/auth/login", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-GSM-Edge-Client-Source": "203.0.113.99",
      },
      body: JSON.stringify({ username: "alice", password: "wrong" }),
    });

    await proxyAuthLogin(request, {
      apiUrl: "http://api:8000",
      fetcher,
      trustedProxySecret: "proxy-secret",
      edgeProxySecret: "edge-secret",
    });

    const headers = new Headers(fetcher.mock.calls[0][1]?.headers);
    expect(headers.get("x-gsm-client-source")).toBe("untrusted-direct");
  });

  test("preserves multiple authentication cookies from Central API", async () => {
    const centralHeaders = new Headers({ "Content-Type": "application/json" });
    centralHeaders.append("Set-Cookie", "gsm_session=session; HttpOnly; Path=/");
    centralHeaders.append("Set-Cookie", "gsm_csrf=csrf; Path=/");
    const fetcher = vi.fn(async () =>
      new Response(JSON.stringify({ member: { username: "alice" } }), {
        status: 200,
        headers: centralHeaders,
      }));
    const request = new Request("https://gsm.test/api/gsm/auth/login", {
      method: "POST",
      headers: { Origin: "https://gsm.test", "Content-Type": "application/json" },
      body: JSON.stringify({ username: "alice", password: "secret" }),
    });

    const response = await proxyAuthLogin(request, {
      apiUrl: "http://api:8000",
      fetcher,
    });

    expect(response.status).toBe(200);
    expect(response.headers.get("set-cookie")).toContain("gsm_session=session");
    expect(response.headers.get("set-cookie")).toContain("gsm_csrf=csrf");
    expect(response.headers.getSetCookie()).toEqual([
      "gsm_session=session; HttpOnly; Path=/",
      "gsm_csrf=csrf; Path=/",
    ]);
  });

  test("splits combined authentication cookies without breaking Expires dates", async () => {
    const centralHeaders = new Headers({
      "Content-Type": "application/json",
      "Set-Cookie": [
        "gsm_session=session; Expires=Wed, 21 Oct 2026 07:28:00 GMT; HttpOnly; Path=/",
        "gsm_csrf=csrf; Path=/",
      ].join(", "),
    });
    Object.defineProperty(centralHeaders, "getSetCookie", { value: undefined });
    const fetcher = vi.fn(async () => ({
      status: 200,
      headers: centralHeaders,
      json: async () => ({ member: { username: "alice" } }),
    }) as Response);
    const request = new Request("https://gsm.test/api/gsm/auth/login", {
      method: "POST",
      headers: { Origin: "https://gsm.test", "Content-Type": "application/json" },
      body: JSON.stringify({ username: "alice", password: "secret" }),
    });

    const response = await proxyAuthLogin(request, {
      apiUrl: "http://api:8000",
      fetcher,
    });

    expect(response.headers.getSetCookie()).toEqual([
      "gsm_session=session; Expires=Wed, 21 Oct 2026 07:28:00 GMT; HttpOnly; Path=/",
      "gsm_csrf=csrf; Path=/",
    ]);
  });

  test("maps every member authentication endpoint", async () => {
    const fetcher = vi.fn(async () => Response.json({ status: "ok" }));
    const options = { apiUrl: "http://api:8000", fetcher };
    const jsonRequest = () => new Request("https://gsm.test/api/gsm/auth", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value: "test" }),
    });
    const emptyPost = () => new Request("https://gsm.test/api/gsm/auth", { method: "POST" });

    await proxyAuthActivate(jsonRequest(), options);
    await proxyAuthLogin(jsonRequest(), options);
    await proxyAuthSession(new Request("https://gsm.test/api/gsm/auth", { method: "GET" }), options);
    await proxyAuthLogout(emptyPost(), options);
    await proxyAuthPasswordChange(jsonRequest(), options);
    await proxyAuthReauthenticate(jsonRequest(), options);

    expect(fetcher.mock.calls.map(([url, init]) => [url, init?.method])).toEqual([
      ["http://api:8000/api/auth/activate", "POST"],
      ["http://api:8000/api/auth/login", "POST"],
      ["http://api:8000/api/auth/session", "GET"],
      ["http://api:8000/api/auth/logout", "POST"],
      ["http://api:8000/api/auth/password/change", "POST"],
      ["http://api:8000/api/auth/reauthenticate", "POST"],
    ]);
  });
});

describe("member workspace proxy", () => {
  test("new role workspace proxies forward only the member session", async () => {
    const fetcher = vi.fn(async () => Response.json({ tasks: {} }));
    const options = { apiUrl: "http://api:8000", fetcher };
    const request = new Request(
      "https://gsm.test/api/gsm/workspace/overview?member_id=forged",
      {
        headers: {
          Cookie: "gsm_session=session; gsm_csrf=csrf",
          Authorization: "Bearer forged-browser-token",
          "X-Untrusted-Identity": "member-admin",
        },
      },
    );

    await proxyWorkspaceOverview(request, options);
    await proxyTeacherStudents(request, options);
    await proxyAccountActivity(request, options);

    expect(fetcher.mock.calls.map(([url, init]) => [url, init?.headers])).toEqual([
      [
        "http://api:8000/api/workspace/overview",
        { Cookie: "gsm_session=session; gsm_csrf=csrf" },
      ],
      [
        "http://api:8000/api/teacher/students",
        { Cookie: "gsm_session=session; gsm_csrf=csrf" },
      ],
      [
        "http://api:8000/api/me/activity",
        { Cookie: "gsm_session=session; gsm_csrf=csrf" },
      ],
    ]);
    expect(
      fetcher.mock.calls.every(([, init]) =>
        !("Authorization" in (init?.headers as Record<string, string>)),
      ),
    ).toBe(true);
  });

  test("teacher academic actions use an explicit action whitelist", async () => {
    const fetcher = vi.fn(async () => Response.json({ status: "ok" }));
    const request = () => new Request(
      "https://gsm.test/api/gsm/teacher/students/member-alice/extend",
      {
        method: "POST",
        headers: {
          Cookie: "gsm_session=session; gsm_csrf=csrf",
          Origin: "https://gsm.test",
          "X-CSRF-Token": "csrf",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ expected_version: 2, reason: "approved" }),
      },
    );
    const options = { apiUrl: "http://api:8000", fetcher };

    await proxyTeacherStudentAction("member/alice", "extend", request(), options);
    const rejected = await proxyTeacherStudentAction(
      "member-alice",
      "containers/transfer",
      request(),
      options,
    );

    expect(fetcher).toHaveBeenCalledWith(
      "http://api:8000/api/teacher/students/member%2Falice/extend",
      expect.objectContaining({ method: "POST" }),
    );
    expect(rejected.status).toBe(404);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  test("maps self-service requests to fixed signed-in-member paths", async () => {
    const fetcher = vi.fn(async () => Response.json({ status: "ok" }));
    const options = { apiUrl: "http://api:8000", fetcher };
    const request = new Request(
      "https://gsm.test/api/gsm/account?member_id=member-admin",
      { headers: { Cookie: "gsm_session=session" } },
    );

    await proxyAccount(request, options);
    await proxyAccountPrograms(request, options);
    await proxyAccountWechatBinding(request, options);
    await proxyAccountSessions(request, options);

    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      "http://api:8000/api/me",
      "http://api:8000/api/me/programs",
      "http://api:8000/api/me/wechat-binding",
      "http://api:8000/api/me/sessions",
    ]);
    expect(fetcher.mock.calls.every(([, init]) => init?.method === "GET")).toBe(true);
  });

  test("maps password-confirmed account mutations to fixed self-service paths", async () => {
    const fetcher = vi.fn(async () => Response.json({ status: "ok" }));
    const options = { apiUrl: "http://api:8000", fetcher };
    const request = () => new Request(
      "https://gsm.test/api/gsm/account?member_id=member-admin",
      {
        method: "POST",
        headers: {
          Cookie: "gsm_session=session; gsm_csrf=csrf",
          Origin: "https://gsm.test",
          "X-CSRF-Token": "csrf",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ password: "member-password" }),
      },
    );

    await proxyAuthRevokeOtherSessions(request(), options);
    await proxyAccountWechatPairingCode(request(), options);
    await proxyAccountWechatUnbind(request(), options);

    expect(fetcher.mock.calls.map(([url, init]) => [url, init?.method, init?.body])).toEqual([
      ["http://api:8000/api/auth/sessions/revoke-others", "POST", JSON.stringify({ password: "member-password" })],
      ["http://api:8000/api/me/wechat-pairing-code", "POST", JSON.stringify({ password: "member-password" })],
      ["http://api:8000/api/me/wechat-unbind", "POST", JSON.stringify({ password: "member-password" })],
    ]);
  });

  test("whitelists member commands and keeps transfer on an explicit path", async () => {
    const fetcher = vi.fn(async () => Response.json({ status: "ok" }));
    const options = { apiUrl: "http://api:8000", fetcher };
    const getRequest = new Request("https://gsm.test/api/gsm/admin/members", {
      headers: { Cookie: "gsm_session=session" },
    });
    const postRequest = () => new Request("https://gsm.test/api/gsm/admin/members", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_version: 2, reason: "approved" }),
    });

    await proxyAdminMembers(getRequest, "GET", options);
    await proxyAdminMembers(postRequest(), "POST", options);
    await proxyAdminMemberBatch(postRequest(), options);
    await proxyAdminMemberDetail("member/alice", getRequest, options);
    await proxyAdminMemberAction("member/alice", "extend", postRequest(), options);
    await proxyAdminTransferContainers("member/alice", postRequest(), options);

    expect(fetcher.mock.calls.map(([url, init]) => [url, init?.method])).toEqual([
      ["http://api:8000/api/admin/members", "GET"],
      ["http://api:8000/api/admin/members", "POST"],
      ["http://api:8000/api/admin/members/batch", "POST"],
      ["http://api:8000/api/admin/members/member%2Falice", "GET"],
      ["http://api:8000/api/admin/members/member%2Falice/extend", "POST"],
      ["http://api:8000/api/admin/members/member%2Falice/containers/transfer", "POST"],
    ]);

    const rejected = await proxyAdminMemberAction(
      "member-alice",
      "containers/transfer",
      postRequest(),
      options,
    );
    expect(rejected.status).toBe(404);
    expect(fetcher).toHaveBeenCalledTimes(6);
  });
});

describe("print multipart proxy", () => {
  test("streams uploaded bytes with the original multipart boundary", async () => {
    const bytes = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x00, 0xff]);
    const boundary = "gsm-test-boundary";
    const prefix = new TextEncoder().encode(
      `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="paper.pdf"\r\nContent-Type: application/pdf\r\n\r\n`,
    );
    const suffix = new TextEncoder().encode(`\r\n--${boundary}--\r\n`);
    const body = new Uint8Array(prefix.length + bytes.length + suffix.length);
    body.set(prefix);
    body.set(bytes, prefix.length);
    body.set(suffix, prefix.length + bytes.length);
    const request = new Request("http://localhost/api/gsm/print/jobs", {
      method: "POST",
      headers: {
        "Content-Type": `multipart/form-data; boundary=${boundary}`,
        "Content-Length": body.byteLength.toString(),
        Cookie: "gsm_session=session; gsm_csrf=csrf",
        Origin: "https://gsm.test",
        "X-CSRF-Token": "csrf",
      },
      body,
    });
    const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const forwarded = new Uint8Array(await new Response(init?.body).arrayBuffer());
      expect(Array.from(forwarded)).toEqual(Array.from(body));
      return {
        status: 201,
        json: async () => ({ id: "job-1", status: "draft" }),
      } as Response;
    });

    const response = await proxyCreatePrintJob(request, {
      apiUrl: "http://api:8000",
      fetcher,
    });

    expect(response.status).toBe(201);
    expect(await response.json()).toEqual({ id: "job-1", status: "draft" });
    const [forwardedUrl, forwardedInit] = fetcher.mock.calls[0];
    expect(forwardedUrl).toBe("http://api:8000/api/print/jobs");
    expect(forwardedInit?.method).toBe("POST");
    expect(forwardedInit?.headers).toEqual({
      Cookie: "gsm_session=session; gsm_csrf=csrf",
      Origin: "https://gsm.test",
      "X-CSRF-Token": "csrf",
      "Content-Type": `multipart/form-data; boundary=${boundary}`,
      "Content-Length": body.byteLength.toString(),
    });
    expect(forwardedInit?.body?.constructor.name).toBe("ReadableStream");
  });

  test("rejects an oversized print upload before contacting Central API", async () => {
    vi.stubEnv("GSM_PRINT_MAX_FILE_BYTES", "5");
    vi.stubEnv("GSM_PRINT_MULTIPART_OVERHEAD_BYTES", "0");
    const fetcher = vi.fn();
    const request = new Request("http://localhost/api/gsm/print/jobs", {
      method: "POST",
      headers: {
        "Content-Type": "multipart/form-data; boundary=test",
        "Content-Length": "6",
      },
      body: "123456",
    });

    const response = await proxyCreatePrintJob(request, { fetcher });

    expect(response.status).toBe(413);
    expect(fetcher).not.toHaveBeenCalled();
    vi.unstubAllEnvs();
  });
});


test("teacher account proxies allow explicit account actions without container/admin routes", async () => {
  const fetcher = vi.fn(async () => Response.json({ ok: true }));
  const options = { apiUrl: "http://api:8000", fetcher };
  const request = () => new Request("https://gsm.test/api/gsm/teacher/students", {
    method: "POST", headers: { Cookie: "gsm_session=session; gsm_csrf=csrf", "X-CSRF-Token": "csrf", "Content-Type": "application/json" },
    body: JSON.stringify({ expected_version: 1, reason: "account maintenance" }),
  });
  await proxyTeacherStudentInvite(request(), options);
  expect(fetcher).toHaveBeenLastCalledWith("http://api:8000/api/teacher/students", expect.objectContaining({ method: "POST" }));
  await proxyTeacherSupervisors(new Request("https://gsm.test", { headers: { Cookie: "gsm_session=session" } }), options);
  expect(fetcher).toHaveBeenLastCalledWith("http://api:8000/api/teacher/supervisors", expect.objectContaining({ method: "GET" }));
  for (const action of ["supervisor", "role", "password-reset", "disable", "reactivate", "archive"]) {
    expect((await proxyTeacherStudentAction("student", action, request(), options)).status).toBe(200);
    expect(fetcher).toHaveBeenLastCalledWith(`http://api:8000/api/teacher/students/student/${action}`, expect.objectContaining({ method: "POST" }));
  }
  const before = fetcher.mock.calls.length;
  for (const action of ["containers/transfer", "ssh-password", "wechat-unbind", "revoke-sessions", "start"]) {
    expect((await proxyTeacherStudentAction("student", action, request(), options)).status).toBe(404);
  }
  expect(fetcher).toHaveBeenCalledTimes(before);
});
