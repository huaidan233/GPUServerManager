import { createHmac, timingSafeEqual } from "node:crypto";

type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

type ProxyOptions = {
  apiUrl?: string;
  developmentToken?: string;
  fetcher?: Fetcher;
  trustedProxySecret?: string;
  edgeProxySecret?: string;
};

export async function proxyGpuHistory(serverId: string, request: Request, options: ProxyOptions = {}): Promise<Response> {
  const minutes = new URL(request.url).searchParams.get("minutes") ?? "60";
  if (!["60", "360", "1440"].includes(minutes)) return Response.json({ detail: "无效的时间范围" }, { status: 422 });
  return proxyCentralRequest(request, `/servers/${encodeURIComponent(serverId)}/gpu-history?minutes=${minutes}`, "GET", options);
}

export async function proxyAuthActivate(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyJsonRequest(request, "/auth/activate", "POST", options);
}

export async function proxyInitialMfa(request: Request, options: ProxyOptions = {}): Promise<Response> {
  return proxyCentralRequest(request, "/auth/initial-mfa", "POST", options);
}

export async function proxyAuthLogin(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyJsonRequest(request, "/auth/login", "POST", options);
}

export async function proxyAuthSession(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(request, "/auth/session", "GET", options);
}

export async function proxyAuthLogout(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(request, "/auth/logout", "POST", options);
}

export async function proxyAuthPasswordChange(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyJsonRequest(request, "/auth/password/change", "POST", options);
}

export async function proxyAuthReauthenticate(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyJsonRequest(request, "/auth/reauthenticate", "POST", options);
}

export async function proxyAgentSettingsReauthenticate(request: Request, options: ProxyOptions = {}): Promise<Response> {
  return proxyJsonRequest(request, "/admin/agent-settings/reauthenticate", "POST", options);
}

export async function proxyAuthRevokeOtherSessions(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyJsonRequest(request, "/auth/sessions/revoke-others", "POST", options);
}

export async function proxyAccount(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(request, "/me", "GET", options);
}

export async function proxyAccountPrograms(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(request, "/me/programs", "GET", options);
}

export async function proxyAccountWechatBinding(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(request, "/me/wechat-binding", "GET", options);
}

export async function proxyAccountSessions(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(request, "/me/sessions", "GET", options);
}

export async function proxyAccountActivity(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(request, "/me/activity", "GET", options);
}

export async function proxyAccountWechatPairingCode(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyJsonRequest(request, "/me/wechat-pairing-code", "POST", options);
}

export async function proxyAccountWechatUnbind(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyJsonRequest(request, "/me/wechat-unbind", "POST", options);
}

export async function proxyWorkspaceOverview(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(request, "/workspace/overview", "GET", options);
}

export async function proxyTeacherStudents(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(request, "/teacher/students", "GET", options);
}

const allowedTeacherStudentActions = new Set([
  "profile-correction",
  "extend",
  "promote-to-doctor",
]);

export async function proxyTeacherStudentAction(
  memberId: string,
  action: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  if (!allowedTeacherStudentActions.has(action)) {
    return Response.json({ detail: "Not Found" }, { status: 404 });
  }
  return proxyJsonRequest(
    request,
    `/teacher/students/${encodeURIComponent(memberId)}/${action}`,
    "POST",
    options,
  );
}

export async function proxyAdminMembers(
  request: Request,
  method: "GET" | "POST",
  options: ProxyOptions = {},
): Promise<Response> {
  return method === "GET"
    ? proxyCentralRequest(request, "/admin/members", "GET", options)
    : proxyJsonRequest(request, "/admin/members", "POST", options);
}

export async function proxyAdminMemberBatch(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyJsonRequest(request, "/admin/members/batch", "POST", options);
}

export async function proxyAdminAgentSettings(
  request: Request,
  method: "GET" | "PUT",
  options: ProxyOptions = {},
): Promise<Response> {
  return method === "GET"
    ? proxyCentralRequest(request, "/admin/agent-settings", "GET", options)
    : proxyJsonRequest(request, "/admin/agent-settings", "PUT", options);
}

export async function proxyAdminAgentSettingsHealth(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(request, "/admin/agent-settings/health", "GET", options);
}

export async function proxyAdminAgentSettingsTest(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyJsonRequest(request, "/admin/agent-settings/test", "POST", options);
}

