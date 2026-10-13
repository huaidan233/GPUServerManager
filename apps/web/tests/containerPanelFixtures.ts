/** Read-only panels load independently of lifecycle/terminal mutations. */
export function panelReadResponse(input: RequestInfo | URL): Response | undefined {
  const url = String(input);
  if (url.includes("/resource-history?")) return Response.json({
    scope: "container", start: "2026-09-30T00:00:00Z", end: "2026-09-30T01:00:00Z",
    latest: null, points: [], disks: [], stale: true,
  });
  if (url.endsWith("/python-environments")) return Response.json({ status: "ok", environments: [], truncated: false });
}
