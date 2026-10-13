import { proxyIssueSshCertificate } from "../../../../../../lib/apiProxy";

export async function POST(
  request: Request,
  context: { params: Promise<{ containerId: string }> },
) {
  const { containerId } = await context.params;
  return proxyIssueSshCertificate(containerId, request);
}
