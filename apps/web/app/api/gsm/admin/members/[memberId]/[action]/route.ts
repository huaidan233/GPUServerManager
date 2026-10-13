import { proxyAdminMemberAction } from "../../../../../../../lib/apiProxy";

type RouteContext = {
  params: Promise<{ memberId: string; action: string }>;
};

export async function POST(request: Request, context: RouteContext) {
  const { memberId, action } = await context.params;
  return proxyAdminMemberAction(memberId, action, request);
}
