import { proxyExecuteApproval } from "../../../../../../lib/apiProxy";

type RouteContext = {
  params: Promise<{ approvalId: string }>;
};

export async function POST(request: Request, context: RouteContext) {
  const { approvalId } = await context.params;
  return proxyExecuteApproval(approvalId, request);
}