export async function proxyAdminMemberDetail(
  memberId: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(
    request,
    `/admin/members/${encodeURIComponent(memberId)}`,
    "GET",
    options,
  );
}

const allowedMemberActions = new Set([
  "profile-correction",
  "supervisor",
  "role",
  "extend",
  "promote-to-doctor",
  "password-reset",
  "revoke-sessions",
  "wechat-pairing-code",
  "wechat-unbind",
  "disable",
  "reactivate",
  "archive",
]);

export async function proxyAdminMemberAction(
  memberId: string,
  action: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  if (!allowedMemberActions.has(action)) {
    return Response.json({ detail: "Not Found" }, { status: 404 });
  }
  return proxyJsonRequest(
    request,
    `/admin/members/${encodeURIComponent(memberId)}/${action}`,
    "POST",
    options,
  );
}

export async function proxyAdminTransferContainers(
  memberId: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyJsonRequest(
    request,
    `/admin/members/${encodeURIComponent(memberId)}/containers/transfer`,
    "POST",
    options,
  );
}

export async function proxyCreateContainer(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyJsonRequest(request, "/containers", "POST", options);
}

export async function proxyGetContainer(
  containerId: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(
    request,
    `/containers/${encodeURIComponent(containerId)}`,
    "GET",
    options,
  );
}

export async function proxyIssueSshCertificate(
  containerId: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyJsonRequest(
    request,
    `/containers/${encodeURIComponent(containerId)}/ssh-certificate`,
    "POST",
    options,
  );
}

export async function proxyPullImage(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyJsonRequest(request, "/images/pull", "POST", options);
}

export async function proxyImagePolicy(request: Request, options: ProxyOptions = {}): Promise<Response> {
  return proxyJsonRequest(request, "/images/policy", "POST", options);
}

export async function proxyStartContainer(
  containerId: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(
    request,
    `/containers/${encodeURIComponent(containerId)}/start`,
    "POST",
    options,
  );
}

export async function proxyStopContainer(
  containerId: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(
    request,
    `/containers/${encodeURIComponent(containerId)}/stop`,
    "POST",
    options,
  );
}

export async function proxyDeleteContainer(
  containerId: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(
    request,
    `/containers/${encodeURIComponent(containerId)}`,
    "DELETE",
    options,
  );
}

export async function proxyConfirmAction(
  confirmationId: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyJsonRequest(
    request,
    `/confirmations/${encodeURIComponent(confirmationId)}/confirm`,
    "POST",
    options,
  );
}

export async function proxyGovernanceRecommendations(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyJsonRequest(request, "/governance/recommendations", "POST", options);
}

export async function proxyListGovernanceQueue(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(request, "/governance/queue", "GET", options);
}

export async function proxyJoinGovernanceQueue(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyJsonRequest(request, "/governance/queue", "POST", options);
}

export async function proxyWithdrawGovernanceQueue(
  requestId: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(
    request,
    `/governance/queue/${encodeURIComponent(requestId)}`,
    "DELETE",
    options,
  );
}

export async function proxyApprovalDecision(
  approvalId: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyJsonRequest(
    request,
    `/governance/approvals/${encodeURIComponent(approvalId)}/decision`,
    "POST",
    options,
  );
}

export async function proxyExecuteApproval(
  approvalId: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(
    request,
    `/governance/approvals/${encodeURIComponent(approvalId)}/execute`,
    "POST",
    options,
  );
}

export async function proxyHealthSummary(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(request, "/operations/health", "GET", options);
}

export async function proxyHealthEvents(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(request, "/operations/health/events", "GET", options);
}

export async function proxyListExecutionReconciliations(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(request, "/operations/reconciliations", "GET", options);
}

export async function proxyInspectExecutionReconciliation(
  confirmationId: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(
    request,
    `/operations/reconciliations/${encodeURIComponent(confirmationId)}/inspect`,
    "POST",
    options,
  );
}

export async function proxyResolveExecutionReconciliation(
  confirmationId: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyJsonRequest(
    request,
    `/operations/reconciliations/${encodeURIComponent(confirmationId)}/resolve`,
    "POST",
    options,
  );
}

export async function proxyCreateDiagnostic(
  containerId: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(
    request,
    `/containers/${encodeURIComponent(containerId)}/diagnostics`,
    "POST",
    options,
  );
}

