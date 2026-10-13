"use client";

import { RefreshCw } from "lucide-react";
import { useRouter } from "next/navigation";


export function ServiceError() {
  const router = useRouter();
  return (
    <main className="auth-shell">
      <p className="auth-message auth-error" role="alert">
        服务暂时不可用，请稍后重试。
      </p>
      <button className="secondary-button" type="button" onClick={() => router.refresh()}>
        <RefreshCw size={16} aria-hidden="true" />
        重试
      </button>
    </main>
  );
}
