from copy import deepcopy
from unittest.mock import Mock

import mongoengine
import pytest
from pymongo import ReadPreference
from pymongo.uri_parser import parse_uri

from blockchain.rpc import RPCError
from db import db, query_cache
from db.models import Address, Block, UTXO
from test_daemon_cache import daemon
from test_review_regressions import block, transaction


def application_documents():
    return {model.__name__: list(model.objects.order_by('id').as_pymongo())
            for model in (Address, Block, UTXO)}


@pytest.mark.parametrize('chunk_size', [1, 2000])
def test_later_block_rpc_failure_finishes_written_prefix_without_replay(daemon, monkeypatch, chunk_size):
    monkeypatch.setattr(daemon, 'MAX_CHUNK_SIZE', chunk_size)
    monkeypatch.setattr(daemon, 'get_block_count', lambda: 12)
    first = block(11, [transaction('first', [{'coinbase': '00'}], ['address-one'])])
    second = block(12, [transaction('second', [{'txid': 'first', 'vout': 0}], ['recipient'])])
    fetch = Mock(side_effect=[deepcopy(first), RPCError('Temporary next-block failure')])
    monkeypatch.setattr(daemon, 'get_block', fetch)

    with pytest.raises(RPCError, match='next-block'):
        daemon.update_blockchain()

    assert Block.objects.count() == 2
    assert Address.objects.get(hash='address-one').balance == 9
    assert query_cache.get_generation() not in (None, 'initial')
    monkeypatch.setattr(daemon, 'get_block', Mock(return_value=deepcopy(second)))
    daemon.update_blockchain()
    assert Block.objects.count() == 3
    assert Address.objects.get(hash='address-one').balance == 7
    assert Address.objects.get(hash='recipient').balance == 2


@pytest.mark.parametrize('chunk_size', [1, 2000])
def test_later_input_rpc_failure_retries_without_losing_written_prefix(daemon, monkeypatch, chunk_size):
    monkeypatch.setattr(daemon, 'MAX_CHUNK_SIZE', chunk_size)
    monkeypatch.setattr(daemon, 'get_block_count', lambda: 12)
    first = block(11, [transaction('first', [{'coinbase': '00'}], ['address-one'])])
    second = block(12, [transaction('second', [
        {'txid': 'first', 'vout': 0}, {'txid': 'untracked', 'vout': 0},
    ], ['recipient'])])
    monkeypatch.setattr(daemon, 'get_block', lambda height: deepcopy(first if height == 11 else second))
    previous = Mock(side_effect=RPCError('Temporary input failure'))
    monkeypatch.setattr(db, 'get_raw_transaction', previous)
    with pytest.raises(RPCError, match='input failure'):
        daemon.update_blockchain()
    assert query_cache.get_generation() not in (None, 'initial')
    assert Address.objects.get(hash='address-one').balance == 9
    assert UTXO.objects.get(id='first0').value == 2
    previous.side_effect = None
    previous.return_value = transaction('untracked', [{'coinbase': '00'}], [])
    daemon.update_blockchain()
    assert Block.objects.count() == 3
    assert Address.objects.get(hash='address-one').balance == 7
    assert Address.objects.get(hash='recipient').balance == 2
    assert previous.call_count == 2


def test_untracked_output_from_same_block_needs_no_rpc_lookup(daemon, monkeypatch):
    monkeypatch.setattr(daemon, 'get_block_count', lambda: 11)
    incoming = block(11, [
        transaction('untracked', [{'coinbase': '00'}], []),
        transaction('spend', [{'txid': 'untracked', 'vout': 0}], ['recipient']),
    ])
    monkeypatch.setattr(daemon, 'get_block', lambda height: deepcopy(incoming))
    previous = Mock(side_effect=AssertionError('same-block output must not need RPC'))
    monkeypatch.setattr(db, 'get_raw_transaction', previous)
    daemon.update_blockchain()
    previous.assert_not_called()
    assert Address.objects.get(hash='recipient').balance == 2
    assert query_cache.get_generation() not in (None, 'initial')


def test_previous_transaction_rpc_failure_precedes_all_block_mutation(daemon, monkeypatch):
    UTXO(id='tracked0', address='address-one', value=2, insert_time=1, update_time=1).save()
    incoming = block(11, [transaction('spend', [
        {'txid': 'tracked', 'vout': 0}, {'txid': 'missing', 'vout': 0},
    ], ['recipient'])])
    baseline = application_documents()
    monkeypatch.setattr(db, 'get_raw_transaction', Mock(side_effect=RPCError('Temporary input failure')))
    with pytest.raises(RPCError, match='input failure'):
        db.insert_block(incoming, {}, {})
    assert application_documents() == baseline


def test_input_rpc_failure_before_first_write_keeps_ready_generation(daemon, monkeypatch):
    incoming = block(11, [transaction('spend', [{'txid': 'missing', 'vout': 0}], ['recipient'])])
    monkeypatch.setattr(daemon, 'get_block_count', lambda: 11)
    monkeypatch.setattr(daemon, 'get_block', lambda height: deepcopy(incoming))
    monkeypatch.setattr(db, 'get_raw_transaction', Mock(side_effect=RPCError('Temporary input failure')))
    baseline = application_documents()
    with pytest.raises(RPCError, match='input failure'):
        daemon.update_blockchain()
    assert query_cache.get_generation() == 'initial'
    assert application_documents() == baseline


def test_historical_chain_failure_is_checked_before_first_new_block(daemon, monkeypatch):
    incoming = block(11, [transaction('new', [{'coinbase': '00'}], ['recipient'])])
    monkeypatch.setattr(daemon, 'get_block_count', lambda: 11)
    fetch = Mock(return_value=incoming)
    monkeypatch.setattr(daemon, 'get_block', fetch)
    monkeypatch.setattr(daemon, 'check_reorg', Mock(side_effect=RuntimeError('Historical linkage failure')))
    baseline = application_documents()
    with pytest.raises(RuntimeError, match='Historical linkage'):
        daemon.update_blockchain()
    fetch.assert_not_called()
    assert application_documents() == baseline


@pytest.mark.parametrize('parameters', [
    '?replicaSet=rs0&readPreference=secondaryPreferred&retryWrites=false',
    '?replicaSet=rs0&readpreference=nearest&readPreferenceTags=zone:backup&maxStalenessSeconds=90',
    '',
])
def test_daemon_connection_reads_primary_even_with_secondary_uri(daemon, monkeypatch, parameters):
    monkeypatch.setattr(db, 'db_para', parameters)
    original_uri = db.get_db_uri()
    clients = []
    uris = []

    def connect(**kwargs):
        uris.append(kwargs['host'])
        client = mongoengine.connect(alias='release_review_writer', connect=False, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(daemon, 'connect', connect)
    try:
        daemon.try_to_connect()
        assert db.get_db_uri() == original_uri
        assert clients[0].read_preference == ReadPreference.PRIMARY
        options = parse_uri(uris[0])['options']
        if parameters:
            assert options['replicaSet'] == 'rs0'
        assert 'readPreferenceTags' not in options
        assert 'maxStalenessSeconds' not in options
        if 'retryWrites' in parameters:
            assert options['retryWrites'] is False
    finally:
        mongoengine.disconnect(alias='release_review_writer')
