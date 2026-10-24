"""Exercise recovery in the real image, using only disposable local fixtures."""
import argparse
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from bson import json_util
from pymongo import MongoClient


CACHE_COLLECTIONS = frozenset(('cache_state', 'query_cache_v2', 'request_cache'))


def validate_environment(values):
    match = re.fullmatch(
        r'(api_cache_http_[A-Za-z0-9_]+)_recovery_(source|restored)',
        values.get('DB_NAME', ''))
    allowed = (
        match is not None and len(match.group(1) + '_recovery_restored') <= 63 and
        values.get('DB_URL') == 'api-cache-test-mongo' and
        values.get('DB_PROTOCOL', 'mongodb') == 'mongodb' and
        values.get('DB_PORT', '27017') == '27017' and
        values.get('RPC_HOST') == 'api-cache-test-rpc' and
        values.get('RPC_PORT', '8332') == '8332' and
        not any(values.get(key) for key in (
            'DB_USERNAME', 'DB_PASSWORD', 'DB_URI_PARAMS')))
    if not allowed:
        raise RuntimeError('Recovery drill requires isolated local services and fixture databases')
    return tuple(match.group(1) + '_recovery_' + role for role in ('source', 'restored'))


def application_digest(database):
    collections = []
    for name in sorted(database.list_collection_names()):
        if name in CACHE_COLLECTIONS:
            continue
        collection = database[name]
        indexes = {}
        for index_name, options in collection.index_information().items():
            indexes[index_name] = {
                key: options[key] for key in (
                    'key', 'unique', 'sparse', 'expireAfterSeconds',
                    'partialFilterExpression', 'collation') if key in options}
        collections.append({
            'name': name,
            'documents': list(collection.find().sort('_id', 1)),
            'indexes': indexes,
        })
    encoded = json_util.dumps(collections, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


def verify_restored_copy(source, restored):
    if CACHE_COLLECTIONS.intersection(restored.list_collection_names()):
        raise RuntimeError('Restored database must not contain cache collections')
    if application_digest(source) != application_digest(restored):
        raise RuntimeError('Restored application documents or indexes do not match the baseline')


def run_once(database, mode='normal', expected_error=None):
    env = dict(os.environ, DB_NAME=database)
    result = subprocess.run(
        [sys.executable, __file__, '--child', mode], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
    if expected_error is None:
        if result.returncode != 0:
            raise RuntimeError('Recovery worker failed: ' + result.stdout + result.stderr[-1500:])
    elif result.returncode != 2 or expected_error not in result.stdout:
        raise RuntimeError('Recovery worker did not reject ' + mode + ': ' + result.stdout)


def kill_at_checkpoint(database, mode):
    with tempfile.TemporaryDirectory(prefix='api-recovery-') as directory:
        checkpoint = Path(directory) / 'checkpoint'
        process = subprocess.Popen(
            [sys.executable, __file__, '--child', mode, '--checkpoint', str(checkpoint)],
            env=dict(os.environ, DB_NAME=database),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        try:
            deadline = time.monotonic() + 30
            while not checkpoint.exists():
                if process.poll() is not None:
                    stdout, stderr = process.communicate(timeout=5)
                    raise RuntimeError('Worker exited before checkpoint: ' + stdout + stderr[-1500:])
                if time.monotonic() >= deadline:
                    raise RuntimeError('Recovery checkpoint timed out')
                time.sleep(0.05)
            process.kill()
            process.communicate(timeout=10)
            if process.returncode != -9:
                raise RuntimeError('Recovery drill did not terminate the worker with SIGKILL')
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)


def child(mode, checkpoint):
    from mongoengine import connect, disconnect
    from db.db import get_db_uri
    from db.models import Address
    from db import query_cache
    import daemon

    def pause():
        Path(checkpoint).write_text('paused', encoding='ascii')
        while True:
            time.sleep(1)

    connect(host=get_db_uri(), serverSelectionTimeoutMS=5000)
    try:
        if mode == 'baseline':
            daemon.get_block_count = lambda: 0
        elif mode == 'partial':
            original = daemon.insert_block

            def insert_then_pause(*args, **kwargs):
                original(*args, **kwargs)
                pause()

            daemon.insert_block = insert_then_pause
        elif mode == 'complete':
            daemon._verify_completed_update = lambda *args: pause()
        elif mode == 'reorg':
            daemon.get_block_hash = lambda height: 'different-fixture-fork'
        daemon.update_analyzer()
        daemon.update_blockchain()
        if mode == 'baseline':
            query_cache.execute([{'$project': {'balance': 1}}], Address.objects,
                                'recovery-baseline', [])
    finally:
        disconnect()


def verify_fixture(database, height):
    from db.models import Address, Block, UTXO

    blocks = database[Block._get_collection_name()]
    addresses = database[Address._get_collection_name()]
    utxos = database[UTXO._get_collection_name()]
    assert blocks.count_documents({}) == height + 1
    assert blocks.find_one({'height': height})['_id'] == 'block-' + str(height)
    sender = addresses.find_one({'_id': 'sender'})
    assert Decimal(str(sender['balance'])) == (2 if height == 0 else 0)
    assert utxos.count_documents({}) == 1
    expected_utxo = 'origin0' if height == 0 else 'spend0'
    assert utxos.find_one({'_id': expected_utxo})['value'] == (2 if height == 0 else 3)
    if height == 1:
        assert Decimal(str(addresses.find_one({'_id': 'recipient'})['balance'])) == 3


def exercise(source, restored):
    verify_fixture(source, 0)
    verify_restored_copy(source, restored)
    verify_fixture(restored, 0)
    baseline = application_digest(source)

    kill_at_checkpoint(source.name, 'partial')
    state = source.cache_state.find_one({'_id': 'blockchain'})
    assert state['ready'] is False and state.get('writes_complete', False) is False
    interrupted = application_digest(source)
    assert interrupted != baseline
    run_once(source.name, expected_error='unfinished')
    assert application_digest(source) == interrupted
    assert source.cache_state.find_one({'_id': 'blockchain'})['ready'] is False

    # The restored database was checked before either interrupted write and is
    # separate from the damaged source; never reset the source's readiness flag.
    assert application_digest(restored) == baseline
    kill_at_checkpoint(restored.name, 'complete')
    verify_fixture(restored, 1)
    state = restored.cache_state.find_one({'_id': 'blockchain'})
    assert state['ready'] is False and state['writes_complete'] is True
    generation = state['generation']
    completed = application_digest(restored)
    run_once(restored.name)
    assert application_digest(restored) == completed
    state = restored.cache_state.find_one({'_id': 'blockchain'})
    assert state['ready'] is True and state['generation'] == generation
    verify_fixture(restored, 1)

    run_once(restored.name, mode='reorg', expected_error='canonical')
    assert application_digest(restored) == completed
    state = restored.cache_state.find_one({'_id': 'blockchain'})
    assert state['ready'] is False and state['writes_complete'] is False
    run_once(restored.name, expected_error='unfinished')
    return {
        'result': 'passed', 'baseline_application_sha256': baseline,
        'backup_documents_and_indexes': 'matched', 'cache_metadata_restored': False,
        'partial_write_restart': 'blocked_without_replay',
        'completed_write_restart': 'verified_without_double_balances',
        'reorg_restart': 'blocked_without_data_mutation',
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', nargs='?', choices=('prepare', 'exercise'))
    parser.add_argument('--child', choices=('normal', 'baseline', 'partial', 'complete', 'reorg'))
    parser.add_argument('--checkpoint')
    args = parser.parse_args()
    source_name, restored_name = validate_environment(os.environ)
    sys.path.insert(0, '/app')
    if args.child:
        if args.child in ('partial', 'complete') and not args.checkpoint:
            raise RuntimeError('An interruption checkpoint is required')
        try:
            child(args.child, args.checkpoint)
        except RuntimeError as error:
            print(json.dumps({'error': str(error)}))
            return 2
        return 0
    if args.phase is None:
        parser.error('prepare or exercise is required')
    client = MongoClient('mongodb://api-cache-test-mongo:27017', serverSelectionTimeoutMS=5000)
    try:
        source, restored = client[source_name], client[restored_name]
        if args.phase == 'prepare':
            if source.list_collection_names() or restored.list_collection_names():
                raise RuntimeError('Recovery fixture databases must be empty')
            run_once(source_name, 'baseline')
            verify_fixture(source, 0)
            assert source.query_cache_v2.count_documents({}) > 0
            source.request_cache.insert_one({'_id': 'legacy-fixture', 'result': []})
            result = {'result': 'prepared', 'application_sha256': application_digest(source)}
        else:
            result = exercise(source, restored)
        print(json.dumps(result, sort_keys=True))
    finally:
        client.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
