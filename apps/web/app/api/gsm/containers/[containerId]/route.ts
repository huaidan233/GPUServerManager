import { proxyDeleteContainer, proxyGetContainer } from "../../../../../lib/apiProxy";

type RouteContext = {
  params: Promise<{ containerId: string }>;
};

export async function DELETE(request: Request, context: RouteContext) {
  const { containerId } = await context.params;
  return proxyDeleteContainer(containerId, request);
}

export async function GET(request: Request, context: RouteContext) {
  const { containerId } = await context.params;
  return proxyGetContainer(containerId, request);
}
