import { proxyAdminMemberDetail } from "../../../../../../lib/apiProxy";

type RouteContext = {
  params: Promise<{ memberId: string }>;
};

export async function GET(request: Request, context: RouteContext) {
  const { memberId } = await context.params;
  return proxyAdminMemberDetail(memberId, request);
}
