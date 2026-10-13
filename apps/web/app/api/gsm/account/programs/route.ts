import { proxyAccountPrograms } from "../../../../../lib/apiProxy";

export async function GET(request: Request) {
  return proxyAccountPrograms(request);
}
