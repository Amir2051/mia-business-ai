import os, sys
from pathlib import Path
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def _client(monkeypatch, tmp_path):
    monkeypatch.setenv('RZ_API_KEY', 'business-doc-test-key')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'test-provider-key')
    monkeypatch.setenv('RZ_LOCAL_DATA_PATH', str(tmp_path / 'rz.sqlite3'))
    monkeypatch.setenv('MIA_BUSINESS_DATA_PATH', str(tmp_path / 'mia-business.sqlite3'))
    monkeypatch.setenv('DATABASE_URL', '')
    sys.modules.pop('app.main', None)
    import app.main as module
    return module, TestClient(module.app)

def test_document_routes_require_auth(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    assert client.get('/v1/business/files').status_code == 401
    assert client.post('/v1/business/files', json={'title':'Proposal','content':'Hello'}).status_code == 401

def test_create_preview_download_and_user_scope(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    headers={'X-API-Key':'business-doc-test-key'}
    made=client.post('/v1/business/files',headers=headers,json={'title':'Client Proposal','content':'# Overview\n\nScope and deliverables.','format':'pdf'})
    assert made.status_code == 200, made.text
    fid=made.json()['file']['id']
    preview=client.get('/v1/business/files/'+fid,headers=headers)
    assert preview.status_code == 200 and preview.json()['file']['content'].startswith('# Overview')
    listing=client.get('/v1/business/files',headers=headers).json()
    assert listing['count']==1 and listing['files'][0]['id']==fid
    download=client.get(f'/v1/business/files/{fid}/download',headers=headers)
    assert download.status_code==200 and download.content.startswith(b'%PDF-')
    assert 'attachment' in download.headers['content-disposition']
    inline=client.get(f'/v1/business/files/{fid}/render',headers=headers)
    assert inline.status_code==200 and inline.content.startswith(b'%PDF-')
    assert inline.headers['content-disposition'].startswith('inline;')
    assert client.get('/v1/business/files/'+fid,headers={'X-API-Key':'wrong-key'}).status_code==401
    assert client.get('/v1/business/files/not-found',headers=headers).status_code==404

def test_file_format_validation_and_text_download(monkeypatch,tmp_path):
    _,client=_client(monkeypatch,tmp_path); h={'X-API-Key':'business-doc-test-key'}
    assert client.post('/v1/business/files',headers=h,json={'title':'Bad','content':'body','format':'exe'}).status_code==422
    made=client.post('/v1/business/files',headers=h,json={'title':'Notes','content':'Hello MIA','format':'txt'})
    assert made.status_code==200
    dl=client.get(made.json()['file']['download_url'],headers=h)
    assert dl.status_code==200 and dl.content==b'Hello MIA'

def test_invoice_calculates_totals_and_downloads_pdf(monkeypatch,tmp_path):
    _,client=_client(monkeypatch,tmp_path); h={'X-API-Key':'business-doc-test-key'}
    payload={'business_name':'MIA Consulting','customer_name':'Northstar Studio','currency':'USD','tax_rate':'8.25','discount':'10','items':[{'description':'Consulting','quantity':'2','unit_price':'125'},{'description':'Research','quantity':'1','unit_price':'75'}]}
    made=client.post('/v1/business/invoices',headers=h,json=payload)
    assert made.status_code==200,made.text
    inv=made.json()['invoice']
    assert inv['subtotal']=='325.00' and inv['discount']=='10.00' and inv['tax']=='25.99' and inv['total']=='340.99'
    preview=client.get(inv['preview_url'],headers=h).json()['file']
    assert preview['kind']=='invoice' and 'TOTAL DUE: 340.99 USD' in preview['content']
    dl=client.get(inv['download_url'],headers=h)
    assert dl.status_code==200 and dl.content.startswith(b'%PDF-')

def test_files_are_scoped_to_authenticated_user(monkeypatch,tmp_path):
    module,client=_client(monkeypatch,tmp_path); h={'X-API-Key':'business-doc-test-key'}
    made=client.post('/v1/business/files',headers=h,json={'title':'Private','content':'private text','format':'txt'}).json()['file']
    from app.business_documents import _file_row
    assert _file_row('another-user',made['id']) is None
    assert _file_row('local',made['id']) is not None

def test_supported_export_formats_generate_downloadable_files(monkeypatch,tmp_path):
    import zipfile, io
    _,client=_client(monkeypatch,tmp_path); h={'X-API-Key':'business-doc-test-key'}
    for fmt in ('docx','md','json','csv','html'):
        made=client.post('/v1/business/files',headers=h,json={'title':'Format check','content':'hello format','format':fmt})
        assert made.status_code==200, (fmt,made.text)
        response=client.get(made.json()['file']['download_url'],headers=h)
        assert response.status_code==200 and len(response.content)>0,fmt
        if fmt=='docx': assert zipfile.is_zipfile(io.BytesIO(response.content))
        if fmt=='json': assert b'"content"' in response.content
        if fmt=='html': assert b'<!doctype html>' in response.content

def test_business_profile_requires_auth_and_persists_per_user(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    assert client.get('/v1/business/profile').status_code == 401
    h = {'X-API-Key': 'business-doc-test-key'}
    profile = {
        'business_name': 'Northstar Studio',
        'industry': 'Consulting',
        'target_customers': 'Solo founders and small teams',
        'offer': 'Operations consulting and automation',
        'goals': 'Win five paying customers this quarter',
        'brand_voice': 'Clear, premium, trustworthy',
        'constraints': 'No unverified claims; ask before external actions',
    }
    saved = client.put('/v1/business/profile', headers=h, json=profile)
    assert saved.status_code == 200, saved.text
    assert saved.json()['saved'] is True
    assert client.get('/v1/business/profile', headers=h).json()['profile']['business_name'] == 'Northstar Studio'
    from app.business_documents import get_business_profile
    assert get_business_profile('another-user')['business_name'] == ''
    assert get_business_profile('local')['constraints'] == profile['constraints']


def test_chat_uses_saved_business_profile_as_reference_context(monkeypatch, tmp_path):
    module, client = _client(monkeypatch, tmp_path)
    h = {'X-API-Key': 'business-doc-test-key'}
    saved = client.put('/v1/business/profile', headers=h, json={
        'business_name': 'Northstar Studio', 'industry': 'Consulting',
        'target_customers': 'Independent consultants',
    })
    assert saved.status_code == 200, saved.text
    captured = {}
    def fake_complete_message(**kwargs):
        captured.update(kwargs)
        return {'reply': 'ok', 'session_id': kwargs['session_id'], 'provider': 'test'}
    monkeypatch.setattr(module, '_complete_message', fake_complete_message)
    response = client.post('/v1/chat', headers=h, json={'message': 'Help me find customers'})
    assert response.status_code == 200, response.text
    assert 'USER-PROVIDED BUSINESS PROFILE' in captured['system']
    assert 'Business name: Northstar Studio' in captured['system']
    assert 'Target customers: Independent consultants' in captured['system']


def test_capability_database_default_is_isolated_from_original_rz_agent(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('RZ_LOCAL_DATA_PATH', raising=False)
    monkeypatch.setenv('DATABASE_URL', '')
    from app import capabilities
    conn, kind = capabilities._db()
    try:
        path = conn.execute('PRAGMA database_list').fetchone()[2]
    finally:
        conn.close()
    assert kind == 'sqlite'
    assert Path(path) == tmp_path / '.mia-business-ai' / 'capabilities.sqlite3'

def test_client_pipeline_create_filter_update_delete_and_private_storage(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    h = {'X-API-Key': 'business-doc-test-key'}
    assert client.get('/v1/business/clients').status_code == 401
    made = client.post('/v1/business/clients', headers=h, json={
        'name': 'Alex Rivera', 'company': 'Northstar Studio', 'email': 'alex@example.com',
        'stage': 'qualified', 'expected_value': '2500', 'next_follow_up': '2026-10-25',
        'notes': 'Needs a short discovery call.',
    })
    assert made.status_code == 200, made.text
    record = made.json()['client']; cid = record['id']
    listing = client.get('/v1/business/clients', headers=h).json()
    assert listing['count'] == 1 and listing['open_pipeline_value'] == '2500.00'
    assert listing['clients'][0]['email'] == 'alex@example.com'
    assert client.get('/v1/business/clients?stage=won', headers=h).json()['count'] == 0
    updated = client.put(f'/v1/business/clients/{cid}', headers=h, json={
        'name': 'Alex Rivera', 'company': 'Northstar Studio', 'email': 'alex@example.com',
        'stage': 'proposal', 'expected_value': '3000', 'next_follow_up': '2026-10-26',
        'notes': 'Proposal sent for review.',
    })
    assert updated.status_code == 200, updated.text
    assert client.get('/v1/business/clients?stage=proposal', headers=h).json()['count'] == 1
    assert client.get('/v1/business/clients', headers=h).json()['open_pipeline_value'] == '3000.00'
    from app.business_documents import _db
    conn = _db()
    try:
        assert conn.execute('SELECT COUNT(*) FROM business_clients WHERE user_id=?', ('another-user',)).fetchone()[0] == 0
    finally:
        conn.close()
    assert client.delete(f'/v1/business/clients/{cid}', headers=h).json()['deleted'] is True
    assert client.delete(f'/v1/business/clients/{cid}', headers=h).status_code == 404


def test_client_pipeline_rejects_invalid_stage_and_followup_date(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path); h = {'X-API-Key': 'business-doc-test-key'}
    base = {'name': 'Taylor', 'expected_value': '10'}
    assert client.post('/v1/business/clients', headers=h, json={**base, 'stage': 'maybe'}).status_code == 422
    assert client.post('/v1/business/clients', headers=h, json={**base, 'next_follow_up': 'next Tuesday'}).status_code == 422

def test_growth_plan_requires_auth_and_saves_updates_by_user_and_date(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    assert client.get('/v1/business/growth-plans').status_code == 401
    h = {'X-API-Key': 'business-doc-test-key'}
    payload = {
        'plan_date': '2026-10-22', 'primary_goal': 'Talk to three target customers',
        'hours_available': 4, 'success_metric': '3 customer conversations',
        'plan_text': '# Daily plan\n1. Prepare questions\n2. Contact prospects',
        'reflection': '', 'status': 'active',
    }
    first = client.post('/v1/business/growth-plans', headers=h, json=payload)
    assert first.status_code == 200, first.text
    assert first.json()['saved'] is True
    edited = {**payload, 'hours_available': 3, 'plan_text': '# Revised plan',
              'reflection': 'Reached out to two people.', 'status': 'completed'}
    second = client.post('/v1/business/growth-plans', headers=h, json=edited)
    assert second.status_code == 200, second.text
    assert second.json()['plan']['status'] == 'completed'
    listing = client.get('/v1/business/growth-plans', headers=h).json()
    assert listing['count'] == 1
    assert listing['plans'][0]['plan_text'] == '# Revised plan'
    assert listing['plans'][0]['reflection'] == 'Reached out to two people.'
    from app.business_documents import _db
    conn = _db()
    try:
        assert conn.execute('SELECT COUNT(*) FROM business_growth_plans WHERE user_id=?', ('another-user',)).fetchone()[0] == 0
    finally:
        conn.close()


def test_growth_plan_validates_date_goal_hours_and_length(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path); h = {'X-API-Key': 'business-doc-test-key'}
    base = {'plan_date': '2026-02-30', 'primary_goal': 'Get customers', 'hours_available': 4, 'plan_text': 'Plan'}
    assert client.post('/v1/business/growth-plans', headers=h, json=base).status_code == 422
    assert client.post('/v1/business/growth-plans', headers=h, json={**base, 'plan_date':'2026-10-22', 'hours_available': 99}).status_code == 422
    assert client.post('/v1/business/growth-plans', headers=h, json={**base, 'plan_date':'2026-10-22', 'primary_goal':'   '}).status_code == 422
    assert client.get('/v1/business/growth-plans?limit=0', headers=h).status_code == 422

def test_postgres_compat_connection_translates_sqlite_placeholders():
    from app.business_documents import _PostgresCompatConnection

    class FakeConnection:
        def __init__(self):
            self.call = None
        def execute(self, sql, params):
            self.call = (sql, params)
            return 'cursor'
        def commit(self):
            return 'committed'
        def close(self):
            return 'closed'

    raw = FakeConnection()
    conn = _PostgresCompatConnection(raw)
    assert conn.execute('SELECT * FROM t WHERE a=? AND b=? LIMIT ?', ('x', 'y', 5)) == 'cursor'
    assert raw.call == ('SELECT * FROM t WHERE a=%s AND b=%s LIMIT %s', ('x', 'y', 5))
    assert conn.commit() == 'committed'
    assert conn.close() == 'closed'


def test_business_overview_aggregates_owner_scoped_pipeline_and_followups(monkeypatch, tmp_path):
    from datetime import date, timedelta
    _, client = _client(monkeypatch, tmp_path)
    h = {'X-API-Key': 'business-doc-test-key'}
    assert client.get('/v1/business/overview').status_code == 401
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    next_week = (date.today() + timedelta(days=3)).isoformat()
    for payload in [
        {'name':'Overdue Prospect','stage':'qualified','expected_value':'1500','next_follow_up':yesterday},
        {'name':'Upcoming Prospect','stage':'proposal','expected_value':'2500','next_follow_up':next_week},
        {'name':'Closed Won','stage':'won','expected_value':'9000','next_follow_up':today},
        {'name':'Lost Prospect','stage':'lost','expected_value':'700','next_follow_up':today},
    ]:
        made = client.post('/v1/business/clients', headers=h, json=payload)
        assert made.status_code == 200, made.text
    doc = client.post('/v1/business/files', headers=h, json={'title':'Proposal','content':'Scope','format':'txt'})
    assert doc.status_code == 200
    invoice = client.post('/v1/business/invoices', headers=h, json={'customer_name':'Client','items':[{'description':'Consulting','quantity':'1','unit_price':'125'}]})
    assert invoice.status_code == 200
    overview = client.get('/v1/business/overview', headers=h)
    assert overview.status_code == 200, overview.text
    data = overview.json()
    assert data['active_leads'] == 2
    assert data['won_clients'] == 1
    assert data['open_pipeline_value'] == '4000.00'
    assert data['followups_due'] == 1
    assert data['followups_next_7_days'] == 1
    assert data['documents_total'] == 2
    assert data['invoices_total'] == 1
    assert len(data['followups']) == 2
    from app.business_documents import _db
    conn = _db()
    try:
        assert conn.execute('SELECT COUNT(*) FROM business_clients WHERE user_id=?', ('another-user',)).fetchone()[0] == 0
    finally:
        conn.close()


def test_client_activity_history_is_private_and_records_next_step(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    h = {'X-API-Key': 'business-doc-test-key'}
    created = client.post('/v1/business/clients', headers=h, json={
        'name': 'Morgan Lee', 'company': 'Solo Co', 'stage': 'contacted',
        'expected_value': '1200',
    })
    assert created.status_code == 200, created.text
    cid = created.json()['client']['id']
    path = f'/v1/business/clients/{cid}/activities'
    assert client.get(path).status_code == 401
    logged = client.post(path, headers=h, json={
        'activity_type': 'call', 'summary': 'Discussed onboarding needs.',
        'next_step': 'Send a proposal draft for review',
    })
    assert logged.status_code == 200, logged.text
    assert logged.json()['saved'] is True
    history = client.get(path, headers=h)
    assert history.status_code == 200
    assert history.json()['count'] == 1
    assert history.json()['activities'][0]['type'] == 'call'
    assert history.json()['activities'][0]['next_step'] == 'Send a proposal draft for review'
    from app.business_documents import _db
    conn = _db()
    try:
        assert conn.execute('SELECT COUNT(*) FROM business_client_activities WHERE user_id=?', ('another-user',)).fetchone()[0] == 0
        note = conn.execute('SELECT notes FROM business_clients WHERE user_id=? AND client_id=?', ('local', cid)).fetchone()[0]
        assert 'Next step: Send a proposal draft for review' in note
    finally:
        conn.close()
    assert client.get(f'/v1/business/clients/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/activities', headers=h).status_code == 404
    assert client.post(path, headers=h, json={'activity_type':'email','summary':'   '}).status_code == 422
