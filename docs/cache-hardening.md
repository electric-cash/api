# Query cache and ingestion safety

## Behavior

The API stores computed query results in `query_cache_v2` instead of reading or
appending to the legacy `request_cache` collection. Existing HTTP response
formats are unchanged.

- A cache entry's unique `_id` hashes the database/collection, query type,
  parameters, BSON-encoded pipeline and completed data generation. BSON encoding
  preserves field order for compound sorts. Empty results are cacheable.
- Entries expire after one hour. Reads check expiry immediately; a TTL index
  removes expired entries asynchronously. Cache hits do not write to MongoDB.
- Supply and nonempty-address statistics reuse cached aggregations. Concurrent
  misses may still compute independently; atomic upserts prevent duplicate rows,
  not duplicate computation.
- The daemon invalidates the generation before its first write. It publishes a
  new ready generation only after address updates and canonical-chain checks.
  Fetched block heights and parent hashes are checked before insertion.
- RPC failures before ingestion preserve the previous ready generation. After
  all writes finish, the daemon records the expected tip and can retry canonical
  verification without replaying balance updates. A crash during mutation or
  detected inconsistency still requires verified recovery.
- A lagging or temporarily unavailable node is retried at the polling interval;
  it is not treated as a confirmed reorg. RPC calls have connection/read timeouts.
- SIGTERM/SIGINT request shutdown at the next block boundary. The daemon finishes
  the in-flight block, applies address updates for the written prefix, verifies
  its actual tip and stops without ingesting the rest of the backlog or warming
  statistics caches. It checks for unfinished state before acknowledging quiescence.
  A fatal error, unresolved verification or forced kill is not a clean shutdown.
- Cache read/write failures fall back to the underlying query. Ingestion-state
  write failures propagate rather than silently certifying incomplete data.

The legacy cache is not migrated or deleted. Its cleanup is separate maintenance.

The [difficulty chart query](difficulty-query.md) also declares a covering index
on block height and difficulty. Its build and query-plan verification are a
separate deployment gate; first collection access can create declared indexes.

Shutdown acknowledgement applies only to this candidate, not an already running
legacy image. It proves that this process has no unfinished ingestion, not that
historical data or other writers are consistent. The deployment's termination
grace period must accommodate the in-flight block and its address updates and
verification, including slow RPC and database operations; this patch does not
change any production grace period or authorize stopping a writer.
If writes completed but RPC verification remains pending, shutdown fails without
acknowledgement and reports that restart will retry verification. This is distinct
from an interrupted write that requires independently verified recovery.

## Compatibility and limitations

**Operational change:** the daemon stops on unfinished updates or noncanonical
data instead of continuing with potentially inconsistent statistics. Accept this
behavior and establish a recovery procedure before deploying.

**ELCASH recovery change:** automatic full-database restoration is disabled.
`check_reorg()` invalidates the cache and raises; `restore_db()` also invalidates
and raises before any backup, deletion or restore command. Its former restore
path could suppress failures and restore stale cache metadata. No automatic
replacement recovery is provided. `drop_db()` must invalidate successfully
before its existing deletions and retains the disabled state marker. Address
transaction-history updates are preserved.

Missing input UTXOs are not ignored blindly. The daemon asks the node for the
source transaction and skips only an output whose decoded script proves it had
no supported address. A missing tracked output remains an integrity error.
This fallback requires verbose `getrawtransaction` access; the supplied node
configuration enables `txindex=1`. Verify that capability before deployment.

The `writes_complete` marker distinguishes a completed batch awaiting RPC
verification from an interrupted batch. Only the former can resume automatically,
and only if the stored height/hash still matches the recorded tip and the node.
Do not set this marker manually to bypass a failed write.

A missing or unready marker makes API queries bypass the cache. This does not
repair underlying data: reads during an update retain the existing uncached
behavior, without transaction-level snapshot isolation. Matching the stored tip
to the node does not prove historical address balances or UTXOs are correct.

`requirements-runtime.lock` constrains the existing runtime so application and
test builds resolve the same libraries. These are legacy dependencies with known
advisories; pinning them is not a security fix. Dependency/runtime modernization
and advisory triage remain separate work. Passing tests or `pip check` does not
establish production readiness or security clearance.

## Run the tests

Run the following Bash commands from the repository root with Docker available.
They build the application image, run tests against MongoDB 6.0.13, and exercise
the unmodified application image over HTTP and through actual daemon ingestion,
using synthetic data and an RPC stub. CI repeats the checks on MongoDB 4.2.24
and 6.0.13; this does not identify or certify a production deployment.

The tests accept only the named test MongoDB host and generate disposable
database names. Do not substitute production connection strings or real data.
Use a clean set of the names below; the preflight refuses existing containers.
No host ports are exposed.

