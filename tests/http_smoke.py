"""Exercise a locally built, unmodified gunicorn image against synthetic data."""
import json
import os
import re
import time
from urllib.parse import urlsplit


def validate_environment(values):
    uri = values.get('TEST_MONGODB_URI', '')
    api = values.get('TEST_API_URL', '')
    name = values.get('TEST_DATABASE', '')
    message = 'HTTP smoke requires isolated local services and a fixture database'
    try:
        mongo = urlsplit(uri)
        http = urlsplit(api)
        host = http.hostname or ''
        port = http.port
        allowed = (
            re.fullmatch(r'api_cache_http_[A-Za-z0-9_]+', name) is not None and
            len(name) <= 63 and
            mongo.scheme == 'mongodb' and
            mongo.netloc in ('api-cache-test-mongo', 'api-cache-test-mongo:27017') and
            mongo.path in ('', '/') and
            http.scheme == 'http' and
            re.fullmatch(r'api-cache-smoke-[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', host) is not None and
            len(host) <= 63 and
            (port is None or 1 <= port <= 65535) and
            http.netloc == host + (':' + str(port) if port is not None else '') and
            http.path in ('', '/') and
            not any(character.isspace() or ord(character) < 32 or
                    ord(character) == 127 or character in '?#' for character in uri + api))
    except ValueError:
        raise RuntimeError(message) from None
    if not allowed:
        raise RuntimeError(message)
    return uri, api.rstrip('/'), name


def main():
    uri, api, name = validate_environment(os.environ)
    import requests
    from pymongo import MongoClient

    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    database = client[name]
    if database.list_collection_names():
        raise RuntimeError('Smoke test database must be empty')
    try:
        database.block.insert_one({'_id': 'block-10', 'height': 10, 'time': 1,
                                   'difficulty': 1, 'insert_time': 1, 'update_time': 1})
        database.address.insert_one({'_id': 'address-one', 'balance': 7,
                                     'txs': [], 'insert_time': 1, 'update_time': 1})
        database.cache_state.insert_one({'_id': 'blockchain', 'generation': 'smoke',
                                         'ready': True})
        database.command('profile', 2)
        for attempt in range(30):
            try:
                response = requests.get(api + '/not-a-route', timeout=2)
                if response.status_code == 404:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError('API did not start')

        for _ in range(10):
            response = requests.get(api + '/status', timeout=5)
            assert response.status_code == 200, response.status_code
            assert response.json() == {'height': 10, 'hashrate': 123, 'total_coin_supply': 7.0}
            response = requests.get(api + '/nonemptywalletsnumber', timeout=5)
            assert response.status_code == 200, response.status_code
            assert response.json() == 1

        aggregates = database.system.profile.count_documents({'command.aggregate': 'address'})
        assert aggregates == 2, aggregates
        database.address.update_one({'_id': 'address-one'}, {'$set': {'balance': 9}})
        database.cache_state.update_one({'_id': 'blockchain'}, {'$set': {'generation': 'next'}})
        assert requests.get(api + '/status', timeout=5).json()['total_coin_supply'] == 9.0
        assert database.system.profile.count_documents({'command.aggregate': 'address'}) == 3
        print(json.dumps({'http_requests': 21, 'initial_address_aggregations': aggregates,
                          'generation_change_observed': True, 'result': 'passed'}))
    finally:
        database.command('profile', 0)
        client.drop_database(name)
        client.close()


if __name__ == '__main__':
    main()
