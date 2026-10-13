# Resource monitoring

Resource history adds host CPU usage, physical memory, configured filesystem capacity and disk I/O, plus each managed container's CPU and working-set memory history. The existing API periodic worker collects independently of page visits. The web page refreshes stored data every 30 seconds and offers 1, 6 and 24 hour views. Raw samples remain in the telemetry SQLite database for 7 days; each chart is averaged into at most 181 points.

## Deployment

Mount host `/proc` read-only at `/host/proc` in Server Agent. `GSM_HOST_PROC_ROOT` can override that path. Set `GSM_RESOURCE_DISKS` to a JSON array of existing host bind-mounted paths, for example:

```json
[{"id":"system","label":"系统存储","path":"/var/lib/gpu-server-manager/server-agent"},{"id":"data","label":"数据盘","path":"/data/users"},{"id":"scratch","label":"临时盘","path":"/scratch/users"}]
```

Use the deployment's actual mounted locations. No host-root mount is needed. Paths on the same device are combined, with all configured labels retained. An unavailable path or a container-overlay path is reported as unavailable instead of being presented as host storage. Device I/O refers to the filesystem's backing device and includes all activity on that device, not just this application's files. Filesystems without a Linux block device currently report no disk sample.

The API uses the already configured `GSM_TELEMETRY_DATABASE_URL`, adding an independent `resource_samples` table. Host history requires `resource.read`; container history requires permission to read that exact container and holds the ownership boundary while reading. Host responses contain no tenant identities or container resource rows.

## Measurement semantics

- Host CPU uses differences in `/proc/stat`, excludes double-counted guest fields and counts I/O wait as idle. It is bounded to 0–100% across all logical CPUs.
- Container CPU is calculated between actual Docker one-shot observations using total CPU/system counter differences and online CPUs. The first observation, reset counters and a different container start time produce no CPU rate. CPU is displayed in used cores separately from the configured quota.
- Host memory is `MemTotal − MemAvailable`. Container memory subtracts reclaimable inactive-file cache. Memory capacity and actual usage are separate fields.
- Disk I/O uses differences in `/proc/diskstats` counters; each Linux sector counts as 512 bytes. First observations and host reboots leave rates empty.
- Known stopped containers consume zero CPU/memory. Collection failures and missing intervals remain null, never zero. A single disappearing container does not discard other samples. An agent collection failure leaves a visible gap. Samples older than 3 minutes are marked stale.
- CPU and I/O readings average the period between samples; this is resource monitoring, not an instantaneous profiler. Host boot ID/time accompany observations for incident correlation.

## Validation

Focused Python tests exercise CPU baselines/reset, cgroup memory, host-vs-container metrics, filesystem deduplication and rate calculation, missing mounts, individual collection failure, authentication, ownership, persistence/retention, bucket gaps and worker tenant mapping. Frontend tests cover actual usage vs quota, keyboard inspection, gap-vs-zero rendering, range switching, retries, cookie forwarding and no-store responses. Local browser review covers desktop and a 390px viewport with explicitly simulated data.

For deployment acceptance, compare timestamps and metrics with independent host reads, exercise 1/6/24-hour ranges, and test an authorized disposable container plus a second non-owner account. Keep real credentials, host inventory and acceptance output outside the repository. Do not alter running research workloads to generate monitoring evidence. See the [deployment guide](deployment-guide.md) for storage placement and runtime capacity protection.
