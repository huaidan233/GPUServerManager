import { proxyAccountWechatPairingCode } from "../../../../../lib/apiProxy";

export async function POST(request: Request) {
  return proxyAccountWechatPairingCode(request);
}
