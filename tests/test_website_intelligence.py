from pathlib import Path
import sys
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv('RZ_API_KEY', 'website-test-key')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'test-provider-key')
    monkeypatch.setenv('RZ_LOCAL_DATA_PATH', str(tmp_path / 'rz.sqlite3'))
    monkeypatch.setenv('MIA_BUSINESS_DATA_PATH', str(tmp_path / 'mia-business.sqlite3'))
    monkeypatch.setenv('DATABASE_URL', '')
    sys.modules.pop('app.main', None)
    import app.main as module
    return module, TestClient(module.app)


def test_website_scan_requires_auth(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    assert client.post('/v1/business/website/scan', json={'domain': 'example.com'}).status_code == 401


def test_scan_structures_pages_and_approval_accepts_edits(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    headers = {'X-API-Key': 'website-test-key'}
    pages = {
      'https://example.com/': ('https://example.com/', '''<html><head><title>Northstar Cleaning | Home</title><meta property="og:site_name" content="Northstar Cleaning"><meta name="description" content="Residential and office cleaning for local customers."><script type="application/ld+json">{"@context":"https://schema.org","@type":"LocalBusiness","name":"Northstar Cleaning","description":"Residential and office cleaning for local customers.","telephone":"555-123-4567","address":{"streetAddress":"10 Main St","addressLocality":"Syracuse","addressRegion":"NY"},"makesOffer":[{"name":"Move-out cleaning"}]}</script></head><body><h1>Northstar Cleaning</h1><p>Professional cleaning for homes and offices.</p><a href="/services">Services</a><a href="/PrivacyPolicy">Privacy</a><a href="mailto:hello@example.com">Email</a></body></html>''', [], ''),
      'https://example.com/services': ('https://example.com/services', '<html><head><title>Cleaning Services</title></head><body><h1>Our Services</h1><p>Deep cleaning services for homes.</p><p>Move-out cleaning service for apartments.</p></body></html>', [], ''),
    }
    monkeypatch.setattr('app.website_intelligence._validate_public_url', lambda url: url)
    monkeypatch.setattr('app.website_intelligence._fetch_page', lambda url: pages.get(url, (url, '', [], 'HTTP 404')))
    response = client.post('/v1/business/website/scan', headers=headers, json={'domain': 'https://example.com', 'max_pages': 5})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['pages_crawled'] == 2
    assert result['profile_suggestions']['business_name'] == 'Northstar Cleaning'
    assert result['business_facts']['structured_data_found'] is True
    assert 'hello@example.com' in result['business_facts']['contact']['emails']
    assert '555-123-4567' in result['business_facts']['contact']['phones']
    assert any(p['type'] == 'services' for p in result['sources'])
    assert not any('PrivacyPolicy' in p['url'] for p in result['sources'])
    scan_id = result['scan_id']
    assert client.get('/v1/business/profile', headers=headers).json()['profile']['business_name'] == ''
    approved = client.post(f'/v1/business/website/scans/{scan_id}/approve', headers=headers, json={'fields': ['business_name', 'offer'], 'overrides': {'business_name': 'Northstar Home Care'}})
    assert approved.status_code == 200, approved.text
    assert approved.json()['profile']['business_name'] == 'Northstar Home Care'
    assert client.post(f'/v1/business/website/scans/{scan_id}/approve', headers=headers, json={'fields': ['business_name']}).status_code == 409


def test_scan_rejects_private_address_and_scans_are_owner_scoped(monkeypatch, tmp_path):
    _, client = _client(monkeypatch, tmp_path)
    headers = {'X-API-Key': 'website-test-key'}
    assert client.post('/v1/business/website/scan', headers=headers, json={'domain': 'http://127.0.0.1'}).status_code == 422
    monkeypatch.setattr('app.website_intelligence._validate_public_url', lambda url: url)
    monkeypatch.setattr('app.website_intelligence._fetch_page', lambda url: (url, '<html><head><title>Example Company</title></head><body><h1>Example Company</h1></body></html>', [], ''))
    result = client.post('/v1/business/website/scan', headers=headers, json={'domain': 'https://example.com'}).json()
    from app.business_documents import _db
    conn = _db()
    try:
        assert conn.execute('SELECT COUNT(*) FROM business_website_scans WHERE user_id=?', ('local',)).fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM business_website_scans WHERE user_id=?', ('another-user',)).fetchone()[0] == 0
    finally:
        conn.close()
    assert client.get('/v1/business/website/scans/' + result['scan_id'], headers={'X-API-Key': 'wrong-key'}).status_code == 401
