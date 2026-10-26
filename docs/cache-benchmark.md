# Local cache benchmark

`tests/cache_benchmark.py` measures the actual `/status` route in separate legacy
and candidate Python processes, using Flask test clients and the **same installed
dependencies**. It does not modify production code. It is not a real HTTP, gunicorn
worker, CPU, throughput, or production-capacity benchmark. No timing threshold is
a production SLA.

## Prerequisites and safety

- The operator builds images and starts fixtures. The runner creates no Docker
  resources and never starts/stops MongoDB or RPC services.
- Use a freshly built candidate application image with candidate sources at
  `/app`; mount the real legacy `src` directory read-only at `/legacy`.
- Legacy is pinned to `ba36f5ee710fd81e35931e0c2a181863a3114121`, not a candidate
  with caching switched off. Its source fingerprint is hard-coded and checked.
  Candidate source fingerprint must be supplied explicitly, independently of the
  running image. Image freshness/build provenance remains an operator check.
- Only `DB_URL=api-cache-test-mongo`, MongoDB port 27017, no authentication, no URI
  parameters, and `RPC_HOST=api-cache-test-rpc` port 8332 are accepted. Nonempty
  DB/RPC credentials are rejected. These guards follow the existing smoke scripts.
  The fixture network must be isolated: hostname validation is not network attestation.
- **No RPC service is contacted.** Only `RPC.execute(getnetworkhashps, [])` is
  replaced with the constant 123, matching `fake_rpc.py`. All Requests HTTP calls
  are blocked. Other RPC methods fail. MongoDB is the only intended network I/O.
- `DB_NAME` is a fresh `api_cache_http_...` prefix, at most 53 characters before
  the `_candidate` suffix. Both derived database names must be unused. Existing
  collections, including empty ones or a previous run's marker, cause rejection.
- Each worker reserves its database with an atomic marker-collection creation.
  The fixed collection name prevents competing claims; its validator stores
  the ownership token in the same create operation, with no later token insert.
  Cleanup checks that metadata, including after a lost create response.
  Cleanup requires that run's random ownership token and an allowed database
  name. Normal/error cleanup drops only the owned database. The coordinator also
  cleans up after a timed-out worker (180 seconds per worker). If Mongo is down
  during cleanup or the entire coordinator is killed, cleanup can fail; use a
  fresh name, inspect the leftover explicitly, and do not reuse it.

## Dataset and phases

There are exactly **1002 application documents per source**: 1000 deterministic
synthetic addresses (balances 1 through 10 repeated), and two lightweight blocks
using `fake_rpc.BLOCKS` headers, with empty transaction lists. There are no real
addresses, funds, transaction histories, or wallet secrets. Ownership and cache
metadata add only a few documents, keeping the total far below 5000.

The default is 20 requests per phase, concurrency 4 only in `concurrent_warm`.
Bounds are enforced at 1..100 requests per phase and 1..4 concurrent clients.
There are five phases per source, so the default total is 200 requests across
both sources; the maximum is 1000. Sources run sequentially.

| Phase | Cache/data preparation, excluded from request timings |
| --- | --- |
| `cold` | Clear cache-result collections before **every** sequential request; never reseed/change application data. |
| `warm` | Retain the final cold request's cache; same documents, sequential requests. |
| `concurrent_warm` | Retain warm cache; independent clients in at most four threads in one process. |
| `post_invalidation_unready` | Candidate's real `query_cache.invalidate()` runs, then one synthetic balance changes 1 -> 11; cache readiness stays false. Legacy cache results are cleared before the same change. |
| `post_invalidation_ready` | Candidate's real `complete_update()` enables only the synthetic generation; first read can fill the new cache. Legacy has no generation protocol. |

Every response must be HTTP status 200 and exactly equal the expected JSON:
height 1, hashrate 123, supply 5500 before the change and 5510 afterwards.
Candidate readiness is set only after this runner owns and seeds its synthetic
database. This is **not** daemon-ingestion, recovery, reorg, or rollback proof.

Application BSON document digests are checked before/after the read-only phases
and after freshness reads. They must match between sources for both dataset
versions. Cache/ownership metadata is excluded. Model indexes are initialized
before measurement; no indexes, Mongo OS pages, query plans, interpreter state,
or connection pools are flushed between requests. `cold` means **result-cache
cold**, not disk/server/process cold. This small dataset is not production-shaped.

