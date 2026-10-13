import { proxyHealthEvents } from "../../../../../../lib/apiProxy";

export async function GET(request: Request) {
  return proxyHealthEvents(request);
}
