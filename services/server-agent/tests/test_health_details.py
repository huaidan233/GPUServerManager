from __future__ import annotations

import json
import subprocess
import pytest

from gsm_server_agent.health import (
    build_health_snapshot,
    collect_health_details,
    collect_xid_codes,
    extract_xid_codes,
    parse_ecc_xml,
    XidCollectorUnavailable,
)


def base_snapshot(**overrides):
    payload = {
        "now": "2026-08-03T00:00:00+00:00",
        "memory": {"total_mb": 64000, "available_mb": 32000},
        "load": {"one": 4.0, "five": 3.0, "fifteen": 2.0, "cpu_count": 16},
        "filesystems": [
            {
                "path": "/",
                "exists": True,
                "free_percent": 50.0,
                "inode_free_percent": 50.0,
                "writable": True,
            }
        ],
        "docker": {"ok": True},
        "gpus": [],
        "expected_gpu_count": 0,
    }
    payload.update(overrides)
    return build_health_snapshot(**payload)


def test_health_snapshot_reports_disk_mount_driver_and_docker_failures():
    snapshot = base_snapshot(
        memory={"total_mb": 64000, "available_mb": 8000},
        filesystems=[
            {"path": "/", "exists": True, "free_percent": 4.0, "inode_free_percent": 20.0, "writable": True},
            {"path": "/data", "exists": True, "free_percent": 50.0, "inode_free_percent": 50.0, "writable": False},
        ],
        docker={"ok": False, "error_type": "RuntimeError"},
        expected_gpu_count=2,
    )

    codes = {item["code"] for item in snapshot["checks"]}
    assert codes == {
        "disk_space_low",
        "mount_not_writable",
        "docker_unavailable",
        "gpu_count_mismatch",
    }
    assert snapshot["status"] == "failed"


def test_health_snapshot_applies_disk_inode_memory_and_load_boundaries():
    snapshot = base_snapshot(
        memory={"total_mb": 1000, "available_mb": 49},
        load={"one": 3.0, "five": 3.1, "fifteen": 2.0, "cpu_count": 2},
        filesystems=[
            {"path": "/", "exists": True, "free_percent": 4.9, "inode_free_percent": 9.9, "writable": True},
            {"path": "/data", "exists": True, "free_percent": 5.0, "inode_free_percent": 10.0, "writable": True},
            {"path": "/scratch", "exists": False},
        ],
    )

    by_code = {}
    for item in snapshot["checks"]:
        by_code.setdefault(item["code"], []).append(item)

    assert by_code["memory_low"][0]["severity"] == "critical"
    assert by_code["load_high"][0]["severity"] == "critical"
    assert [item["severity"] for item in by_code["disk_space_low"]] == ["critical", "warning"]
    assert by_code["inode_low"][0]["evidence"]["path"] == "/"
    assert by_code["mount_missing"][0]["evidence"] == {"path": "/scratch"}


def test_health_snapshot_reports_gpu_temperature_ecc_and_xid():
    snapshot = base_snapshot(
        gpus=[
            {"uuid": "GPU-a", "temperature_c": 80, "ecc_uncorrected": 0, "xid_errors": []},
            {"uuid": "GPU-b", "temperature_c": 88, "ecc_uncorrected": 2, "xid_errors": [31, 79]},
        ],
        expected_gpu_count=2,
    )

    temperatures = [item for item in snapshot["checks"] if item["code"] == "gpu_temperature_high"]
    assert [item["severity"] for item in temperatures] == ["warning", "critical"]
    hardware = next(item for item in snapshot["checks"] if item["code"] == "gpu_hardware_error")
    assert hardware["evidence"] == {
        "uuid": "GPU-b",
        "ecc_uncorrected": 2,
        "xid_codes": [31, 79],
    }


def test_exact_warning_boundaries_do_not_trigger_lower_thresholds():
    snapshot = base_snapshot(
        memory={"total_mb": 1000, "available_mb": 100},
        load={"one": 2.0, "five": 2.0, "fifteen": 2.0, "cpu_count": 2},
        filesystems=[
            {"path": "/", "exists": True, "free_percent": 10.0, "inode_free_percent": 10.0, "writable": True},
        ],
    )

    assert snapshot == {
        "observed_at": "2026-08-03T00:00:00+00:00",
        "status": "ok",
        "checks": [],
    }


