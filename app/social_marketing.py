"""Social OAuth and explicit-approval marketing API for RZ Agent."""
import hashlib,json,os,secrets,time,uuid
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError
from fastapi import APIRouter,Depends,HTTPException,Request as StarletteRequest,Header
from pydantic import BaseModel,Field
from cryptography.fernet import Fernet
from app import capabilities as cap
router=APIRouter(prefix="/v1/social",tags=["social-marketing"])
def _current_user(request:StarletteRequest,authorization:str=Header(default=""),x_api_key:str=Header(default="")):
 return __import__("app.main",fromlist=["require_app_user"]).require_app_user(request,authorization,x_api_key)
def _uid(u): return str(u.get("id",""))
def _db():
 c,k=cap._db(); t="DOUBLE PRECISION" if k=="postgres" else "REAL"
 c.execute(f"CREATE TABLE IF NOT EXISTS rz_social_oauth_states(state_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,expires_at {t} NOT NULL)")
 c.execute(f"CREATE TABLE IF NOT EXISTS rz_social_accounts(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,provider TEXT NOT NULL,external_id TEXT NOT NULL,label TEXT NOT NULL,token_cipher TEXT NOT NULL,created_at {t} NOT NULL,UNIQUE(user_id,provider,external_id))")
 c.execute(f"CREATE TABLE IF NOT EXISTS rz_marketing_items(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,provider TEXT NOT NULL,target_id TEXT NOT NULL,caption TEXT NOT NULL,status TEXT NOT NULL,created_at {t} NOT NULL,decided_at {t},result TEXT)")
 c.commit(); return c,k
def _fernet():
 key=os.getenv("SOCIAL_TOKEN_ENCRYPTION_KEY","").strip()
 if not key: raise HTTPException(503,"Configure SOCIAL_TOKEN_ENCRYPTION_KEY before connecting accounts.")
 try:return Fernet(key.encode())
 except Exception:raise HTTPException(503,"SOCIAL_TOKEN_ENCRYPTION_KEY is invalid.") from None
def _meta():
 a=os.getenv("META_APP_ID","").strip(); s=os.getenv("META_APP_SECRET","").strip(); r=os.getenv("META_REDIRECT_URI","").strip()
 if not a or not s or not r.startswith("https://"): raise HTTPException(503,"Configure META_APP_ID, META_APP_SECRET, and HTTPS META_REDIRECT_URI.")
 return a,s,r
def _request(path,params,post=False):
 data=urlencode(params).encode() if post else None
 req=Request("https://graph.facebook.com/v23.0/"+path.lstrip("/"),data=data,headers={"Content-Type":"application/x-www-form-urlencoded"} if post else {})
 try:
  with urlopen(req,timeout=15) as r: result=json.loads(r.read(1000000).decode())
 except (HTTPError,URLError,TimeoutError): raise HTTPException(502,"Meta API request failed.") from None
 if not isinstance(result,dict) or result.get("error"): raise HTTPException(502,"Meta API returned an error.")
 return result
class Draft(BaseModel):
 account_id:str=Field(min_length=1,max_length=80)
 target_id:str=Field(min_length=1,max_length=100)
 caption:str=Field(min_length=1,max_length=2200)
class Decision(BaseModel): decision:str=Field(pattern="^(approve|deny)$")
@router.get("/providers")
def providers(): return {"providers":[{"id":"meta","name":"Facebook & Instagram","configured":bool(os.getenv("META_APP_ID") and os.getenv("META_APP_SECRET") and os.getenv("META_REDIRECT_URI","").startswith("https://"))},{"id":"slack","name":"Slack","events_endpoint":"/v1/slack/events","configured":bool(os.getenv("SLACK_SIGNING_SECRET") and os.getenv("SLACK_BOT_TOKEN"))}]}
@router.get("/meta/connect")
def connect(user:dict=Depends(_current_user)):
 app_id,secret,redirect=_meta(); _fernet(); state=secrets.token_urlsafe(32); c,k=_db()
 try:
  q="INSERT INTO rz_social_oauth_states(state_hash,user_id,expires_at) VALUES(%s,%s,%s)" if k=="postgres" else "INSERT INTO rz_social_oauth_states(state_hash,user_id,expires_at) VALUES(?,?,?)"
  c.execute(q,(hashlib.sha256(state.encode()).hexdigest(),_uid(user),time.time()+600)); c.commit()
 finally:c.close()
 return {"authorization_url":"https://www.facebook.com/v23.0/dialog/oauth?"+urlencode({"client_id":app_id,"redirect_uri":redirect,"state":state,"response_type":"code","scope":"pages_show_list,pages_read_engagement,pages_manage_posts,instagram_basic,instagram_content_publish"}),"expires_in_seconds":600}
