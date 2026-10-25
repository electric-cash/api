from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mongoengine.errors import NotUniqueError
from pymongo.errors import OperationFailure

from db import db, query_cache
from db.models import Address, Block, CacheState, QueryCacheEntry, ScriptPubKey, Tx, UTXO, Vin, Vout
from test_daemon_cache import daemon


def test_legacy_reorg_check_invalidates_without_restoring(daemon, monkeypatch):
    token = query_cache.begin_update()
    restore = Mock()
    monkeypatch.setattr(daemon, 'restore_db', restore)
    monkeypatch.setattr(daemon.analyzer, 'check_chain', lambda size: False)

    with pytest.raises(RuntimeError, match='recovery required'):
        daemon.check_reorg(1)

    restore.assert_not_called()
    assert Block.objects.count() == 1
    assert query_cache.get_generation() is None
    with pytest.raises(RuntimeError, match='generation changed'):
        query_cache.complete_update(token)
    with pytest.raises(RuntimeError, match='unfinished'):
        daemon.update_blockchain()


@pytest.mark.parametrize('reorg', [False, True])
@pytest.mark.parametrize('pending', [False, True])
def test_restore_is_retired_and_cannot_resurrect_ready_state(daemon, monkeypatch, reorg, pending):
    token = query_cache.begin_update() if pending else 'initial'
    drop = Mock()
    restore = Mock()
    backup = Mock()
    shell = Mock()
    monkeypatch.setattr(daemon, 'drop_db', drop)
    monkeypatch.setattr(daemon, 'restore', restore, raising=False)
    monkeypatch.setattr(daemon, 'backup', backup)
    monkeypatch.setattr(daemon, 'os', SimpleNamespace(popen=shell), raising=False)

    with pytest.raises(RuntimeError, match='restore.*disabled.*recovery required'):
        daemon.restore_db(reorg)

    for operation in (drop, restore, backup, shell):
        operation.assert_not_called()
    assert Block.objects.count() == 1
    assert Address.objects.first().balance == 7
    assert query_cache.get_generation() is None
    with pytest.raises(RuntimeError, match='generation changed'):
        query_cache.complete_update(token)
    with pytest.raises(RuntimeError, match='unfinished'):
        daemon.update_blockchain()


@pytest.mark.parametrize('pending', [False, True])
def test_drop_invalidates_before_deleting_and_preserves_disabled_marker(
        daemon, monkeypatch, pending):
    token = query_cache.begin_update() if pending else 'initial'
    original = Block.drop_collection
    states = []

    def drop_blocks():
        states.append(query_cache.get_generation())
        original()

    monkeypatch.setattr(Block, 'drop_collection', drop_blocks)
    db.drop_db()

    assert states == [None]
    assert Block.objects.count() == 0
    assert Address.objects.count() == 0
    assert CacheState.objects.count() == 1
    assert query_cache.get_generation() is None
    with pytest.raises(RuntimeError, match='generation changed'):
        query_cache.complete_update(token)
    with pytest.raises(RuntimeError, match='unfinished'):
        daemon.update_blockchain()


def test_drop_aborts_before_deletion_if_invalidation_fails(daemon, monkeypatch):
    drops = []
    for model in (Block, Address, UTXO):
        operation = Mock()
        drops.append(operation)
        monkeypatch.setattr(model, 'drop_collection', operation)
    monkeypatch.setattr(query_cache, 'invalidate', Mock(side_effect=OperationFailure('state write failed')))

    with pytest.raises(OperationFailure, match='state write failed'):
        db.drop_db()

    for operation in drops:
        operation.assert_not_called()
    assert Block.objects.count() == 1


def test_restore_aborts_if_invalidation_fails(daemon, monkeypatch):
    drop = Mock()
    monkeypatch.setattr(daemon, 'drop_db', drop)
    monkeypatch.setattr(query_cache, 'invalidate', Mock(side_effect=OperationFailure('state write failed')))
    with pytest.raises(OperationFailure, match='state write failed'):
        daemon.restore_db()
    drop.assert_not_called()
    assert Block.objects.count() == 1


def test_all_chunks_finish_address_updates_before_publication(daemon, monkeypatch):
    monkeypatch.setattr(daemon, 'MAX_CHUNK_SIZE', 1)
    monkeypatch.setattr(daemon, 'get_block_count', lambda: 12)
    monkeypatch.setattr(daemon, 'get_block', lambda height: {
        'hash': 'block-' + str(height), 'height': height,
        'previousblockhash': 'block-' + str(height - 1),
    })
    addresses_updated = []
    original_complete = query_cache.complete_update

    def insert(block, new, deleted, before_write):
        before_write()
        assert query_cache.get_generation() is None
        height = int(block['hash'].split('-')[1])
        Block(hash=block['hash'], previousblockhash='block-' + str(height - 1),
              height=height, time=1, difficulty=1, insert_time=1, update_time=1).save()

    def update(new, deleted):
        assert query_cache.get_generation() is None
        addresses_updated.append(True)

    def complete(token):
        assert len(addresses_updated) == 3
        assert Block.objects.order_by('-height').first().height == 12
        original_complete(token)

    monkeypatch.setattr(daemon, 'insert_block', insert)
    monkeypatch.setattr(daemon, 'update_addresses', update)
    monkeypatch.setattr(query_cache, 'complete_update', complete)
    daemon.update_blockchain()
    assert query_cache.get_generation() not in (None, 'initial')


