from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import mongoengine
from mongoengine import connection
import pytest

from db import db, models, query_cache


@pytest.fixture
def rollback_example(monkeypatch):
    document = (Path(__file__).resolve().parents[1] / 'docs' / 'cache-hardening.md').read_text()
    example = document.split("python - <<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
    values = {
        'EXPECTED_DB_NAME': 'fixture', 'DB_NAME': 'fixture',
        'EXPECTED_DB_HOST': 'api-cache-test-mongo', 'DB_URL': 'api-cache-test-mongo',
        'EXPECTED_DB_PORT': '27017', 'DB_PORT': '27017',
        'EXPECTED_DB_PROTOCOL': 'mongodb', 'DB_PROTOCOL': 'mongodb',
        'EXPECTED_REPLICA_SET': '',
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(db, 'get_db_uri', lambda **kwargs: 'mongodb://api-cache-test-mongo:27017/fixture')
    connect = Mock()
    invalidate = Mock()
    monkeypatch.setattr(mongoengine, 'connect', connect)
    monkeypatch.setattr(query_cache, 'invalidate', invalidate)
    monkeypatch.setattr(connection, 'get_db', lambda: SimpleNamespace(
        name='fixture', client=SimpleNamespace(address=('api-cache-test-mongo', 27017))))
    monkeypatch.setattr(models, 'CacheState', SimpleNamespace(objects=SimpleNamespace(
        get=lambda **kwargs: SimpleNamespace(ready=False, writes_complete=False))))
    return compile(example, 'rollback-example', 'exec'), connect, invalidate


@pytest.mark.parametrize('key,value', [
    ('DB_PORT', '27018'), ('DB_PROTOCOL', 'mongodb+srv'),
    ('EXPECTED_REPLICA_SET', 'different-replica-set'), ('EXPECTED_DB_HOST', None),
])
def test_rollback_example_rejects_unapproved_target_before_connect(
        rollback_example, monkeypatch, key, value):
    example, connect, invalidate = rollback_example
    if value is None:
        monkeypatch.delenv(key)
    else:
        monkeypatch.setenv(key, value)
    with pytest.raises((KeyError, SystemExit)):
        exec(example, {})
    connect.assert_not_called()
    invalidate.assert_not_called()


def test_rollback_example_prints_verified_target_and_disabled_state(rollback_example, capsys):
    example, connect, invalidate = rollback_example
    exec(example, {})
    connect.assert_called_once_with(host='mongodb://api-cache-test-mongo:27017/fixture')
    invalidate.assert_called_once_with()
    assert '"ready": false' in capsys.readouterr().out
