import hashlib, hmac, json, sys, time
from fastapi.testclient import TestClient

def test_slack_events_verify_signature_and_answer_challenge(monkeypatch):
    monkeypatch.setenv('RZ_API_KEY','slack-test-key')
    monkeypatch.setenv('RZ_LOCAL_DATA_PATH','/tmp/rz-slack-test.sqlite3')
    monkeypatch.setenv('DATABASE_URL','')
    monkeypatch.setenv('SLACK_SIGNING_SECRET','unit-test-secret')
    sys.modules.pop('app.main',None)
    import app.main as module
    client=TestClient(module.app)
    try:
        raw=json.dumps({'type':'url_verification','challenge':'challenge-ok'}).encode()
        stamp=str(int(time.time()))
        sig='v0='+hmac.new(b'unit-test-secret',b'v0:'+stamp.encode()+b':'+raw,hashlib.sha256).hexdigest()
        headers={'X-Slack-Request-Timestamp':stamp,'X-Slack-Signature':sig,'Content-Type':'application/json'}
        assert client.post('/v1/slack/events',content=raw,headers=headers).json()['challenge']=='challenge-ok'
        headers['X-Slack-Signature']='v0=bad'
        assert client.post('/v1/slack/events',content=raw,headers=headers).status_code==401
    finally:
        client.close()
        sys.modules.pop('app.main',None)
