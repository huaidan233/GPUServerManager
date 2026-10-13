import { proxyPythonEnvironments } from "../../../../../../../lib/pythonEnvironmentProxy";
export async function GET(request: Request, context: { params: Promise<{ containerId: string }> }) {
  const { containerId } = await context.params;
  return proxyPythonEnvironments(containerId, request, true);
}
