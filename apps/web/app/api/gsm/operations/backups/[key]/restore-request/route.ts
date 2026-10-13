import { proxyRequestBackupRestore } from "../../../../../../../lib/apiProxy";

type RouteContext = { params: Promise<{ key: string }> };

export async function POST(request: Request, context: RouteContext) {
  const { key } = await context.params;
  return proxyRequestBackupRestore(key, request);
}
