import { proxyTeacherStudents, proxyTeacherStudentInvite } from "../../../../../lib/apiProxy";

export async function GET(request: Request) {
  return proxyTeacherStudents(request);
}

export async function POST(request: Request) {
  return proxyTeacherStudentInvite(request);
}