## Measurements and interpretation

Each phase reports `wall.median_ms`, `wall.p95_ms` and sample count. The clock is
`perf_counter()` around `client.get('/status')`; client creation, JSON validation,
cache clearing, data writes, and index setup are outside those request timings.
P95 uses nearest rank (`ceil(0.95 * n)`); at small sample sizes it approaches the
maximum. Concurrent samples exclude executor queue wait. No requests/second or
process CPU figure is inferred.

`mongo.started_commands` counts PyMongo **command-start events** on the application's
connection to its database, including cache reads/writes and `getMore` separately.
`mongo.aggregate_commands_by_collection` is the subset of started `aggregate`
commands, not documents scanned, query-plan operations, successful completions,
or server-wide traffic. Failed commands may be included. The uninstrumented
fixture client, connection setup and verification reads are excluded. No Mongo
profiler or global server settings are changed.

At the reviewed revisions, legacy does not cache either `/status` aggregation.
Candidate caches address supply but still aggregates block height. Expected
address-aggregation counts for N requests are N in `cold`, 0 in `warm` and
`concurrent_warm`, N while unready, and 1 in the final ready phase. Legacy normally
has N in every phase; both normally have N block aggregations per phase. These
are semantic expectations to inspect in the report, **not latency thresholds**.
The runner validates response/data equality; timings or a `passed` result alone
must not be treated as proof of a speedup. A lower aggregate count need not mean
fewer total commands or lower latency on this small dataset.

The JSON includes both source fingerprints, the full legacy revision, Python
version/executable and installed distribution versions per worker. The coordinator
requires identical dependency records and response results across workers. Source
digests use sorted relative POSIX paths, NUL separators and SHA-256 of each `.py`
file, normalizing CRLF to LF. Symlinks and compiled artifacts (`.pyc`, `.pyo`,
`.so`, `.pyd`) are rejected: `-B` prevents bytecode writes, not reads.
Source validation is
repeated after the run. This adapts `version_transition_smoke.py` fingerprinting
to Windows checkout versus Linux image line endings; hashes are not Git SHAs.

## Operator commands (PowerShell)

No build, fixture creation, shared-fixture stop, cloud, cluster, commit, or push
command is part of this procedure. Existing fixture/image names below identify
the operator-prepared `919dba7` validation environment.

```powershell
$worktree = (Get-Location).Path
$legacy = 'C:\path\to\verified-legacy-checkout\src' # Operator-selected source path.
# Verify this independently when preparing a new image/source snapshot.
$candidateSha = 'b445f23724dd12bc9365bd647fc099d4dc31dd113c66fd2d993ade54ca17737b'
$name = 'api_cache_http_bench_' + [guid]::NewGuid().ToString('N').Substring(0, 12)

# Unit tests only, no DB dependency or repo-wide Mongo autouse fixtures.
docker run --rm --network gw-fresh-runtime-20261026 `
  --mount "type=bind,source=$worktree,target=/work,readonly" `
  elcash-tests:fresh-20261026-919dba7 `
  --noconftest tests/test_cache_benchmark.py -q

# Execute both actual source trees in the fresh candidate application image.
docker run --rm --network gw-fresh-runtime-20261026 `
  --mount "type=bind,source=$worktree\tests,target=/bench,readonly" `
  --mount "type=bind,source=$legacy,target=/legacy,readonly" `
  -e DB_URL=api-cache-test-mongo -e "DB_NAME=$name" `
  -e RPC_HOST=api-cache-test-rpc `
  elcash-api:fresh-20261026-919dba7 `
  python -B /bench/cache_benchmark.py coordinator `
  --candidate-sha256 $candidateSha --requests 20 --concurrency 4
```

Capture stdout to the operator's authorized evidence destination if a persistent
JSON report is wanted; this script writes no report files. Check the command's
exit code as well as the JSON. `worker` is the internal second CLI mode, launched
by the coordinator with the selected source, database and ownership token. Use
the coordinator for its timeout and fallback cleanup.

To recalculate a candidate pin from a separately verified checkout, from its root:

```powershell
python -B -c "import sys; from pathlib import Path; sys.path.insert(0,'tests'); from cache_benchmark import source_digest; print(source_digest(Path('src')))"
python -B -m pytest --noconftest -p no:cacheprovider tests/test_cache_benchmark.py -q
```
