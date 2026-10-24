"""Offline smoke guard regressions; can run with pytest --noconftest."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


TESTS = Path(__file__).resolve().parent
ENVIRONMENT = {
    'daemon_smoke': {
        'DB_URL': 'api-cache-test-mongo',
        'DB_NAME': 'api_cache_http_guard_daemon',
        'RPC_HOST': 'api-cache-test-rpc',
    },
    'http_smoke': {
        'TEST_MONGODB_URI': 'mongodb://api-cache-test-mongo:27017',
        'TEST_DATABASE': 'api_cache_http_guard',
        'TEST_API_URL': 'http://api-cache-smoke-ci:5000',
    },
}
IMPORT_BOUNDARY = 91
READINESS_BOUNDARY = 92
REJECTED = 2
RUNNER = '''
import builtins
import json
import os
import runpy
import sys
from types import ModuleType

original_import = builtins.__import__

class FixtureClient:
    def __init__(self, uri, **options):
        self.uri = uri
        self.options = options

    def __getitem__(self, name):
        self.name = name
        return self

    def list_collection_names(self):
        print(json.dumps({'uri': self.uri, 'database': self.name, 'options': self.options}))
        sys.exit(92)

def guarded_import(name, *args, **kwargs):
    if sys.argv[2] == 'readiness' and name in ('pymongo', 'requests'):
        module = ModuleType(name)
        if name == 'pymongo':
            module.MongoClient = FixtureClient
        return module
    if name.split('.')[0] in (
            'pymongo', 'mongoengine', 'requests', 'db', 'daemon', 'blockchain'):
        sys.exit(91)
    return original_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
try:
    if sys.argv[2] == 'validate':
        namespace = runpy.run_path(sys.argv[1])
        print(json.dumps(namespace['validate_environment'](os.environ)))
    else:
        runpy.run_path(sys.argv[1], run_name='__main__')
except RuntimeError as error:
    print(str(error))
    sys.exit(2)
'''


def run_smoke(module, changes=None, missing=(), mode='main', optimized=False):
    # Do not inherit the parent's DB/RPC settings or pytest's MongoDB fixture.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('DB_', 'RPC_', 'TEST_', 'PYTHON'))}
    env.update(ENVIRONMENT[module])
    env.update(changes or {})
    for key in missing:
        env.pop(key, None)
    command = [sys.executable, '-S']
    if optimized:
        command.append('-O')
    path = str(TESTS / (module + '.py'))
    arguments = [path] if mode == 'script' else ['-c', RUNNER, path, mode]
    return subprocess.run(
        command + arguments, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True, timeout=10)


def assert_rejected(result):
    output = result.stdout + result.stderr
    assert 'guard-secret' not in output
    assert result.returncode == REJECTED, output
    assert 'isolated local services' in result.stdout
    assert not result.stderr


@pytest.mark.parametrize('key,value', [
    ('DB_URL', 'production.example'),
    ('DB_URL', 'api-cache-test-mongo.example'),
    ('DB_URL', 'api-cache-test-mongo:27017,production.example'),
    ('DB_PROTOCOL', 'mongodb+srv'),
    ('DB_PROTOCOL', ''),
    ('DB_PORT', '27018'),
    ('DB_PORT', '27017/other'),
    ('DB_PORT', ''),
    ('DB_USERNAME', 'guard-secret-user'),
    ('DB_PASSWORD', 'guard-secret-password'),
    ('DB_USERNAME', 'guard-secret@other.invalid:27017/path?query#fragment'),
    ('DB_PASSWORD', 'guard-secret@other.invalid:27017/path?query#fragment'),
    ('DB_URI_PARAMS', '?authSource=guard-secret'),
    ('RPC_HOST', 'real-node.example'),
    ('RPC_HOST', 'api-cache-test-rpc.example'),
    ('RPC_HOST', 'api-cache-test-rpc:8332/path'),
    ('RPC_PORT', '8333'),
    ('RPC_PORT', '8332/path'),
    ('RPC_PORT', ''),
    ('RPC_USER', 'guard-secret-user'),
    ('RPC_PASSWORD', 'guard-secret-password'),
    ('RPC_USER', 'guard-secret@other.invalid:8332/path?query#fragment'),
    ('RPC_PASSWORD', 'guard-secret@other.invalid:8332/path?query#fragment'),
    ('DB_NAME', 'blockchain'),
    ('DB_NAME', 'api_cache_http_guard'),
    ('DB_NAME', 'api_cache_http__daemon'),
    ('DB_NAME', 'api_cache_http_guard/other_daemon'),
    ('DB_NAME', 'api_cache_http_guard.daemon_daemon'),
    ('DB_NAME', 'api_cache_http_guard\n_daemon'),
    ('DB_NAME', 'api_cache_http_' + 'x' * 42 + '_daemon'),
    ('DB_NAME', ''),
])
def test_daemon_rejects_unsafe_settings_before_client_or_application_imports(key, value):
    assert_rejected(run_smoke('daemon_smoke', {key: value}))


@pytest.mark.parametrize('key,value', [
    ('TEST_MONGODB_URI', 'mongodb://production.example:27017'),
    ('TEST_MONGODB_URI', 'mongodb://api-cache-test-mongo.example:27017'),
    ('TEST_MONGODB_URI', 'mongodb+srv://api-cache-test-mongo'),
    ('TEST_MONGODB_URI', 'http://api-cache-test-mongo:27017'),
    ('TEST_MONGODB_URI', 'mongodb://guard-secret@api-cache-test-mongo:27017'),
    ('TEST_MONGODB_URI', 'mongodb://user:guard-secret@api-cache-test-mongo:27017'),
    ('TEST_MONGODB_URI', 'mongodb://api-cache-test-mongo:27018'),
    ('TEST_MONGODB_URI', 'mongodb://api-cache-test-mongo:'),
    ('TEST_MONGODB_URI', 'mongodb://api-cache-test-mongo:guard-secret'),
    ('TEST_MONGODB_URI', 'mongodb://api-cache-test-mongo:65536'),
    ('TEST_MONGODB_URI', 'mongodb://api-cache-test-mongo:27017/other'),
    ('TEST_MONGODB_URI', 'mongodb://api-cache-test-mongo:27017/?authSource=guard-secret'),
    ('TEST_MONGODB_URI', 'mongodb://api-cache-test-mongo:27017/#guard-secret'),
    ('TEST_MONGODB_URI', 'mongodb://api-cache-test-mongo:27017?'),
    ('TEST_MONGODB_URI', 'mongodb://api-cache-test-mongo:27017#'),
    ('TEST_MONGODB_URI', 'mongodb://api-cache-test-mongo:27017,production.example:27017'),
    ('TEST_MONGODB_URI', 'mongodb://[guard-secret'),
    ('TEST_MONGODB_URI', 'mongodb://api-cache-test-mongo:27017\n'),
    ('TEST_MONGODB_URI', ' mongodb://api-cache-test-mongo:27017'),
    ('TEST_MONGODB_URI', ''),
    ('TEST_API_URL', 'https://api-cache-smoke-ci:5000'),
    ('TEST_API_URL', 'file://api-cache-smoke-ci:5000'),
    ('TEST_API_URL', 'http://production.example:5000'),
    ('TEST_API_URL', 'http://api-cache-smoke-ci.example:5000'),
    ('TEST_API_URL', 'http://api-cache-smoke-:5000'),
    ('TEST_API_URL', 'http://api-cache-smoke-ci-:5000'),
    ('TEST_API_URL', 'http://api-cache-smoke-guard_secret:5000'),
    ('TEST_API_URL', 'http://guard-secret@api-cache-smoke-ci:5000'),
    ('TEST_API_URL', 'http://user:guard-secret@api-cache-smoke-ci:5000'),
    ('TEST_API_URL', 'http://api-cache-smoke-ci:'),
    ('TEST_API_URL', 'http://api-cache-smoke-ci:guard-secret'),
    ('TEST_API_URL', 'http://api-cache-smoke-ci:0'),
    ('TEST_API_URL', 'http://api-cache-smoke-ci:65536'),
    ('TEST_API_URL', 'http://api-cache-smoke-ci:5000/path'),
    ('TEST_API_URL', 'http://api-cache-smoke-ci:5000/?query=guard-secret'),
    ('TEST_API_URL', 'http://api-cache-smoke-ci:5000/#guard-secret'),
    ('TEST_API_URL', 'http://api-cache-smoke-ci:5000?'),
    ('TEST_API_URL', 'http://api-cache-smoke-ci:5000#'),
    ('TEST_API_URL', 'http://[guard-secret'),
    ('TEST_API_URL', 'http://api-cache-smoke-ci:5000\t'),
    ('TEST_API_URL', ' http://api-cache-smoke-ci:5000'),
    ('TEST_API_URL', ''),
    ('TEST_DATABASE', 'blockchain'),
    ('TEST_DATABASE', 'api_cache_http_'),
    ('TEST_DATABASE', 'api_cache_http_guard/other'),
    ('TEST_DATABASE', 'api_cache_http_guard.other'),
    ('TEST_DATABASE', 'api_cache_http_guard\n'),
    ('TEST_DATABASE', 'api_cache_http_' + 'x' * 49),
    ('TEST_DATABASE', ''),
])
def test_http_rejects_unsafe_settings_before_client_imports(key, value):
    assert_rejected(run_smoke('http_smoke', {key: value}))


@pytest.mark.parametrize('module,key', [
    (module, key) for module, values in ENVIRONMENT.items() for key in values
])
def test_missing_required_settings_are_rejected_without_key_or_parser_errors(module, key):
    assert_rejected(run_smoke(module, missing=(key,)))


@pytest.mark.parametrize('module', list(ENVIRONMENT))
def test_fixture_settings_reach_the_client_import_only_after_validation(module):
    result = run_smoke(module)
    assert result.returncode == IMPORT_BOUNDARY, result.stdout + result.stderr
    assert not result.stdout and not result.stderr


@pytest.mark.parametrize('module', list(ENVIRONMENT))
def test_real_entrypoints_select_only_the_fixture_before_mocked_database_readiness(module):
    result = run_smoke(module, mode='readiness')
    assert result.returncode == READINESS_BOUNDARY, result.stdout + result.stderr
    assert not result.stderr
    values = ENVIRONMENT[module]
    assert json.loads(result.stdout) == {
        'uri': 'mongodb://api-cache-test-mongo:27017',
        'database': values.get('DB_NAME', values.get('TEST_DATABASE')),
        'options': {'serverSelectionTimeoutMS': 5000},
    }


@pytest.mark.parametrize('changes', [
    {},
    {'DB_PROTOCOL': 'mongodb', 'DB_PORT': '27017', 'RPC_PORT': '8332'},
    {key: '' for key in ('DB_USERNAME', 'DB_PASSWORD', 'DB_URI_PARAMS',
                        'RPC_USER', 'RPC_PASSWORD')},
    {'DB_NAME': 'api_cache_http_' + 'x' * 41 + '_daemon'},
])
def test_daemon_validation_preserves_its_db_environment_contract(changes):
    result = run_smoke('daemon_smoke', changes, mode='validate')
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == changes.get('DB_NAME', ENVIRONMENT['daemon_smoke']['DB_NAME'])


@pytest.mark.parametrize('uri', [
    'mongodb://api-cache-test-mongo',
    'mongodb://api-cache-test-mongo/',
    'mongodb://api-cache-test-mongo:27017',
    'mongodb://api-cache-test-mongo:27017/',
])
@pytest.mark.parametrize('api', [
    'http://api-cache-smoke-ci:5000',
    'http://api-cache-smoke-local:5000/',
    'http://api-cache-smoke-ci',
    'http://api-cache-smoke-local:8080',
])
def test_http_validation_preserves_test_inputs_without_requiring_db_or_rpc_settings(uri, api):
    result = run_smoke('http_smoke', {'TEST_MONGODB_URI': uri, 'TEST_API_URL': api}, mode='validate')
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == [uri, api.rstrip('/'), ENVIRONMENT['http_smoke']['TEST_DATABASE']]


def test_http_accepts_a_fixture_database_at_the_mongodb_name_length_limit():
    name = 'api_cache_http_' + 'x' * 48
    result = run_smoke('http_smoke', {'TEST_DATABASE': name}, mode='validate')
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)[2] == name


@pytest.mark.parametrize('module,key', [
    ('daemon_smoke', 'RPC_PASSWORD'),
    ('http_smoke', 'TEST_MONGODB_URI'),
])
def test_optimized_python_cannot_skip_guards(module, key):
    assert_rejected(run_smoke(module, {key: 'guard-secret'}, optimized=True))


@pytest.mark.parametrize('module,key,value', [
    ('daemon_smoke', 'DB_USERNAME', 'guard-secret-user'),
    ('daemon_smoke', 'DB_PASSWORD', 'guard-secret-password'),
    ('daemon_smoke', 'DB_URI_PARAMS', '?authSource=guard-secret'),
    ('daemon_smoke', 'RPC_USER', 'guard-secret-user'),
    ('daemon_smoke', 'RPC_PASSWORD', 'guard-secret-password'),
    ('http_smoke', 'TEST_MONGODB_URI', 'mongodb://user:guard-secret@api-cache-test-mongo'),
    ('http_smoke', 'TEST_MONGODB_URI', 'mongodb://[guard-secret'),
    ('http_smoke', 'TEST_API_URL', 'http://user:guard-secret@api-cache-smoke-ci'),
    ('http_smoke', 'TEST_API_URL', 'http://[guard-secret'),
    ('http_smoke', 'TEST_API_URL', 'http://api-cache-smoke-ci:guard-secret'),
])
def test_direct_cli_rejections_do_not_echo_secrets_or_parser_exceptions(module, key, value):
    result = run_smoke(module, {key: value}, mode='script')
    output = result.stdout + result.stderr
    assert 'guard-secret' not in output
    assert result.returncode == 1, output
    assert 'RuntimeError:' in result.stderr
    assert 'isolated local services' in result.stderr
    assert 'ValueError:' not in result.stderr
    assert 'ModuleNotFoundError:' not in result.stderr
