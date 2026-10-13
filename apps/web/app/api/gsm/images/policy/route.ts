import { proxyImagePolicy } from "../../../../../lib/apiProxy";
export async function POST(request: Request) { return proxyImagePolicy(request); }
