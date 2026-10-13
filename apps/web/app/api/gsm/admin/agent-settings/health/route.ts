import { proxyAdminAgentSettingsHealth } from "../../../../../../lib/apiProxy";

export async function GET(request: Request) {
  return proxyAdminAgentSettingsHealth(request);
}
