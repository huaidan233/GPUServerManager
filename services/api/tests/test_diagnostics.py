from __future__ import annotations

import pytest

from gsm_api.diagnostics import diagnose_rules, redact_logs


@pytest.mark.parametrize(
    ("logs", "code"),
    [
        ("CUDA out of memory. Tried to allocate 2.00 GiB", "cuda_oom"),
        ("OSError: [Errno 28] No space left on device", "disk_full"),
        ("bind: address already in use", "port_conflict"),
        ("Permission denied: '/datasets/a.csv'", "mount_permission"),
        ("Failed to initialize NVML: Driver/library version mismatch", "driver_mismatch"),
        ("manifest unknown while pulling image", "image_pull_failed"),
        ("ModuleNotFoundError: No module named torch", "python_dependency"),
    ],
)
def test_known_failures_are_diagnosed_without_model(logs, code):
    report = diagnose_rules(logs, evidence={})

    assert report["findings"][0]["code"] == code
    assert report["needs_model"] is False


def test_structured_exit_evidence_detects_container_oom():
    report = diagnose_rules("worker exited", evidence={"exit_code": 137, "oom_killed": True})

    assert report["findings"][0]["code"] == "container_oom_killed"


def test_redaction_removes_credentials_and_user_paths():
    source = (
        "Authorization: Bearer abc.def\n"
        "OPENAI_API_KEY=sk-secret\n"
        "/data/users/alice/project/train.py\n"
    )

    redacted, counts = redact_logs(source)

    assert "abc.def" not in redacted
    assert "sk-secret" not in redacted
    assert "/data/users/alice" not in redacted
    assert "<redacted-token>" in redacted
    assert "/data/users/<user>" in redacted
    assert counts == {"authorization": 1, "secret_assignment": 1, "user_path": 1}


@pytest.mark.parametrize(
    "source,secret",
    [
        ("PASSWORD=hunter2", "hunter2"),
        ("client_secret: ultra-secret", "ultra-secret"),
        ("HF_TOKEN=hf_abcdef123", "hf_abcdef123"),
        ("ssh-ed25519 AAAAC3NzaSensitive user@host", "AAAAC3NzaSensitive"),
        ("https://oss.example/a?X-Amz-Signature=signed-value&x=1", "signed-value"),
        (
            "-----BEGIN PRIVATE KEY-----\nprivate-value\n-----END PRIVATE KEY-----",
            "private-value",
        ),
    ],
)
def test_representative_secret_values_never_remain(source, secret):
    redacted, counts = redact_logs(source)

    assert secret not in redacted
    assert sum(counts.values()) >= 1


def test_diagnostic_output_is_bounded_and_excerpts_are_small():
    lines = [f"line {index} CUDA out of memory {'x' * 400}" for index in range(500)]

    report = diagnose_rules("\n".join(lines), evidence={})
    redacted = report["redacted_logs"]
    excerpts = report["findings"][0]["excerpts"]

    assert len(redacted.splitlines()) <= 400
    assert len(redacted.encode("utf-8")) <= 65536
    assert len(excerpts) <= 3
    assert all(len(line) <= 300 for line in excerpts)


def test_unknown_failure_requests_model_fallback():
    report = diagnose_rules("unexpected framework failure 0xabc", evidence={})

    assert report["findings"] == []
    assert report["needs_model"] is True
