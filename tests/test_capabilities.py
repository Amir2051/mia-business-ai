import os, sys, tempfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('RZ_API_KEY','test-key')
    monkeypatch.setenv('OPENROUTER_API_KEY','test-provider-key')
    monkeypatch.setenv('RZ_LOCAL_DATA_PATH',str(tmp_path/'cap.sqlite3'))
    monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.delenv('IMAGE_GENERATION_ENDPOINT',raising=False)
    monkeypatch.delenv('IMAGE_GENERATION_API_KEY',raising=False)
    monkeypatch.delenv('IMAGE_GENERATION_MODEL',raising=False)
    sys.modules.pop('app.main',None)
    import app.main as module
    yield module
    with module._store_lock: module._conversations.clear()
    sys.modules.pop('app.main',None)

def test_tools_requires_auth_and_lists_real_capabilities(app):
    client=TestClient(app.app)
    assert client.get('/v1/tools').status_code==401
    data=client.get('/v1/tools',headers={'X-API-Key':'test-key'}).json()
    assert data['capabilities']['github']['read_only'] is True
    assert data['capabilities']['image_generation']['available'] is True
    assert data['capabilities']['image_generation']['provider'] == 'openrouter'
    assert data['capabilities']['image_generation']['model'] == 'bytedance-seed/seedream-4.5'

def test_memory_is_persistent_and_user_scoped(app):
    client=TestClient(app.app); h={'X-API-Key':'test-key'}
    created=client.post('/v1/memory',headers=h,json={'content':'I prefer concise project summaries'}).json()['memory']
    assert client.get('/v1/memory',headers=h).json()['count']==1
    # Account IDs are derived from authenticated identity; separate identity is isolated at service layer.
    from app import capabilities
    assert capabilities.list_memories('another-user')['count']==0
    assert client.delete('/v1/memory/'+created['id'],headers=h).json()['deleted'] is True
    assert client.get('/v1/memory',headers=h).json()['count']==0

def test_tasks_track_steps_and_enforce_owner_scope(app):
    from app import capabilities
    task=capabilities.create_task('alice','Ship capability platform',['Audit','Implement','Test'])
    assert task['status']=='pending'
    updated=capabilities.update_task_step('alice',task['id'],0,'completed')
    assert updated['status']=='in_progress'
    with pytest.raises(KeyError): capabilities.update_task_step('bob',task['id'],1,'completed')
    updated=capabilities.update_task_step('alice',task['id'],1,'completed')
    updated=capabilities.update_task_step('alice',task['id'],2,'completed')
    assert updated['status']=='completed'

def test_task_limits_validate_input(app):
    from app import capabilities
    with pytest.raises(ValueError): capabilities.create_task('alice','bad',[])
    with pytest.raises(ValueError): capabilities.create_task('alice','bad',['x']*21)

def test_research_input_validation(app,monkeypatch):
    from app import capabilities
    with pytest.raises(ValueError): capabilities.web_research('',5)
    with pytest.raises(ValueError): capabilities.web_research('topic',100)
    monkeypatch.setattr(capabilities,'urlopen',lambda *a,**k: (_ for _ in ()).throw(AssertionError('network should not be called')))
    with pytest.raises(ValueError): capabilities.web_research('x',0)

def test_image_generation_uses_openrouter_image_api_and_shared_key(app,monkeypatch):
    import json
    from io import BytesIO
    seen={}
    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self,*args): return False
        def read(self,n=-1):
            return json.dumps({'data':[{'b64_json':'aGVsbG8=','media_type':'image/jpeg'}],'usage':{'cost':0.04}}).encode()
    def fake_urlopen(req,timeout):
        seen['url']=req.full_url
        seen['headers']=req.headers
        seen['body']=json.loads(req.data.decode())
        seen['timeout']=timeout
        return FakeResponse()
    monkeypatch.setattr(app,'urlopen',fake_urlopen)
    client=TestClient(app.app)
    response=client.post('/v1/images/generations',headers={'X-API-Key':'test-key'},json={'prompt':'a cinematic mountain sunrise'})
    assert response.status_code==200
    result=response.json()
    assert result['model']=='bytedance-seed/seedream-4.5'
    assert result['image']['b64_json']=='aGVsbG8='
    assert result['image']['media_type']=='image/jpeg'
    assert result['usage']['cost']==0.04
    assert seen['url']=='https://openrouter.ai/api/v1/images'
    assert seen['headers']['Authorization']=='Bearer test-provider-key'
    assert seen['body']=={'model':'bytedance-seed/seedream-4.5','prompt':'a cinematic mountain sunrise'}
    assert seen['timeout']==120

def test_image_generation_requires_openrouter_key(app,monkeypatch):
    monkeypatch.setattr(app,'OPENROUTER_API_KEY','')
    monkeypatch.delenv('IMAGE_GENERATION_API_KEY',raising=False)
    client=TestClient(app.app)
    response=client.post('/v1/images/generations',headers={'X-API-Key':'test-key'},json={'prompt':'a cinematic mountain sunrise'})
    assert response.status_code==503
    assert 'OPENROUTER_API_KEY' in response.json()['detail']

