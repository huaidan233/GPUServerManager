import { proxyAccountSessions } from "../../../../../lib/apiProxy";

export async function GET(request: Request) {
  return proxyAccountSessions(request);
}
