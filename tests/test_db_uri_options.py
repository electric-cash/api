import pytest
from pymongo.uri_parser import parse_uri

from db import db


@pytest.mark.parametrize('properties', [
    'SERVICE_NAME:mongodb',
    'SERVICE_NAME:mongodb,CANONICALIZE_HOST_NAME:true',
])
def test_primary_uri_preserves_opaque_authentication_properties(monkeypatch, properties):
    parameters = ('?authMechanismProperties=' + properties +
                  '&readPreference=secondaryPreferred&retryWrites=false')
    monkeypatch.setattr(db, 'db_para', parameters)
    uri = db.get_db_uri(primary_reads=True)
    assert 'authMechanismProperties=' + properties + '&' in uri
    options = parse_uri(uri)['options']
    assert options['readPreference'] == 'primary'
    assert options['authMechanismProperties']['SERVICE_NAME'] == 'mongodb'
    assert options['retryWrites'] is False
