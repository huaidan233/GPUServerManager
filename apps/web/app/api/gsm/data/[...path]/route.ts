import { proxyCentralRequest, proxyJsonRequest } from "../../../../../lib/apiProxy";
async function handle(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const route = (await context.params).path.join("/");
  if (!((request.method === "GET" && route === "status") || (request.method === "POST" && ["list", "mkdir", "rename", "read", "begin", "chunk", "finish", "cancel"].includes(route)))) return Response.json({ detail: "Not Found" }, { status: 404 });
  const response = request.method === "GET" ? await proxyCentralRequest(request, `/data/${route}`, "GET", {}) : await proxyJsonRequest(request, `/data/${route}`, "POST", {});
  response.headers.set("Cache-Control", "no-store"); return response;
}
export const GET = handle;
export const POST = handle;
