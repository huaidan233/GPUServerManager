import { proxyGovernanceRecommendations } from "../../../../../lib/apiProxy";

export async function POST(request: Request) {
  return proxyGovernanceRecommendations(request);
}
