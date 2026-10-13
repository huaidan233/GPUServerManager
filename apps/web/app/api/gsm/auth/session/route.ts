import { proxyAuthSession } from "../../../../../lib/apiProxy";

export async function GET(request: Request) {
  return proxyAuthSession(request);
}
