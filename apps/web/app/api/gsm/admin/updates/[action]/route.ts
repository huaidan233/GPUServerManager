import { proxyCentralRequest, proxyJsonRequest } from "../../../../../../lib/apiProxy";

type Context = { params: Promise<{ action: string }> };

export async function GET(request: Request, { params }: Context) {
  const { action } = await params;
  if (action !== "status") return Response.json({ detail: "Not Found" }, { status: 404 });
  const response = await proxyCentralRequest(request, "/admin/updates/status", "GET", {});
  response.headers.set("Cache-Control", "no-store");
  return response;
}

export async function POST(request: Request, { params }: Context) {
  const { action } = await params;
  if (!["prepare", "apply", "rollback"].includes(action)) return Response.json({ detail: "Not Found" }, { status: 404 });
  const response = await proxyJsonRequest(request, `/admin/updates/${action}`, "POST", {});
  response.headers.set("Cache-Control", "no-store");
  return response;
}
