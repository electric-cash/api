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
3. Compare restored application documents and index definitions with the
   quiescent baseline. Reject missing indexes, changed data, and restored cache
   metadata. Also check the fixture's known balances, UTXOs, and canonical tip.
4. Kill an actual daemon subprocess with SIGKILL after a block write but before
   address updates. A restart must reject the unfinished batch without replaying
   it or marking its cache generation ready.
5. Use the independently verified restored database, not a readiness reset on
   the interrupted source. Kill its daemon after all writes complete, before
   canonical verification. A restart must verify the pending batch without
   duplicating balances or changing its generation.
6. Simulate a conflicting canonical tip. The daemon must invalidate readiness,
   preserve application data, and refuse another unverified restart.

The interruptions and fork response are controlled test injections. The MongoDB
server, dump/restore tools, application image, persisted state, and subprocess
termination are real. This is not production-scale load or live-chain recovery
validation.

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

The script refuses an external Docker network, a missing test-container network
attachment, a reused archive, or an invalid database prefix. The Python worker
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

Before a rollout, identify an actual backup, its age, storage location, access
controls, and restoration procedure. Demonstrate restoration to an isolated
environment and independently reconcile application data with a canonical
checkpoint. A dump from a running writer is not certified consistent by this
exercise: the synthetic baseline is deliberately quiescent.

Do not delete readiness markers or start an old daemon just to bypass a data
integrity failure. Follow the deployment and rollback ordering in
[the cache safety guide](cache-hardening.md#recovery-and-rollback). Passing this
drill does not authorize a production restore, cache deletion, or deployment.
