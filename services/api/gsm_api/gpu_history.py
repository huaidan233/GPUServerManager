"""Bounded, identity-free GPU time series for the resource page."""
from datetime import datetime, timedelta, timezone
from math import ceil, isfinite


def history(store, server_id, *, minutes, now):
    start = now - timedelta(minutes=minutes)
    step = max(60, ceil(minutes * 60 / 180))
    series = []
    for sid, uuid in store.series_keys():
        if sid != server_id:
            continue
        samples = store.between(sid, uuid, start, now + timedelta(microseconds=1))
        buckets = {}
        discontinuities = set()
        previous = None
        latest_observed_at = None
        latest_sample = None
        for sample in samples:
            try:
                at = datetime.fromisoformat(sample['observed_at']).astimezone(timezone.utc)
                bucket = int((at - start).total_seconds() // step)
            except (KeyError, ValueError, TypeError):
                continue
            if previous is not None:
                previous_at, previous_bucket = previous
                if (at - previous_at).total_seconds() > 90 and bucket <= previous_bucket + 1:
                    discontinuities.add(bucket)
            previous = at, bucket
            try:
                utilization = float(sample['utilization'])
                used, total = float(sample['memory_used_mb']), float(sample['memory_total_mb'])
                if not all(isfinite(v) for v in (utilization, used, total)) or not (0 <= utilization <= 100 and 0 <= used <= total and total > 0):
                    discontinuities.add(bucket)
                    continue
                buckets.setdefault(bucket, []).append((utilization, used, total))
                latest_observed_at = at.isoformat()
                latest_sample = {'at': latest_observed_at, 'utilization': utilization,
                                 'memory_used_mb': used, 'memory_total_mb': total}
            except (KeyError, ValueError, TypeError):
                discontinuities.add(bucket)
                continue
        points = []
        for bucket in range(ceil(minutes * 60 / step) + 1):
            rows = [] if bucket in discontinuities else buckets.get(bucket, [])
            points.append({
                'at': (start + timedelta(seconds=bucket * step)).isoformat(),
                'utilization': round(sum(r[0] for r in rows) / len(rows), 2) if rows else None,
                'memory_used_mb': round(sum(r[1] for r in rows) / len(rows), 2) if rows else None,
                'memory_total_mb': rows[-1][2] if rows else None,
            })
        if samples:
            series.append({'gpu_uuid': uuid, 'index': samples[-1].get('index'),
                           'latest_observed_at': latest_observed_at, 'latest_sample': latest_sample,
                           'points': points})
    return {'server_id': server_id, 'start': start.isoformat(), 'end': now.isoformat(), 'step_seconds': step, 'series': series}
