from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from gsm_api import main
from gsm_api.gpu_history import history
from gsm_api.telemetry_store import MemoryTelemetryStore
import pytest


def test_history_averages_real_samples_preserves_gaps_and_omits_identities():
    store = MemoryTelemetryStore()
    now = datetime(2026, 9, 28, 4, 0, tzinfo=timezone.utc)
    for minutes, utilization in [(59.9, 20), (59.5, 80), (57.5, 0)]:
        store.append({'server_id': 'server1', 'gpu_uuid': 'GPU-a', 'index': 0,
            'observed_at': (now - timedelta(minutes=minutes)).isoformat(),
            'utilization': utilization, 'memory_used_mb': 1024, 'memory_total_mb': 4096,
            'processes': [{'pid': 12, 'user_id': 'private-owner'}]})
    result = history(store, 'server1', minutes=60, now=now)
    points = result['series'][0]['points']
    assert points[0]['utilization'] == 50
    assert points[1]['utilization'] is None
    assert points[2]['utilization'] == 0
    assert 'private-owner' not in str(result) and 'processes' not in str(result)
    assert history(store, 'other-server', minutes=60, now=now)['series'] == []
    assert len(history(store, 'server1', minutes=1440, now=now)['series'][0]['points']) <= 181


def test_history_endpoint_accepts_numeric_query_and_requires_resource_access(monkeypatch):
    monkeypatch.setattr(main, '_telemetry_store', MemoryTelemetryStore)
    monkeypatch.setattr(main, '_get_server_or_404', lambda _: object())
    client = TestClient(main.app)
    assert client.get('/api/servers/server1/gpu-history?minutes=60').status_code == 401
    headers = {'Authorization': 'Bearer user-alice-token'}
    result = client.get('/api/servers/server1/gpu-history?minutes=1440', headers=headers)
    assert result.status_code == 200
    assert result.headers['cache-control'] == 'no-store'
    assert client.get('/api/servers/server1/gpu-history?minutes=61', headers=headers).status_code == 422


@pytest.mark.parametrize('minutes', [360, 1440])
@pytest.mark.parametrize('failure', ['outage', 'invalid'])
def test_wide_gpu_buckets_preserve_interruptions_and_invalid_observations(minutes, failure):
    store = MemoryTelemetryStore()
    now = datetime(2026, 9, 30, 14, tzinfo=timezone.utc)
    start = now - timedelta(minutes=minutes)
    for index, seconds in enumerate([5, 110 if failure == 'outage' else 65]):
        store.append({'server_id': 'server1', 'gpu_uuid': 'GPU-a', 'index': 0,
            'observed_at': (start + timedelta(seconds=seconds)).isoformat(),
            'utilization': None if failure == 'invalid' and index == 1 else 20,
            'memory_used_mb': 128, 'memory_total_mb': 4096})
    result = history(store, 'server1', minutes=minutes, now=now)
    assert result['series'][0]['points'][0]['utilization'] is None
    assert result['series'][0]['points'][0]['memory_used_mb'] is None
    assert len(result['series'][0]['points']) <= 181


def test_latest_timestamp_uses_valid_raw_sample_instead_of_wide_bucket_start():
    store = MemoryTelemetryStore()
    now = datetime(2026, 9, 30, 14, tzinfo=timezone.utc)
    for seconds, utilization in [(40, 20), (10, None)]:
        store.append({'server_id': 'server1', 'gpu_uuid': 'GPU-a', 'index': 0,
            'observed_at': (now - timedelta(seconds=seconds)).isoformat(),
            'utilization': utilization, 'memory_used_mb': 128, 'memory_total_mb': 4096})
    result = history(store, 'server1', minutes=1440, now=now)
    assert result['series'][0]['latest_observed_at'] == (now - timedelta(seconds=40)).isoformat()
    assert result['series'][0]['latest_sample'] == {
        'at': (now - timedelta(seconds=40)).isoformat(), 'utilization': 20,
        'memory_used_mb': 128, 'memory_total_mb': 4096}
    assert all(point['utilization'] is None for point in result['series'][0]['points'])
