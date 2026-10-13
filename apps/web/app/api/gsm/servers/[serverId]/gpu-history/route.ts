import { proxyGpuHistory } from "../../../../../../lib/apiProxy";

export async function GET(request: Request, { params }: { params: Promise<{ serverId: string }> }) {
  return proxyGpuHistory((await params).serverId, request);
}
