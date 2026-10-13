import { proxyAdminAgentSettings } from "../../../../../lib/apiProxy";

export async function GET(request: Request) {
  return proxyAdminAgentSettings(request, "GET");
}

export async function PUT(request: Request) {
  return proxyAdminAgentSettings(request, "PUT");
}