def test_parses_uncorrected_ecc_counts_by_gpu_uuid():
    xml = """
    <nvidia_smi_log>
      <gpu><uuid>GPU-a</uuid><ecc_errors><volatile><double_bit><device_memory>2</device_memory><register_file>1</register_file></double_bit></volatile></ecc_errors></gpu>
      <gpu><uuid>GPU-b</uuid><ecc_errors><volatile><double_bit><device_memory>N/A</device_memory></double_bit></volatile></ecc_errors></gpu>
    </nvidia_smi_log>
    """

    assert parse_ecc_xml(xml) == {"GPU-a": 3, "GPU-b": 0}


def test_extracts_unique_xid_codes_from_kernel_lines():
    output = "\n".join(
        [
            "NVRM: Xid (PCI:0000:81:00): 31, pid=1",
            "NVRM: Xid (PCI:0000:81:00): 79, GPU has fallen off the bus",
            "NVRM: Xid (PCI:0000:81:00): 31, duplicate",
        ]
    )

    assert extract_xid_codes(output) == [31, 79]


def test_failed_collector_is_bounded_and_does_not_raise(monkeypatch):
    class Runtime:
        def list_gpus(self):
            return [{"uuid": "GPU-a", "temperature_c": 40}]

        def docker_health(self):
            return {"ok": True}

    def fail_memory():
        raise PermissionError("hidden")

    monkeypatch.delenv("GSM_AGENT_EXPECTED_GPU_COUNT", raising=False)
    snapshot = collect_health_details(
        Runtime(),
        memory_collector=fail_memory,
        load_collector=lambda: {"one": 0, "five": 0, "fifteen": 0, "cpu_count": 8},
        filesystem_collector=lambda paths: [
            {
                "path": path,
                "exists": True,
                "free_percent": 50,
                "inode_free_percent": 50,
                "writable": True,
            }
            for path in paths
        ],
        ecc_collector=lambda: {},
        xid_collector=lambda: [],
    )

    failed = next(item for item in snapshot["checks"] if item["code"] == "collector_failed")
    assert failed == {
        "code": "collector_failed",
        "status": "failed",
        "severity": "warning",
        "scope": "infrastructure",
        "evidence": {"collector": "memory", "error_type": "PermissionError"},
    }
    assert snapshot["hardware"]["memory_bytes"] is None
    assert snapshot["hardware"]["gpus"] is None


def test_health_details_reports_cpu_memory_and_gpu_identity_for_inventory_checks():
    class Runtime:
        def list_gpus(self):
            return [{"index": 1, "uuid": "GPU-test", "temperature_c": 40}]

        def docker_health(self):
            return {"ok": True}

    snapshot = collect_health_details(
        Runtime(),
        memory_collector=lambda: {"total_mb": 64000, "available_mb": 32000},
        load_collector=lambda: {"one": 0, "five": 0, "fifteen": 0, "cpu_count": 64},
        filesystem_collector=lambda paths: [],
        ecc_collector=lambda: {}, xid_collector=lambda: [],
    )
    assert snapshot["hardware"] == {
        "cpu_count": 64, "memory_bytes": 64000 * 1024 ** 2,
        "gpus": [{"index": 1, "uuid": "GPU-test"}],
    }


HOST_BOOT = "12345678-1234-1234-1234-123456789abc"
JOURNAL_PROBE = json.dumps({"_BOOT_ID": HOST_BOOT.replace("-", ""), "_TRANSPORT": "kernel", "MESSAGE": "boot"})


@pytest.mark.parametrize(("returncode", "payload", "expected"), [
    (1, "", []),
    (0, "NVRM: Xid (PCI:0000:81:00): 79, bus failure\nNVRM: Xid: 31\nNVRM: Xid: 79", [31, 79]),
])
def test_xid_reads_mounted_host_journal_with_explicit_host_boot(returncode, payload, expected):
    calls = []
    def run(arguments, **options):
        calls.append(arguments)
        assert options["timeout"] == 5
        if len(calls) == 1:
            return subprocess.CompletedProcess(arguments, 0, JOURNAL_PROBE, "")
        return subprocess.CompletedProcess(arguments, returncode, payload, "")
    def read(path):
        assert path == "/host/proc/sys/kernel/random/boot_id"
        return HOST_BOOT + "\n"
    assert collect_xid_codes(run=run, read_text=read) == expected
    assert len(calls) == 2
    for call in calls:
        assert "--directory=/host/log/journal" in call
        assert "_BOOT_ID=" + HOST_BOOT.replace("-", "") in call
        assert "_TRANSPORT=kernel" in call
        assert "-k" not in call
    assert "--since=-10min" in calls[1]


