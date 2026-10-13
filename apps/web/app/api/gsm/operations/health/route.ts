import { proxyHealthSummary } from "../../../../../lib/apiProxy";

export async function GET(request: Request) {
  return proxyHealthSummary(request);
}
