import { proxyAuthRevokeOtherSessions } from "../../../../../../lib/apiProxy";

export async function POST(request: Request) {
  return proxyAuthRevokeOtherSessions(request);
}