@router.get("/meta/callback")
def callback(code:str="",state:str="",error:str=""):
 if error: raise HTTPException(400,"Meta authorization was declined.")
 if not code or not state or len(code)>4096 or len(state)>200: raise HTTPException(400,"Invalid OAuth callback parameters.")
 app_id,secret,redirect=_meta(); cipher=_fernet(); digest=hashlib.sha256(state.encode()).hexdigest(); c,k=_db()
 try:
  q="SELECT user_id,expires_at FROM rz_social_oauth_states WHERE state_hash=%s" if k=="postgres" else "SELECT user_id,expires_at FROM rz_social_oauth_states WHERE state_hash=?"; row=c.execute(q,(digest,)).fetchone()
  if not row or row[1]<time.time(): raise HTTPException(400,"OAuth state invalid or expired.")
  q="DELETE FROM rz_social_oauth_states WHERE state_hash=%s" if k=="postgres" else "DELETE FROM rz_social_oauth_states WHERE state_hash=?"; c.execute(q,(digest,)); c.commit(); uid=row[0]
 finally:c.close()
 token=_request("oauth/access_token",{"client_id":app_id,"client_secret":secret,"redirect_uri":redirect,"code":code},post=True).get("access_token")
 if not isinstance(token,str) or not token: raise HTTPException(502,"Meta did not return an access token.")
 long=_request("oauth/access_token",{"grant_type":"fb_exchange_token","client_id":app_id,"client_secret":secret,"fb_exchange_token":token},post=True); token=long.get("access_token",token)
 pages=_request("me/accounts",{"fields":"id,name,access_token,instagram_business_account{id,username}","access_token":token},post=True).get("data",[])
 c,k=_db(); saved=[]
 try:
  for p in pages:
   if not isinstance(p,dict) or not p.get("id") or not p.get("access_token"): continue
   items=[("facebook",str(p["id"]),str(p.get("name") or "Facebook Page"),str(p["access_token"]))]; ig=p.get("instagram_business_account")
   if isinstance(ig,dict) and ig.get("id"): items.append(("instagram",str(ig["id"]),"@"+str(ig.get("username") or ig["id"]),str(p["access_token"])))
   for provider,external,label,ptoken in items:
    aid=uuid.uuid4().hex; encrypted=cipher.encrypt(ptoken.encode()).decode(); now=time.time()
    q=("INSERT INTO rz_social_accounts(id,user_id,provider,external_id,label,token_cipher,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,provider,external_id) DO UPDATE SET label=EXCLUDED.label,token_cipher=EXCLUDED.token_cipher" if k=="postgres" else "INSERT INTO rz_social_accounts(id,user_id,provider,external_id,label,token_cipher,created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,provider,external_id) DO UPDATE SET label=excluded.label,token_cipher=excluded.token_cipher")
    c.execute(q,(aid,uid,provider,external,label,encrypted,now)); saved.append({"provider":provider,"external_id":external,"label":label})
  c.commit()
 finally:c.close()
 return {"connected":saved,"count":len(saved),"note":"Facebook text posts can be published only after explicit approval; Instagram needs a media workflow."}
@router.get("/accounts")
def accounts(user:dict=Depends(_current_user)):
 c,k=_db()
 try:
  q="SELECT id,provider,external_id,label,created_at FROM rz_social_accounts WHERE user_id=%s ORDER BY created_at DESC" if k=="postgres" else "SELECT id,provider,external_id,label,created_at FROM rz_social_accounts WHERE user_id=? ORDER BY created_at DESC"; rows=c.execute(q,(_uid(user),)).fetchall()
 finally:c.close()
 return {"accounts":[{"id":r[0],"provider":r[1],"external_id":r[2],"label":r[3],"created_at":r[4]} for r in rows]}
