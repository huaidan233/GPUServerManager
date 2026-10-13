import { proxyAccountWechatBinding } from "../../../../../lib/apiProxy";

export async function GET(request: Request) {
  return proxyAccountWechatBinding(request);
}
