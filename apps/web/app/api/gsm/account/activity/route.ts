import { proxyAccountActivity } from "../../../../../lib/apiProxy";

export async function GET(request: Request) {
  return proxyAccountActivity(request);
}
