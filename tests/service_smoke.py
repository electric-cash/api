"""Run real daemon/API processes only against isolated synthetic recovery data."""
import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

import requests
from pymongo import MongoClient

from fake_rpc import BLOCKS, ORIGIN, SPEND
from recovery_smoke import (
    application_digest, require, validate_environment, verify_fixture, verify_restored_copy,
)


class FixtureRPC(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        method, params = payload.get('method'), payload.get('params', [])
        if method == 'getblockcount':
            self.server.height_requested.set()
            result = 2 if self.server.pause_spend else 1
        elif method == 'getnetworkhashps':
            result = 123
        elif method == 'getblockhash' and params and params[0] in (0, 1, 2):
            result = 'block-' + str(params[0])
        elif method == 'getblock' and params and params[0] in ('block-0', 'block-1', 'block-2'):
            if params[0] == 'block-1' and self.server.pause_spend:
                self.server.waiting.set()
                if not self.server.release.wait(15):
                    self.send_error(504)
                    return
            if params[0] == 'block-2':
                self.server.backlog_requested.set()
                result = {'hash': 'block-2', 'height': 2, 'previousblockhash': 'block-1',
                          'time': 3, 'difficulty': 1, 'tx': []}
            else:
                result = BLOCKS[int(params[0][-1])]
        elif method == 'getrawtransaction' and params and params[0] in ('origin', 'spend'):
            result = ORIGIN if params[0] == 'origin' else SPEND
        else:
            self.send_error(400)
            return
        body = json.dumps({'result': result, 'error': None, 'id': payload.get('id')}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


@contextmanager
def fixture_rpc(pause_spend):
    server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureRPC)
    server.pause_spend = pause_spend
    server.waiting = threading.Event()
    server.release = threading.Event()
    server.height_requested = threading.Event()
    server.backlog_requested = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def service(command, env):
    with tempfile.TemporaryFile(mode='w+t') as log:
        process = subprocess.Popen(command, cwd='/app', env=env,
                                   stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        try:
            yield process, log
        except Exception as error:
            log.seek(0)
            raise RuntimeError(str(error) + '\n' + log.read()[-2000:]) from error
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)


def wait_for(predicate, process):
    deadline = time.monotonic() + 20
    while not predicate():
        require(process.poll() is None, 'Service exited before reaching the checkpoint')
        require(time.monotonic() < deadline, 'Service checkpoint timed out')
        time.sleep(0.05)


def stop_daemon(database, env, rpc, mid_batch):
    with service([sys.executable, '/app/daemon.py'], env) as (process, log):
        wait_for(rpc.waiting.is_set if mid_batch else rpc.height_requested.is_set, process)
        if mid_batch:
            require(database.block.count_documents({}) == 1,
                    'Signal checkpoint must be after the first block write')
            state = database.cache_state.find_one({'_id': 'blockchain'})
            require(state is not None and state.get('ready') is False,
                    'Signal checkpoint must have an incomplete update')
        process.send_signal(signal.SIGTERM)
        if mid_batch:
            time.sleep(0.25)
            require(process.poll() is None, 'SIGTERM interrupted an incomplete batch')
            rpc.release.set()
        require(process.wait(timeout=20) == 0, 'Daemon did not exit successfully')
        log.seek(0)
        require('Daemon quiesced;' in log.read(), 'Daemon did not acknowledge quiescence')


def check_api(database, env, expect_cache_bypass):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    command = ['gunicorn', '--workers', '1',
               '--bind', '127.0.0.1:' + str(port), '--access-logfile', '-', 'api:app']
    url = 'http://127.0.0.1:' + str(port)
    with service(command, env) as (process, _):
        def ready():
            try:
                return requests.get(url + '/not-a-route', timeout=1).status_code == 404
            except requests.RequestException:
                return False

        wait_for(ready, process)
        for _ in range(2):
            response = requests.get(url + '/status', timeout=5)
            require(response.status_code == 200 and response.json() == {
                'height': 1, 'hashrate': 123, 'total_coin_supply': 3.0,
            }, 'Restored API status does not match the canonical fixture')
            response = requests.get(url + '/nonemptywalletsnumber', timeout=5)
            require(response.status_code == 200 and response.json() == 1,
                    'Restored API returned the wrong wallet count')
        if expect_cache_bypass:
            require(database.cache_state.count_documents({}) == 0,
                    'Idle startup must not manufacture a ready generation')
            require(database.query_cache_v2.count_documents({}) == 0,
                    'Missing generation must bypass caching')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('prepare', 'verify'))
    args = parser.parse_args()
    recovery_source, _ = validate_environment(os.environ)
    prefix = recovery_source[:-len('_recovery_source')]
    source_name, restored_name = prefix + '_service_source', prefix + '_service_restored'
    client = MongoClient('mongodb://api-cache-test-mongo:27017', serverSelectionTimeoutMS=5000)
    sys.path.insert(0, '/app')
    try:
        source, restored = client[source_name], client[restored_name]
        if args.phase == 'prepare':
            require(not source.list_collection_names() and not restored.list_collection_names(),
                    'Full-service recovery fixture databases must be empty')
            database = source
        else:
            verify_restored_copy(source, restored)
            verify_fixture(restored, 1)
            database = restored
        baseline = application_digest(database)
        with fixture_rpc(pause_spend=args.phase == 'prepare') as rpc:
            env = dict(os.environ, DB_NAME=database.name, RPC_HOST='127.0.0.1',
                       RPC_PORT=str(rpc.server_port))
            stop_daemon(database, env, rpc, mid_batch=args.phase == 'prepare')
            require(not rpc.backlog_requested.is_set(),
                    'Shutdown must not continue ingesting the remaining backlog')
            verify_fixture(database, 1)
            if args.phase == 'prepare':
                state = database.cache_state.find_one({'_id': 'blockchain'})
                require(state is not None and state.get('ready') is True,
                        'Completed shutdown must leave a verified generation')
            else:
                require(application_digest(database) == baseline,
                        'Restart replayed application writes')
            check_api(database, env, expect_cache_bypass=args.phase == 'verify')
        print(json.dumps({'phase': args.phase, 'result': 'passed',
                          'real_daemon_and_gunicorn': True,
                          'sigterm_during_batch': args.phase == 'prepare',
                          'restored_without_cache': args.phase == 'verify'}))
    finally:
        client.close()


if __name__ == '__main__':
    main()
