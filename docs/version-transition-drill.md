# Isolated version-transition drill

This drill executes two different, reviewed source trees against synthetic
three-block data in disposable MongoDB. It verifies cache behavior during an
upgrade and rollback without connecting to a live chain or changing a deployment.

## Scenarios and expected results

Both scenarios first ingest block 0 with the legacy daemon. The candidate API
must bypass caching when no generation exists. The candidate daemon then ingests
block 1 and publishes a ready generation; the candidate API warms that cache.

| Scenario | Rollback action | Expected result after the legacy daemon ingests block 2 |
| --- | --- | --- |
| `safe-rollback` | Stop the candidate API, use the legacy API, and invalidate with the candidate code before restarting the legacy writer | Legacy API reports supply 7. A diagnostic candidate API restart bypasses the disabled cache and also reports 7. Candidate daemon restart refuses the unfinished marker without changing application data. |
| `negative-control` | Deliberately omit invalidation in the same isolated sequence | Legacy API reports supply 7, but the diagnostic candidate API restart reuses the stale generation and reports 3. The test must detect this stale result to pass. |

The numbers are fixture values, not live chain statistics. A passing negative
control demonstrates why the omitted step is unsafe; it does not approve that
sequence for deployment. Even in the safe scenario, restarting the candidate API
with a legacy writer is a diagnostic probe only, not a supported mixed deployment.

The safe scenario also checks that invalidation and the rejected daemon restart
leave application data unchanged. It does not clear the disabled marker or
perform an automatic second upgrade. That requires the independent consistency
verification described in [Recovery and rollback](cache-hardening.md#recovery-and-rollback).

## Run locally

Use Bash and Docker with a local daemon. Build the candidate image using the
[cache test instructions](cache-hardening.md#run-the-tests). Provide an absolute
path to `src` in a separate, clean checkout of the reviewed legacy revision;
do not change the checkout while the test runs. Record the legacy commit, candidate
commit and image ID alongside the output. Do not mount secrets or real data.

The following creates its own internal network and fresh MongoDB container.
The guard refuses an existing container with the same name rather than reusing
or deleting it. No host ports are published. Run from the candidate repository root:

```bash
set -euo pipefail
: "${LEGACY_SOURCE:?Set an absolute path to the reviewed legacy src directory}"
API_IMAGE="${API_IMAGE:-api-cache-candidate:local}"
TEST_NETWORK=api-cache-transition-network
if docker container inspect api-cache-test-mongo >/dev/null 2>&1; then
  echo 'A MongoDB fixture already exists; do not reuse it for this drill' >&2
  exit 1
fi
docker image inspect "$API_IMAGE" --format '{{.Id}}'
NETWORK_ID=$(docker network create --internal "$TEST_NETWORK")
MONGO_ID=''
cleanup() {
  if [ -n "$MONGO_ID" ]; then
    docker rm -fv "$MONGO_ID" >/dev/null 2>&1 || true
  fi
  docker network rm "$NETWORK_ID" >/dev/null 2>&1 || true
}
trap cleanup EXIT
MONGO_ID=$(docker run --detach --name api-cache-test-mongo \
  --network "$NETWORK_ID" --memory 1g --cpus 2 \
  mongo:6.0.13 --bind_ip_all --quiet --wiredTigerCacheSizeGB 0.25)
ready=false
for attempt in $(seq 1 30); do
  if docker exec "$MONGO_ID" mongosh --quiet --eval 'db.adminCommand({ping: 1})' >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done
[ "$ready" = true ] || { echo 'MongoDB did not become ready' >&2; exit 1; }
for scenario in safe-rollback negative-control; do
  suffix=${scenario//-/_}
  docker run --rm --network "$NETWORK_ID" --memory 512m --cpus 1 \
    -e DB_URL=api-cache-test-mongo \
    -e "DB_NAME=api_cache_http_${suffix}_recovery_source" \
    -e RPC_HOST=api-cache-test-rpc \
    --mount "type=bind,source=$PWD/tests,target=/checks,readonly" \
    --mount "type=bind,source=$LEGACY_SOURCE,target=/legacy,readonly" \
    --entrypoint python "$API_IMAGE" /checks/version_transition_smoke.py "$scenario"
done
```

`RPC_HOST=api-cache-test-rpc` is the required fixture-only preflight value. The
worker replaces it with its own loopback-only synthetic RPC server before
starting application subprocesses; this drill needs no external RPC container.
The environment guard rejects other database/RPC hosts, credentials, URI
options and non-fixture database names. A nonempty fixture database is refused.
Each successful scenario emits JSON including source-tree fingerprints and
`result: "passed"`. A failed assertion exits nonzero. The shell cleanup removes
only the MongoDB container and network created by this invocation.

## Evidence limits

- Ingestion calls the actual `update_analyzer()` and `update_blockchain()` from
  each source tree; HTTP checks use actual gunicorn workers. This is not a mock
  of the application's cache implementation.
- Both source trees run with the candidate image's pinned Python dependencies.
  This is **not** a test of the historical deployed image or dependency set.
  Source fingerprints identify the files exercised, not a deployed release.
- The three-block fixture cannot establish historical balance correctness,
  production throughput, concurrent-writer safety or Kubernetes rollout behavior.
- This manual drill is separate from the existing CI recovery drill. It does not
  fetch a legacy checkout or run automatically in the pull-request workflow.

Complete the [backup and rollout gates](backup-and-rollout.md) separately,
including an identifiable release artifact, verified backup, actual legacy-writer
quiescence method and measured staging behavior. Passing this drill does not
authorize a deployment, production cache invalidation or removal of safety markers.
