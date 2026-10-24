import signal
from unittest.mock import Mock

import pytest

from blockchain.rpc import RPCError
from db import query_cache
from db.models import Address, Block
from test_daemon_cache import daemon


@pytest.fixture
def shutdown_control(daemon, monkeypatch):
    previous = {signal.SIGTERM: object(), signal.SIGINT: object()}
    handlers = dict(previous)

    def register(number, handler):
        old = handlers[number]
        handlers[number] = handler
        return old

    def request(number=signal.SIGTERM):
        handler = handlers[number]
        assert callable(handler), 'Daemon must install a graceful shutdown handler'
        handler(number, None)

    monkeypatch.setattr(signal, 'signal', register)
    monkeypatch.setattr(daemon, 'try_to_connect', Mock())
    monkeypatch.setattr(daemon, 'update_analyzer', Mock())
    monkeypatch.setattr(daemon.time, 'sleep', Mock(side_effect=AssertionError(
        'Daemon slept or retried after a shutdown request')))
    return request, handlers, previous


@pytest.mark.parametrize('number', [signal.SIGTERM, signal.SIGINT])
def test_shutdown_finishes_current_update_before_exit(
        daemon, monkeypatch, shutdown_control, number, caplog):
    caplog.set_level('INFO')
    request, handlers, previous = shutdown_control
    events = []

    def update(**kwargs):
        token = query_cache.begin_update()
        events.append('started')
        request(number)
        assert query_cache.get_generation() is None
        events.append('writes-completed')
        query_cache.complete_update(token)

    monkeypatch.setattr(daemon, 'update_blockchain', update)
    daemon.run()
    assert events == ['started', 'writes-completed']
    assert query_cache.get_generation() is not None
    assert handlers == previous
    assert 'quiesced' in caplog.text.lower()


def test_shutdown_requested_during_initialization_starts_no_update(
        daemon, monkeypatch, shutdown_control):
    request, handlers, previous = shutdown_control
    update = Mock()
    monkeypatch.setattr(daemon, 'update_analyzer', lambda: request())
    monkeypatch.setattr(daemon, 'update_blockchain', update)
    daemon.run()
    update.assert_not_called()
    assert handlers == previous


def test_shutdown_during_idle_sleep_starts_no_second_update(
        daemon, monkeypatch, shutdown_control):
    request, handlers, previous = shutdown_control
    update = Mock()
    monkeypatch.setattr(daemon, 'update_blockchain', update)
    monkeypatch.setattr(daemon.time, 'sleep', lambda _: request())
    daemon.run()
    assert update.call_count == 1
    assert handlers == previous


def test_shutdown_after_verification_rpc_failure_is_not_acknowledged(
        daemon, monkeypatch, shutdown_control, caplog):
    request, handlers, previous = shutdown_control

    def update(**kwargs):
        token = query_cache.begin_update()
        query_cache.mark_writes_complete(token, 10, 'block-10')
        request()
        raise RPCError('Verification node unavailable')

    monkeypatch.setattr(daemon, 'update_blockchain', update)
    with pytest.raises(RuntimeError, match='verification pending'):
        daemon.run()
    assert handlers == previous
    assert query_cache.get_generation() is None
    assert 'quiesced' not in caplog.text.lower()


def test_shutdown_cannot_acknowledge_incomplete_writes(
        daemon, monkeypatch, shutdown_control, caplog):
    request, handlers, previous = shutdown_control

    def update(**kwargs):
        query_cache.begin_update()
        request()

    monkeypatch.setattr(daemon, 'update_blockchain', update)
    with pytest.raises(RuntimeError, match='unfinished'):
        daemon.run()
    assert handlers == previous
    assert 'quiesced' not in caplog.text.lower()


def test_rpc_failure_before_writes_still_retries_without_shutdown(
        daemon, monkeypatch, shutdown_control):
    request, handlers, previous = shutdown_control
    calls = []

    def update(**kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise RPCError('Temporary node failure')
        request()

    sleep = Mock()
    monkeypatch.setattr(daemon, 'update_blockchain', update)
    monkeypatch.setattr(daemon.time, 'sleep', sleep)
    daemon.run()
    assert len(calls) == 2
    sleep.assert_called_once_with(5)
    assert handlers == previous


def test_fatal_update_error_restores_signal_handlers(
        daemon, monkeypatch, shutdown_control, caplog):
    _, handlers, previous = shutdown_control
    monkeypatch.setattr(daemon, 'update_blockchain', Mock(
        side_effect=RuntimeError('Fatal write failure')))
    with pytest.raises(RuntimeError, match='Fatal write failure'):
        daemon.run()
    assert handlers == previous
    assert 'quiesced' not in caplog.text.lower()


@pytest.mark.parametrize('chunk_size', [1, 2000])
def test_shutdown_finishes_written_prefix_without_ingesting_the_backlog(
        daemon, monkeypatch, chunk_size):
    inserted = []
    monkeypatch.setattr(daemon, 'MAX_CHUNK_SIZE', chunk_size)
    monkeypatch.setattr(daemon, 'get_block_count', lambda: 13)
    monkeypatch.setattr(daemon, 'get_block', lambda height: {
        'hash': 'block-' + str(height), 'height': height,
        'previousblockhash': 'block-' + str(height - 1),
    })

    def insert(block, new, deleted):
        inserted.append(block['height'])
        Block(hash=block['hash'], height=block['height'],
              previousblockhash=block['previousblockhash'], time=1,
              difficulty=1, insert_time=1, update_time=1).save()

    monkeypatch.setattr(daemon, 'insert_block', insert)
    monkeypatch.setattr(daemon, 'update_addresses', lambda *args: Address.objects.update(set__balance=9))
    daemon.update_blockchain(should_stop=lambda: bool(inserted))
    assert inserted == [11]
    assert Block.objects.order_by('-height').first().height == 11
    assert Address.objects.first().balance == 9
    assert query_cache.get_generation() not in (None, 'initial')


def test_shutdown_before_first_write_preserves_existing_generation(daemon, monkeypatch):
    monkeypatch.setattr(daemon, 'get_block_count', lambda: 13)
    insert = Mock()
    monkeypatch.setattr(daemon, 'insert_block', insert)
    daemon.update_blockchain(should_stop=lambda: True)
    insert.assert_not_called()
    assert query_cache.get_generation() == 'initial'
