import { proxyReprintJob } from "../../../../../../../lib/apiProxy";

export async function POST(request: Request, context: { params: Promise<{ jobId: string }> }) {
  const { jobId } = await context.params;
  return proxyReprintJob(jobId, request);
}
