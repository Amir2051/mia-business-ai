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
        # Verify the real chat endpoint supplies saved preferences to MIA's completion layer.
        captured = {}
        def fake_complete_message(**kwargs):
            captured.update(kwargs)
            return {'reply': 'test reply', 'session_id': kwargs['session_id'], 'provider': 'test'}
        monkeypatch.setattr(module, '_complete_message', fake_complete_message)
        chat_response = client.post('/v1/chat', headers=headers, json={'message': 'Explain a concept'})
        assert chat_response.status_code == 200
        assert 'warm, encouraging' in captured['system']
        assert 'Use practical examples.' in captured['system']
        assert 'Preferred response language: en' in captured['system']
        assert 'useful detail' in captured['system']
    finally:
        client.close()
        sys.modules.pop('app.main',None)

def test_preferences_are_isolated_between_user_ids(monkeypatch, tmp_path):
    monkeypatch.setenv('RZ_API_KEY','prefs-isolation-key')
    monkeypatch.setenv('RZ_LOCAL_DATA_PATH',str(tmp_path / 'prefs-isolation.sqlite3'))
    monkeypatch.setenv('DATABASE_URL','')
    sys.modules.pop('app.main',None)
    import app.main as module
    from app.user_preferences import get_preferences, save_preferences, Preferences
    from unittest.mock import patch
    client=TestClient(module.app)
    try:
        # Use the same persistence layer with two distinct authenticated identities.
        with patch('app.user_preferences._user', return_value={'id':'user-one'}):
            save_preferences(Preferences(personality='creative',custom_instructions='User one only',response_length='detailed',preferred_language='en'), {'id':'user-one'})
        assert get_preferences('user-one')['custom_instructions']=='User one only'
        assert get_preferences('user-two')['custom_instructions']==''
        assert get_preferences('user-two')['personality']=='balanced'
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