@pytest.mark.parametrize(("code", "output", "error"), [
    (0, "", ""),  # no current-boot kernel journal is not a clean GPU result
    (1, "", "No journal files were found."),
    (0, JOURNAL_PROBE, "Permission denied"),
    (0, "not-json", ""),
    (0, json.dumps({"_BOOT_ID": "0" * 32, "_TRANSPORT": "kernel"}), ""),
])
def test_unavailable_or_wrong_boot_journal_is_not_reported_as_zero_xids(code, output, error):
    with pytest.raises(XidCollectorUnavailable):
        collect_xid_codes(run=lambda *args, **kwargs: subprocess.CompletedProcess(args, code, output, error),
                          read_text=lambda path: HOST_BOOT)


@pytest.mark.parametrize(("exception", "reason"), [
    (FileNotFoundError("private path"), "journalctl_missing"),
    (PermissionError("private permission"), "journal_query_unavailable"),
    (subprocess.TimeoutExpired("journalctl", 5), "journal_query_timeout"),
])
def test_journal_binary_and_execution_failures_are_bounded(exception, reason):
    def fail(*args, **kwargs):
        raise exception
    with pytest.raises(XidCollectorUnavailable) as caught:
        collect_xid_codes(run=fail, read_text=lambda path: HOST_BOOT)
    assert caught.value.reason == reason
    assert "private" not in str(caught.value)


@pytest.mark.parametrize(("code", "output", "error"), [
    (1, "", "Permission denied"),
    (2, "", ""),
    (1, "partial output", ""),
])
def test_failed_xid_query_after_successful_probe_remains_unavailable(code, output, error):
    results = iter([subprocess.CompletedProcess([], 0, JOURNAL_PROBE, ""), subprocess.CompletedProcess([], code, output, error)])
    with pytest.raises(XidCollectorUnavailable, match="journal_query_failed"):
        collect_xid_codes(run=lambda *args, **kwargs: next(results), read_text=lambda path: HOST_BOOT)


def test_missing_host_boot_identity_never_falls_back_to_container_boot():
    def read(path):
        raise FileNotFoundError(path)
    def run(*args, **kwargs):
        pytest.fail("must not query the container's default journal")
    with pytest.raises(XidCollectorUnavailable, match="host_boot_id_unavailable"):
        collect_xid_codes(run=run, read_text=read)


@pytest.mark.parametrize("exception", [XidCollectorUnavailable("host_kernel_journal_unavailable"), FileNotFoundError("private")])
def test_unavailable_xid_collector_degrades_health_without_hardware_failure(monkeypatch, exception):
    class Runtime:
        def list_gpus(self):
            return [{"index": 0, "uuid": "GPU-test", "temperature_c": 40}]
        def docker_health(self):
            return {"ok": True}
    def unavailable():
        raise exception
    monkeypatch.delenv("GSM_AGENT_EXPECTED_GPU_COUNT", raising=False)
    snapshot = collect_health_details(Runtime(),
        memory_collector=lambda: {"total_mb": 100, "available_mb": 50},
        load_collector=lambda: {"five": 0, "cpu_count": 8}, filesystem_collector=lambda paths: [],
        ecc_collector=lambda: {}, xid_collector=unavailable)
    assert snapshot["status"] == "degraded"
    assert len(snapshot["checks"]) == 1
    assert snapshot["checks"][0]["code"] == "collector_unavailable"
    assert snapshot["checks"][0]["evidence"]["collector"] == "gpu_xid"
    assert "private" not in json.dumps(snapshot)
    assert snapshot["hardware"]["gpus"] == [{"index": 0, "uuid": "GPU-test"}]
