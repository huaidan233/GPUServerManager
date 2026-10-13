import { proxyAuthLogout } from "../../../../../lib/apiProxy";

export async function POST(request: Request) {
  return proxyAuthLogout(request);
}