@router.post("/drafts")
def create_draft(body:Draft,user:dict=Depends(_current_user)):
 c,k=_db(); uid=_uid(user)
 try:
  q="SELECT id,provider,external_id FROM rz_social_accounts WHERE id=%s AND user_id=%s" if k=="postgres" else "SELECT id,provider,external_id FROM rz_social_accounts WHERE id=? AND user_id=?"; a=c.execute(q,(body.account_id,uid)).fetchone()
  if not a: raise HTTPException(404,"Connected social account not found.")
  if body.target_id != a[2]: raise HTTPException(422,"Draft target must match the selected connected account.")
  did=uuid.uuid4().hex; now=time.time(); q="INSERT INTO rz_marketing_items(id,user_id,provider,target_id,caption,status,created_at) VALUES(%s,%s,%s,%s,%s,'draft',%s)" if k=="postgres" else "INSERT INTO rz_marketing_items(id,user_id,provider,target_id,caption,status,created_at) VALUES(?,?,?,?,?,'draft',?)"
  c.execute(q,(did,uid,a[1],body.target_id,body.caption.strip(),now)); c.commit()
 finally:c.close()
 return {"id":did,"provider":a[1],"target_id":body.target_id,"caption":body.caption.strip(),"status":"draft","published":False}
@router.get("/drafts")
def drafts(user:dict=Depends(_current_user)):
 c,k=_db()
 try:
  q="SELECT id,provider,target_id,caption,status,created_at,decided_at,result FROM rz_marketing_items WHERE user_id=%s ORDER BY created_at DESC LIMIT 100" if k=="postgres" else "SELECT id,provider,target_id,caption,status,created_at,decided_at,result FROM rz_marketing_items WHERE user_id=? ORDER BY created_at DESC LIMIT 100"; rows=c.execute(q,(_uid(user),)).fetchall()
 finally:c.close()
 return {"drafts":[{"id":r[0],"provider":r[1],"target_id":r[2],"caption":r[3],"status":r[4],"created_at":r[5],"decided_at":r[6],"result":r[7]} for r in rows]}
@router.post("/drafts/{draft_id}/decision")
def decision(draft_id:str,body:Decision,user:dict=Depends(_current_user)):
 if len(draft_id)!=32 or any(x not in "0123456789abcdef" for x in draft_id): raise HTTPException(400,"Invalid draft id.")
 c,k=_db(); uid=_uid(user)
 try:
  q="SELECT provider,target_id,caption,status FROM rz_marketing_items WHERE id=%s AND user_id=%s" if k=="postgres" else "SELECT provider,target_id,caption,status FROM rz_marketing_items WHERE id=? AND user_id=?"; row=c.execute(q,(draft_id,uid)).fetchone()
  if not row: raise HTTPException(404,"Draft not found.")
  if row[3]!="draft": raise HTTPException(409,"Draft already decided.")
  if body.decision=="deny":
   q="UPDATE rz_marketing_items SET status='rejected',decided_at=%s WHERE id=%s AND user_id=%s" if k=="postgres" else "UPDATE rz_marketing_items SET status='rejected',decided_at=? WHERE id=? AND user_id=?"; c.execute(q,(time.time(),draft_id,uid)); c.commit(); return {"id":draft_id,"status":"rejected","published":False}
  if row[0]!="facebook": raise HTTPException(422,"Instagram publishing requires a media-asset workflow; this draft was not published.")
  q="SELECT token_cipher FROM rz_social_accounts WHERE user_id=%s AND provider=%s AND external_id=%s" if k=="postgres" else "SELECT token_cipher FROM rz_social_accounts WHERE user_id=? AND provider=? AND external_id=?"; acct=c.execute(q,(uid,row[0],row[1])).fetchone()
  if not acct: raise HTTPException(404,"Facebook Page account not found.")
  token=_fernet().decrypt(acct[0].encode()).decode()
  result=_request(row[1]+"/feed",{"message":row[2],"access_token":token},post=True)
  q="UPDATE rz_marketing_items SET status='published',decided_at=%s,result=%s WHERE id=%s AND user_id=%s" if k=="postgres" else "UPDATE rz_marketing_items SET status='published',decided_at=?,result=? WHERE id=? AND user_id=?"
  c.execute(q,(time.time(),json.dumps({"post_id":result.get("id")}),draft_id,uid)); c.commit()
 finally:c.close()
 return {"id":draft_id,"status":"published","post_id":result.get("id"),"published":True}
