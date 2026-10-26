"""Offline harness tests: run with pytest --noconftest (no MongoDB required)."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import cache_benchmark as bench


@pytest.fixture
def environment():
    return {'DB_URL': 'api-cache-test-mongo', 'DB_NAME': 'api_cache_http_unit',
            'RPC_HOST': 'api-cache-test-rpc'}


@pytest.mark.parametrize('key,value', [
    ('DB_URL', 'production'), ('DB_URL', 'api-cache-test-mongo:27017,other'),
    ('DB_PROTOCOL', 'mongodb+srv'), ('DB_PORT', '27018'),
    ('DB_USERNAME', 'secret'), ('DB_PASSWORD', 'secret'),
    ('DB_URI_PARAMS', '?replicaSet=production'), ('RPC_HOST', 'production'),
    ('RPC_PORT', '8333'), ('RPC_USER', 'secret'), ('RPC_PASSWORD', 'secret'),
    ('DB_NAME', 'blockchain'), ('DB_NAME', 'api_cache_http_'),
    ('DB_NAME', 'api_cache_http_bad\n'), ('DB_NAME', 'api_cache_http_bad/name'),
    ('DB_NAME', 'api_cache_http_' + 'x' * 50),
])
def test_environment_rejects_unsafe_values(environment, key, value):
    environment[key] = value
    with pytest.raises(RuntimeError, match='isolated'):
        bench.validate_environment(environment)


def test_environment_and_worker_names(environment):
    assert bench.validate_environment(environment) == 'api_cache_http_unit'
    environment['DB_NAME'] += '_candidate'
    assert bench.validate_environment(environment, 'candidate') == environment['DB_NAME']
    with pytest.raises(RuntimeError, match='worker database'):
        bench.validate_environment(environment, 'legacy')


@pytest.mark.parametrize('requests,concurrency', [(0, 1), (101, 1), (1, 0), (10, 5)])
def test_limits(requests, concurrency):
    with pytest.raises(RuntimeError, match='bounds'):
        bench.validate_limits(requests, concurrency)


@pytest.fixture
def sources(tmp_path, monkeypatch):
    roots = {name: tmp_path / name for name in ('legacy', 'candidate')}
    for name, root in roots.items():
        root.mkdir()
        (root / 'api.py').write_text('# ' + name, encoding='ascii')
        (root / 'daemon.py').write_text('# daemon', encoding='ascii')
    monkeypatch.setattr(bench, 'ROOTS', roots)
    monkeypatch.setattr(bench, 'LEGACY_SHA256', bench.source_digest(roots['legacy']))
    return roots


def test_sources_are_pinned_and_line_ending_independent(sources):
    expected = bench.source_digest(sources['candidate'])
    result = bench.validate_sources(expected)
    assert result['candidate'] == expected
    path = sources['candidate'] / 'api.py'
    path.write_bytes(b'# candidate\n')
    lf = bench.source_digest(sources['candidate'])
    path.write_bytes(b'# candidate\r\n')
    assert bench.source_digest(sources['candidate']) == lf
    with pytest.raises(RuntimeError, match='candidate'):
        bench.validate_sources(expected)
    (sources['legacy'] / 'api.py').write_text('# altered', encoding='ascii')
    with pytest.raises(RuntimeError, match='ba36f5e'):
        bench.validate_sources(lf)


def test_sources_require_entrypoints_and_reject_symlinks(sources, monkeypatch):
    path = sources['candidate'] / 'api.py'
    original = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda self: self == path or original(self))
    with pytest.raises(RuntimeError, match='Symlink'):
        bench.source_digest(sources['candidate'])
    monkeypatch.undo()
    path.unlink()
    with pytest.raises(RuntimeError, match='source tree'):
        bench.source_digest(sources['candidate'])


@pytest.mark.parametrize('relative', ['__pycache__/api.cpython-37.pyc', 'api.pyc', 'api.pyo', 'api.so', 'api.pyd'])
def test_sources_reject_executable_artifacts_not_covered_by_source_hash(sources, relative):
    artifact = sources['candidate'] / relative
    artifact.parent.mkdir(exist_ok=True)
    artifact.write_bytes(b'not-source')
    with pytest.raises(RuntimeError, match='Compiled'):
        bench.source_digest(sources['candidate'])


def test_fixture_is_fixed_bounded_and_fresh():
    first = bench.fixture_documents()
    second = bench.fixture_documents()
    assert first == second
    assert sum(len(docs) for docs in first.values()) == 1002
    assert sum(doc['balance'] for doc in first['address']) == 5500
    first['address'][0]['balance'] = -1
    assert second['address'][0]['balance'] == 1


def test_fixture_digest_changes_with_application_data_but_ignores_cache():
    database = MagicMock()
    collections = {name: MagicMock() for name in ('address', 'block', 'query_cache_v2')}
    database.__getitem__.side_effect = collections.__getitem__
    collections['address'].find.return_value.sort.return_value = [{'_id': 'one', 'balance': 1}]
    collections['block'].find.return_value.sort.return_value = [{'_id': 'block', 'height': 1}]
    original = bench.fixture_digest(database)
    assert bench.fixture_digest(database) == original
    collections['query_cache_v2'].find.assert_not_called()
    collections['address'].find.return_value.sort.return_value[0]['balance'] = 2
    assert bench.fixture_digest(database) != original


def test_timings_use_nearest_rank_p95():
    assert bench.timing_summary([0.001, 0.003, 0.002, 0.004]) == {
        'median_ms': 2.5, 'p95_ms': 4.0, 'samples': 4}
    assert bench.timing_summary([0.005])['p95_ms'] == 5.0


def test_counts_are_thread_safe_command_counts_not_document_counts():
    counts = bench.CommandCounts('api_cache_http_unit')
    event = SimpleNamespace(database_name='api_cache_http_unit', command_name='aggregate',
                            command={'aggregate': 'address'})
    counts.started(event)
    counts.begin()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: counts.started(event), range(100)))
    counts.started(SimpleNamespace(database_name='other', command_name='aggregate',
                                   command={'aggregate': 'address'}))
    counts.started(SimpleNamespace(database_name='api_cache_http_unit', command_name='getMore',
                                   command={'getMore': 123, 'collection': 'address'}))
    result = counts.end()
    counts.started(event)
    assert result == {'started_commands': {'aggregate': 100, 'getMore': 1},
                      'aggregate_commands_by_collection': {'address': 100}}


@pytest.mark.parametrize('status,payload', [(500, {}), (200, {'stale': True})])
def test_requests_reject_errors_and_stale_results(status, payload):
    app = MagicMock()
    response = app.test_client.return_value.__enter__.return_value.get.return_value
    response.status_code = status
    response.get_json.return_value = payload
    with pytest.raises(RuntimeError, match='Unexpected /status'):
        bench.request_status(app, {'height': 1})


def test_request_times_get_but_checks_the_json(monkeypatch):
    app = MagicMock()
    response = app.test_client.return_value.__enter__.return_value.get.return_value
    response.status_code = 200
    response.get_json.return_value = {'height': 1}
    monkeypatch.setattr(bench.time, 'perf_counter', MagicMock(side_effect=[1.0, 1.25]))
    assert bench.request_status(app, {'height': 1}) == 0.25
    response.get_json.assert_called_once_with()


def test_phase_clears_only_cold_and_validates_every_request(monkeypatch):
    request = MagicMock(return_value=0.002)
    monkeypatch.setattr(bench, 'request_status', request)
    clear = MagicMock()
    result = bench.measure_phase(None, bench.CommandCounts('db'), {'height': 1}, 4, 1, clear)
    assert clear.call_count == request.call_count == 4
    assert result['wall']['samples'] == 4
    assert result['result'] == {'height': 1}
    assert result['all_results_equal'] is True
    bench.measure_phase(None, bench.CommandCounts('db'), {'height': 1}, 8, 4)
    assert clear.call_count == 4
    assert request.call_count == 12


def test_reused_database_is_never_seeded_or_dropped():
    client = MagicMock()
    database = client.__getitem__.return_value
    database.list_collection_names.return_value = ['address']
    with pytest.raises(RuntimeError, match='unused'):
        with bench.owned_database(client, 'api_cache_http_unit_candidate', 'a' * 32):
            pytest.fail('must not enter')
    database.create_collection.assert_not_called()
    client.drop_database.assert_not_called()


def test_cleanup_only_drops_database_with_our_marker():
    client = MagicMock()
    name, token = 'api_cache_http_unit_candidate', 'a' * 32
    client[name].list_collections.return_value = []
    bench.cleanup_owned(client, name, token)
    client.drop_database.assert_not_called()
    client[name].list_collections.return_value = [
        {'options': {'validator': {'_benchmark_owner': 'b' * 32}}}]
    bench.cleanup_owned(client, name, token)
    client.drop_database.assert_not_called()
    client[name].list_collections.return_value = [
        {'options': {'validator': {'_benchmark_owner': token}}}]
    bench.cleanup_owned(client, name, token)
    client.drop_database.assert_called_once_with(name)
    with pytest.raises(RuntimeError, match='database'):
        bench.cleanup_owned(client, 'blockchain', token)


def test_owned_database_cleans_up_after_failure():
    client = MagicMock()
    name, token = 'api_cache_http_unit_legacy', 'b' * 32
    client[name].list_collection_names.return_value = []
    client[name].list_collections.return_value = [
        {'options': {'validator': {'_benchmark_owner': token}}}]
    with pytest.raises(ValueError, match='exercise failed'):
        with bench.owned_database(client, name, token):
            raise ValueError('exercise failed')
    client.drop_database.assert_called_once_with(name)


def test_reservation_identifies_owner_atomically_even_if_setup_reply_is_lost():
    client = MagicMock()
    name, token = 'api_cache_http_unit_candidate', 'c' * 32
    database = client[name]
    database.list_collection_names.return_value = []
    database.list_collections.return_value = []

    def create(name, **options):
        database.list_collections.return_value = [{'options': options}]
        raise ConnectionError('fixture lost create reply')

    database.create_collection.side_effect = create
    with pytest.raises(ConnectionError):
        with bench.owned_database(client, name, token):
            pytest.fail('must not enter')
    bench.cleanup_owned(client, name, token)
    database.create_collection.assert_called_once_with(
        'benchmark_owner', validator={'_benchmark_owner': token})
    database['benchmark_owner'].insert_one.assert_not_called()
    client.drop_database.assert_called_once_with(name)


def test_coordinator_uses_same_python_timeout_and_source_pin(monkeypatch, environment):
    result = {'source': 'legacy'}
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, json.dumps(result), ''))
    monkeypatch.setattr(bench.subprocess, 'run', run)
    assert bench.run_worker('legacy', environment, 'c' * 64, 20, 4, 'a' * 32) == result
    args, kwargs = run.call_args
    assert args[0][0] == bench.sys.executable
    assert args[0][1] == '-B'
    assert 'c' * 64 in args[0]
    assert kwargs['timeout'] == bench.WORKER_TIMEOUT
    assert kwargs['env']['DB_NAME'] == 'api_cache_http_unit_legacy'
    assert 'PYTHONPATH' not in kwargs['env']
    run.return_value.returncode = 1
    with pytest.raises(RuntimeError, match='worker failed'):
        bench.run_worker('legacy', environment, 'c' * 64, 20, 4, 'a' * 32)


def test_rpc_stub_only_allows_synthetic_hashrate():
    assert bench.synthetic_rpc(SimpleNamespace(method='getnetworkhashps', params=[])) == 123
    with pytest.raises(RuntimeError, match='RPC'):
        bench.synthetic_rpc(SimpleNamespace(method='getblock', params=['real-block']))


def test_comparison_rejects_different_data_results_or_dependencies():
    sample = {'fixture_sha256': 'x', 'updated_fixture_sha256': 'y',
              'runtime': {'python': '3.7'}, 'phases': {'warm': {'result': {'height': 1}}}}
    assert bench.compare_workers(sample, copy.deepcopy(sample)) is True
    for key in ('fixture_sha256', 'updated_fixture_sha256', 'runtime', 'phases'):
        different = copy.deepcopy(sample)
        different[key] = None
        with pytest.raises(RuntimeError, match='differ'):
            bench.compare_workers(sample, different)


@pytest.mark.parametrize('source', ['legacy', 'candidate'])
def test_exercise_checks_freshness_after_invalidation(monkeypatch, source):
    database, cache = MagicMock(), MagicMock()
    monkeypatch.setitem(bench.sys.modules, 'db', SimpleNamespace(query_cache=cache))
    monkeypatch.setattr(bench, 'fixture_digest', MagicMock(side_effect=['old', 'old', 'new', 'new']))
    database.cache_state.find_one.side_effect = [{'ready': False}, {'generation': 'next'}]
    events = []
    cache.invalidate.side_effect = lambda: events.append('invalidate')
    database.address.update_one.side_effect = lambda *args: events.append('write')
    cache.complete_update.side_effect = lambda generation: events.append('ready')

    def measure(app, counts, expected, requests, concurrency, clear=None):
        events.append(expected['total_coin_supply'])
        return {'result': expected}

    monkeypatch.setattr(bench, 'measure_phase', measure)
    result = bench.exercise(None, database, None, source, 2, 2)
    assert result['fixture_sha256'] == 'old'
    assert result['updated_fixture_sha256'] == 'new'
    if source == 'candidate':
        assert events == [5500, 5500, 5500, 'invalidate', 'write', 5510, 'ready', 5510]
        cache.complete_update.assert_called_once_with('next')
        database.cache_state.insert_one.assert_called_once()
    else:
        assert events == [5500, 5500, 5500, 'write', 5510, 5510]
        cache.invalidate.assert_not_called()
        database.cache_state.insert_one.assert_not_called()


def test_main_rejects_environment_before_sources_or_connection(monkeypatch, environment):
    monkeypatch.setattr(bench.os, 'environ', dict(environment, DB_URL='external'))
    monkeypatch.setattr(bench.sys, 'argv', ['benchmark', 'coordinator', '--candidate-sha256', 'c' * 64])
    sources = MagicMock(side_effect=AssertionError('must not load sources'))
    monkeypatch.setattr(bench, 'validate_sources', sources)
    with pytest.raises(RuntimeError, match='isolated'):
        bench.main()
    sources.assert_not_called()


def test_coordinator_cleans_only_owned_database_on_worker_timeout(monkeypatch, environment):
    client = MagicMock()
    client.__getitem__.return_value.list_collection_names.return_value = []
    token = 'a' * 32
    client.__getitem__.return_value.list_collections.return_value = [
        {'options': {'validator': {'_benchmark_owner': token}}}]
    monkeypatch.setitem(bench.sys.modules, 'pymongo', SimpleNamespace(MongoClient=lambda *a, **k: client))
    monkeypatch.setattr(bench.os, 'environ', environment)
    monkeypatch.setattr(bench.sys, 'argv', ['benchmark', 'coordinator', '--candidate-sha256', 'c' * 64])
    monkeypatch.setattr(bench, 'validate_sources', lambda _: {})
    monkeypatch.setattr(bench.uuid, 'uuid4', lambda: SimpleNamespace(hex=token))
    monkeypatch.setattr(bench, 'run_worker', MagicMock(side_effect=subprocess.TimeoutExpired('worker', 180)))
    with pytest.raises(subprocess.TimeoutExpired):
        bench.main()
    client.drop_database.assert_called_once_with('api_cache_http_unit_legacy')
    client.close.assert_called_once_with()


def test_coordinator_reports_verified_sources_and_equal_results(monkeypatch, environment, capsys):
    client = MagicMock()
    client.__getitem__.return_value.list_collection_names.return_value = []
    client.__getitem__.return_value.list_collections.return_value = []
    monkeypatch.setitem(bench.sys.modules, 'pymongo', SimpleNamespace(MongoClient=lambda *a, **k: client))
    monkeypatch.setattr(bench.os, 'environ', environment)
    monkeypatch.setattr(bench.sys, 'argv', ['benchmark', 'coordinator', '--candidate-sha256', 'c' * 64])
    sources = {'legacy': 'b' * 64, 'candidate': 'c' * 64}
    check = MagicMock(return_value=sources)
    monkeypatch.setattr(bench, 'validate_sources', check)
    result = {'fixture_sha256': 'before', 'updated_fixture_sha256': 'after',
              'runtime': {'python': '3.7'}, 'phases': {'warm': {'result': {'height': 1}}}}
    run = MagicMock(return_value=result)
    monkeypatch.setattr(bench, 'run_worker', run)
    bench.main()
    report = json.loads(capsys.readouterr().out)
    assert report['result'] == 'passed'
    assert report['source_sha256_lf'] == sources
    assert report['equal_results_and_inputs'] is True
    assert [call[0][0] for call in run.call_args_list] == ['legacy', 'candidate']
    assert check.call_count == 2
    client.drop_database.assert_not_called()
    client.close.assert_called_once_with()
