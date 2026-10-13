import { proxyPullImage } from "../../../../../lib/apiProxy";

export async function POST(request: Request) {
  return proxyPullImage(request);
}
