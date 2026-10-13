import { proxyAdminMembers } from "../../../../../lib/apiProxy";

export async function GET(request: Request) {
  return proxyAdminMembers(request, "GET");
}

export async function POST(request: Request) {
  return proxyAdminMembers(request, "POST");
}
