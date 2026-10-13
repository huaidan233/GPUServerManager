from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


MAX_LOG_BYTES = 65536
MAX_LOG_LINES = 400


@dataclass(frozen=True)
class DiagnosticRule:
    code: str
    pattern: re.Pattern[str]
    cause: str
    suggestions: tuple[str, ...]
    confidence: float


RULES = (
    DiagnosticRule(
        "cuda_oom",
        re.compile(r"cuda.*out of memory|cublas.*alloc", re.I),
        "GPU 显存不足",
        ("降低 batch size", "选择显存余量更高的 GPU"),
        0.98,
    ),
    DiagnosticRule(
        "disk_full",
        re.compile(r"no space left on device|errno 28", re.I),
        "磁盘空间或 inode 已耗尽",
        ("清理临时文件", "检查目标挂载空间"),
        0.98,
    ),
    DiagnosticRule(
        "port_conflict",
        re.compile(r"address already in use|errno 98", re.I),
        "监听端口已被占用",
        ("改用未占用端口", "停止重复启动的服务进程"),
        0.95,
    ),
    DiagnosticRule(
        "mount_permission",
        re.compile(
            r"permission denied.*(?:/data|/datasets|/models|/workspace|/scratch)",
            re.I,
        ),
        "挂载路径权限不足",
        ("检查挂载模板读写模式", "请管理员核对宿主机目录权限"),
        0.94,
    ),
    DiagnosticRule(
        "driver_mismatch",
        re.compile(r"failed to initialize nvml|driver/library version mismatch", re.I),
        "NVIDIA 驱动与用户态库不匹配",
        ("通知管理员检查驱动状态", "不要在容器内覆盖宿主机驱动库"),
        0.98,
    ),
    DiagnosticRule(
        "image_pull_failed",
        re.compile(r"manifest unknown|pull access denied|failed to resolve reference", re.I),
        "镜像地址、权限或仓库连接异常",
        ("核对镜像名称和标签", "检查镜像仓库凭据与网络"),
        0.94,
    ),
    DiagnosticRule(
        "container_oom_killed",
        re.compile(r"oomkilled|exit code 137|exited \(137\)", re.I),
        "容器内存上限可能不足",
        ("提高容器内存额度", "检查主机内存压力"),
        0.92,
    ),
    DiagnosticRule(
        "python_dependency",
        re.compile(r"modulenotfounderror|cannot import name|importerror", re.I),
        "Python 依赖缺失或版本不兼容",
        ("核对锁文件和镜像依赖版本", "在隔离环境复现导入失败"),
        0.86,
    ),
)


REDACTION_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
            re.I | re.S,
        ),
        "<redacted-private-key>",
    ),
    (
        "authorization",
        re.compile(r"(?im)(authorization\s*:\s*bearer\s+)[^\s]+"),
        r"\1<redacted-token>",
    ),
    (
        "secret_assignment",
        re.compile(
            r"(?im)\b("
            r"[A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE_KEY|ACCESS_KEY)"
            r"|password|passwd|client_secret|api_key|token|secret"
            r")\s*[:=]\s*([^\s]+)"
        ),
        r"\1=<redacted-secret>",
    ),
    (
        "signed_query",
        re.compile(
            r"(?i)([?&](?:x-amz-signature|signature|sig|access_token|token|key)=)[^&\s]+"
        ),
        r"\1<redacted-token>",
    ),
    (
        "ssh_key",
        re.compile(
            r"(?m)\b(ssh-(?:rsa|ed25519|dss)|ecdsa-sha2-\S+)\s+[A-Za-z0-9+/=]+(?:\s+\S+)?"
        ),
        r"\1 <redacted-ssh-key>",
    ),
    (
        "user_path",
        re.compile(r"/(data|scratch)/users/[^/\s]+", re.I),
        r"/\1/users/<user>",
    ),
    (
        "home_path",
        re.compile(r"/home/[^/\s]+", re.I),
        "/home/<user>",
    ),
)


def redact_logs(logs: str) -> tuple[str, dict[str, int]]:
    bounded = _bounded_text(logs)
    counts: dict[str, int] = {}
    for name, pattern, replacement in REDACTION_RULES:
        bounded, count = pattern.subn(replacement, bounded)
        if count:
            counts[name] = counts.get(name, 0) + count
    return _bounded_text(bounded), counts


def diagnose_rules(logs: str, *, evidence: dict[str, Any]) -> dict[str, Any]:
    redacted_logs, counts = redact_logs(logs)
    evidence_source = _evidence_text(evidence)
    redacted_evidence, evidence_counts = redact_logs(evidence_source)
    for name, count in evidence_counts.items():
        counts[name] = counts.get(name, 0) + count
    combined = "\n".join(part for part in (redacted_logs, redacted_evidence) if part)
    lines = combined.splitlines()
    findings = []
    for rule in RULES:
        if not rule.pattern.search(combined):
            continue
        excerpts = [line[:300] for line in lines if rule.pattern.search(line)][:3]
        findings.append(
            {
                "code": rule.code,
                "cause": rule.cause,
                "suggestions": list(rule.suggestions),
                "confidence": rule.confidence,
                "excerpts": excerpts,
            }
        )
    return {
        "engine": "rules",
        "findings": findings,
        "needs_model": not findings,
        "redacted_logs": redacted_logs,
        "redaction_counts": counts,
    }


def diagnose(
    logs: str,
    *,
    evidence: dict[str, Any],
    model: Any | None = None,
) -> dict[str, Any]:
    rules_report = diagnose_rules(logs, evidence=evidence)
    high_confidence = any(
        float(finding.get("confidence", 0)) >= 0.8
        for finding in rules_report["findings"]
    )
    if high_confidence or model is None:
        return {**rules_report, "source": "rules"}

    redacted_evidence, _counts = redact_logs(_evidence_text(evidence))
    model_input = _bounded_text(
        "\n".join(
            part
            for part in (rules_report["redacted_logs"], redacted_evidence)
            if part
        )
    )
    model_result = model.diagnose(model_input)
    if model_result is None:
        return {**rules_report, "source": "rules"}
    return {
        "source": "model",
        "model": model.model_name,
        "findings": [{"code": "model_diagnosis", **model_result}],
        "needs_model": False,
        "redaction_counts": rules_report["redaction_counts"],
    }


def _evidence_text(evidence: dict[str, Any]) -> str:
    lines = [json.dumps(evidence, ensure_ascii=False, sort_keys=True, default=str)]
    if evidence.get("oom_killed"):
        lines.append("OOMKilled")
    if evidence.get("exit_code") == 137:
        lines.append("exit code 137")
    return "\n".join(lines)


def _bounded_text(value: str) -> str:
    lines = str(value).splitlines()[-MAX_LOG_LINES:]
    payload = "\n".join(lines).encode("utf-8")[-MAX_LOG_BYTES:]
    text = payload.decode("utf-8", errors="replace")
    while len(text.encode("utf-8")) > MAX_LOG_BYTES:
        text = text[1:]
    return text


__all__ = ["DiagnosticRule", "RULES", "diagnose", "diagnose_rules", "redact_logs"]
