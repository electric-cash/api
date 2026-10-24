# ELCASH backup and controlled rollout

Status: draft component procedure. The gates below must be completed for a
specific environment before execution. This document is not a production change
approval or evidence that a usable backup already exists.

Scope: one ELCASH API database/network at a time. It is not a whole-system
backup of other applications sharing MongoDB. Do not collect wallet seeds,
private keys, PINs, or users' local wallet databases.

## 1. Establish the source and recovery target

Record the exact cluster, namespace, database, node/network identity, image
digests, source/GitOps revisions, MongoDB topology/version and backup-tool
version. Inventory every writer, including ingestion, API-side writes, jobs and
other consumers. A writer list must come from the actual environment, not only
the expected deployment manifest.

Choose an approved encrypted destination outside the source storage failure
boundary. Verify backup and recovery-role access without exposing credentials.
Record retention, a recovery point objective and an observation window agreed
with the service owner. Do not invent service-level guarantees or enable paid
provider resources as a side effect of this procedure.

Stop if the source identity, authorized destination, writer inventory, access,
capacity or recovery method is unknown. A successful upload is not proof that
the archive can be restored.

## 2. Establish application consistency before the dump

The daemon writes blocks and UTXOs before finishing the corresponding address
updates. An interruption between those operations can leave an inconsistent
application state. It has no operator pause/acknowledgement protocol that proves
a completed ingestion boundary. A stopped process, an idle database sample, a
matching tip hash or a successful filesystem snapshot does not establish that
balances and UTXOs are consistent.

Do not use `scale 0`, SIGTERM or SIGKILL as the consistency proof. First define
and rehearse an operator-approved way to stop new work at a completed boundary,
or a recovery/rebuild procedure that independently validates the captured state.
If this cannot be demonstrated, stop the rollout rather than certifying a dump
as consistent. Preserve any diagnostic copy with its limitations clearly marked.

For standalone MongoDB, do not assume an oplog-based online backup is available.
`mongodump --oplog` requires a replica-set member and a full-member dump; it
cannot be combined with a database-scoped dump. Do not convert the production
topology during an incident just to enable that option.

Once the chosen consistency method is approved and rehearsed, pause only the
identified writers for the selected application database. Do not lock the
shared MongoDB server or pause unrelated applications without separate review.
Record the completed checkpoint and distinguish application consistency from
storage-engine consistency.

## 3. Capture the recovery archive

Capture the selected database's application collections and index definitions
using backup tools compatible with the server. Exclude `cache_state`,
`query_cache_v2` and `request_cache` from the application recovery archive; they
must not certify or populate a restored generation. Do not delete the original
collections as part of taking a backup.

Use deliberately bounded collection parallelism, start with one collection at
a time, and monitor impact on shared services. Set an abort condition before
starting. Do not write the only archive onto the source data volume. Protect
credentials from process listings and logs; do not place passwords in command
arguments, Git or tickets.

Record archive size, checksum, start/end time, source checkpoint and storage
object identity. Verify the protected copy is readable by the recovery role.
Resume original writers only according to the approved consistency procedure;
do not start replacement writers concurrently.

## 4. Restore and validate in isolation

Restore into a new database on the approved isolated target. Do not overwrite
production or delete the original database. Verify target identity before every
restore and prevent unintended external side effects or automated cutover.

Compare application documents, BSON types, collection options and index
definitions with the recorded baseline. Independently check balances, UTXOs
and the canonical chain checkpoint. A byte-for-byte copy preserves existing
inconsistency too, so a matching archive checksum or tip is insufficient.

Run the candidate ingestion and API against this restored database. Test pending
verification, interrupted writes, a conflicting tip and the recovery path.
Measure resynchronization and restoration rather than assuming they fit the
recovery objective. The [synthetic drill](recovery-drill.md) verifies mechanisms
but does not replace this real-data rehearsal or full service-startup checks.

## 5. Deploy one network and observe

Only after recovery validation, verify the approved image pair and GitOps
change. Confirm the old writer is safely quiesced; deploy the candidate daemon
before the candidate API. Never run the new API with the old daemon, or two
ingestion writers for one database. Observe a complete update and ready
generation before completing the API rollout.

Compare error rate, request latency, cache hit behavior, canonical-tip progress
and database load over the agreed window. Shared-server CPU can remain high
because other applications are unchanged. Keep legacy cache cleanup, unrelated
runtime upgrades and CDN configuration changes outside this rollout.

## 6. Distinguish code rollback from data recovery

Reverting an image does not undo database writes. Follow the code rollback order
in [the cache safety guide](cache-hardening.md#recovery-and-rollback), including
generation invalidation before restarting an old daemon. Do not roll back merely
to bypass an integrity error.

If application data is inconsistent, stop the affected writers, preserve the
evidence and restore/rebuild into an isolated database. Independently validate
that state before a separately approved cutover. Define how any blocks received
since the archive checkpoint will be replayed. Never reset readiness on damaged
data, overwrite production as a shortcut, or leave old and restored writers
running together.

## Required completion record

Record source/target identities and revisions; approved consistency method and
writer inventory; archive/object identity and checksum; restoration checks;
observed service behavior; rollback and replay limits; unresolved risks; and the
owner's decision. Retain the working backup before scheduling any cleanup.
After this rollout, establish recurring protected backups and restore tests
against the agreed recovery objectives.