def test_image_generation_rejects_http_endpoint(app,monkeypatch):
    monkeypatch.setenv('IMAGE_GENERATION_ENDPOINT','http://insecure.example/generate')
    monkeypatch.setenv('IMAGE_GENERATION_API_KEY','test')
    client=TestClient(app.app)
    response=client.post('/v1/images/generations',headers={'X-API-Key':'test-key'},json={'prompt':'a boy riding a horse'})
    assert response.status_code==503
    assert 'HTTPS' in response.json()['detail']


def test_document_extraction_supports_text_and_rejects_paths(app):
    import base64
    from app import capabilities
    result=capabilities.extract_document('notes.md',base64.b64encode(b'# Notes\nhello').decode())
    assert result['text']=='# Notes\nhello'
    assert result['format']=='md'
    with pytest.raises(ValueError): capabilities.extract_document('../secret.txt',base64.b64encode(b'x').decode())
    with pytest.raises(ValueError): capabilities.extract_document('notes.exe',base64.b64encode(b'x').decode())

def test_document_analysis_validates_upload_without_calling_model(app):
    import base64
    client=TestClient(app.app)
    monkeypatch=pytest.MonkeyPatch()
    monkeypatch.setattr(app, '_call_openrouter', lambda *a, **k: {'choices':[{'message':{'content':'Summary: hello'}}]})
    response=client.post('/v1/documents/analyze',headers={'X-API-Key':'test-key'},json={'filename':'notes.txt','content_base64':base64.b64encode(b'hello').decode()})
    assert response.status_code==200
    assert response.json()['analysis']=='Summary: hello'
    monkeypatch.undo()


def test_explicit_research_chat_uses_search_results(app, monkeypatch):
    from app import capabilities
    monkeypatch.setattr(capabilities, 'web_research', lambda query, limit=5: {'query':query,'results':[{'title':'Example source','url':'https://example.com','snippet':'Grounded snippet'}],'count':1})
    seen={}
    def fake(messages, model, temperature, max_tokens):
        seen['messages']=messages
        return {'choices':[{'message':{'content':'According to Example source: https://example.com'}}]}
    monkeypatch.setattr(app, '_call_openrouter', fake)
    response=app._complete_message('system','Please search the web for this topic','local:research',None,None,None)
    assert response.reply.startswith('According to')
    assert any('Grounded snippet' in str(message.get('content')) for message in seen['messages'])

def test_code_review_is_bounded_and_does_not_execute(app, monkeypatch):
    from fastapi.testclient import TestClient
    client=TestClient(app.app)
    monkeypatch.setattr(app, '_call_openrouter', lambda *a, **k: {'choices':[{'message':{'content':'Review result'}}]})
    h={'X-API-Key':'test-key'}
    result=client.post('/v1/code/review',headers=h,json={'language':'python','code':'print(1)','question':'Review it'})
    assert result.status_code==200
    assert result.json()['execution_performed'] is False
    bad=client.post('/v1/code/review',headers=h,json={'code':'x'*20001})
    assert bad.status_code==400

def test_duckduckgo_parser_keeps_title_snippet_and_unwraps_https_destination():
    from app.capabilities import _DDGParser
    parser = _DDGParser()
    parser.feed("""
      <div class="result">
        <a class="result__a" href="/l/?uddg=https%3A%2F%2Fexample.com%2Farticle">Example title</a>
        <a class="result__snippet">A useful <b>source</b> snippet.</a>
      </div>
      <div class="result">
        <a class="result__a" href="https://example.org/page">Second title</a>
        <div class="result__snippet">Second snippet.</div>
      </div>
    """)
    parser.finish()
    assert parser.items == [
        {"title": "Example title", "url": "https://example.com/article", "snippet": "A useful source snippet."},
        {"title": "Second title", "url": "https://example.org/page", "snippet": "Second snippet."},
    ]


def test_duckduckgo_parser_rejects_non_https_destinations():
    from app.capabilities import _DDGParser
    parser = _DDGParser()
    parser.feed('<a class="result__a" href="javascript:alert(1)">Unsafe</a>')
    parser.finish()
    assert parser.items == []


def test_capability_status_discloses_local_sqlite_durability_limit(app, monkeypatch):
    from app import capabilities
    monkeypatch.delenv("DATABASE_URL", raising=False)
    status = capabilities.capabilities()
    assert status["persistent_memory"]["storage"] == "local_sqlite"
    assert status["persistent_memory"]["durable_across_host_restarts"] is False
    assert "may not survive" in status["persistent_memory"]["warning"]
    assert status["web_research"]["available"] is True
    assert status["web_research"]["provider_configured"] is False
