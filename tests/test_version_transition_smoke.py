from pathlib import Path
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

import version_transition_smoke as smoke


@pytest.fixture
def environment(monkeypatch):
    for name in ('DB_PROTOCOL', 'DB_PORT', 'DB_USERNAME', 'DB_PASSWORD',
                 'DB_URI_PARAMS', 'RPC_PORT', 'RPC_USER', 'RPC_PASSWORD'):
        monkeypatch.delenv(name, raising=False)
    for name, value in {
        'DB_URL': 'api-cache-test-mongo',
        'DB_NAME': 'api_cache_http_transition_recovery_source',
        'RPC_HOST': 'api-cache-test-rpc',
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, 'argv', ['version_transition_smoke.py', 'safe-rollback'])
    monkeypatch.setattr(sys, 'path', list(sys.path))


@pytest.mark.parametrize('key,value', [
    ('DB_URL', 'external.example'),
    ('DB_NAME', 'blockchain'),
    ('DB_URI_PARAMS', '?authSource=admin'),
    ('RPC_HOST', 'external.example'),
    ('RPC_USER', 'fixture-user'),
    ('RPC_PASSWORD', 'fixture-password'),
])
def test_rejects_unsafe_environment_before_sources_or_connection(environment, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    sources = MagicMock(side_effect=AssertionError('Sources must not be loaded'))
    connection = MagicMock(side_effect=AssertionError('Must not connect'))
    monkeypatch.setattr(smoke, 'validate_sources', sources)
    monkeypatch.setattr(smoke, 'MongoClient', connection)
    with pytest.raises(RuntimeError, match='isolated'):
        smoke.main()
    sources.assert_not_called()
    connection.assert_not_called()


@pytest.fixture
def sources(tmp_path, monkeypatch):
    roots = [tmp_path / 'legacy', tmp_path / 'candidate']
    for root in roots:
        root.mkdir()
        (root / 'daemon.py').write_text('# ' + root.name, encoding='ascii')
        (root / 'api.py').write_text('# api', encoding='ascii')
    monkeypatch.setattr(smoke, 'LEGACY_ROOT', roots[0])
    monkeypatch.setattr(smoke, 'CANDIDATE_ROOT', roots[1])
    return roots


def test_source_fingerprints_are_repeatable_and_detect_content_changes(sources):
    first = smoke.validate_sources()
    assert first == smoke.validate_sources()
    assert set(first) == {'legacy', 'candidate'}
    assert all(len(value) == 64 for value in first.values())
    (sources[0] / 'daemon.py').write_text('# changed', encoding='ascii')
    second = smoke.validate_sources()
    assert first['legacy'] != second['legacy']
    assert first['candidate'] == second['candidate']


@pytest.mark.parametrize('root_index,file_name', [(0, 'api.py'), (1, 'daemon.py')])
def test_missing_source_entrypoints_are_rejected(sources, root_index, file_name):
    (sources[root_index] / file_name).unlink()
    with pytest.raises(RuntimeError, match='source trees'):
        smoke.validate_sources()


def test_identical_source_trees_are_rejected(sources):
    (sources[1] / 'daemon.py').write_bytes((sources[0] / 'daemon.py').read_bytes())
    with pytest.raises(RuntimeError, match='must differ'):
        smoke.validate_sources()


def test_symlinked_source_is_rejected(sources):
    link = sources[0] / 'alias.py'
    link.symlink_to(sources[0] / 'api.py')
    with pytest.raises(RuntimeError, match='Symlink'):
        smoke.validate_sources()


@pytest.mark.parametrize('root_index', [0, 1])
def test_symlinked_package_directory_is_rejected(sources, tmp_path, root_index):
    package = tmp_path / 'external_package'
    package.mkdir()
    (package / '__init__.py').write_text('# external code', encoding='ascii')
    (sources[root_index] / 'db').symlink_to(package, target_is_directory=True)
    with pytest.raises(RuntimeError, match='Symlink'):
        smoke.validate_sources()


def test_symlinked_source_root_is_rejected(sources, tmp_path, monkeypatch):
    alias = tmp_path / 'legacy_alias'
    alias.symlink_to(sources[0], target_is_directory=True)
    monkeypatch.setattr(smoke, 'LEGACY_ROOT', alias)
    with pytest.raises(RuntimeError, match='Symlink'):
        smoke.validate_sources()


@pytest.mark.parametrize('relative', [
    'api.pyc', 'api.pyo', 'api.so', 'api.pyd',
    '__pycache__/api.cpython-37.pyc', 'db/module.PYC',
])
@pytest.mark.parametrize('root_index', [0, 1])
def test_compiled_artifacts_are_rejected(sources, root_index, relative):
    path = sources[root_index] / relative
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b'not-source')
    with pytest.raises(RuntimeError, match='Compiled'):
        smoke.validate_sources()


