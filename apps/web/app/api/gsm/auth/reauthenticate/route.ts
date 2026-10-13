import { proxyAuthReauthenticate } from "../../../../../lib/apiProxy";

export async function POST(request: Request) {
  return proxyAuthReauthenticate(request);
}
