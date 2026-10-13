import { proxyCreateDiagnostic } from "../../../../../../lib/apiProxy";

type RouteContext = { params: Promise<{ containerId: string }> };

export async function POST(request: Request, context: RouteContext) {
  const { containerId } = await context.params;
  return proxyCreateDiagnostic(containerId, request);
}
