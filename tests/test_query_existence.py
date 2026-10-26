from copy import deepcopy

import pytest
from pymongo.collection import Collection

from blockchain import blockchain_analyzer
from db import db, db_queries
from db.models import Address, Block


@pytest.fixture
def large_documents(chain_data):
    Block._get_collection().update_one(
        {'_id': 'block-10'}, {'$set': {'tx': [{
            'txid': 'synthetic', 'hash': 'x' * 262144, 'vin': [], 'vout': [],
        }]}})
    Address._get_collection().update_one(
        {'_id': 'address-one'}, {'$set': {'txs': ['x' * 262144]}})


@pytest.fixture
def application_finds(monkeypatch):
    recorded = []
    original = Collection.find
    names = {Block._get_collection().full_name, Address._get_collection().full_name}

    def record(collection, *args, **kwargs):
        if collection.full_name in names:
            recorded.append((collection.name, deepcopy(kwargs)))
        return original(collection, *args, **kwargs)

    monkeypatch.setattr(Collection, 'find', record)
    return recorded


def test_status_probes_do_not_load_transaction_payloads(
        large_documents, application_finds, address_aggregations, monkeypatch):
    monkeypatch.setattr(blockchain_analyzer, 'get_hashrate', lambda: 123)
    analyzer = blockchain_analyzer.BlockchainAnalyzer(blockchain=db.get_blockchain())

    for _ in range(2):
        assert analyzer.status() == {
            'height': 10, 'hashrate': 123, 'total_coin_supply': 7.0,
        }

    assert len(address_aggregations) == 1
    assert len(application_finds) == 4
    for _, options in application_finds:
        assert options.get('projection') == {'_id': 1}
        assert not options.get('sort')


def test_status_does_not_hydrate_unused_documents(large_documents, monkeypatch):
    def unexpected_hydration(*args, **kwargs):
        raise AssertionError('Status does not need a hydrated block or address')

    monkeypatch.setattr(Block, '_from_son', unexpected_hydration)
    monkeypatch.setattr(Address, '_from_son', unexpected_hydration)
    monkeypatch.setattr(blockchain_analyzer, 'get_hashrate', lambda: 123)
    analyzer = blockchain_analyzer.BlockchainAnalyzer(blockchain=db.get_blockchain())
    assert analyzer.status() == {
        'height': 10, 'hashrate': 123, 'total_coin_supply': 7.0,
    }


@pytest.mark.parametrize('projection', ['all', 'only', 'exclude'])
def test_probe_preserves_caller_query_and_aggregate_result(
        large_documents, application_finds, projection):
    queryset = db.get_addresses().filter(hash='address-one').order_by('-balance')
    if projection == 'only':
        queryset = queryset.only('balance', 'txs')
    elif projection == 'exclude':
        queryset = queryset.exclude('pk')
    original_fields = deepcopy(queryset._loaded_fields.as_dict())
    original_query = deepcopy(queryset._query)
    original_order = deepcopy(queryset._ordering)

    assert db.execute_query(db_queries.get_total_coin_supply_query(), queryset) == [
        {'total_coin_supply': 7.0}]
    assert queryset._loaded_fields.as_dict() == original_fields
    assert queryset._query == original_query
    assert queryset._ordering == original_order
    assert application_finds[0][1].get('projection') == {'_id': 1}
    assert not application_finds[0][1].get('sort')


@pytest.mark.parametrize('source', ['none', 'list', 'empty', 'filtered', 'skipped'])
def test_empty_sources_preserve_default_and_do_not_fill_cache(
        chain_data, clean_database, source):
    choices = {
        'none': None,
        'list': [],
        'empty': Address.objects(hash='missing'),
        'filtered': Address.objects(balance__gt=100),
        'skipped': Address.objects.skip(10),
    }
    default = ['Not enough data']
    assert db.execute_query(
        db_queries.get_total_coin_supply_query(), choices[source],
        type='sum', params=(), default=default) is default
    assert clean_database.query_cache_v2.count_documents({}) == 0


def test_empty_queryset_without_default_still_raises():
    with pytest.raises(Exception, match='empty collection'):
        db.execute_query(db_queries.get_total_coin_supply_query(), db.get_addresses())


def test_nonempty_filtered_query_uses_its_own_documents(chain_data):
    Address(hash='address-two', balance=11, insert_time=1, update_time=1).save()
    assert db.execute_query(
        db_queries.get_total_coin_supply_query(), Address.objects(hash='address-two')) == [
        {'total_coin_supply': 11.0}]
