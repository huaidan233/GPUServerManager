import { proxyCentralRequest, proxyJsonRequest, proxyBinaryDownload } from "../../../../../lib/apiProxy";
async function handle(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const path = (await context.params).path;
  const route = path.join("/");
  if (request.method === "GET" && route === "installer") return proxyBinaryDownload(request, "/print-admin/installer", "gsm-print-agent-windows.zip", "application/zip");
  const allowed = (request.method === "GET" && route === "agents") || (request.method === "POST" && /^(agents|agents\/[A-Za-z0-9._-]+\/(rotate|revoke))$/.test(route));
  if (!allowed) return Response.json({ detail: "Not Found" }, { status: 404 });
  const target = `/print-admin/${path.map(encodeURIComponent).join("/")}`;
  const response = request.method === "GET" ? await proxyCentralRequest(request, target, "GET", {}) : await proxyJsonRequest(request, target, "POST", {});
  response.headers.set("Cache-Control", "no-store"); return response;
}
export const GET = handle;
export const POST = handle;
