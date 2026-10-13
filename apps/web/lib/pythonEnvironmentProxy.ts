import { proxyCentralRequest } from "./apiProxy";

type Options = Parameters<typeof proxyCentralRequest>[3];

async function read(request: Request, path: string, options: Options) {
  try {
    const response = await proxyCentralRequest(request, path, "GET", options);
    response.headers.set("Cache-Control", "no-store");
    return response;
  } catch {
    return Response.json({ detail: "无法读取环境状态，请稍后重试。" }, { status: 502, headers: { "Cache-Control": "no-store" } });
  }
}

export async function proxyPythonEnvironments(containerId: string, request: Request, check = false, options: Options = {}) {
  let path = `/containers/${encodeURIComponent(containerId)}/python-environments`;
  if (check) {
    const python = new URL(request.url).searchParams.get("python_path") ?? "";
    if (!/^\/[A-Za-z0-9_./+-]{1,240}$/.test(python) || python.split("/").some(part => part === "." || part === "..")) {
      return Response.json({ detail: "Python 路径无效。" }, { status: 422 });
    }
    path += `/check?${new URLSearchParams({ python_path: python })}`;
  }
  return read(request, path, options);
}

export async function proxyApplicationJob(containerId: string, application: string, jobId: string, request: Request, options: Options = {}) {
  if (!["jupyter", "tensorboard"].includes(application) || !/^[a-f0-9]{32}$/.test(jobId)) {
    return Response.json({ detail: "应用启动任务不存在。" }, { status: 404 });
  }
  return read(request, `/containers/${encodeURIComponent(containerId)}/applications/${application}/jobs/${jobId}`, options);
}
