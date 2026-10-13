import { proxyCentralRequest, proxyJsonRequest } from "../../../../../lib/apiProxy";

async function handle(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  const route = path.join("/");
  const method = request.method;
  const allowed = (method === "GET" && /^(me|requests|settings)$/.test(route))
    || (method === "POST" && /^(requests|requests\/[\w-]+\/decision)$/.test(route))
    || (method === "PUT" && /^(settings|members\/[\w:-]+)$/.test(route));
  if (!allowed) return Response.json({ detail: "Not Found" }, { status: 404 });
  const central = `/quota/${path.map(encodeURIComponent).join("/")}`;
  const result = method === "GET" ? await proxyCentralRequest(request, central, method, {}) : await proxyJsonRequest(request, central, method, {});
  result.headers.set("Cache-Control", "no-store");
  return result;
}
export const GET = handle;
export const POST = handle;
export const PUT = handle;