def test_source_fingerprints_match_benchmark_and_ignore_line_endings(sources):
    from cache_benchmark import source_digest

    for root in sources:
        (root / 'api.py').write_bytes(b'# api\n# second line\n')
    expected = smoke.validate_sources()
    for label, root in zip(('legacy', 'candidate'), sources):
        assert expected[label] == source_digest(root)
        (root / 'api.py').write_bytes(b'# api\r\n# second line\r\n')
    assert smoke.validate_sources() == expected


def test_line_endings_do_not_make_identical_sources_distinct(sources):
    (sources[0] / 'daemon.py').write_bytes(b'# same\n')
    (sources[1] / 'daemon.py').write_bytes(b'# same\r\n')
    with pytest.raises(RuntimeError, match='must differ'):
        smoke.validate_sources()


def test_unsafe_source_rejected_before_database_connection(environment, sources, monkeypatch):
    (sources[0] / 'api.so').write_bytes(b'not-source')
    connection = MagicMock(side_effect=AssertionError('Must not connect'))
    monkeypatch.setattr(smoke, 'MongoClient', connection)
    with pytest.raises(RuntimeError, match='Compiled'):
        smoke.main()
    connection.assert_not_called()


def test_rejects_nonempty_database_before_starting_workers(environment, sources, monkeypatch):
    client = MagicMock()
    client.__getitem__.return_value.list_collection_names.return_value = ['address']
    exercise = MagicMock(side_effect=AssertionError('Must not start workers'))
    monkeypatch.setattr(smoke, 'MongoClient', MagicMock(return_value=client))
    monkeypatch.setattr(smoke, 'exercise', exercise)
    with pytest.raises(RuntimeError, match='must be empty'):
        smoke.main()
    exercise.assert_not_called()
    client.close.assert_called_once_with()


@pytest.mark.parametrize('exit_code,stderr,expected_error,accepted', [
    (0, '', None, True),
    (1, 'worker error', None, False),
    (1, 'unfinished batch', 'unfinished', True),
    (0, 'unfinished batch', 'unfinished', False),
    (1, 'another failure', 'unfinished', False),
])
def test_source_worker_requires_correct_exit_and_error(
        monkeypatch, exit_code, stderr, expected_error, accepted):
    result = subprocess.CompletedProcess([], exit_code, stdout='', stderr=stderr)
    run = MagicMock(return_value=result)
    monkeypatch.setattr(smoke.subprocess, 'run', run)
    if accepted:
        smoke.run_code(Path('/legacy'), {}, 'pass', expected_error)
    else:
        with pytest.raises(RuntimeError, match='Source worker'):
            smoke.run_code(Path('/legacy'), {}, 'pass', expected_error)
    assert run.call_args[1]['cwd'] == '/legacy'
    assert run.call_args[1]['timeout'] == 30


@pytest.mark.parametrize('status_code,supply,accepted', [(200, 7, True), (200, 3, False), (500, 7, False)])
def test_status_verification_rejects_errors_and_stale_data(monkeypatch, status_code, supply, accepted):
    response = MagicMock(status_code=status_code, text='fixture response')
    response.json.return_value = {'height': 2, 'hashrate': 123, 'total_coin_supply': supply}
    get = MagicMock(return_value=response)
    monkeypatch.setattr(smoke.requests, 'get', get)
    if accepted:
        smoke.check_status('http://127.0.0.1:1234', 2, 7)
    else:
        with pytest.raises(RuntimeError, match='Unexpected API status'):
            smoke.check_status('http://127.0.0.1:1234', 2, 7)
    get.assert_called_once_with('http://127.0.0.1:1234/status', timeout=5)
