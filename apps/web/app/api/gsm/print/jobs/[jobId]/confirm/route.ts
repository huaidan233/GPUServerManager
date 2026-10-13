import { proxyConfirmPrintJob } from "../../../../../../../lib/apiProxy";

export async function POST(request: Request, context: { params: Promise<{ jobId: string }> }) {
  const { jobId } = await context.params;
  return proxyConfirmPrintJob(jobId, request);
}
