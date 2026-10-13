import { proxyCreateBackup, proxyListBackups } from "../../../../../lib/apiProxy";

export async function GET(request: Request) {
  return proxyListBackups(request);
}

export async function POST(request: Request) {
  return proxyCreateBackup(request);
}
