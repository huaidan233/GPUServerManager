import { proxyResourceHistory } from "../../../../../../lib/resourceMonitoringProxy";

export async function GET(request: Request, { params }: { params: Promise<{ serverId: string }> }) {
  return proxyResourceHistory("servers", (await params).serverId, request);
}
