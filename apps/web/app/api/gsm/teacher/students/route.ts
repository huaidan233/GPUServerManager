import { proxyTeacherStudents } from "../../../../../lib/apiProxy";

export async function GET(request: Request) {
  return proxyTeacherStudents(request);
}
