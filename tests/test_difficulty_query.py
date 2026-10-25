from copy import deepcopy

import pytest

from db import db, db_queries
from db.models import Block


@pytest.fixture
def difficulty_blocks():
    collection = Block._get_collection()
    collection.insert_many([
        {
            '_id': 'difficulty-block-{}'.format(height),
            'height': height,
            'time': 1000 + height,
            'difficulty': float(height % 8 + 1),
            'tx': [{
                'txid': 'coinbase-{}'.format(height),
                'hash': 'x' * 8192,
                'vin': [{'coinbase': 'synthetic'}],
                'vout': [],
            }],
            'insert_time': 1,
            'update_time': 1,
        }
        for height in range(1, 1025)
    ])
    return collection


def legacy_difficulty_pipeline(interval, lower, upper):
    boundaries = [lower + i * interval for i in range(int((upper - lower) / interval) + 1)]
    boundaries[-1] += 1
    return [
        {'$match': {'height': {'$gte': lower}}},
        {'$match': {'height': {'$lte': upper}}},
        {'$sort': {'height': 1}},
        {'$project': {'time': 1, 'height': 1, 'difficulty': 1}},
        {'$bucket': {
            'groupBy': '$height',
            'boundaries': boundaries,
            'default': boundaries[-1],
            'output': {
                'lower_height': {'$min': '$height'},
                'upper_height': {'$max': '$height'},
                'avg_difficulty': {'$avg': '$difficulty'},
            },
        }},
    ]


def cursor_stats(database, pipeline):
    explain = database.command('explain', {
        'aggregate': Block._get_collection_name(),
        'pipeline': pipeline,
        'cursor': {},
    }, verbosity='executionStats')
    if 'stages' in explain:
        return next(stage['$cursor']['executionStats']
                    for stage in explain['stages'] if '$cursor' in stage)
    return explain['executionStats']


@pytest.mark.parametrize('interval,lower,upper', [
    (6, 1, 1009),
    (144, 1, 1024),
    (1008, 1, 1024),
    (2, 17, 21),
    (1, 1, 2),
    (6, 2000, 2006),
])
def test_difficulty_buckets_match_legacy(difficulty_blocks, interval, lower, upper):
    pipeline = db_queries.get_blocks_average_difficulty_query(interval, lower, upper)
    expected = list(difficulty_blocks.aggregate(legacy_difficulty_pipeline(interval, lower, upper)))
    assert db.execute_query(pipeline, db.get_blockchain()) == expected


def test_difficulty_chart_values_and_boundaries(difficulty_blocks):
    pipeline = db_queries.get_blocks_average_difficulty_query(2, 1, 6)
    assert db.execute_query(pipeline, db.get_blockchain()) == [
        {'_id': 1, 'lower_height': 1, 'upper_height': 2, 'avg_difficulty': 2.5},
        {'_id': 3, 'lower_height': 3, 'upper_height': 5, 'avg_difficulty': 5.0},
        {'_id': 6, 'lower_height': 6, 'upper_height': 6, 'avg_difficulty': 7.0},
    ]


def test_difficulty_aggregation_does_not_fetch_transaction_documents(
        difficulty_blocks, clean_database):
    legacy = cursor_stats(clean_database, legacy_difficulty_pipeline(6, 1, 1024))
    assert legacy['totalDocsExamined'] == 1024
    pipeline = db_queries.get_blocks_average_difficulty_query(6, 1, 1024)
    stats = cursor_stats(clean_database, pipeline)
    assert stats['totalKeysExamined'] > 0
    assert stats['totalDocsExamined'] == 0


def test_difficulty_query_remains_correct_without_covering_index(difficulty_blocks):
    pipeline = db_queries.get_blocks_average_difficulty_query(6, 1, 1024)
    expected = list(difficulty_blocks.aggregate(legacy_difficulty_pipeline(6, 1, 1024)))
    covering = [name for name, spec in difficulty_blocks.index_information().items()
                if spec['key'] == [('height', 1), ('difficulty', 1)]]
    try:
        for name in covering:
            difficulty_blocks.drop_index(name)
        assert list(difficulty_blocks.aggregate(pipeline)) == expected
    finally:
        Block.ensure_indexes()


def test_difficulty_pipeline_is_independent_of_transaction_projection():
    volume = deepcopy(db_queries.get_transactions_average_volume_query(6, 1, 1024))
    db_queries.get_blocks_average_difficulty_query(6, 1, 1024)
    assert db_queries.get_transactions_average_volume_query(6, 1, 1024) == volume
