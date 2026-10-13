import { proxyGetDiagnostic } from "../../../../../lib/apiProxy";

type RouteContext = { params: Promise<{ reportId: string }> };

export async function GET(request: Request, context: RouteContext) {
  const { reportId } = await context.params;
  return proxyGetDiagnostic(reportId, request);
}
