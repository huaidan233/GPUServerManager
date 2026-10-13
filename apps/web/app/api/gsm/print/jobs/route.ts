import { proxyCreatePrintJob, proxyListPrintJobs } from "../../../../../lib/apiProxy";

export async function GET(request: Request) {
  return proxyListPrintJobs(request);
}

export async function POST(request: Request) {
  return proxyCreatePrintJob(request);
}
