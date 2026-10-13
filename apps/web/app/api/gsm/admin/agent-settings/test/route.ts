import { proxyAdminAgentSettingsTest } from "../../../../../../lib/apiProxy";

export async function POST(request: Request) {
  return proxyAdminAgentSettingsTest(request);
}
