import { proxyListPrintPrinters } from "../../../../../lib/apiProxy";

export async function GET(request: Request) {
  return proxyListPrintPrinters(request);
}
