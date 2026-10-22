import sys
from fastapi.testclient import TestClient

def test_preferences_are_saved_and_used_for_user(monkeypatch, tmp_path):
    monkeypatch.setenv('RZ_API_KEY','prefs-test-key')
    monkeypatch.setenv('RZ_LOCAL_DATA_PATH',str(tmp_path / 'prefs.sqlite3'))
    monkeypatch.setenv('DATABASE_URL','')
    sys.modules.pop('app.main',None)
    import app.main as module
    client=TestClient(module.app)
    headers={'X-API-Key':'prefs-test-key'}
    try:
        response=client.get('/v1/user/preferences',headers=headers)
        assert response.status_code==200
        assert response.json()['preferences']['personality']=='balanced'
        payload={'personality':'friendly','custom_instructions':'Use practical examples.','response_length':'detailed','preferred_language':'en'}
        response=client.put('/v1/user/preferences',headers=headers,json=payload)
        assert response.status_code==200
        assert response.json()['preferences']['custom_instructions']=='Use practical examples.'
        from app.user_preferences import personalized_system
        system=personalized_system('local','Base prompt')
        assert 'warm, encouraging' in system
        assert 'Use practical examples.' in system
        assert 'useful detail' in system
    finally:
        client.close()
        sys.modules.pop('app.main',None)

def test_preferences_reject_unsupported_values(monkeypatch, tmp_path):
    monkeypatch.setenv('RZ_API_KEY','prefs-test-key')
    monkeypatch.setenv('RZ_LOCAL_DATA_PATH',str(tmp_path / 'prefs-invalid.sqlite3'))
    monkeypatch.setenv('DATABASE_URL','')
    sys.modules.pop('app.main',None)
    import app.main as module
    client=TestClient(module.app)
    try:
        r=client.put('/v1/user/preferences',headers={'X-API-Key':'prefs-test-key'},json={'personality':'unsafe','custom_instructions':'','response_length':'standard','preferred_language':'auto'})
        assert r.status_code==422
    finally:
        client.close()
        sys.modules.pop('app.main',None)
