import { proxyWithdrawGovernanceQueue } from "../../../../../../lib/apiProxy";

type RouteContext = {
  params: Promise<{ requestId: string }>;
};

export async function DELETE(request: Request, context: RouteContext) {
  const { requestId } = await context.params;
  return proxyWithdrawGovernanceQueue(requestId, request);
}
