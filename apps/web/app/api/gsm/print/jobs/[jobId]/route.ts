import { proxyUpdatePrintJob } from "../../../../../../lib/apiProxy";

export async function PATCH(request: Request, context: { params: Promise<{ jobId: string }> }) {
  const { jobId } = await context.params;
  return proxyUpdatePrintJob(jobId, request);
}
