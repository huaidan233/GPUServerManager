import { proxyInitialMfa } from "../../../../../lib/apiProxy";

export async function POST(request: Request) {
  return proxyInitialMfa(request);
}
