import { proxyConfirmAction } from "../../../../../../lib/apiProxy";

type RouteContext = {
  params: Promise<{ confirmationId: string }>;
};

export async function POST(request: Request, context: RouteContext) {
  const { confirmationId } = await context.params;
  return proxyConfirmAction(confirmationId, request);
}
