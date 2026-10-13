import { proxyListExecutionReconciliations } from "../../../../../lib/apiProxy";

export async function GET(request: Request) {
  return proxyListExecutionReconciliations(request);
}
