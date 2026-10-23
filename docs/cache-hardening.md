# API cache hardening

Base source: `ba36f5ee710fd81e35931e0c2a181863a3114121` (ELCASH).
This change is prepared locally. It has not been deployed to production.
`requirements-runtime.lock` freezes a read-only inventory of deployed package
versions, including Python 3.7-compatible dependencies and PyMongo 3.11.4. Tests
used only isolated synthetic data; no production configuration or data was changed.
The production Dockerfile constrains its existing requirements with this lock;
the test image installs the same runtime lock plus test-only dependencies.

## Scope

- Stop consulting or adding to the legacy `request_cache` collection.
- Store new results in `query_cache_v2`, using the built-in unique `_id` index.
- Include the database/collection, query type, parameters, pipeline and data
  generation in the key. BSON encoding preserves compound-sort field order.
  Empty results are valid cache hits.
- Expire entries after one hour. The TTL index is created on the new collection,
  not on the existing large collection. Application reads also check expiry,
  independently of the asynchronous MongoDB TTL cleanup.
- Cache total supply and nonempty-address counts without changing JSON shapes.
- Publish a new generation only after the daemon finishes its address updates
  and checks its stored tip against the node. Cache hits do not write to MongoDB.
- Check each fetched block's height and parent hash against the verified anchor
  and preceding fetched block, including across chunk boundaries, before insert.
- Preserve ELCASH address transaction-history updates.
- Leave interrupted updates and detected reorgs fail-closed. This is a safety
  boundary, not an implementation of automatic blockchain recovery.

Normal queries deliberately leave the old cache intact for rollback. Deleting it is a separate
approved maintenance operation. This patch does not change miners or GitOps.

## Limitations and operational change

The daemon persists blocks/UTXOs before all address balances have been updated.
Previously ignored write failures could therefore leave inconsistent statistics.
Write failures now propagate, and an unfinished `cache_state` marker prevents a
restart from silently resuming and publishing that data as a completed snapshot.
A same-height node/stored-tip mismatch also requires verified recovery.

ELCASH's legacy automatic full-database restore is retired. `check_reorg()` now
invalidates the cache and raises a recovery-required error instead of restoring.
`restore_db()` also invalidates and raises before any shell command, backup,
drop or restore. Its old body could swallow restore failures and could restore
stale cache metadata alongside partially restored application collections.
No automatic replacement recovery procedure is implemented in this patch.
`drop_db()` invalidates before its existing collection deletions and preserves
the disabled `cache_state` document. Failure to invalidate aborts the deletion.

Do not deploy without accepting this fail-closed behavior and having an operator
available to handle an interrupted update. Do not reset the marker merely to make
the daemon start. Cache safety does not repair a pre-existing inconsistent DB.
Reads during a normal update preserve the existing uncached behavior; this is
not transaction-level snapshot isolation for the HTTP API.

Concurrent cold requests can still compute the same result independently; atomic
upserts prevent duplicate rows, but do not provide a distributed computation lock.
The legacy data and other uncached queries may still require separate maintenance.

## Unresolved release and security risk

An OSV baseline check was performed across both BTCV and ELCASH
runtime locks: 30 unique package/version pairs were checked and 15 pairs had
advisories. The affected package names across that combined baseline are Flask,
Jinja2, Werkzeug, certifi, dnspython, gunicorn, idna, pymongo, requests and urllib3.
These totals describe both locks together, not 15 findings specific to ELCASH.

The affected versions are pre-existing deployed dependencies frozen for
compatibility, not package upgrades introduced by this cache fix. No dependency
remediation or risk acceptance is claimed here. Package upgrades remain outside
this task and the advisory findings remain an unresolved, separate release risk.
This dependency lookup is not a full security assessment or security clearance.
Passing cache tests, `pip check`,
image builds and the local HTTP smoke do not resolve these advisories.

## Local verification

The tests use a real, isolated MongoDB 6 container and generated database names.
No production connection string is used. The test setup accepts only the
existing `gw-cache-mongo-20261023:27017` container, rejects database paths,
credentials and URI options, selects a random `cache_fix_tests_*` database,
and blocks HTTP/chain RPC. The shared daemon fixtures include valid parent
hashes so ELCASH's additional stored-chain check can run.

```powershell
docker network create gw-cache-tests-20261023
docker run --detach --rm --name gw-cache-mongo-20261023 --network gw-cache-tests-20261023 mongo:6.0.13 --bind_ip_all --quiet
docker build --pull=false --build-arg PYTHON_VERSION=3.7 -f tests/Dockerfile -t gw-cache-test:py37-20261023 .
docker run --rm --network gw-cache-tests-20261023 -e TEST_MONGODB_URI=mongodb://gw-cache-mongo-20261023:27017 -e COVERAGE_FILE=/tmp/cache.coverage --mount "type=bind,source=$($PWD.Path),target=/work,readonly" gw-cache-test:py37-20261023 tests -q --cov=db.query_cache --cov-branch --cov-report=term-missing --cov-fail-under=80
```

Create the dedicated network and MongoDB container only once for a test session.
Each test process removes only its randomly generated database on exit. After all
runs, remove only the dedicated fixtures you created; do not remove unrelated
containers. Do not reuse production credentials or restore production data.

