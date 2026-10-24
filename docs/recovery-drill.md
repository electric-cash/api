# Isolated backup and recovery drill

This exercise runs the candidate application's actual daemon code against
synthetic RPC fixtures and disposable MongoDB databases. It does not connect to
a live chain, export production data, publish an image, or deploy an application.
It is a recovery-mechanism test, not evidence that a production backup exists.

## What it verifies

1. Ingest a known first block and populate derived cache entries.
2. Run real `mongodump` and `mongorestore`, restoring application collections
   into a different, empty fixture database. Exclude `cache_state`,
   `query_cache_v2`, and the legacy `request_cache`.
3. Compare restored application BSON documents, collection options, and index definitions with the
   quiescent baseline. Reject missing indexes, changed data, and restored cache
   metadata. Also check the fixture's known balances, UTXOs, and canonical tip.
4. Kill an actual ingestion-worker subprocess with SIGKILL after a block write but before
   address updates. A restart must reject the unfinished batch without replaying
   it or marking its cache generation ready.
5. Use the independently verified restored database, not a readiness reset on
   the interrupted source. Kill its ingestion worker after all writes complete, before
   canonical verification. A restart must verify the pending batch without
   duplicating balances or changing its generation.
6. Simulate a conflicting canonical tip. The daemon must invalidate readiness,
   preserve application data, and refuse another unverified restart.
7. Start the actual `python /app/daemon.py` process against an empty fixture
   database. Pause the synthetic RPC response after the first block is written,
   send SIGTERM, and verify the process remains alive until the response is
   released and the written block/address prefix and verification finish. A
   further fixture block remains available but must not be fetched after SIGTERM.
8. Require a successful daemon exit and quiescence acknowledgement, check the
   known balances/UTXOs, then start real gunicorn and verify HTTP responses.
9. Dump that completed database and restore to a separate, empty database
   without cache metadata. Start the full daemon and gunicorn again; verify
   unchanged application data, correct HTTP responses and cache bypass without
   fabricating a ready generation when no new blocks arrive.

The interruptions and fork response are controlled test injections. The MongoDB
server, dump/restore tools, application image, persisted state, and subprocess
termination are real. This is not production-scale load or live-chain recovery
validation. The SIGKILL workers import the application's actual
`update_analyzer()` and `update_blockchain()` functions. The separate
`service_smoke.py` checks run the normal daemon entry point and gunicorn as
subprocesses inside the application image. Their synthetic RPC server listens
only on container loopback. This is process-level lifecycle coverage, not a
test of Kubernetes termination grace periods or production orchestration.

## Run it

Use Bash and the application/test images and RPC fixture described in
[Query cache and ingestion safety](cache-hardening.md). Create the fixture
network with `docker network create --internal`, expose no host ports, and name
the MongoDB container `api-cache-test-mongo`. The RPC fixture must be reachable
only as `api-cache-test-rpc` on the same network.

After those fixtures are ready, run from the repository root:

```bash
TEST_NETWORK=api-cache-test-network \
TEST_DATABASE=api_cache_http_recovery_local \
API_IMAGE=api-cache-candidate:local \
bash tests/recovery_smoke.sh
```

The script refuses an external Docker network, extra fixture-container network
attachments, published ports, a reused archive, or an invalid database prefix. The Python worker
also rejects real database/RPC hostnames, database credentials, URI options, and
names outside its fixture pair. A reused, nonempty source or restore database
is rejected; choose a fresh prefix rather than deleting unknown data.

The archive stays inside the disposable MongoDB container and is removed after
a successful exercise. The fixture databases remain there for inspection until
the operator or CI removes that test container. Do not run these commands
against existing infrastructure or substitute production connection strings.

The pull-request workflow includes the exercise on MongoDB 4.2.24 and 6.0.13.
Its existing teardown removes the disposable database/RPC containers and network.

## Production gate remains separate

Complete the [component backup and rollout gates](backup-and-rollout.md),
including a verified writer-quiescence method. This drill does not provide an
operator pause protocol for the legacy daemon; only the new candidate handles
graceful shutdown requests. The first transition from the legacy version still
requires an independently verified consistency/recovery method.

Before a rollout, identify an actual backup, its age, storage location, access
controls, and restoration procedure. Demonstrate restoration to an isolated
environment and independently reconcile application data with a canonical
checkpoint. A dump from a running writer is not certified consistent by this
exercise: the synthetic baseline is deliberately quiescent.

Do not delete readiness markers or start an old daemon just to bypass a data
integrity failure. Follow the deployment and rollback ordering in
[the cache safety guide](cache-hardening.md#recovery-and-rollback). Passing this
drill does not authorize a production restore, cache deletion, or deployment.