```bash
set -euo pipefail
for name in api-cache-test-mongo api-cache-test-rpc api-cache-smoke-local; do
  if docker container inspect "$name" >/dev/null 2>&1; then
    echo "Test container already exists: $name" >&2
    exit 1
  fi
done
docker network create --internal api-cache-test-network
cleanup() {
  docker rm -f api-cache-smoke-local api-cache-test-rpc api-cache-test-mongo >/dev/null 2>&1 || true
  docker network rm api-cache-test-network >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker build -t api-cache-candidate:local .
docker run --rm --network none --entrypoint python api-cache-candidate:local -m pip check
docker build --build-arg PYTHON_VERSION=3.7 -f tests/Dockerfile -t api-cache-tests:local .
docker run --detach --rm --name api-cache-test-mongo \
  --network api-cache-test-network mongo:6.0.13 --bind_ip_all --quiet
docker run --rm --network api-cache-test-network \
  -e TEST_MONGODB_URI=mongodb://api-cache-test-mongo:27017 \
  -e COVERAGE_FILE=/tmp/cache.coverage \
  --mount "type=bind,source=$PWD,target=/work,readonly" \
  api-cache-tests:local tests -q --cov=db.query_cache --cov-branch \
  --cov-report=term-missing --cov-fail-under=80

docker run --detach --rm --name api-cache-test-rpc \
  --network api-cache-test-network \
  --mount "type=bind,source=$PWD,target=/work,readonly" \
  --entrypoint python api-cache-tests:local /work/tests/fake_rpc.py
TEST_DATABASE="api_cache_http_${RANDOM}_$$"
docker run --detach --rm --name api-cache-smoke-local \
  --network api-cache-test-network \
  -e DB_URL=api-cache-test-mongo -e "DB_NAME=$TEST_DATABASE" \
  -e RPC_HOST=api-cache-test-rpc api-cache-candidate:local
docker run --rm --network api-cache-test-network \
  -e TEST_MONGODB_URI=mongodb://api-cache-test-mongo:27017 \
  -e "TEST_DATABASE=$TEST_DATABASE" -e TEST_API_URL=http://api-cache-smoke-local:5000 \
  --mount "type=bind,source=$PWD,target=/work,readonly" \
  --entrypoint python api-cache-tests:local /work/tests/http_smoke.py
docker run --rm --network api-cache-test-network \
  -e DB_URL=api-cache-test-mongo -e "DB_NAME=${TEST_DATABASE}_daemon" \
  -e RPC_HOST=api-cache-test-rpc \
  --mount "type=bind,source=$PWD/tests,target=/checks,readonly" \
  --entrypoint python api-cache-candidate:local /checks/daemon_smoke.py
```

Coverage is reported for `db.query_cache`, not the whole application. The suite
covers query identity, expiry, concurrent upserts, write errors, ingestion,
generation changes and reorg checks. The HTTP smoke expects 20 repeated requests
to cause two address aggregations, then verifies fresh data after a generation
change. Synthetic tests do not replace real-chain integration or recovery drills.

The pull-request workflow runs these checks and uploads JUnit/coverage reports
and a candidate-image archive identified by source SHA and MongoDB test version,
retained for seven days.
The workflow has read-only repository permissions and does not publish images
to a registry or deploy them. See the separate [image publication workflow](image-publication.md)
for the master-branch registry publication contract and its versioning boundaries.

The separate [isolated recovery drill](recovery-drill.md) exercises actual
dump/restore and interrupted daemon processes. It does not certify an existing
production backup or the consistency of production application data.

## Deployment prerequisites

Use the [component backup and rollout procedure](backup-and-rollout.md) to
record the environment-specific gates. In particular, stopping a daemon does
not by itself prove a completed, consistent ingestion boundary.

1. Review and validate the API and daemon together on an isolated database.
   Exercise interrupted ingestion, reorg detection and the recovery procedure.
2. Verify existing blocks, UTXOs and balances against a canonical checkpoint.
   Neither a matching tip hash nor a successful subsequent update proves the
   consistency of historical data.
3. Confirm a backup and application-data-only recovery path. Do not restore
   `cache_state` or `query_cache_v2` from a backup.
4. Deploy the daemon before the API. A database without a ready marker bypasses
   caching until a complete new ingestion has published a ready generation.
5. Verify canonical tip, ready state and repeated-request behavior. Compare
   HTTP errors, worker timeouts, slow queries and database load with baseline.
   Leave legacy-cache deletion outside this rollout.

## Recovery and rollback

