#!/usr/bin/env bash
set -euo pipefail

: "${TEST_DATABASE:?Set a disposable api_cache_http_ fixture prefix}"
TEST_NETWORK="${TEST_NETWORK:-api-cache-ci}"
API_IMAGE="${API_IMAGE:-api-candidate}"
if [[ ! "$TEST_DATABASE" =~ ^api_cache_http_[A-Za-z0-9_]+$ ]] ||
   (( ${#TEST_DATABASE} + 18 > 63 )); then
  echo 'Recovery drill requires a short disposable fixture prefix' >&2
  exit 1
fi
if [[ "$(docker network inspect "$TEST_NETWORK" --format '{{.Internal}}')" != true ]]; then
  echo 'Recovery drill requires an internal Docker network' >&2
  exit 1
fi
if [[ -z "$(docker inspect api-cache-test-mongo --format "{{with index .NetworkSettings.Networks \"$TEST_NETWORK\"}}{{.NetworkID}}{{end}}")" ]]; then
  echo 'Test MongoDB is not attached to the isolated network' >&2
  exit 1
fi

checks_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if command -v cygpath >/dev/null 2>&1; then
  checks_dir="$(cygpath -w "$checks_dir")"
  export MSYS_NO_PATHCONV=1
fi
source_db="${TEST_DATABASE}_recovery_source"
restored_db="${TEST_DATABASE}_recovery_restored"
archive="/tmp/${TEST_DATABASE}_recovery.archive.gz"
docker exec api-cache-test-mongo test ! -e "$archive"
run_phase() {
  docker run --rm --network "$TEST_NETWORK" \
    -e DB_URL=api-cache-test-mongo -e "DB_NAME=$source_db" \
    -e RPC_HOST=api-cache-test-rpc \
    --mount "type=bind,source=$checks_dir,target=/checks,readonly" \
    --entrypoint python "$API_IMAGE" /checks/recovery_smoke.py "$1"
}

run_phase prepare
docker exec api-cache-test-mongo mongodump --host=127.0.0.1 --port=27017 \
  --db="$source_db" --excludeCollection=cache_state \
  --excludeCollection=query_cache_v2 --excludeCollection=request_cache \
  --archive="$archive" --gzip
docker exec api-cache-test-mongo mongorestore --host=127.0.0.1 --port=27017 \
  --archive="$archive" --gzip --nsInclude="$source_db.*" \
  --nsFrom="$source_db.*" --nsTo="$restored_db.*" --stopOnError
run_phase exercise
docker exec api-cache-test-mongo rm -- "$archive"
