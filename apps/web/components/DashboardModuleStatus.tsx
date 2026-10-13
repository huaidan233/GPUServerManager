"use client";

import type { DashboardModuleError } from "../lib/mockData";

export function DashboardModuleStatus({ errors }: { errors: DashboardModuleError[] }) {
  if (errors.length === 0) return null;

  return (
    <section className="panel" aria-label="数据加载提示" role="status">
      <strong>部分数据暂不可用</strong>
      <p className="form-help">核心功能仍可使用；刷新页面可以重新尝试加载以下模块。</p>
      <ul>
        {errors.map((error) => <li key={error.module}>{error.module}：{error.message}</li>)}
      </ul>
    </section>
  );
}
