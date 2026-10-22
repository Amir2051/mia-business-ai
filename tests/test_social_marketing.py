import sys
from fastapi.testclient import TestClient

def test_social_providers_and_meta_fail_closed(monkeypatch):
    monkeypatch.setenv('RZ_API_KEY','social-test-key')
    monkeypatch.setenv('RZ_LOCAL_DATA_PATH','/tmp/rz-social-test.sqlite3')
    monkeypatch.setenv('DATABASE_URL','')
    monkeypatch.delenv('META_APP_ID',raising=False)
    monkeypatch.delenv('META_APP_SECRET',raising=False)
    monkeypatch.delenv('META_REDIRECT_URI',raising=False)
    sys.modules.pop('app.main',None)
    import app.main as module
    client=TestClient(module.app)
    try:
        headers={'X-API-Key':'social-test-key'}
        r=client.get('/v1/social/providers',headers=headers)
        assert r.status_code==200
        assert r.json()['providers'][0]['configured'] is False
        r=client.get('/v1/social/meta/connect',headers=headers)
        assert r.status_code==503
        assert 'META_APP_ID' in r.json()['detail']
        assert client.get('/v1/social/accounts').status_code==401
    finally:
        client.close()
        sys.modules.pop('app.main',None)
