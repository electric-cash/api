from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4
import json

import pytest
from pymongo.errors import OperationFailure

import difficulty_index_check as check
from db import db
from db.models import Block


def index_specs():
    return [
        {'name': 'height_1', 'key': {'height': 1}},
        {'name': 'height_1_difficulty_1', 'key': {'height': 1, 'difficulty': 1}},
    ]


def test_accepts_completed_ordinary_indexes():
    check.validate_indexes(index_specs())


@pytest.mark.parametrize('position', [0, 1])
def test_rejects_missing_required_index(position):
    indexes = index_specs()
    indexes.pop(position)
    with pytest.raises(check.PreflightError, match='missing'):
        check.validate_indexes(indexes)


@pytest.mark.parametrize('option,value', [
    ('unique', True), ('sparse', True), ('hidden', True),
    ('partialFilterExpression', {}), ('expireAfterSeconds', 0),
    ('collation', {'locale': 'en'}), ('prepareUnique', True),
])
@pytest.mark.parametrize('position', [0, 1])
def test_rejects_incompatible_options(position, option, value):
    indexes = index_specs()
    indexes[position][option] = value
    with pytest.raises(check.PreflightError, match='options'):
        check.validate_indexes(indexes)


@pytest.mark.parametrize('key', [
    {'difficulty': 1, 'height': 1}, {'height': -1, 'difficulty': 1},
    {'height': 1}, {'height': 1, 'difficulty': 1, 'time': 1},
])
def test_rejects_incompatible_key_order_or_fields(key):
    indexes = index_specs()
    indexes[1]['key'] = key
    with pytest.raises(check.PreflightError, match='keys'):
        check.validate_indexes(indexes)


def test_rejects_equivalent_index_with_different_name():
    indexes = index_specs()
    indexes[1]['name'] = 'custom_difficulty'
    with pytest.raises(check.PreflightError, match='missing'):
        check.validate_indexes(indexes)


@pytest.mark.parametrize('position', [0, 1])
def test_rejects_in_progress_build(position):
    indexes = index_specs()
    indexes[position] = {'spec': indexes[position], 'buildUUID': 'synthetic-build'}
    with pytest.raises(check.PreflightError, match='building'):
        check.validate_indexes(indexes)


def test_allows_unrelated_index_build_and_simple_collation():
    indexes = index_specs()
    indexes[1]['collation'] = {'locale': 'simple'}
    indexes.append({'spec': {'name': 'other'}, 'buildUUID': 'synthetic-build'})
    check.validate_indexes(indexes)


def test_rejects_duplicate_required_name():
    indexes = index_specs()
    indexes.append(deepcopy(indexes[1]))
    with pytest.raises(check.PreflightError, match='duplicate'):
        check.validate_indexes(indexes)


@pytest.fixture
def index_database(connection):
    name = 'api_cache_preflight_' + uuid4().hex
    database = connection[name]
    database.block.insert_one({'height': 1, 'difficulty': 2})
    database.block.create_index([('height', 1)])
    database.block.create_index([('height', 1), ('difficulty', 1)])
    try:
        yield database
    finally:
        connection.drop_database(name)


def test_real_metadata_check_does_not_access_models_or_write(index_database, monkeypatch):
    before = list(index_database.block.list_indexes())
    documents = list(index_database.block.find())

    def forbidden(*args, **kwargs):
        raise AssertionError('Preflight must not invoke MongoEngine index creation')

    monkeypatch.setattr(Block, 'ensure_indexes', forbidden)
    monkeypatch.setattr(Block, '_get_collection', forbidden)
    assert check.check_database(index_database) == ['height_1', 'height_1_difficulty_1']
    assert list(index_database.block.list_indexes()) == before
    assert list(index_database.block.find()) == documents


def test_missing_index_is_not_created(index_database):
    index_database.block.drop_index('height_1_difficulty_1')
    before = list(index_database.block.list_indexes())
    with pytest.raises(check.PreflightError, match='missing'):
        check.check_database(index_database)
    assert list(index_database.block.list_indexes()) == before


def test_missing_collection_is_not_created(index_database):
    index_database.block.drop()
    with pytest.raises(OperationFailure):
        check.check_database(index_database)
    assert 'block' not in index_database.list_collection_names()


@pytest.mark.parametrize('cursor_change', [
    {'id': 123}, {'ns': 'other.block'}, {'firstBatch': None},
])
def test_rejects_incomplete_or_wrong_namespace(cursor_change):
    cursor = {'id': 0, 'ns': 'test.block', 'firstBatch': index_specs()}
    cursor.update(cursor_change)
    commands = []

    def command(*args, **kwargs):
        commands.append((args, kwargs))
        return {'ok': 1, 'cursor': cursor}

    with pytest.raises(check.PreflightError):
        check.check_database(SimpleNamespace(name='test', command=command))
    assert commands[0][0] == ('listIndexes', 'block')
    assert commands[0][1]['includeBuildUUIDs'] is True
    assert commands[0][1]['maxTimeMS'] == 5000
    assert len(commands) == 1


def cli_args():
    return ['--expected-database', db.db_name, '--expected-host', db.db_host]


@pytest.mark.parametrize('field', ['--expected-database', '--expected-host'])
def test_cli_refuses_mismatched_target_before_connecting(field, monkeypatch, capsys):
    args = cli_args()
    args[args.index(field) + 1] = 'wrong-target'

    def forbidden(*args, **kwargs):
        raise AssertionError('Must validate target before connecting')

    monkeypatch.setattr(check, 'MongoClient', forbidden)
    assert check.main(args) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'blocked'


def test_cli_success_is_explicitly_metadata_only(monkeypatch, capsys):
    opened = []

    class Client:
        def __init__(self, uri, **kwargs):
            opened.append((uri, kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def __getitem__(self, name):
            assert name == db.db_name
            return 'test-database'

    def verify(database):
        assert database == 'test-database'
        return ['height_1', 'height_1_difficulty_1']

    monkeypatch.setattr(check, 'MongoClient', Client)
    monkeypatch.setattr(check, 'check_database', verify)
    assert check.main(cli_args()) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'metadata_verified'
    assert result['query_plan_verified'] is False
    assert result['deployment_approved'] is False
    assert 'readPreference=primary' in opened[0][0]
    assert opened[0][1]['socketTimeoutMS'] == 5000
    assert opened[0][1]['serverSelectionTimeoutMS'] == 5000
    assert opened[0][1]['retryReads'] is False


def test_cli_does_not_print_connection_errors_or_credentials(monkeypatch, capsys):
    def failed(*args, **kwargs):
        raise OperationFailure('synthetic-private-credential@internal-host')

    monkeypatch.setattr(check, 'MongoClient', failed)
    assert check.main(cli_args()) == 1
    output = capsys.readouterr()
    assert 'synthetic-private' not in output.out + output.err
    assert 'internal-host' not in output.out + output.err
    assert json.loads(output.out)['reason'] == 'OperationFailure'