Observed local verification: 59 tests passed on Python 3.7.17 and MongoDB 6.0.13,
with 100% line and branch coverage of `db.query_cache` (61 statements, 14 branches).
This includes actual coinbase and spend processing through `insert_block()` and
`update_addresses()`, checking balances, spent-UTXO removal and generation rotation.
The pre-fix regression run failed 5 tests; the additional review regressions also
failed 5 tests before the BSON/error-handling/continuity fixes were applied.
Both images contain all 21 supplied runtime package versions exactly, and
`pip check` passes. The frozen packages are not an assertion that the legacy
runtime is supported or free of vulnerabilities.

## Production-image local smoke

The actual ELCASH Dockerfile built successfully as
`gw-api-candidate:elcash-cache-20261023`, image ID
`sha256:500e884cf8a5dabe50c2e4a718b78a1c7590926200342f37978812e9912cc2f3`.
The unmodified image ran Gunicorn against a random `cache_fix_http_*` database
and the existing local `gw-cache-rpc-20261023` stub. Ten `/status` plus ten
`/nonemptywalletsnumber` requests caused exactly two address aggregations. A
further `/status` request after changing the generation returned the new supply;
the aggregation count became three. The count response remained the number `1`.
The readiness request is separate from these 21 contract requests.

```powershell
docker build --pull=false -t gw-api-candidate:elcash-cache-20261023 .
docker run --detach --rm --name gw-cache-rpc-20261023 --network gw-cache-tests-20261023 --mount "type=bind,source=$($PWD.Path),target=/work,readonly" --entrypoint python gw-cache-test:py37-20261023 /work/tests/fake_rpc.py
$testDb = 'cache_fix_http_' + [guid]::NewGuid().ToString('N')
$apiName = 'gw-api-smoke-elcash-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
docker run --detach --rm --name $apiName --network gw-cache-tests-20261023 -e DB_URL=gw-cache-mongo-20261023 -e DB_NAME=$testDb -e RPC_HOST=gw-cache-rpc-20261023 -e RPC_USER=test -e RPC_PASSWORD=test gw-api-candidate:elcash-cache-20261023
try {
    docker run --rm --network gw-cache-tests-20261023 -e TEST_MONGODB_URI=mongodb://gw-cache-mongo-20261023:27017 -e TEST_DATABASE=$testDb -e TEST_API_URL=http://${apiName}:5000 --mount "type=bind,source=$($PWD.Path),target=/work,readonly" --entrypoint python gw-cache-test:py37-20261023 tests/http_smoke.py
} finally {
    docker stop $apiName
}
```

The smoke database and smoke API container were removed after the run. The
dedicated MongoDB and RPC fixtures were stopped after final validation. This is local
candidate-image evidence, not a deployment or real-chain recovery test.

## Proposed rollout, not performed

The PR workflow now builds the candidate, runs the isolated MongoDB tests and
the HTTP image smoke, and uploads test reports plus a Docker image archive.
The archive is named for the exact checked-out source SHA and retained for
seven days. Loading/publishing that archive for staging is a separate approved
operation. This workflow has read-only repository permissions, receives no AWS
secrets, and does not push to ECR, invoke Argo CD, or run the release workflow.
Its shell steps were executed locally; actual GitHub job and artifact status
must be checked separately after publication. Existing master release behavior
is unchanged.

1. Review the complete API/daemon patch together. Build from the pinned source
   plus this patch, not from an unrelated newer branch.
2. Test the paired API and daemon on an isolated non-production database. Cover
   successful ingestion, interruption, reorg detection, rollback and HTTP shapes.
3. Confirm backups and an application-data-only recovery procedure. A restore
   must not restore `cache_state` or `query_cache_v2` from a dump.
4. Require an initial data-consistency preflight before enabling the new cache:
   independently verify that stored blocks, UTXOs and address balances agree
   with the canonical chain at a consistent checkpoint. Matching the stored tip
   height and hash alone cannot detect pre-existing wrong address/UTXO balances.
   Do not treat a successful new ingestion or a ready marker as that evidence.
   If consistency is unverified or fails, keep caching disabled and use the
   separately approved recovery procedure before proceeding.
5. In an approved window, roll out the daemon first. A pre-existing database has
   no ready marker; caching starts only after the next successful ingestion.
6. Verify the daemon completed an update, `cache_state.ready` is true, and the
   stored height/hash agrees with the node. Then roll out the API.
7. Confirm repeated identical requests use the `_id` index and reuse results.
   Compare worker timeouts, HTTP 5xx, MongoDB slow queries and CPU with baseline.
8. Do not remove old cache data in the rollout. Schedule cleanup independently.

## Interrupted update or reorg

Stop the affected daemon before recovery. Preserve evidence and a backup first.
Verify or rebuild the affected `block`, `address` and `utxo` data from a canonical
source in an isolated database. A height match alone is not a balance/UTXO check.
Never restore the new cache/state collections from a backup.

After application data has been independently verified and with the daemon still
stopped, an approved operator can remove only the `_id: "blockchain"` marker from
`cache_state`. Restart the daemon and verify a complete new block update creates
a fresh ready generation. Until then the API bypasses the cache. This procedure
does not authorize database changes or define an unverified restore as safe.

## Rollback

Roll back the API first and verify all API instances are on the previously
recorded image digest before rolling back the daemon. Never leave the new API
running with the old daemon: the old daemon does not invalidate cache generations
when it mutates data, so the new API could continue serving stale cached results.
Use the normal approved deployment path for both steps.

No legacy collection was migrated or removed. The previous code ignores the new
cache collections, but also lacks the new failure guards. Do not roll back to
bypass a known data-integrity incident. The initial data-consistency preflight
remains required when the new cache is enabled again.
