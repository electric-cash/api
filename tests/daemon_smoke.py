"""Run actual image ingestion against disposable MongoDB and the RPC fixture."""
import json
import os
import re
import sys


def validate_environment(values):
    name = values.get('DB_NAME', '')
    allowed = (
        re.fullmatch(r'api_cache_http_[A-Za-z0-9_]+_daemon', name) is not None and
        len(name) <= 63 and
        values.get('DB_URL') == 'api-cache-test-mongo' and
        values.get('DB_PROTOCOL', 'mongodb') == 'mongodb' and
        values.get('DB_PORT', '27017') == '27017' and
        values.get('RPC_HOST') == 'api-cache-test-rpc' and
        values.get('RPC_PORT', '8332') == '8332' and
        not any(values.get(key) for key in (
            'DB_USERNAME', 'DB_PASSWORD', 'DB_URI_PARAMS', 'RPC_USER', 'RPC_PASSWORD')))
    if not allowed:
        raise RuntimeError('Daemon smoke requires isolated local services and a fixture database')
    return name


def main():
    name = validate_environment(os.environ)
    from pymongo import MongoClient

    client = MongoClient('mongodb://api-cache-test-mongo:27017', serverSelectionTimeoutMS=5000)
    if client[name].list_collection_names():
        raise RuntimeError('Daemon smoke database must be empty')

    sys.path.insert(0, '/app')
    from mongoengine import connect, disconnect
    from db.db import get_db_uri
    from db.models import Address, Block, UTXO
    from db import query_cache
    import daemon

    try:
        connect(host=get_db_uri())
        daemon.update_analyzer()
        daemon.update_blockchain()
        generation = query_cache.get_generation()
        assert generation is not None
        assert Block.objects.count() == 2
        assert Address.objects.get(hash='sender').balance == 0
        assert Address.objects.get(hash='recipient').balance == 3
        assert UTXO.objects(id='origin0').count() == 0
        assert UTXO.objects(id='origin1').count() == 0
        assert UTXO.objects.get(id='spend0').value == 3
        daemon.update_blockchain()
        assert query_cache.get_generation() == generation
        print(json.dumps({'blocks': 2, 'recipient_balance': 3,
                          'untracked_output_spend': 'passed', 'result': 'passed'}))
    finally:
        disconnect()
        client.drop_database(name)
        client.close()


if __name__ == '__main__':
    main()
