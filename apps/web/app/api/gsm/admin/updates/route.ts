import { proxyCentralRequest } from "../../../../../lib/apiProxy";

export async function GET(request: Request) {
  const response = await proxyCentralRequest(request, "/admin/updates", "GET", {});
  response.headers.set("Cache-Control", "no-store");
  return response;
}
