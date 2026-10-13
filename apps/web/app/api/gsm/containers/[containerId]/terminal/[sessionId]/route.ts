import { proxyContainerManagement } from "../../../../../../../lib/apiProxy";

type Context = { params: Promise<{ containerId: string; sessionId: string }> };

async function handle(request: Request, context: Context) {
  const { containerId, sessionId } = await context.params;
  return proxyContainerManagement(containerId, "terminal", request, sessionId);
}

export const GET = handle;
export const POST = handle;
export const DELETE = handle;
