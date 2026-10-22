"""Per-user MIA customization, isolated by authenticated account."""
import time
from fastapi import APIRouter, Depends, HTTPException, Request, Header
from pydantic import BaseModel, Field
from app import capabilities as cap
router=APIRouter(prefix="/v1/user",tags=["user-preferences"])
PERSONALITIES={"balanced":"Be helpful, balanced, warm, and direct.","friendly":"Be warm, encouraging, conversational, and approachable without being overly familiar.","professional":"Use a polished, professional tone with precise wording.","concise":"Prioritize the answer and essential next steps; avoid unnecessary detail.","creative":"Explore original ideas and useful alternatives while remaining practical."}
LENGTHS={"concise":"Keep most answers brief and focused.","standard":"Use moderate detail and clear structure.","detailed":"Explain reasoning, tradeoffs, and steps in useful detail."}
LANGUAGES={"auto","en","es","fr","pt","de","it","ar","zh","hi","ur","ru","ja","ko"}
def _user(request:Request,authorization:str=Header(default=""),x_api_key:str=Header(default="")):
 return __import__("app.main",fromlist=["require_app_user"]).require_app_user(request,authorization,x_api_key)
def _db():
 c,k=cap._db(); t="DOUBLE PRECISION" if k=="postgres" else "REAL"
 c.execute(f"CREATE TABLE IF NOT EXISTS rz_user_preferences(user_id TEXT PRIMARY KEY,personality TEXT NOT NULL,custom_instructions TEXT NOT NULL,response_length TEXT NOT NULL,preferred_language TEXT NOT NULL,updated_at {t} NOT NULL)")
 c.commit(); return c,k
def _query(k,pg,sqlite):return pg if k=="postgres" else sqlite
def _defaults():return {"personality":"balanced","custom_instructions":"","response_length":"standard","preferred_language":"auto"}
def get_preferences(user_id):
 c,k=_db()
 try:
  q=_query(k,"SELECT personality,custom_instructions,response_length,preferred_language FROM rz_user_preferences WHERE user_id=%s","SELECT personality,custom_instructions,response_length,preferred_language FROM rz_user_preferences WHERE user_id=?")
  row=c.execute(q,(str(user_id),)).fetchone()
 finally:c.close()
 if not row:return _defaults()
 return {"personality":row[0],"custom_instructions":row[1],"response_length":row[2],"preferred_language":row[3]}
class Preferences(BaseModel):
 personality:str=Field(default="balanced",pattern="^(balanced|friendly|professional|concise|creative)$")
 custom_instructions:str=Field(default="",max_length=1200)
 response_length:str=Field(default="standard",pattern="^(concise|standard|detailed)$")
 preferred_language:str=Field(default="auto",pattern="^(auto|en|es|fr|pt|de|it|ar|zh|hi|ur|ru|ja|ko)$")
@router.get("/preferences")
def read_preferences(user:dict=Depends(_user)):
 return {"preferences":get_preferences(user.get("id",""))}
@router.put("/preferences")
def save_preferences(body:Preferences,user:dict=Depends(_user)):
 uid=str(user.get("id","")); c,k=_db(); now=time.time()
 try:
  q=_query(k,"INSERT INTO rz_user_preferences(user_id,personality,custom_instructions,response_length,preferred_language,updated_at) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET personality=EXCLUDED.personality,custom_instructions=EXCLUDED.custom_instructions,response_length=EXCLUDED.response_length,preferred_language=EXCLUDED.preferred_language,updated_at=EXCLUDED.updated_at","INSERT INTO rz_user_preferences(user_id,personality,custom_instructions,response_length,preferred_language,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET personality=excluded.personality,custom_instructions=excluded.custom_instructions,response_length=excluded.response_length,preferred_language=excluded.preferred_language,updated_at=excluded.updated_at")
  c.execute(q,(uid,body.personality,body.custom_instructions.strip(),body.response_length,body.preferred_language,now)); c.commit()
 finally:c.close()
 return {"preferences":get_preferences(uid),"saved":True}
def personalized_system(user_id,base_system):
 p=get_preferences(user_id)
 sections=[base_system, "User-selected MIA style: "+PERSONALITIES[p["personality"]], "User-selected response depth: "+LENGTHS[p["response_length"]]]
 if p["preferred_language"]!="auto":sections.append("Preferred response language: "+p["preferred_language"]+". Follow this preference unless the user explicitly requests another language.")
 if p["custom_instructions"]:sections.append("User's additional preferences (follow when safe and relevant): "+p["custom_instructions"])
 return "\n\n".join(sections)