def test_elcash_address_update_keeps_transaction_history(daemon):
    txid = 'a' * 64
    new = {
        'one': UTXO(id=txid + '0', address='address-one', value=1),
        'two': UTXO(id=txid + '1', address='address-one', value=2),
    }
    deleted = {'old': UTXO(id='old0', address='address-one', value=2)}
    daemon.update_addresses(new, deleted)
    address = Address.objects.first()
    assert address.balance == 8
    assert address.txs == [txid]


@pytest.mark.parametrize('model', [Block, UTXO])
def test_block_and_utxo_write_conflicts_propagate(chain_data, monkeypatch, model):
    transaction = Tx(txid='new-tx', hash='new-tx', vin=[Vin(coinbase='coinbase')],
                     vout=[Vout(n=0, value=1, scriptPubKey=ScriptPubKey(addresses=['address-one']))])
    block = Block(hash='block-11', previousblockhash='block-10', height=11,
                  time=1, difficulty=1, insert_time=1, update_time=1, tx=[transaction])
    monkeypatch.setattr(db, 'create_block', lambda incoming: block)
    monkeypatch.setattr(model, 'save', Mock(side_effect=NotUniqueError('write conflict')))
    with pytest.raises(NotUniqueError, match='write conflict'):
        db.insert_block({'hash': 'block-11'}, {}, {})
    assert Block.objects(hash='block-11').count() == 0


def test_begin_update_failure_prevents_block_mutation(daemon, monkeypatch):
    monkeypatch.setattr(daemon, 'get_block_count', lambda: 11)
    monkeypatch.setattr(daemon, 'get_block', lambda height: {
        'hash': 'block-11', 'height': height, 'previousblockhash': 'block-10',
    })
    monkeypatch.setattr(query_cache, 'begin_update', Mock(side_effect=OperationFailure('state write failed')))
    mutation = Mock()

    def insert(block, new, deleted, before_write):
        before_write()
        mutation()

    monkeypatch.setattr(daemon, 'insert_block', insert)
    with pytest.raises(OperationFailure, match='state write failed'):
        daemon.update_blockchain()
    mutation.assert_not_called()
    assert Block.objects.count() == 1


def test_real_ingestion_failure_cannot_publish_or_resume(daemon, monkeypatch):
    monkeypatch.setattr(db, 'get_raw_transaction', lambda txid: {
        'txid': txid, 'vout': [{'n': 0, 'scriptPubKey': {'addresses': ['address-one']}}],
    })
    transaction = Tx(txid='new-tx', hash='new-tx', vin=[Vin(txid='missing', vout=0)])
    block = Block(hash='block-11', height=11, time=1, difficulty=1,
                  insert_time=1, update_time=1, tx=[transaction])
    monkeypatch.setattr(daemon, 'get_block_count', lambda: 11)
    monkeypatch.setattr(daemon, 'get_block', lambda height: {
        'hash': 'block-11', 'height': height, 'previousblockhash': 'block-10',
    })
    monkeypatch.setattr(db, 'create_block', lambda incoming: block)
    with pytest.raises(UTXO.DoesNotExist):
        daemon.update_blockchain()
    assert query_cache.get_generation() is None
    assert QueryCacheEntry.objects.count() == 0
    with pytest.raises(RuntimeError, match='unfinished'):
        daemon.update_blockchain()


def test_next_chunk_must_extend_previously_fetched_block_before_insert(daemon, monkeypatch):
    monkeypatch.setattr(daemon, 'MAX_CHUNK_SIZE', 1)
    monkeypatch.setattr(daemon, 'get_block_count', lambda: 12)
    inserted = []

    def get_block(height):
        return {'hash': 'block-' + str(height), 'height': height,
                'previousblockhash': 'block-10' if height == 11 else 'other-block-11'}

    def insert(block, new, deleted, before_write):
        before_write()
        inserted.append(block['height'])
        Block(hash=block['hash'], height=block['height'], previousblockhash=block['previousblockhash'],
              time=1, difficulty=1, insert_time=1, update_time=1).save()

    monkeypatch.setattr(daemon, 'get_block', get_block)
    monkeypatch.setattr(daemon, 'insert_block', insert)
    monkeypatch.setattr(daemon, 'update_addresses', lambda *args: None)
    with pytest.raises(RuntimeError, match='canonical'):
        daemon.update_blockchain()
    assert inserted == [11]
    assert query_cache.get_generation() is None