export async function proxyGetDiagnostic(
  reportId: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(
    request,
    `/diagnostics/${encodeURIComponent(reportId)}`,
    "GET",
    options,
  );
}

export async function proxyListBackups(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(request, "/operations/backups", "GET", options);
}

export async function proxyCreateBackup(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyJsonRequest(request, "/operations/backups", "POST", options);
}

export async function proxyBackupManifest(
  key: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(request, `/operations/backups/${encodeURIComponent(key)}/manifest`, "GET", options);
}

export async function proxyVerifyBackup(
  key: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(request, `/operations/backups/${encodeURIComponent(key)}/verify`, "POST", options);
}

export async function proxyDrillBackup(
  key: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(request, `/operations/backups/${encodeURIComponent(key)}/drill`, "POST", options);
}

export async function proxyRequestBackupRestore(
  key: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(request, `/operations/backups/${encodeURIComponent(key)}/restore-request`, "POST", options);
}

export async function proxyListPrintPrinters(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(request, "/print/printers", "GET", options);
}

export async function proxyListPrintJobs(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(request, "/print/jobs", "GET", options);
}

export async function proxyCreatePrintJob(
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyMultipartRequest(request, "/print/jobs", "POST", options);
}

export async function proxyUpdatePrintJob(
  jobId: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyJsonRequest(request, `/print/jobs/${encodeURIComponent(jobId)}`, "PATCH", options);
}

export async function proxyConfirmPrintJob(
  jobId: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyJsonRequest(request, `/print/jobs/${encodeURIComponent(jobId)}/confirm`, "POST", options);
}

export async function proxyCancelPrintJob(
  jobId: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(request, `/print/jobs/${encodeURIComponent(jobId)}/cancel`, "POST", options);
}

export async function proxyReprintJob(
  jobId: string,
  request: Request,
  options: ProxyOptions = {},
): Promise<Response> {
  return proxyCentralRequest(request, `/print/jobs/${encodeURIComponent(jobId)}/reprint`, "POST", options);
}

export async function proxyMultipartRequest(
  request: Request,
  centralPath: string,
  method: string,
  options: ProxyOptions = {},
): Promise<Response> {
  const contentLength = request.headers.get("content-length");
  if (contentLength === null) {
    return Response.json({ detail: "Content-Length is required" }, { status: 411 });
  }
  const parsedLength = Number(contentLength);
  if (!Number.isSafeInteger(parsedLength) || parsedLength < 0) {
    return Response.json({ detail: "Invalid Content-Length" }, { status: 400 });
  }
  if (parsedLength > printUploadMaximumBytes() + multipartOverheadMaximumBytes()) {
    return Response.json({ detail: "Print upload exceeds the size limit" }, { status: 413 });
  }
  const apiUrl = normalizeApiUrl(
    options.apiUrl ?? process.env.GSM_WEB_API_URL ?? process.env.GSM_API_URL ?? "http://api:8000",
  );
  const fetcher = options.fetcher ?? fetch;
  const headers = forwardedHeaders(request, options);
  const contentType = request.headers.get("content-type");
  if (contentType) {
    headers["Content-Type"] = contentType;
  }
  if (contentLength) {
    headers["Content-Length"] = contentLength;
  }
  const init: RequestInit & { duplex?: "half" } = {
    method,
    headers,
    body: request.body,
  };
  if (request.body) {
    init.duplex = "half";
  }
  const response = await fetcher(`${apiUrl}${centralPath}`, init);
  return boundedJsonResponse(response);
}

export async function proxyContainerManagement(
  containerId: string,
  operation: "details" | "logs" | "terminal" | "configuration" | "ssh-password" | "environment" | "applications",
  request: Request,
  sessionId?: string,
  options: ProxyOptions = {},
): Promise<Response> {
  if (sessionId && !/^[A-Za-z0-9_-]{32}$/.test(sessionId)) {
    return Response.json({ detail: "Invalid terminal session" }, { status: 400 });
  }
  const incoming = new URL(request.url);
  const query = new URLSearchParams();
  for (const key of operation === "logs" ? ["tail"] : operation === "terminal" ? ["cursor"] : []) {
    const value = incoming.searchParams.get(key);
    if (value !== null) query.set(key, value);
  }
  const path = `/containers/${encodeURIComponent(containerId)}/${operation}${sessionId ? `/${sessionId}` : ""}${query.size ? `?${query}` : ""}`;
  try {
    const response = ["POST", "PUT"].includes(request.method)
      ? await proxyJsonRequest(request, path, request.method, options)
      : await proxyCentralRequest(request, path, request.method, options);
    response.headers.set("Cache-Control", "no-store");
    return response;
  } catch {
    return Response.json({ detail: "无法连接管理服务，请稍后重试。" }, {
      status: 502, headers: { "Cache-Control": "no-store" },
    });
  }
}

