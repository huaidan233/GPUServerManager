import { proxyContainerManagement } from "../../../../../../lib/apiProxy";

export async function GET(request: Request, context: { params: Promise<{ containerId: string }> }) {
  const { containerId } = await context.params;
  return proxyContainerManagement(containerId, "ssh-password", request);
}

export async function POST(request: Request, context: { params: Promise<{ containerId: string }> }) {
  const { containerId } = await context.params;
  return proxyContainerManagement(containerId, "ssh-password", request);
}