After an interrupted update or detected reorg, stop the affected daemon and
preserve diagnostic evidence and a backup. Verify or rebuild application data
from a canonical source in isolation. Do not reset the marker just to restart
ingestion, or assume that bypassing the cache makes inconsistent data safe.

Only after data consistency has been independently verified, with the daemon
still stopped, may the operator remove the `_id: "blockchain"` marker from
`cache_state`. Restart and verify a complete update publishes a fresh ready
generation. This is a prerequisite, not an automated data-recovery implementation.

Roll back the API first, confirm all API instances use the previous image, then
stop all new daemon instances. In the same controlled window, invalidate cache
state using the new image and the approved database configuration, before
starting an old daemon:

```bash
python - <<'PY'
import json
import os
from mongoengine import connect
from mongoengine.connection import get_db
from db.db import get_db_uri
from db.models import CacheState
from db.query_cache import invalidate
from pymongo.uri_parser import parse_uri

expected_name = os.environ['EXPECTED_DB_NAME']
expected_host = os.environ['EXPECTED_DB_HOST']
expected_port = os.environ['EXPECTED_DB_PORT']
expected_protocol = os.environ['EXPECTED_DB_PROTOCOL']
expected_replica_set = os.environ['EXPECTED_REPLICA_SET']
if (not all((expected_name, expected_host, expected_port, expected_protocol)) or
        os.environ['DB_NAME'] != expected_name or
        os.environ['DB_URL'] != expected_host or
        os.environ.get('DB_PORT', '27017') != expected_port or
        os.environ.get('DB_PROTOCOL', 'mongodb') != expected_protocol):
    raise SystemExit('Database identity does not match the approved rollback target')
uri = get_db_uri(primary_reads=True)
if parse_uri(uri)['options'].get('replicaSet', '') != expected_replica_set:
    raise SystemExit('Replica-set selection does not match the approved rollback target')
connect(host=uri)
database = get_db()
if database.name != expected_name:
    raise SystemExit('Connected database does not match the approved rollback target')
invalidate()
state = CacheState.objects.get(id='blockchain')
if state.ready is not False or state.writes_complete is not False:
    raise SystemExit('Cache invalidation could not be confirmed; do not restart the old daemon')
print(json.dumps({'database': database.name, 'server': database.client.address,
                  'ready': state.ready,
                  'writes_complete': state.writes_complete}))
PY
```

Set all `EXPECTED_*` values from the independently recorded, approved target;
use an explicitly empty `EXPECTED_REPLICA_SET` for a standalone target. Do not
derive these values automatically from the container defaults.
The command deliberately refuses missing or mismatched database settings. Check
the printed database name and disabled state against that record before
continuing. It must run in the selected environment with the selected writers
already stopped, not as part of the synthetic test drill.

This rotates the generation and clears the completed-write marker. If it fails,
stop the rollback rather than starting the old daemon. On a later re-upgrade,
verify application-data consistency and follow the recovery prerequisites before
removing the disabled marker. Do not reuse a ready generation across rollback.

**Do not run the new API with the old daemon:** the old
daemon does not invalidate generations and could leave cached results stale.
The previous code ignores the new collections but also lacks these safety
guards. Do not roll back merely to bypass a known data-integrity failure.

The [isolated version-transition drill](version-transition-drill.md) exercises
this ordering with legacy and candidate source code. Its negative control
deliberately omits invalidation to detect stale results on synthetic data; that
sequence is not a supported deployment procedure.

## Remaining availability and shutdown limits

RPC input proofs are resolved before a block's first write. If a classified RPC
failure prevents the next block from starting, the daemon finishes address
updates and canonical verification of the written prefix before retrying that
next block. Failed database writes remain fail-stop; the daemon does not replay
an uncertain partial block. Its connection enforces primary reads even when the
API URI requests secondary reads. Historical linkage is checked before new
application writes, not after them.

A detected canonical-chain conflict still disables readiness and requires
verified recovery. No fixed confirmation delay, automatic reorg rollback or
automatic restore has been introduced. Changing that policy needs separate
validation of explorer/API freshness and recovery semantics.

With readiness absent or disabled, the API bypasses the cache and can repeatedly
run expensive full-chain aggregations. This may amplify shared-database load
until recovery. Do not treat bypass as a bounded-load or safe-data guarantee;
verify the environment's request/concurrency limits and observation/abort plan
before rollout. Do not serve a stale generation just to hide the failure.

Graceful shutdown finishes the current block and address updates for the
written chunk; its duration is not bounded to the default container or
Kubernetes grace period. Rehearse the actual catch-up workload, RPC timeouts and
termination settings before relying on acknowledgement. A forced kill or
unfinished acknowledgement fails the consistency gate; increasing a timeout
without measurement is not proof of safe shutdown.
