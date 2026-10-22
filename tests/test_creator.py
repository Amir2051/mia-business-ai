import os
import sys
from pathlib import Path
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def _client(monkeypatch, tmp_path):
    monkeypatch.setenv('RZ_API_KEY', 'creator-test-key')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'test-provider-key')
    monkeypatch.setenv('RZ_LOCAL_DATA_PATH', str(tmp_path / 'creator.sqlite3'))
    monkeypatch.setenv('DATABASE_URL', '')
    sys.modules.pop('app.main', None)
    import app.main as module
    return module, TestClient(module.app)

def test_creator_requires_existing_auth(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    assert client.get('/v1/creator/projects').status_code == 401
    assert client.post('/v1/creator/projects', json={'title':'A book'}).status_code == 401

def test_creator_project_autosave_history_and_restore(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    headers = {'X-API-Key':'creator-test-key'}
    created = client.post('/v1/creator/projects', headers=headers, json={
        'title':'The Glass City', 'kind':'book', 'description':'A science-fiction novel'
    })
    assert created.status_code == 200, created.text
    project = created.json()['project']
    pid = project['id']
    assert project['content'] == ''
    first = client.put(f'/v1/creator/projects/{pid}/draft', headers=headers, json={'content':'Chapter one'})
    assert first.status_code == 200 and first.json()['changed'] is True
    unchanged = client.put(f'/v1/creator/projects/{pid}/draft', headers=headers, json={'content':'Chapter one'})
    assert unchanged.status_code == 200 and unchanged.json()['changed'] is False
    second = client.put(f'/v1/creator/projects/{pid}/draft', headers=headers, json={'content':'Chapter one\nNew paragraph'})
    assert second.status_code == 200 and second.json()['changed'] is True
    revisions = client.get(f'/v1/creator/projects/{pid}/revisions', headers=headers).json()['revisions']
    assert len(revisions) == 2
    previous = next(r for r in revisions if r['content_length'] == len('Chapter one'))
    old = client.get(f"/v1/creator/projects/{pid}/revisions/{previous['id']}", headers=headers)
    assert old.status_code == 200 and old.json()['revision']['content'] == 'Chapter one'
    restored = client.post(f"/v1/creator/projects/{pid}/restore/{previous['id']}", headers=headers)
    assert restored.status_code == 200 and restored.json()['content'] == 'Chapter one'
    fetched = client.get(f'/v1/creator/projects/{pid}', headers=headers).json()['project']
    assert fetched['content'] == 'Chapter one'
    assert client.get('/v1/creator/projects', headers=headers).json()['count'] == 1

def test_creator_validates_types_and_limits(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    headers = {'X-API-Key':'creator-test-key'}
    assert client.post('/v1/creator/projects', headers=headers, json={'title':'   ','kind':'book'}).status_code == 422
    assert client.post('/v1/creator/projects', headers=headers, json={'title':'Bad type','kind':'shell'}).status_code == 422
    project = client.post('/v1/creator/projects', headers=headers, json={'title':'Valid','kind':'story'}).json()['project']
    assert client.put(f"/v1/creator/projects/{project['id']}/draft", headers=headers, json={'content':'x'*100001}).status_code == 422
    assert client.get('/v1/creator/projects/not-owned-or-found', headers=headers).status_code == 404
