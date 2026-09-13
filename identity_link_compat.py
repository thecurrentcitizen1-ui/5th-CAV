from __future__ import annotations
import logging
from typing import Any
import hllv_rcon

log=logging.getLogger('battalion-clerk.identity-compat')
VERSION='V304-MICROSOFT-PC-IDENTITY'

def _s(v:Any)->str: return str(v or '').strip()

def _family(v:Any)->str:
    c=''.join(x for x in _s(v).casefold() if x.isalnum())
    if not c: return 'UNKNOWN'
    if 'steam' in c: return 'STEAM'
    if any(x in c for x in ('xbox','microsoft','msstore','microsoftstore','windowsstore','gamepass','xboxapp','gdk','wingdk','xboxpc','pcxbox')): return 'XBOX'
    if any(x in c for x in ('playstation','ps5','psn','sony')): return 'PS5'
    if c in {'pc','windows','win','win32','win64','eos','epic','unknown','desktop','computer'}: return 'NEUTRAL'
    return 'UNKNOWN'

def _key(row)->str: return _s(row.get('steam_id') if row else '')

async def _candidates(self,name):
    rows=await self.db.fetch("""SELECT steam_id,player_name,platform,platform_user_id,eos_id,last_seen_at
      FROM hll_player_match_stats WHERE LOWER(TRIM(COALESCE(player_name,'')))=LOWER(TRIM($1))
      ORDER BY last_seen_at DESC NULLS LAST LIMIT 100""",name)
    out={}
    for obj in rows:
        row=dict(obj); k=_key(row)
        if k and k not in out: out[k]=row
    return list(out.values())

async def _resolve(self,platform,name):
    platform=_s(platform).upper(); rows=await _candidates(self,name)
    if not rows:
        return None,'NOT_OBSERVED',f"No {platform} player named '{name}' has been observed by Battalion Clerk yet. Have them join the 1/5 CAV server once, then retry."
    direct=[r for r in rows if _family(r.get('platform'))==platform]
    if len(direct)==1: return direct[0],'PLATFORM_FAMILY',None
    if len(direct)>1: return None,'AMBIGUOUS',f"Multiple durable HLL identities named '{name}' were observed for {platform}. Battalion Clerk will not guess."
    safe=[r for r in rows if _family(r.get('platform')) in {'NEUTRAL','UNKNOWN',platform}]
    if len(safe)==1: return safe[0],'UNIQUE_EXACT_NAME',None
    if len(safe)>1: return None,'AMBIGUOUS',f"More than one durable HLL identity uses the exact in-game name '{name}'. Battalion Clerk will not guess."
    labels=', '.join(sorted({_s(r.get('platform')) or 'UNKNOWN' for r in rows}))
    return None,'PLATFORM_CONFLICT',f"The exact in-game name '{name}' was observed only on a conflicting platform label ({labels})."

async def _link(self,guild_id,discord_user_id,platform,gamertag,linked_by):
    await self.collector.start(); platform=_s(platform).upper(); gamertag=_s(gamertag)
    if platform not in {'XBOX','PS5'}: return {'ok':False,'error':'Platform must be Xbox/Microsoft Store PC or PlayStation 5.'}
    if not gamertag: return {'ok':False,'error':'Enter the exact in-game Xbox/Microsoft gamertag or PSN Online ID.'}
    person=await self._person_for_discord(guild_id,discord_user_id)
    if not person: return {'ok':False,'error':'No active Soldier Record is linked to that Discord account.'}
    row,basis,error=await _resolve(self,platform,gamertag)
    if not row: return {'ok':False,'error':error,'match_state':basis}
    k=_key(row); pid=_s(person['personnel_id'])
    if not k: return {'ok':False,'error':'The server saw that exact name but did not expose a durable player ID.'}
    other=await self.db.fetchrow('SELECT personnel_id FROM hll_personnel_links WHERE steam_id=$1 LIMIT 1',k)
    if other and _s(other.get('personnel_id'))!=pid: return {'ok':False,'error':'That HLL identity is already linked to another Soldier Record. Command/S-1 must resolve it.'}
    owned=await self.db.fetchrow('SELECT steam_id FROM hll_personnel_links WHERE personnel_id=$1 LIMIT 1',pid)
    if owned and _s(owned.get('steam_id'))!=k: return {'ok':False,'error':'Your Soldier Record still has a different game identity on file. Run /unlink-game first.'}
    try:
        await self.db.execute("""INSERT INTO hll_personnel_links(steam_id,personnel_id,discord_user_id,hll_player_name,platform,platform_user_id,eos_id,linked_by,verified,updated_at)
          VALUES($1,$2,$3,$4,$5,$6,$7,$8,TRUE,NOW()) ON CONFLICT(steam_id) DO UPDATE SET
          personnel_id=EXCLUDED.personnel_id,discord_user_id=EXCLUDED.discord_user_id,hll_player_name=EXCLUDED.hll_player_name,
          platform=EXCLUDED.platform,platform_user_id=EXCLUDED.platform_user_id,eos_id=EXCLUDED.eos_id,linked_by=EXCLUDED.linked_by,verified=TRUE,updated_at=NOW()""",
          k,pid,str(discord_user_id),row.get('player_name'),row.get('platform'),row.get('platform_user_id'),row.get('eos_id'),linked_by)
        await self.db.execute('UPDATE hll_player_match_stats SET personnel_id=$1 WHERE steam_id=$2 AND (personnel_id IS NULL OR personnel_id=$1)',pid,k)
        try: await self.db.execute('UPDATE hll_research_samples SET personnel_id=$1 WHERE steam_id=$2 AND (personnel_id IS NULL OR personnel_id=$1)',pid,k)
        except Exception: pass
    except Exception as exc: return {'ok':False,'error':f'Link conflict: {exc}'}
    soldier=f"{person.get('rank_code') or ''} {person.get('first_name') or ''} {person.get('last_name') or ''}".strip()
    log.info('[HLL IDENTITY LINK %s] personnel=%s name=%s requested=%s observed_platform=%s',basis,pid,row.get('player_name'),platform,row.get('platform'))
    return {'ok':True,'status':'VERIFIED','verified':True,'personnel_id':pid,'soldier':soldier,'player_name':row.get('player_name'),'platform':platform,'observed_platform':row.get('platform'),'player_key':k,'matched_by':basis}

