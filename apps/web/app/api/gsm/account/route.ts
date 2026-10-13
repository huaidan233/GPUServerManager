import { proxyAccount } from "../../../../lib/apiProxy";

export async function GET(request: Request) {
  return proxyAccount(request);
}
