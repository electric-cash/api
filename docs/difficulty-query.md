# Difficulty chart query

The difficulty chart needs block height and difficulty, not transaction bodies.
Its projection excludes `_id` and the unused `time` field. The additional
non-unique `height_1_difficulty_1` index can cover the height filter, height sort
and difficulty values used by the aggregation. The existing `height_1` index
is retained for other callers.

Bucket boundaries, range filters, averages, result fields and ordering are
unchanged. Transaction-volume queries, cache generation handling, ingestion
and application-data recovery are outside this change. The pipeline forms part
of the v2 cache key, so changed queries do not reuse entries from the old pipeline.

## Validation

`tests/test_difficulty_query.py` uses isolated synthetic blocks containing wide
transaction fields. It compares results with the previous pipeline and checks
the exact aggregation sent by the analyzer on a cold cache. The unhinted plan
must use the compound index, examine index keys and fetch zero block documents.
Tests cover week/month/all-shaped ranges, gaps, empty ranges, bucket boundaries,
a 1,027-boundary request and correctness when the compound index is absent.
The actual-image HTTP smoke also checks the chart values over cold and warm
requests and confirms from the isolated database profiler that the single
difficulty aggregation fetches no block documents.

Run the normal isolated test suite from [cache-hardening.md](cache-hardening.md).
The focused test selection is `tests/test_difficulty_query.py`. These tests
prove query semantics and a plan on their fixtures, not production CPU savings.
The query still scans the index entries in the requested height range; an
all-history request is not constant-time. Concurrent cold misses may still
duplicate computation.

## Deployment gate

This model change is not a passive index recommendation: MongoEngine may create
declared indexes at first collection access. Do not deploy it assuming index
creation can always be postponed. Review the target collection's size, existing
indexes, data types, free disk, memory and load first. Pre-build and verify the
exact index in an approved, observed maintenance step before replacing the
application image. Do not substitute a unique, sparse or partial index.

The model retains the existing background-index setting. It does not guarantee
a nonblocking build on every MongoDB version. Account for resource use and brief
locking; use the target version's supported build/abort procedures.

After creation, inspect an unhinted explain for the actual pipeline and data:
the presence of the index alone does not prove the planner selected it. Require
a nonempty indexed range, `totalDocsExamined: 0`, positive keys examined and no
`FETCH` for the difficulty aggregation. Compare response values, errors,
latencies and resource use before and after any approved rollout.

The existing [backup and rollout gates](backup-and-rollout.md) still apply to
the full API/daemon candidate. This query optimization does not repair balances,
certify historical data or allow the generation-aware API to run with an old
daemon that does not maintain that generation.

## Rollback

Reverting the difficulty projection does not require deleting the extra index.
Keeping an unused index temporarily avoids another immediate database change,
but still costs storage and write work. If removing it is necessary, first deploy
an application version that no longer declares it, then remove only the verified
`height_1_difficulty_1` index in a separately controlled operation. Otherwise an
application process can recreate it. Never remove the existing `height_1` index
or clear block/cache collections as part of this rollback.
