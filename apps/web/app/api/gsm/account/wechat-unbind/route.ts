import { proxyAccountWechatUnbind } from "../../../../../lib/apiProxy";

export async function POST(request: Request) {
  return proxyAccountWechatUnbind(request);
}
