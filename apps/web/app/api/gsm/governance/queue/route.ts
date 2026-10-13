import {
  proxyJoinGovernanceQueue,
  proxyListGovernanceQueue,
} from "../../../../../lib/apiProxy";

export async function GET(request: Request) {
  return proxyListGovernanceQueue(request);
}

export async function POST(request: Request) {
  return proxyJoinGovernanceQueue(request);
}
