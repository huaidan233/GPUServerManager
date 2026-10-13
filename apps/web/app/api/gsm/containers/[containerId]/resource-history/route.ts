import { proxyResourceHistory } from "../../../../../../lib/resourceMonitoringProxy";

export async function GET(request: Request, { params }: { params: Promise<{ containerId: string }> }) {
  return proxyResourceHistory("containers", (await params).containerId, request);
}
