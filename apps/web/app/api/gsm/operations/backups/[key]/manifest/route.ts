import { proxyBackupManifest } from "../../../../../../../lib/apiProxy";

type RouteContext = { params: Promise<{ key: string }> };

export async function GET(request: Request, context: RouteContext) {
  const { key } = await context.params;
  return proxyBackupManifest(key, request);
}
