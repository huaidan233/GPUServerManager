import { proxyCreateContainer } from "../../../../lib/apiProxy";

export async function POST(request: Request) {
  return proxyCreateContainer(request);
}
