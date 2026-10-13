import { proxyAgentSettingsReauthenticate } from "../../../../../../lib/apiProxy";

export async function POST(request: Request) {
  return proxyAgentSettingsReauthenticate(request);
}
