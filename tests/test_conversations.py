import sys
from fastapi.testclient import TestClient


def test_conversation_persists_and_is_owner_scoped(monkeypatch, tmp_path):
    monkeypatch.setenv('RZ_API_KEY', 'conversation-test-key')
    monkeypatch.setenv('RZ_LOCAL_DATA_PATH', str(tmp_path / 'conversation.sqlite3'))
    monkeypatch.setenv('DATABASE_URL', '')
    sys.modules.pop('app.main', None)
    import app.main as module
    client = TestClient(module.app)
    headers = {'X-API-Key': 'conversation-test-key'}
    try:
        owner = 'local'
        session = owner + ':persistent-session'
        history = [{'role': 'user', 'content': 'Remember this conversation'}, {'role': 'assistant', 'content': 'I can restore it.'}]
        module.cap_services.save_conversation(owner, session, history, 'test-model')
        found = module.cap_services.get_conversation(owner, session)
        assert found['history'] == history
        assert found['model'] == 'test-model'
        # Simulate a process restart: the in-memory session is gone but the DB record remains.
        with module._store_lock:
            module._conversations.clear()
        restored_session = module._get_session(session)
        assert restored_session['history'] == history
        assert restored_session['model'] == 'test-model'
        assert module.cap_services.get_conversation('another-user', session) is None
        listed = client.get('/v1/conversations', headers=headers)
        assert listed.status_code == 200
        assert any(item['session_id'] == session for item in listed.json()['conversations'])
        restored = client.get('/v1/conversations/' + session, headers=headers)
        assert restored.status_code == 200
        assert restored.json()['conversation']['history'] == history
        forbidden = client.get('/v1/conversations/other-user:private', headers=headers)
        assert forbidden.status_code == 404
        deleted = client.post('/v1/conversations/' + session, headers=headers)
        assert deleted.status_code == 200
        assert module.cap_services.get_conversation(owner, session) is None
    finally:
        client.close()
        sys.modules.pop('app.main', None)
