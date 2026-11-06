"""Bucket-cost regressions for the existing isolated real-MongoDB suite."""

import json

import pytest

from db import db_queries


def legacy_pipeline(interval, lower, upper):
    # Freeze the pre-optimization contract, including its unusual final boundary.
    boundaries = [
        lower + i * interval
        for i in range(int((upper - lower) / interval) + 1)
    ]
    boundaries[-1] += 1
    return [
        {'$match': {'height': {'$gte': lower}}},
        {'$match': {'height': {'$lte': upper}}},
        {'$sort': {'height': 1}},
        {'$project': {'_id': 0, 'height': 1, 'difficulty': 1}},
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


def pipeline_nodes(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from pipeline_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from pipeline_nodes(child)


@pytest.fixture
def bucket_collection():
    from db.models import Block

    return Block._get_collection()


def insert_rows(collection, rows):
    collection.insert_many([
        dict(row, _id='bucket-cost-{}'.format(index))
        for index, row in enumerate(rows)
    ])


def assert_legacy_equivalent(collection, interval, lower, upper):
    expected = list(collection.aggregate(
        legacy_pipeline(interval, lower, upper), maxTimeMS=5000))
    pipeline = db_queries.get_blocks_average_difficulty_query(interval, lower, upper)
    actual = list(collection.aggregate(pipeline, maxTimeMS=5000))
    assert actual == expected
    return actual


def test_exact_boundaries_penultimate_extension_and_default(bucket_collection):
    insert_rows(bucket_collection, [
        {'height': height, 'difficulty': float(height)}
        for height in reversed(range(9, 25))
    ])
    # Boundaries [10, 14, 18, 23]: height 22 still belongs to bucket 18.
    assert assert_legacy_equivalent(bucket_collection, 4, 10, 23) == [
        {'_id': 10, 'lower_height': 10, 'upper_height': 13, 'avg_difficulty': 11.5},
        {'_id': 14, 'lower_height': 14, 'upper_height': 17, 'avg_difficulty': 15.5},
        {'_id': 18, 'lower_height': 18, 'upper_height': 22, 'avg_difficulty': 20.0},
        {'_id': 23, 'lower_height': 23, 'upper_height': 23, 'avg_difficulty': 23.0},
    ]


@pytest.mark.parametrize('interval,lower,upper', [
    pytest.param(1, 0, 1, id='smallest-valid-range'),
    pytest.param(5, 7, 12, id='two-boundaries-exact-upper'),
    pytest.param(5, 7, 13, id='two-boundaries-default'),
    pytest.param(2, 17, 21, id='offset-exact-multiple'),
    pytest.param(144, 1, 1024, id='offset-remainder'),
    pytest.param(1008, 0, 303555, id='full-history-sparse'),
    pytest.param(3, 2 ** 31 - 3, 2 ** 31 + 9, id='int32-transition'),
    pytest.param(1, 2 ** 53 + 1, 2 ** 53 + 9, id='above-double-precision-unit'),
    pytest.param(3, 2 ** 53 + 1, 2 ** 53 + 14, id='above-double-precision-offset'),
    pytest.param(4, 2 ** 63 - 33, 2 ** 63 - 2, id='near-int64-limit'),
])
def test_integral_ranges_match_legacy(bucket_collection, interval, lower, upper):
    boundaries = legacy_pipeline(interval, lower, upper)[-1]['$bucket']['boundaries']
    heights = {lower - 1, lower, upper, upper + 1}
    for boundary in boundaries:
        heights.update([boundary - 1, boundary, boundary + 1])
    insert_rows(bucket_collection, [
        {'height': height, 'difficulty': float((height - lower) % 16)}
        for height in sorted(heights, reverse=True)
    ])
    actual = assert_legacy_equivalent(bucket_collection, interval, lower, upper)
    assert actual
    assert actual[0]['lower_height'] == lower
    assert actual[-1]['upper_height'] == upper
    assert all(isinstance(row['_id'], int) and not isinstance(row['_id'], bool)
               for row in actual)


def test_offset_gaps_do_not_create_empty_buckets(bucket_collection):
    insert_rows(bucket_collection, [
        {'height': height, 'difficulty': float(height)}
        for height in [24, 22, 18, 15, 8, 7, 6, 26]
    ])
    assert assert_legacy_equivalent(bucket_collection, 4, 7, 25) == [
        {'_id': 7, 'lower_height': 7, 'upper_height': 8, 'avg_difficulty': 7.5},
        {'_id': 15, 'lower_height': 15, 'upper_height': 18, 'avg_difficulty': 16.5},
        {'_id': 19, 'lower_height': 22, 'upper_height': 22, 'avg_difficulty': 22.0},
        {'_id': 24, 'lower_height': 24, 'upper_height': 24, 'avg_difficulty': 24.0},
    ]


def test_missing_and_nonnumeric_difficulty_preserve_mongo_average(bucket_collection):
    insert_rows(bucket_collection, [
        {'height': 1},
        {'height': 2, 'difficulty': None},
        {'height': 3, 'difficulty': '100'},
        {'height': 4, 'difficulty': True},
        {'height': 5, 'difficulty': [100]},
        {'height': 6, 'difficulty': 2},
        {'height': 7, 'difficulty': 4.0},
        {'height': 8, 'difficulty': 6.0},
        {'height': 9, 'difficulty': [100]},
        {'height': 10},
        {'height': 11, 'difficulty': {'value': 100}},
        {'height': 12, 'difficulty': False},
        {'height': 13, 'difficulty': None},
        {'height': 14, 'difficulty': 'ignored'},
        {'height': 15},
        {'height': 16, 'difficulty': 10.0},
        {'height': 17, 'difficulty': 8.0},
        {'height': 18, 'difficulty': 12.0},
    ])
    assert assert_legacy_equivalent(bucket_collection, 5, 1, 18) == [
        {'_id': 1, 'lower_height': 1, 'upper_height': 5, 'avg_difficulty': None},
        {'_id': 6, 'lower_height': 6, 'upper_height': 10, 'avg_difficulty': 4.0},
        {'_id': 11, 'lower_height': 11, 'upper_height': 16, 'avg_difficulty': 10.0},
        {'_id': 17, 'lower_height': 17, 'upper_height': 18, 'avg_difficulty': 10.0},
    ]


def test_no_matching_blocks_returns_no_buckets(bucket_collection):
    insert_rows(bucket_collection, [
        {'height': 6, 'difficulty': 1.0},
        {'height': 26, 'difficulty': 2.0},
    ])
    assert assert_legacy_equivalent(bucket_collection, 4, 7, 25) == []


def test_arithmetic_results_do_not_require_compound_index(bucket_collection):
    from db.models import Block

    insert_rows(bucket_collection, [
        {'height': height, 'difficulty': float(height % 8)}
        for height in range(7, 26)
    ])
    expected = list(bucket_collection.aggregate(legacy_pipeline(4, 7, 25)))
    covering = [
        name for name, spec in bucket_collection.index_information().items()
        if spec['key'] == [('height', 1), ('difficulty', 1)]
    ]
    assert covering
    try:
        for name in covering:
            bucket_collection.drop_index(name)
        indexes = bucket_collection.index_information()
        assert indexes['height_1']['key'] == [('height', 1)]
        assert all(spec['key'] != [('height', 1), ('difficulty', 1)]
                   for spec in indexes.values())
        pipeline = db_queries.get_blocks_average_difficulty_query(4, 7, 25)
        assert list(bucket_collection.aggregate(pipeline, maxTimeMS=5000)) == expected
    finally:
        Block.ensure_indexes()


@pytest.mark.parametrize('interval,lower,upper', [
    (1, 0, 1),
    (4, 10, 23),
    (1008, 0, 303555),
    (1, 2 ** 53 + 1, 2 ** 53 + 9),
    (4, 2 ** 63 - 33, 2 ** 63 - 2),
])
def test_integral_pipeline_uses_group_not_bucket_or_switch(interval, lower, upper):
    pipeline = db_queries.get_blocks_average_difficulty_query(interval, lower, upper)
    operators = {
        key for node in pipeline_nodes(pipeline) if isinstance(node, dict)
        for key in node
    }
    assert '$bucket' not in operators
    assert '$switch' not in operators
    groups = [stage['$group'] for stage in pipeline if '$group' in stage]
    assert len(groups) == 1
    assert {key: value for key, value in groups[0].items() if key != '_id'} == {
        'lower_height': {'$min': '$height'},
        'upper_height': {'$max': '$height'},
        'avg_difficulty': {'$avg': '$difficulty'},
    }
    assert pipeline[-1] == {'$sort': {'_id': 1}}
    assert {'$project': {'_id': 0, 'height': 1, 'difficulty': 1}} in pipeline


def test_integral_pipeline_size_does_not_grow_with_bucket_count():
    small = db_queries.get_blocks_average_difficulty_query(1, 7, 17)
    large = db_queries.get_blocks_average_difficulty_query(1, 7, 10007)
    assert len(list(pipeline_nodes(large))) == len(list(pipeline_nodes(small)))
    assert len(json.dumps(large)) < 2048


def test_overflowing_height_offset_keeps_legacy_pipeline(bucket_collection):
    interval, lower, upper = 2 ** 62 + 1, -(2 ** 63), 2 ** 62 + 3
    insert_rows(bucket_collection, [
        {'height': 0, 'difficulty': 2.0},
        {'height': 1, 'difficulty': 4.0},
    ])
    assert assert_legacy_equivalent(bucket_collection, interval, lower, upper) == [
        {'_id': -(2 ** 62) + 1, 'lower_height': 0,
         'upper_height': 1, 'avg_difficulty': 3.0},
    ]
    assert db_queries.get_blocks_average_difficulty_query(interval, lower, upper) == (
        legacy_pipeline(interval, lower, upper))


@pytest.mark.parametrize('interval,lower,upper', [
    pytest.param(2.0, 1, 6, id='float-interval'),
    pytest.param(2, 1.0, 6, id='float-lower'),
    pytest.param(2, 1, 6.0, id='float-upper'),
    pytest.param(2.5, 1.5, 9.5, id='fractional-arguments'),
    pytest.param(True, 1, 6, id='bool-interval'),
    pytest.param(2, True, 6, id='bool-lower'),
    pytest.param(1, 0, True, id='bool-upper'),
    pytest.param(5, 7, 7, id='single-height'),
    pytest.param(5, 7, 9, id='less-than-one-interval'),
    pytest.param(5, 9, 7, id='reversed-small-range'),
    pytest.param(-2, 5, 1, id='negative-interval-reversed-range'),
])
def test_unsupported_parameters_keep_legacy_pipeline(interval, lower, upper):
    assert db_queries.get_blocks_average_difficulty_query(interval, lower, upper) == (
        legacy_pipeline(interval, lower, upper))


@pytest.mark.parametrize('interval,lower,upper,error', [
    (0, 1, 6, ZeroDivisionError),
    (-2, 1, 6, IndexError),
    (2, 6, 1, IndexError),
    ('2', 1, 6, TypeError),
    (2, None, 6, TypeError),
    (2, 1, '6', TypeError),
    (2, 1, float('nan'), ValueError),
    (2, 1, float('inf'), OverflowError),
])
def test_unsupported_parameters_keep_legacy_builder_errors(interval, lower, upper, error):
    with pytest.raises(error) as legacy_error:
        legacy_pipeline(interval, lower, upper)
    with pytest.raises(error) as actual_error:
        db_queries.get_blocks_average_difficulty_query(interval, lower, upper)
    assert str(actual_error.value) == str(legacy_error.value)


@pytest.mark.parametrize('interval,lower,upper', [
    (2.0, 1, 6),
    (2, 1.0, 6),
    (2, 1, 6.0),
    (2.5, 1.5, 9.5),
])
def test_nonintegral_fallback_results_match_legacy(bucket_collection, interval, lower, upper):
    insert_rows(bucket_collection, [
        {'height': height, 'difficulty': float(height)}
        for height in range(1, 11)
    ])
    assert_legacy_equivalent(bucket_collection, interval, lower, upper)


@pytest.mark.parametrize('interval,lower,upper', [
    (5, 7, 7),
    (5, 7, 9),
    (5, 9, 7),
    (-2, 5, 1),
])
def test_invalid_bucket_ranges_keep_mongo_rejection(bucket_collection, interval, lower, upper):
    from pymongo.errors import OperationFailure

    insert_rows(bucket_collection, [{'height': 7, 'difficulty': 1.0}])
    with pytest.raises(OperationFailure) as legacy_error:
        list(bucket_collection.aggregate(legacy_pipeline(interval, lower, upper)))
    pipeline = db_queries.get_blocks_average_difficulty_query(interval, lower, upper)
    with pytest.raises(OperationFailure) as actual_error:
        list(bucket_collection.aggregate(pipeline))
    assert actual_error.value.code == legacy_error.value.code
