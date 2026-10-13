import { proxyAdminMemberBatch } from "../../../../../../lib/apiProxy";

export async function POST(request: Request) {
  return proxyAdminMemberBatch(request);
}
