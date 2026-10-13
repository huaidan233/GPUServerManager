import { proxyAuthPasswordChange } from "../../../../../../lib/apiProxy";

export async function POST(request: Request) {
  return proxyAuthPasswordChange(request);
}
