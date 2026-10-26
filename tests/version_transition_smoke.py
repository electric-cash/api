"""Exercise candidate/legacy ordering on disposable local data, never live services."""
import argparse
from contextlib import contextmanager
from decimal import Decimal
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading

import requests
from pymongo import MongoClient

from fake_rpc import BLOCKS, ORIGIN, SPEND, transaction
from recovery_smoke import application_digest, require, validate_environment, verify_fixture
from service_smoke import service, wait_for


LEGACY_ROOT = Path('/legacy')
CANDIDATE_ROOT = Path('/app')
REWARD = transaction('reward-2', [{'coinbase': '02'}], [
    {'n': 0, 'value': 4, 'scriptPubKey': {
        'addresses': ['recipient'], 'hex': '51', 'type': 'pubkeyhash'}},
])
CHAIN = list(BLOCKS) + [
    {'hash': 'block-2', 'height': 2, 'previousblockhash': 'block-1',
     'time': 3, 'difficulty': 1, 'tx': [REWARD]},
]


class TransitionRPC(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        method, params = payload.get('method'), payload.get('params', [])
        if method == 'getblockcount':
            result = self.server.height
        elif method == 'getnetworkhashps':
            result = 123
        elif method == 'getblockhash' and len(params) == 1 and params[0] in (0, 1, 2):
            result = CHAIN[params[0]]['hash']
        elif method == 'getblock' and params and params[0] in ('block-0', 'block-1', 'block-2'):
            result = CHAIN[int(params[0][-1])]
        elif method == 'getrawtransaction' and params and params[0] in ('origin', 'spend', 'reward-2'):
            result = {'origin': ORIGIN, 'spend': SPEND, 'reward-2': REWARD}[params[0]]
        else:
            self.send_error(400)
            return
        body = json.dumps({'result': result, 'error': None, 'id': payload.get('id')}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@contextmanager
def fixture_rpc():
    server = ThreadingHTTPServer(('127.0.0.1', 0), TransitionRPC)
    server.height = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def run_code(root, env, code, expected_error=None):
    result = subprocess.run([sys.executable, '-c', code], cwd=str(root), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=30)
    if expected_error is None:
        require(result.returncode == 0, 'Source worker failed: ' + result.stderr[-2000:])
    else:
        require(result.returncode != 0 and expected_error in result.stderr,
                'Source worker did not reject the expected unsafe state')


def ingest(root, env, expected_error=None):
    run_code(root, env, """
from mongoengine import connect, disconnect
from db.db import get_db_uri
import daemon
connect(host=get_db_uri(), serverSelectionTimeoutMS=5000)
try:
    daemon.update_analyzer()
    daemon.update_blockchain()
finally:
    disconnect()
""", expected_error)


@contextmanager
def api(root, env):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    url = 'http://127.0.0.1:' + str(port)
    command = ['gunicorn', '--chdir', str(root), '--workers', '1',
               '--bind', '127.0.0.1:' + str(port), 'api:app']
    with service(command, env) as (process, _):
        def ready():
            try:
                return requests.get(url + '/not-a-route', timeout=1).status_code == 404
            except requests.RequestException:
                return False
        wait_for(ready, process)
        yield url


def check_status(url, height, supply):
    response = requests.get(url + '/status', timeout=5)
    require(response.status_code == 200 and response.json() == {
        'height': height, 'hashrate': 123, 'total_coin_supply': float(supply)},
        'Unexpected API status: ' + response.text[:300])


def validate_sources():
    result = {}
    for label, root in [('legacy', LEGACY_ROOT), ('candidate', CANDIDATE_ROOT)]:
        files = sorted(root.rglob('*.py'))
        require(files and (root / 'daemon.py').is_file() and (root / 'api.py').is_file(),
                'Both reviewed source trees must be mounted')
        digest = hashlib.sha256()
        for path in files:
            require(not path.is_symlink(), 'Symlink in source tree')
            digest.update(str(path.relative_to(root)).encode() + b'\0')
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        result[label] = digest.hexdigest()
    require(result['legacy'] != result['candidate'], 'Source trees must differ')
    return result


def exercise(database, env, omit_invalidation):
    with fixture_rpc() as rpc:
        env = dict(env, RPC_HOST='127.0.0.1', RPC_PORT=str(rpc.server_port))
        ingest(LEGACY_ROOT, env)
        verify_fixture(database, 0)
        require(database.cache_state.count_documents({}) == 0, 'Legacy source created cache state')
        with api(CANDIDATE_ROOT, env) as url:
            for _ in range(2):
                check_status(url, 0, 2)
        require(database.query_cache_v2.count_documents({}) == 0, 'Missing state did not bypass cache')

        rpc.height = 1
        ingest(CANDIDATE_ROOT, env)
        verify_fixture(database, 1)
        require(database.cache_state.find_one({'_id': 'blockchain'})['ready'] is True,
                'Candidate did not complete its generation')
        with api(CANDIDATE_ROOT, env) as url:
            for _ in range(2):
                check_status(url, 1, 3)
        require(database.query_cache_v2.count_documents({}) > 0, 'Candidate cache was not populated')
        cached = database.query_cache_v2.count_documents({})

        # API rollback precedes invalidation and the first legacy writer update.
        with api(LEGACY_ROOT, env) as url:
            check_status(url, 1, 3)
            baseline = application_digest(database)
            if not omit_invalidation:
                run_code(CANDIDATE_ROOT, env, """
from mongoengine import connect, disconnect
from db.db import get_db_uri
from db.query_cache import invalidate
connect(host=get_db_uri(), serverSelectionTimeoutMS=5000)
try:
    invalidate()
finally:
    disconnect()
""")
                require(application_digest(database) == baseline, 'Invalidation changed application data')
                require(database.cache_state.find_one({'_id': 'blockchain'})['ready'] is False,
                        'Rollback did not disable the old generation')
            rpc.height = 2
            ingest(LEGACY_ROOT, env)
            check_status(url, 2, 7)

        require(database.block.count_documents({}) == 3, 'Legacy update lost blocks')
        recipient = database.address.find_one({'_id': 'recipient'})
        require(Decimal(str(recipient['balance'])) == 7, 'Legacy update has wrong balance')
        require(database.u_t_x_o.count_documents({}) == 2, 'Legacy update has wrong UTXOs')
        with api(CANDIDATE_ROOT, env) as url:
            check_status(url, 2, 3 if omit_invalidation else 7)
        require(database.query_cache_v2.count_documents({}) == cached,
                'Unexpected cache publication across the rollback')
        if not omit_invalidation:
            baseline = application_digest(database)
            ingest(CANDIDATE_ROOT, env, expected_error='unfinished')
            require(application_digest(database) == baseline, 'Rejected restart changed application data')
        return {'result': 'passed', 'legacy_to_candidate': 'verified',
                'missing_generation': 'bypasses_cache',
                'rollback_invalidation': not omit_invalidation,
                'stale_value_reproduced': omit_invalidation,
                'candidate_restart_after_rollback': 'requires_verified_recovery' if not omit_invalidation
                    else 'unsafe_order_negative_control',
                'legacy_height_after_rollback': 2, 'actual_supply': 7,
                'candidate_reported_supply': 3 if omit_invalidation else 7}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('scenario', choices=('safe-rollback', 'negative-control'))
    args = parser.parse_args()
    source_name, _ = validate_environment(os.environ)
    sources = validate_sources()
    sys.path.insert(0, str(CANDIDATE_ROOT))
    client = MongoClient('mongodb://api-cache-test-mongo:27017', serverSelectionTimeoutMS=5000)
    try:
        database = client[source_name]
        require(not database.list_collection_names(), 'Transition fixture must be empty')
        result = exercise(database, dict(os.environ, DB_NAME=source_name),
                          args.scenario == 'negative-control')
        result.update(source_sha256=sources, scenario=args.scenario,
                      runtime='candidate dependencies for both source trees; not the historical image')
        print(json.dumps(result, sort_keys=True))
    finally:
        client.close()


if __name__ == '__main__':
    main()
