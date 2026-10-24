from copy import deepcopy
import uuid

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