export async function proxyJsonRequest(
  request: Request,
  centralPath: string,
  method: string,
  options: ProxyOptions,
): Promise<Response> {
  if (options.developmentToken && process.env.NODE_ENV === "production") {
    throw new Error("development token is unavailable in production");
  }
  const contentLengthError = boundedRequestLength(
    request,
    jsonRequestMaximumBytes(),
    "JSON request",
  );
  if (contentLengthError) {
    return contentLengthError;
  }
  const bodyResult = await readBoundedJson(request, jsonRequestMaximumBytes());
  if (!bodyResult.ok) {
    return bodyResult.response;
  }
  return proxyCentralRequest(request, centralPath, method, options, bodyResult.value);
}

async function readBoundedJson(
  request: Request,
  maximumBytes: number,
): Promise<
  | { ok: true; value: unknown }
  | { ok: false; response: Response }
> {
  const reader = request.body?.getReader();
  if (!reader) {
    return {
      ok: false,
      response: Response.json({ detail: "JSON request body is required" }, { status: 400 }),
    };
  }

  const chunks: Uint8Array[] = [];
  let totalBytes = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      totalBytes += value.byteLength;
      if (totalBytes > maximumBytes) {
        await reader.cancel();
        return {
          ok: false,
          response: Response.json(
            { detail: "JSON request exceeds the size limit" },
            { status: 413 },
          ),
        };
      }
      chunks.push(value);
    }
  } catch {
    return {
      ok: false,
      response: Response.json({ detail: "Unable to read JSON request" }, { status: 400 }),
    };
  }

  const bytes = new Uint8Array(totalBytes);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return { ok: true, value: JSON.parse(new TextDecoder().decode(bytes)) };
  } catch {
    return {
      ok: false,
      response: Response.json({ detail: "Invalid JSON request" }, { status: 400 }),
    };
  }
}

export async function proxyCentralRequest(
  request: Request,
  centralPath: string,
  method: string,
  options: ProxyOptions,
  body?: unknown,
): Promise<Response> {
  const apiUrl = normalizeApiUrl(
    options.apiUrl ?? process.env.GSM_WEB_API_URL ?? process.env.GSM_API_URL ?? "http://api:8000",
  );
  const fetcher = options.fetcher ?? fetch;
  const headers = forwardedHeaders(request, options);
  const init: RequestInit = { method, headers };

  if (body !== undefined) {
    headers["Content-Type"] = acceptedJsonContentType(request) ?? "application/json";
    init.body = JSON.stringify(body);
  }

  const response = await fetcher(`${apiUrl}${centralPath}`, init);
  return boundedJsonResponse(response);
}

async function boundedJsonResponse(response: Response): Promise<Response> {
  const headers = new Headers({ "Content-Type": "application/json" });
  for (const cookie of getSetCookieHeaders(response.headers)) {
    headers.append("Set-Cookie", cookie);
  }

  try {
    const payload = await response.json();
    return Response.json(payload, { status: response.status, headers });
  } catch {
    return Response.json(
      { detail: "Central API returned an invalid response" },
      { status: 502, headers },
    );
  }
}

