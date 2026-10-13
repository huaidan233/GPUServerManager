import { proxyWorkspaceOverview } from "../../../../../lib/apiProxy";

export async function GET(request: Request) {
  return proxyWorkspaceOverview(request);
}
