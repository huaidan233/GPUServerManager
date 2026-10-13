import pytest

from gsm_api.server_inventory import GIB, make_inventory, write_inventory


@pytest.fixture
def initialized_server(tmp_path, monkeypatch):
    """Production API tests provide an explicit host configuration like the installer."""
    path = tmp_path / "server.json"
    document = make_inventory(
        {
            "hostname": "test-server",
            "cpu_count": 64,
            "memory_bytes": 64 * GIB,
            "gpus": [
                {
                    "index": index,
                    "uuid": f"GPU-test-{index}",
                    "name": "Test GPU",
                    "memory_mib": 46068,
                }
                for index in range(2)
            ],
        }
    )
    write_inventory(path, document)
    monkeypatch.setenv("GSM_SERVER_INVENTORY_PATH", str(path))
    return path
