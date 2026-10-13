import { proxyContainerManagement } from "../../../../../../lib/apiProxy";
export async function GET(request: Request, context: { params: Promise<{ containerId: string }> }) {
  const { containerId } = await context.params;
  return proxyContainerManagement(containerId, "environment", request);
}
export async function PUT(request: Request, context: { params: Promise<{ containerId: string }> }) {
  const { containerId } = await context.params;
  return proxyContainerManagement(containerId, "environment", request);
}
