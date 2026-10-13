import { proxyTeacherSupervisors } from "../../../../../lib/apiProxy";

export async function GET(request: Request) {
  return proxyTeacherSupervisors(request);
}
