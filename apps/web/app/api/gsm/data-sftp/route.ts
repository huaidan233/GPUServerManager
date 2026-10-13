import { proxyCentralRequest, proxyJsonRequest } from "../../../../lib/apiProxy";
export async function GET(request: Request) {
  const response = await proxyCentralRequest(request, "/data-sftp", "GET", {}); response.headers.set("Cache-Control", "no-store"); return response;
}
export async function PUT(request: Request) {
  const response = await proxyJsonRequest(request, "/data-sftp", "PUT", {}); response.headers.set("Cache-Control", "no-store"); return response;
}
