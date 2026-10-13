import { proxyAdminTransferContainers } from "../../../../../../../../lib/apiProxy";

type RouteContext = {
  params: Promise<{ memberId: string }>;
};

export async function POST(request: Request, context: RouteContext) {
  const { memberId } = await context.params;
  return proxyAdminTransferContainers(memberId, request);
}