function forwardedHeaders(
  request: Request,
  options: ProxyOptions,
): Record<string, string> {
  if (options.developmentToken && process.env.NODE_ENV === "production") {
    throw new Error("development token is unavailable in production");
  }

  const headers: Record<string, string> = {};
  const forwarded = [
    ["cookie", "Cookie"],
    ["origin", "Origin"],
    ["x-csrf-token", "X-CSRF-Token"],
    ["user-agent", "User-Agent"],
  ] as const;

  for (const [source, target] of forwarded) {
    const value = request.headers.get(source);
    if (value) {
      headers[target] = value;
    }
  }

  if (options.developmentToken) {
    headers.Authorization = `Bearer ${options.developmentToken}`;
  }
  const proxySecret = options.trustedProxySecret ?? process.env.GSM_TRUSTED_PROXY_SECRET;
  if (proxySecret) {
    const source = clientSource(request, options);
    const timestamp = Math.floor(Date.now() / 1000).toString();
    const signature = createHmac("sha256", proxySecret)
      .update(`${timestamp}.${source}`, "utf8")
      .digest("hex");
    headers["X-GSM-Client-Source"] = source;
    headers["X-GSM-Client-Timestamp"] = timestamp;
    headers["X-GSM-Client-Signature"] = `sha256=${signature}`;
  }
  return headers;
}

function clientSource(request: Request, options: ProxyOptions): string {
  const expected = options.edgeProxySecret ?? process.env.GSM_EDGE_PROXY_SECRET;
  const received = request.headers.get("x-gsm-edge-secret") ?? "";
  if (!expected || !received || !safeSecretEqual(expected, received)) {
    return "untrusted-direct";
  }
  const source = request.headers.get("x-gsm-edge-client-source") ?? "unknown";
  return source.trim().slice(0, 255) || "unknown";
}

function safeSecretEqual(expected: string, received: string): boolean {
  const expectedBytes = Buffer.from(expected, "utf8");
  const receivedBytes = Buffer.from(received, "utf8");
  return expectedBytes.length === receivedBytes.length
    && timingSafeEqual(expectedBytes, receivedBytes);
}

function printUploadMaximumBytes(): number {
  const configured = process.env.GSM_PRINT_MAX_FILE_BYTES ?? "104857600";
  const parsed = Number(configured);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : 104857600;
}

function multipartOverheadMaximumBytes(): number {
  const configured = process.env.GSM_PRINT_MULTIPART_OVERHEAD_BYTES ?? "1048576";
  const parsed = Number(configured);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : 1048576;
}

function jsonRequestMaximumBytes(): number {
  const configured = process.env.GSM_WEB_JSON_MAX_BYTES ?? "1048576";
  const parsed = Number(configured);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : 1048576;
}

function boundedRequestLength(
  request: Request,
  maximumBytes: number,
  label: string,
): Response | undefined {
  const value = request.headers.get("content-length");
  if (value === null) {
    if (process.env.NODE_ENV !== "production") {
      return undefined;
    }
    return Response.json({ detail: `${label} requires Content-Length` }, { status: 411 });
  }
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 0) {
    return Response.json({ detail: "Invalid Content-Length" }, { status: 400 });
  }
  if (parsed > maximumBytes) {
    return Response.json({ detail: `${label} exceeds the size limit` }, { status: 413 });
  }
  return undefined;
}

function acceptedJsonContentType(request: Request): string | undefined {
  const value = request.headers.get("content-type")?.trim();
  if (!value) {
    return undefined;
  }
  const mediaType = value.split(";", 1)[0].trim().toLowerCase();
  return mediaType === "application/json" || mediaType.endsWith("+json")
    ? value
    : undefined;
}

function getSetCookieHeaders(headers?: Headers): string[] {
  if (!headers) {
    return [];
  }
  if (typeof headers.getSetCookie === "function") {
    return headers.getSetCookie();
  }
  const combined = headers.get("set-cookie");
  return combined
    ? combined.split(/,\s*(?=[!#$%&'*+\-.^_`|~0-9A-Za-z]+=)/)
    : [];
}

function normalizeApiUrl(apiUrl: string): string {
  const trimmed = apiUrl.replace(/\/+$/, "");
  return trimmed.endsWith("/api") ? trimmed : `${trimmed}/api`;
}

export async function proxyBinaryDownload(request: Request, path: string, filename: string, contentType: string): Promise<Response> {
  const apiUrl = normalizeApiUrl(process.env.GSM_WEB_API_URL ?? process.env.GSM_API_URL ?? "http://api:8000");
  const response = await fetch(`${apiUrl}${path}`, { headers: forwardedHeaders(request, {}), cache: "no-store" });
  if (!response.ok) return boundedJsonResponse(response);
  return new Response(response.body, { status: response.status, headers: {
    "Content-Type": contentType, "Content-Disposition": `attachment; filename*=UTF-8''${encodeURIComponent(filename)}`,
    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
  } });
}
