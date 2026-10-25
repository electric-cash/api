"""Read-only index metadata check; never creates indexes or certifies a rollout."""

import argparse
import json

from pymongo import MongoClient, ReadPreference
from pymongo.errors import PyMongoError

from db import db


REQUIRED_INDEXES = {
    'height_1': [('height', 1)],
    'height_1_difficulty_1': [('height', 1), ('difficulty', 1)],
}


class PreflightError(ValueError):
    pass


def validate_indexes(indexes):
    found = set()
    for entry in indexes:
        spec = entry.get('spec', entry)
        name = spec.get('name')
        if name not in REQUIRED_INDEXES:
            continue
        if name in found:
            raise PreflightError('Required index has a duplicate name: ' + name)
        if 'buildUUID' in entry:
            raise PreflightError('Required index is still building: ' + name)
        if list(spec.get('key', {}).items()) != REQUIRED_INDEXES[name]:
            raise PreflightError('Required index has incompatible keys: ' + name)
        if (any(spec.get(option, False) for option in
                ('unique', 'sparse', 'hidden', 'prepareUnique')) or
                any(option in spec for option in ('partialFilterExpression', 'expireAfterSeconds')) or
                spec.get('collation', {}).get('locale', 'simple') != 'simple'):
            raise PreflightError('Required index has incompatible options: ' + name)
        found.add(name)
    missing = set(REQUIRED_INDEXES) - found
    if missing:
        raise PreflightError('Required index is missing: ' + ', '.join(sorted(missing)))


def check_database(database):
    # Plain listIndexes also includes unfinished builds, so request their UUIDs.
    result = database.command(
        'listIndexes', 'block', includeBuildUUIDs=True,
        cursor={'batchSize': 100}, maxTimeMS=5000,
        read_preference=ReadPreference.PRIMARY,
    )
    cursor = result.get('cursor', {})
    if (cursor.get('id') != 0 or cursor.get('ns') != database.name + '.block' or
            not isinstance(cursor.get('firstBatch'), list)):
        raise PreflightError('Incomplete or unexpected index metadata; check stopped')
    validate_indexes(cursor['firstBatch'])
    return list(REQUIRED_INDEXES)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-database', required=True)
    parser.add_argument('--expected-host', required=True)
    args = parser.parse_args(argv)
    try:
        if (not args.expected_database or not args.expected_host or
                args.expected_database != db.db_name or args.expected_host != db.db_host):
            raise PreflightError('Configured database or host does not match the expected target')
        with MongoClient(
                db.get_db_uri(primary_reads=True), serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000, socketTimeoutMS=5000, retryReads=False,
                appname='difficulty-index-preflight') as client:
            indexes = check_database(client[args.expected_database])
    except (PyMongoError, ValueError) as error:
        # Driver messages can contain connection details; return only their class.
        reason = str(error) if isinstance(error, PreflightError) else type(error).__name__
        print(json.dumps({'status': 'blocked', 'reason': reason}))
        return 1
    print(json.dumps({
        'status': 'metadata_verified', 'indexes': indexes,
        'query_plan_verified': False, 'deployment_approved': False,
    }))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
