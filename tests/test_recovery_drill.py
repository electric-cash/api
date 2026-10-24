from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
import uuid

from bson.int64 import Int64
import pytest

from recovery_smoke import application_digest, validate_environment, verify_restored_copy


def environment():
    return {
        'DB_URL': 'api-cache-test-mongo',
        'DB_NAME': 'api_cache_http_drill_recovery_source',
        'RPC_HOST': 'api-cache-test-rpc',
    }


@pytest.mark.parametrize('key,value', [
    ('DB_URL', 'production.example'),
    ('DB_URL', 'api-cache-test-mongo.example'),
    ('DB_PROTOCOL', 'mongodb+srv'),
    ('DB_PORT', '27018'),
    ('DB_USERNAME', 'real-user'),
    ('DB_PASSWORD', 'not-a-fixture'),
    ('DB_URI_PARAMS', '?authSource=admin'),
    ('DB_NAME', 'blockchain'),
    ('DB_NAME', 'api_cache_http_drill_recovery_source/other'),
    ('DB_NAME', 'api_cache_http_drill'),
    ('DB_NAME', 'api_cache_http_' + 'x' * 33 + '_recovery_source'),
    ('RPC_HOST', 'real-node.example'),
    ('RPC_PORT', '8333'),
])
def test_recovery_drill_refuses_nonisolated_settings(key, value):
    values = environment()
    values[key] = value
    with pytest.raises(RuntimeError, match='isolated'):
        validate_environment(values)


def test_recovery_drill_accepts_only_its_fixture_database_pair():
    assert validate_environment(environment()) == (
        'api_cache_http_drill_recovery_source',
        'api_cache_http_drill_recovery_restored',
    )


@pytest.fixture
def copies(connection):
    prefix = 'api_cache_tests_' + uuid.uuid4().hex
    source = connection[prefix + '_source']
    restored = connection[prefix + '_restored']
    try:
        source.address.insert_one({'_id': 'sender', 'balance': '2.00000000'})
        source.address.create_index('balance')
        restored.address.insert_many([deepcopy(row) for row in source.address.find()])
        restored.address.create_index('balance')
        yield source, restored
    finally:
        connection.drop_database(source.name)
        connection.drop_database(restored.name)


def test_application_digest_ignores_derived_cache_metadata(copies):
    source, restored = copies
    source.cache_state.insert_one({'_id': 'blockchain', 'ready': True})
    source.query_cache_v2.insert_one({'_id': 'stale', 'result': []})
    source.request_cache.insert_one({'_id': 'legacy', 'result': []})
    assert application_digest(source) == application_digest(restored)
    verify_restored_copy(source, restored)


@pytest.mark.parametrize('collection', ['cache_state', 'query_cache_v2', 'request_cache'])
def test_restored_copy_must_not_reuse_cache_metadata(copies, collection):
    source, restored = copies
    restored[collection].insert_one({'_id': 'stale'})
    with pytest.raises(RuntimeError, match='cache'):
        verify_restored_copy(source, restored)


def test_restored_copy_rejects_changed_application_data(copies):
    source, restored = copies
    restored.address.update_one({'_id': 'sender'}, {'$set': {'balance': '9.00000000'}})
    with pytest.raises(RuntimeError, match='application'):
        verify_restored_copy(source, restored)


def test_restored_copy_rejects_a_missing_application_index(copies):
    source, restored = copies
    restored.address.drop_index('balance_1')
    with pytest.raises(RuntimeError, match='application'):
        verify_restored_copy(source, restored)


def test_restored_copy_rejects_changed_bson_integer_types(copies):
    source, restored = copies
    source.address.update_one({'_id': 'sender'}, {'$set': {'counter': Int64(7)}})
    restored.address.update_one({'_id': 'sender'}, {'$set': {'counter': 7}})
    with pytest.raises(RuntimeError, match='application'):
        verify_restored_copy(source, restored)


def test_restored_copy_rejects_changed_embedded_document_order(copies):
    source, restored = copies
    source.address.update_one({'_id': 'sender'}, {'$set': {'ordered': {'a': 1, 'b': 2}}})
    restored.address.update_one({'_id': 'sender'}, {'$set': {'ordered': {'b': 2, 'a': 1}}})
    with pytest.raises(RuntimeError, match='application'):
        verify_restored_copy(source, restored)


def test_restored_copy_rejects_changed_text_index_options(copies):
    source, restored = copies
    source.address.create_index([('description', 'text')], weights={'description': 2})
    restored.address.create_index([('description', 'text')], weights={'description': 3})
    with pytest.raises(RuntimeError, match='application'):
        verify_restored_copy(source, restored)


def test_restored_copy_rejects_changed_collection_options(copies):
    source, restored = copies
    restored.command('collMod', 'address', validator={'balance': {'$type': 'string'}})
    with pytest.raises(RuntimeError, match='application'):
        verify_restored_copy(source, restored)


def test_optimized_python_must_not_skip_fixture_verification():
    root = Path(__file__).resolve().parents[1]
    code = '''
import sys
from unittest.mock import MagicMock
from recovery_smoke import verify_fixture
database = MagicMock()
database.__getitem__.return_value.count_documents.return_value = 999
try:
    verify_fixture(database, 0)
except RuntimeError:
    sys.exit(0)
sys.exit(1)
'''
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(root / 'src'), str(root / 'tests'))))
    result = subprocess.run([sys.executable, '-O', '-c', code], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    assert result.returncode == 0, result.stderr.decode()


def test_stopped_worker_check_rejects_data_changed_before_failure(copies):
    from recovery_smoke import verify_stopped_state

    source, _ = copies
    source.cache_state.insert_one({'_id': 'blockchain', 'ready': False, 'writes_complete': False})
    expected = application_digest(source)
    source.address.delete_one({'_id': 'sender'})
    with pytest.raises(RuntimeError, match='application'):
        verify_stopped_state(source, expected)


@pytest.mark.parametrize('field', ['ready', 'writes_complete'])
def test_stopped_worker_check_rejects_state_changed_before_failure(copies, field):
    from recovery_smoke import verify_stopped_state

    source, _ = copies
    source.cache_state.insert_one({'_id': 'blockchain', 'ready': False, 'writes_complete': False})
    expected = application_digest(source)
    source.cache_state.update_one({'_id': 'blockchain'}, {'$set': {field: True}})
    with pytest.raises(RuntimeError, match='readiness'):
        verify_stopped_state(source, expected)
