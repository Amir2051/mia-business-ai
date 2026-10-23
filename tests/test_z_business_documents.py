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
