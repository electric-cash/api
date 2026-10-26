"""Bounded, synthetic /status benchmark; never a production load generator."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import threading
import time
from unittest.mock import patch
import uuid

from fake_rpc import BLOCKS
from source_fingerprint import source_digest


ROOTS = {'legacy': Path('/legacy'), 'candidate': Path('/app')}
LEGACY_REVISION = 'ba36f5ee710fd81e35931e0c2a181863a3114121'
LEGACY_SHA256 = 'f1cb7b0c387637a150756777426e56165d287aa98671f6a80338982e7c13bff2'
MONGO_URI = 'mongodb://api-cache-test-mongo:27017'
WORKER_TIMEOUT = 180


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def validate_environment(values, source=None):
    name = values.get('DB_NAME', '')
    require(
        re.fullmatch(r'api_cache_http_[A-Za-z0-9_]+', name) is not None and
        len(name if source else name + '_candidate') <= 63 and
        values.get('DB_URL') == 'api-cache-test-mongo' and
        values.get('DB_PROTOCOL', 'mongodb') == 'mongodb' and
        values.get('DB_PORT', '27017') == '27017' and
        values.get('RPC_HOST') == 'api-cache-test-rpc' and
        values.get('RPC_PORT', '8332') == '8332' and
        not any(values.get(key) for key in (
            'DB_USERNAME', 'DB_PASSWORD', 'DB_URI_PARAMS', 'RPC_USER', 'RPC_PASSWORD')),
        'Benchmark requires isolated local services and fixture databases')
    if source:
        require(source in ROOTS and name.endswith('_' + source), 'Invalid worker database')
    return name


def validate_limits(requests, concurrency):
    require(1 <= requests <= 100 and 1 <= concurrency <= 4, 'Benchmark bounds exceeded')


def validate_sources(candidate_sha256):
    result = {name: source_digest(root) for name, root in ROOTS.items()}
    require(result['legacy'] == LEGACY_SHA256, 'Legacy source does not match ba36f5e')
    require(result['candidate'] == candidate_sha256 and candidate_sha256 != LEGACY_SHA256,
            'Unexpected candidate source fingerprint')
    return result


def fixture_documents():
    return {
        'block': [dict({'_id': block['hash'], 'tx': [], 'insert_time': 1, 'update_time': 1},
                       **{key: block[key] for key in ('height', 'time', 'difficulty')})
                  for block in BLOCKS],
        'address': [{'_id': 'synthetic-{:04d}'.format(index), 'balance': index % 10 + 1,
                     'txs': [], 'insert_time': 1, 'update_time': 1} for index in range(1000)],
    }


def fixture_digest(database):
    from bson import BSON

    digest = hashlib.sha256()
    for name in ('address', 'block'):
        digest.update(name.encode() + b'\0')
        for document in database[name].find().sort('_id', 1):
            digest.update(BSON.encode(document))
    return digest.hexdigest()


def validate_owner(name, token):
    require(re.fullmatch(r'api_cache_http_[A-Za-z0-9_]+_(legacy|candidate)', name) is not None
            and len(name) <= 63 and re.fullmatch(r'[a-f0-9]{32}', token) is not None,
            'Invalid owned benchmark database or token')


def cleanup_owned(client, name, token):
    validate_owner(name, token)
    markers = list(client[name].list_collections(filter={'name': 'benchmark_owner'}))
    if (len(markers) == 1 and
            markers[0].get('options', {}).get('validator') == {'_benchmark_owner': token}):
        client.drop_database(name)


@contextmanager
def owned_database(client, name, token):
    validate_owner(name, token)
    database = client[name]
    require(not database.list_collection_names(), 'Benchmark database must be unused')
    # Fixed name prevents competing claims; options persist ownership atomically.
    database.create_collection('benchmark_owner', validator={'_benchmark_owner': token})
    try:
        yield database
    finally:
        cleanup_owned(client, name, token)


class CommandCounts:
    """Counts started driver commands, not documents, query plans or CPU work."""
    def __init__(self, database):
        self.database = database
        self.lock = threading.Lock()
        self.active = False

    def begin(self):
        with self.lock:
            self.commands, self.aggregates = Counter(), Counter()
            self.active = True

    def started(self, event):
        with self.lock:
            if self.active and event.database_name == self.database:
                self.commands[event.command_name] += 1
                if event.command_name == 'aggregate':
                    self.aggregates[str(event.command['aggregate'])] += 1

    def succeeded(self, event):
        pass

    def failed(self, event):
        pass

    def end(self):
        with self.lock:
            self.active = False
            return {'started_commands': dict(self.commands),
                    'aggregate_commands_by_collection': dict(self.aggregates)}


def timing_summary(seconds):
    ordered = sorted(seconds)
    return {'samples': len(ordered), 'median_ms': statistics.median(ordered) * 1000,
            'p95_ms': ordered[math.ceil(len(ordered) * 0.95) - 1] * 1000}


def request_status(app, expected):
    with app.test_client() as client:
        started = time.perf_counter()
        response = client.get('/status')
        elapsed = time.perf_counter() - started
        require(response.status_code == 200 and response.get_json() == expected,
                'Unexpected /status response (error or stale result)')
    return elapsed


def measure_phase(app, counts, expected, requests, concurrency, clear=None):
    validate_limits(requests, concurrency)
    require(clear is None or concurrency == 1, 'Cold phase must be sequential')
    counts.begin()
    try:
        if concurrency == 1:
            timings = []
            for _ in range(requests):
                if clear:
                    clear()
                timings.append(request_status(app, expected))
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                timings = list(pool.map(lambda _: request_status(app, expected), range(requests)))
    finally:
        operations = counts.end()
    return {'wall': timing_summary(timings), 'mongo': operations,
            'concurrency': min(requests, concurrency),
            'result': expected, 'all_results_equal': True}


def synthetic_rpc(rpc):
    require(rpc.method == 'getnetworkhashps' and rpc.params == [],
            'Unexpected RPC; only synthetic hashrate is allowed')
    return 123


def exercise(app, database, counts, source, requests, concurrency):
    expected = {'height': 1, 'hashrate': 123, 'total_coin_supply': 5500}
    if source == 'candidate':
        from db import query_cache
        database.cache_state.insert_one({'_id': 'blockchain', 'generation': 'benchmark-initial',
                                         'ready': True})
    baseline = fixture_digest(database)

    def clear():
        database.request_cache.delete_many({})
        database.query_cache_v2.delete_many({})

    phases = {}
    for phase, parallelism, reset in (
            ('cold', 1, clear), ('warm', 1, None), ('concurrent_warm', concurrency, None)):
        phases[phase] = measure_phase(app, counts, expected, requests, parallelism, reset)
    require(fixture_digest(database) == baseline, 'Read phases changed application documents')

    if source == 'candidate':
        query_cache.invalidate()
        require(database.cache_state.find_one({'_id': 'blockchain'})['ready'] is False,
                'Invalidation did not disable the generation')
    else:
        clear()
    database.address.update_one({'_id': 'synthetic-0000'}, {'$set': {'balance': 11}})
    expected = dict(expected, total_coin_supply=5510)
    updated = fixture_digest(database)
    require(updated != baseline, 'Freshness fixture did not change')
    phases['post_invalidation_unready'] = measure_phase(app, counts, expected, requests, 1)
    if source == 'candidate':
        state = database.cache_state.find_one({'_id': 'blockchain'})
        # Synthetic data only: this is not evidence of daemon recovery correctness.
        query_cache.complete_update(state['generation'])
    phases['post_invalidation_ready'] = measure_phase(app, counts, expected, requests, 1)
    require(fixture_digest(database) == updated, 'Freshness reads changed application documents')
    return {'phases': phases, 'fixture_sha256': baseline, 'updated_fixture_sha256': updated}


def worker(source, name, token, requests, concurrency):
    from mongoengine import connect, disconnect
    from mongoengine.connection import get_db
    from pymongo import MongoClient, monitoring
    import pkg_resources

    class Listener(CommandCounts, monitoring.CommandListener):
        pass

    counts = Listener(name)
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000,
                         connectTimeoutMS=5000, socketTimeoutMS=5000)
    try:
        with owned_database(client, name, token) as database:
            for collection, documents in fixture_documents().items():
                database[collection].insert_many(documents)
            require(not any(key in sys.modules for key in ('api', 'db', 'blockchain')),
                    'Worker already imported application modules')
            sys.path.insert(0, str(ROOTS[source]))
            with patch('requests.sessions.Session.request',
                       side_effect=RuntimeError('HTTP disabled in benchmark')):
                rpc = importlib.import_module('blockchain.rpc')
                with patch.object(rpc.RPC, 'execute', synthetic_rpc):
                    api = importlib.import_module('api')
                    for key, module in list(sys.modules.items()):
                        if key == 'api' or key.split('.')[0] in ('db', 'blockchain'):
                            require(ROOTS[source].resolve() in Path(module.__file__).resolve().parents,
                                    'Application module outside selected source tree')
                    disconnect()
                    connect(host=MONGO_URI + '/' + name, connect=False,
                            serverSelectionTimeoutMS=5000, connectTimeoutMS=5000,
                            socketTimeoutMS=5000, maxPoolSize=4, event_listeners=[counts])
                    require(get_db().name == name, 'Wrong application database')
                    models = importlib.import_module('db.models')
                    for model in ('Address', 'Block', 'RequestCache', 'CacheState', 'QueryCacheEntry'):
                        if hasattr(models, model):
                            getattr(models, model).ensure_indexes()
                    api.app.testing = True
                    result = exercise(api.app, database, counts, source, requests, concurrency)
            result.update(source=source, database=name, runtime={
                'python': sys.version, 'executable': sys.executable,
                'packages': {dist.project_name: dist.version for dist in pkg_resources.working_set}},
                mongo_version=client.server_info()['version'])
        return result
    finally:
        disconnect()
        client.close()


def run_worker(source, environment, candidate_sha256, requests, concurrency, token):
    env = {key: value for key, value in environment.items()
           if key not in ('PYTHONPATH', 'PYTHONHOME')}
    env.update(DB_NAME=environment['DB_NAME'] + '_' + source, PYTHONDONTWRITEBYTECODE='1')
    command = [sys.executable, '-B', str(Path(__file__).resolve()), 'worker', '--source', source,
               '--token', token, '--candidate-sha256', candidate_sha256,
               '--requests', str(requests), '--concurrency', str(concurrency)]
    result = subprocess.run(command, env=env, cwd=str(ROOTS[source]),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=WORKER_TIMEOUT)
    require(result.returncode == 0, 'Benchmark worker failed: ' + result.stderr[-2000:])
    return json.loads(result.stdout)


def compare_workers(legacy, candidate):
    for key in ('fixture_sha256', 'updated_fixture_sha256', 'runtime'):
        require(legacy[key] == candidate[key], 'Worker inputs differ: ' + key)
    require(isinstance(candidate['phases'], dict), 'Worker results differ')
    require({name: phase['result'] for name, phase in legacy['phases'].items()} ==
            {name: phase['result'] for name, phase in candidate['phases'].items()},
            'Worker results differ')
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('coordinator', 'worker'))
    parser.add_argument('--candidate-sha256', required=True)
    parser.add_argument('--requests', type=int, default=20)
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--source', choices=tuple(ROOTS))
    parser.add_argument('--token')
    args = parser.parse_args()
    if args.mode == 'worker' and (not args.source or not args.token):
        parser.error('worker requires --source and --token')
    name = validate_environment(os.environ, args.source if args.mode == 'worker' else None)
    validate_limits(args.requests, args.concurrency)
    sources = validate_sources(args.candidate_sha256)
    if args.mode == 'worker':
        result = worker(args.source, name, args.token, args.requests, args.concurrency)
    else:
        from pymongo import MongoClient

        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000,
                             connectTimeoutMS=5000, socketTimeoutMS=5000)
        try:
            for source in ROOTS:
                require(not client[name + '_' + source].list_collection_names(),
                        'Both benchmark databases must be unused')
            workers = {}
            for source in ROOTS:
                token = uuid.uuid4().hex
                try:
                    workers[source] = run_worker(source, os.environ, args.candidate_sha256,
                                                 args.requests, args.concurrency, token)
                finally:
                    # Also covers a killed/timed-out worker, never a pre-existing database.
                    cleanup_owned(client, name + '_' + source, token)
            result = {'result': 'passed', 'workers': workers,
                      'equal_results_and_inputs': compare_workers(workers['legacy'], workers['candidate'])}
        finally:
            client.close()
    require(validate_sources(args.candidate_sha256) == sources, 'Source changed during benchmark')
    result.update(source_sha256_lf=sources, legacy_revision=LEGACY_REVISION,
                  measurement='in-process Flask test client /status; not real HTTP or CPU',
                  fixture_application_documents=1002, requests_per_phase=args.requests)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
