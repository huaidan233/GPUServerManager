import { proxyCentralRequest, proxyJsonRequest } from "../../../../../lib/apiProxy";

async function handle(request: Request, context: { params: Promise<{ path?: string[] }> }) {
  const path = (await context.params).path ?? [];
  const route = path.join("/");
  const allowed = (request.method === "GET" && ["", "settings"].includes(route))
    || (["PUT", "DELETE"].includes(request.method) && route === "settings") || (request.method === "POST" && route === "test");
  if (!allowed) return Response.json({ detail: "Not Found" }, { status: 404 });
  const target = `/notifications${route ? `/${route}` : ""}`;
  const response = ["POST", "PUT"].includes(request.method)
    ? await proxyJsonRequest(request, target, request.method, {}) : await proxyCentralRequest(request, target, request.method, {});
  response.headers.set("Cache-Control", "no-store"); return response;
}
export const GET = handle;
export const PUT = handle;
export const DELETE = handle;
export const POST = handle;
