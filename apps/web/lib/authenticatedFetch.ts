export function authenticatedFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
  const csrfToken = readCookie("gsm_csrf");

  if (method === "GET" || !csrfToken || !isSameOrigin(input)) {
    return fetch(input, init);
  }

  const headers = new Headers(input instanceof Request ? input.headers : undefined);
  if (init?.headers) {
    new Headers(init.headers).forEach((value, key) => headers.set(key, value));
  }
  headers.set("X-CSRF-Token", csrfToken);
  return fetch(input, { ...init, headers });
}

function isSameOrigin(input: RequestInfo | URL): boolean {
  if (typeof window === "undefined") {
    return false;
  }
  const value = input instanceof Request ? input.url : input.toString();
  return new URL(value, window.location.href).origin === window.location.origin;
}

function readCookie(name: string): string | undefined {
  if (typeof document === "undefined") {
    return undefined;
  }
  const prefix = `${name}=`;
  const value = document.cookie
    .split(";")
    .map((item) => item.trim())
    .find((item) => item.startsWith(prefix))
    ?.slice(prefix.length);
  if (!value) {
    return undefined;
  }
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}
