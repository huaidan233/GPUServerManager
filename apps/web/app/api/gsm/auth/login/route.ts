import { proxyAuthLogin } from "../../../../../lib/apiProxy";

export async function POST(request: Request) {
  return proxyAuthLogin(request);
}
