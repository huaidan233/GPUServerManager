import { proxyAuthActivate } from "../../../../../lib/apiProxy";

export async function POST(request: Request) {
  return proxyAuthActivate(request);
}
