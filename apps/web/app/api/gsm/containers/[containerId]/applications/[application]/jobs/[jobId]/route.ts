import { proxyApplicationJob } from "../../../../../../../../../lib/pythonEnvironmentProxy";
export async function GET(request: Request, context: { params: Promise<{ containerId: string; application: string; jobId: string }> }) {
  const { containerId, application, jobId } = await context.params;
  return proxyApplicationJob(containerId, application, jobId, request);
}
