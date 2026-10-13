import { proxyCentralRequest } from "./apiProxy";

export async function proxyResourceHistory(scope: "servers" | "containers", id: string, request: Request,
  options: Parameters<typeof proxyCentralRequest>[3] = {}): Promise<Response> {
  const minutes = new URL(request.url).searchParams.get("minutes") ?? "60";
  if (!["60", "360", "1440"].includes(minutes)) return Response.json({ detail: "无效的时间范围" }, { status: 422 });
  try {
    const response = await proxyCentralRequest(request, `/${scope}/${encodeURIComponent(id)}/resource-history?minutes=${minutes}`, "GET", options);
    response.headers.set("Cache-Control", "no-store");
    return response;
  } catch {
    return Response.json({ detail: "暂时无法读取资源历史，请稍后重试。" }, { status: 502, headers: { "Cache-Control": "no-store" } });
  }
}