async def _finish_claim(self,claim):
    platform=_s(claim.get('platform')).upper(); name=_s(claim.get('claimed_identity')); pid=_s(claim.get('personnel_id'))
    if platform not in {'XBOX','PS5'} or not name or not pid: return False
    row,basis,error=await _resolve(self,platform,name)
    if not row:
        if basis!='NOT_OBSERVED': await self.db.execute("UPDATE hll_identity_claims SET status='CONFLICT',error=$1,updated_at=NOW() WHERE id=$2",error[:500],claim['id'])
        return False
    k=_key(row)
    if not k: return False
    other=await self.db.fetchrow('SELECT personnel_id FROM hll_personnel_links WHERE steam_id=$1',k)
    if other and _s(other.get('personnel_id'))!=pid:
        await self.db.execute("UPDATE hll_identity_claims SET status='CONFLICT',error='Observed HLL account is already linked to another Soldier',updated_at=NOW() WHERE id=$1",claim['id']); return False
    owned=await self.db.fetchrow('SELECT steam_id FROM hll_personnel_links WHERE personnel_id=$1 LIMIT 1',pid)
    if owned and _s(owned.get('steam_id'))!=k:
        await self.db.execute("UPDATE hll_identity_claims SET status='CONFLICT',error='Soldier already linked to a different HLL identity',updated_at=NOW() WHERE id=$1",claim['id']); return False
    await self.db.execute("""INSERT INTO hll_personnel_links(steam_id,personnel_id,discord_user_id,hll_player_name,platform,platform_user_id,eos_id,linked_by,verified,updated_at)
      VALUES($1,$2,$3,$4,$5,$6,$7,'IDENTITY COMPAT AUTO-VERIFY',TRUE,NOW()) ON CONFLICT(steam_id) DO UPDATE SET
      personnel_id=EXCLUDED.personnel_id,discord_user_id=EXCLUDED.discord_user_id,hll_player_name=EXCLUDED.hll_player_name,
      platform=EXCLUDED.platform,platform_user_id=EXCLUDED.platform_user_id,eos_id=EXCLUDED.eos_id,linked_by='IDENTITY COMPAT AUTO-VERIFY',verified=TRUE,updated_at=NOW()""",
      k,pid,_s(claim.get('discord_user_id')) or None,row.get('player_name'),row.get('platform'),row.get('platform_user_id'),row.get('eos_id'))
    await self.db.execute('UPDATE hll_player_match_stats SET personnel_id=$1 WHERE steam_id=$2 AND (personnel_id IS NULL OR personnel_id=$1)',pid,k)
    try: await self.db.execute('UPDATE hll_research_samples SET personnel_id=$1 WHERE steam_id=$2 AND (personnel_id IS NULL OR personnel_id=$1)',pid,k)
    except Exception: pass
    await self.db.execute("UPDATE hll_identity_claims SET status='VERIFIED',linked_player_key=$1,error=NULL,linked_at=NOW(),updated_at=NOW() WHERE id=$2",k,claim['id'])
    try:
        if claim.get('recruiting_case_id'): await self.db.execute("UPDATE recruiting_cases SET game_identity_link_status='VERIFIED',game_identity_link_error=NULL,game_identity_linked_at=NOW(),updated_at=NOW() WHERE id=$1",claim.get('recruiting_case_id'))
    except Exception: pass
    log.info('[HLL PENDING CLAIM %s] claim=%s personnel=%s name=%s requested=%s observed_platform=%s',basis,claim.get('id'),pid,row.get('player_name'),platform,row.get('platform'))
    return True

def install():
    cls=hllv_rcon.HLLVTelemetryCollector
    if getattr(cls,'_identity_compat_patch_version',None)==VERSION: return
    original=cls._reconcile_pending_identity_claims
    async def reconcile(self):
        await original(self)
        claims=await self.db.fetch("SELECT * FROM hll_identity_claims WHERE status='PENDING' AND UPPER(COALESCE(platform,'')) IN ('XBOX','PS5') ORDER BY created_at ASC LIMIT 100")
        for claim in claims:
            try: await _finish_claim(self,claim)
            except Exception as exc:
                log.warning('[HLL IDENTITY COMPAT PENDING] claim=%s error=%s',claim.get('id'),exc)
                try: await self.db.execute('UPDATE hll_identity_claims SET error=$1,updated_at=NOW() WHERE id=$2',str(exc)[:500],claim['id'])
                except Exception: pass
    cls.link_console_personnel=_link
    cls._reconcile_pending_identity_claims=reconcile
    cls._identity_compat_patch_version=VERSION

install()
