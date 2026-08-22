from __future__ import annotations
from psycopg.types.json import Json

import logging
import re
import secrets
import string
import json
import urllib.parse
import urllib.request
import urllib.error
import hashlib
import base64
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, Response, abort, flash, redirect, render_template, request, session, url_for, g
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from cryptography.fernet import Fernet, InvalidToken

from auth import login_required, role_required
from config import CONFIG
from database import connection, execute, fetch_all, fetch_one, init_schema, close_request_connection

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("5th-cavalry-web")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
app.secret_key = CONFIG.secret_key
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

app.teardown_appcontext(close_request_connection)


def database_ready() -> bool:
    return bool(CONFIG.database_url)


def bootstrap() -> None:
    if not database_ready():
        log.warning("DATABASE_URL missing; site will show setup screen until configured.")
        return
    init_schema()
    execute(
        """
        INSERT INTO site_users (username,password_hash,access_role)
        VALUES (%s,%s,'admin')
        ON CONFLICT (username) DO UPDATE SET
            password_hash = EXCLUDED.password_hash,
            access_role = 'admin',
            is_active = TRUE,
            updated_at = NOW()
        """,
        (CONFIG.admin_username, generate_password_hash(CONFIG.admin_password)),
    )
    log.info("Initial site administrator ensured: %s", CONFIG.admin_username)


try:
    bootstrap()
except Exception:
    log.exception("Website bootstrap failed. Check DATABASE_URL and PostgreSQL connectivity.")


@app.errorhandler(Exception)
def unhandled_application_error(exc):
    # Preserve explicit HTTP errors such as 403/404.
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return exc
    ref=secrets.token_hex(4).upper()
    log.exception("UNHANDLED WEBSITE ERROR [%s] endpoint=%s path=%s",ref,request.endpoint,request.path)
    if request.path.startswith("/internal/"):
        return {"ok":False,"error":"internal server error","reference":ref},500
    return render_template("server_error.html",error_reference=ref),500



def _random_digits(length: int) -> str:
    return "".join(secrets.choice(string.digits) for _ in range(length))


def _random_field_code(length: int = 8) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _unique_value(sql: str, candidate_factory) -> str:
    for _ in range(50):
        candidate = candidate_factory()
        if not fetch_one(sql, (candidate,)):
            return candidate
    raise RuntimeError("Could not allocate a unique battalion record number")


def allocate_service_number() -> str:
    return _unique_value("SELECT 1 FROM personnel WHERE service_number=%s", lambda: f"5C-{_random_digits(6)}")


def allocate_roster_number() -> str:
    return _unique_value("SELECT 1 FROM battle_roster_cards WHERE roster_number=%s", lambda: f"BR-{_random_digits(6)}")


def allocate_m16_serial() -> str:
    # Period-style six-digit armory serial. It is a community inventory identifier,
    # not a claim that a specific surviving historical rifle carried this number.
    return _unique_value("SELECT 1 FROM weapon_inventory WHERE serial_number=%s", lambda: str(secrets.randbelow(700000) + 250000))


def allocate_rack_number() -> str:
    for n in range(1, 1000):
        candidate = f"A-{n:03d}"
        if not fetch_one("SELECT 1 FROM weapon_inventory WHERE rack_number=%s", (candidate,)):
            return candidate
    raise RuntimeError("No armory rack numbers available")




def _document_class(prefix: str) -> str:
    kind=str(prefix or "ORDER").upper()
    return {
        "REPLACEMENT":"SO", "ASSIGNMENT":"SO", "PROMOTION":"SO", "APPOINTMENT":"AO",
        "AWARD":"GO", "GENERAL":"GO", "GENERAL ORDER":"GO", "SPECIAL":"SO", "SPECIAL ORDER":"SO", "LEAVE":"SO", "RETURN":"SO", "SEPARATION":"SO",
        "TOUR EXTENSION":"SO", "TRAINING":"TRNG", "QUALIFICATION":"Q", "QUALIFICATION RECORD":"Q", "MORNING REPORT":"MR", "OPERATION":"OP",
        "WEAPON":"MEMO", "EQUIPMENT":"MEMO", "AMENDMENT":"AMD"
    }.get(kind, kind[:4] or "DOC")

def _order_number(prefix: str) -> str:
    doc_class=_document_class(prefix)
    year=date.today().year
    row=fetch_one(
        """INSERT INTO battalion_document_sequences(document_class,document_year,next_number)
           VALUES(%s,%s,2)
           ON CONFLICT(document_class,document_year) DO UPDATE SET next_number=battalion_document_sequences.next_number+1
           RETURNING next_number-1 AS issued""", (doc_class,year)) or {"issued":1}
    return f"{doc_class}-{str(year)[-2:]}-{int(row['issued']):04d}"


def create_personnel_order(personnel_id, document_type, title, body_text, *, effective_date=None, authority=None, details=None, source_key=None, document_number=None):
    if source_key:
        existing = fetch_one("SELECT * FROM personnel_documents WHERE source_key=%s", (source_key,))
        if existing:
            return existing
    guild = fetch_one("SELECT guild_id FROM website_member_links WHERE personnel_id=%s LIMIT 1", (str(personnel_id),))
    row=fetch_one(
        """INSERT INTO personnel_documents
           (personnel_id,document_type,document_number,title,effective_date,authority,body_text,details_json,source_key,source_guild_id,workflow_status,by_order_of,signature_block)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'FILED',%s,%s) RETURNING *""",
        (personnel_id, document_type.upper(), document_number or _order_number(document_type), title,
         effective_date or date.today(), authority or "HEADQUARTERS — 1ST BATTALION, 5TH CAVALRY",
         body_text, Json(details or {}), source_key, guild.get("guild_id") if guild else None,
         "BY ORDER OF THE BATTALION COMMANDER" if document_type.upper() in {"REPLACEMENT","ASSIGNMENT","PROMOTION","APPOINTMENT","AWARD","SEPARATION","TOUR EXTENSION"} else "FOR THE COMMANDER",
         authority or "HEADQUARTERS — 1ST BATTALION, 5TH CAVALRY"),
    )
    if row:
        try:
            notify_soldier(personnel_id,"HEADQUARTERS",f"New official document — {row['document_number']}",title,notification_type="ORDERS",source_key=f"DOC-NOTICE:{row['id']}",target_anchor="official-orders")
            battalion_history_entry("OFFICIAL DOCUMENT",f"{row['document_number']} — {title}",body_text[:500],personnel_id,reference_number=row['document_number'])
        except Exception:
            log.exception("Document notification/history filing failed for %s",row.get("document_number"))
    return row


def replacement_orders_for(personnel_id):
    person = fetch_one("SELECT * FROM personnel WHERE id=%s", (personnel_id,))
    if not person:
        return None
    rank_issue = fetch_one(
        """SELECT effective_date FROM promotion_history
           WHERE personnel_id=%s AND new_rank_code=%s
           ORDER BY effective_date,created_at LIMIT 1""",
        (personnel_id, person.get("rank_code")),
    )
    rank_issue_date = (rank_issue or {}).get("effective_date") or person.get("date_joined") or date.today()
    roster = battle_roster_for(person)
    body = (
        f"Effective this date, {person.get('rank_code') or 'PVT'} {person['first_name']} {person['last_name']} "
        "is assigned as a replacement to 1st Battalion, 5th Cavalry Regiment, 1st Cavalry Division (Airmobile). "
        "The Soldier will report to S-1 Personnel for processing and further assignment, report to S-4 for issue "
        "and accountability of government property, and complete Replacement Training before normal battalion duty."
    )
    return create_personnel_order(
        person["id"], "REPLACEMENT", "ORDERS TO VIETNAM — ASSIGNMENT TO 1/5 CAV", body,
        effective_date=rank_issue_date,
        authority="BY ORDER OF THE BATTALION COMMANDER",
        details={
            "rank": person.get("rank_code"),
            "mos_code": person.get("mos_code"),
            "mos_title": person.get("duty_position"),
            "battle_roster_number": (roster or {}).get("roster_number"),
            "rank_issue_date": str(rank_issue_date),
            "unit": "1st Battalion, 5th Cavalry Regiment",
        },
        source_key=f"REPLACEMENT:{person['id']}"
    )

def write_service_entry(personnel_id, entry_type: str, title: str, narrative: str = "", authority: str | None = None, reference_number: str | None = None, entry_date: date | None = None) -> None:
    row=fetch_one(
        """INSERT INTO personnel_service_history
        (personnel_id, entry_date, entry_type, title, narrative, authority, reference_number)
        VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id,entry_date""",
        (personnel_id, entry_date or date.today(), entry_type, title, narrative, authority, reference_number),
    )
    if row:
        emit_state_event(_state_event_type_for_service_entry(entry_type,title),personnel_id=personnel_id,
                         effective_date=row.get('entry_date'),title=title,narrative=narrative,
                         reference_number=reference_number,source_key=f"SERVICEHIST:{row['id']}",
                         details={'entry_type':entry_type,'authority':authority})


def ensure_member_site_user(personnel_id, roster_number: str):
    existing = fetch_one("SELECT user_id FROM user_personnel_links WHERE personnel_id=%s", (personnel_id,))
    if existing:
        return existing["user_id"]
    internal_username = f"roster:{roster_number.lower()}"
    random_secret = secrets.token_urlsafe(32)
    row = fetch_one(
        """
        INSERT INTO site_users (username,password_hash,access_role)
        VALUES (%s,%s,'member')
        ON CONFLICT (username) DO UPDATE SET is_active=TRUE
        RETURNING id
        """,
        (internal_username, generate_password_hash(random_secret)),
    )
    execute(
        "INSERT INTO user_personnel_links (user_id,personnel_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
        (row["id"], personnel_id),
    )
    return row["id"]


def issue_battle_roster_card(personnel_id):
    current = fetch_one("SELECT * FROM battle_roster_cards WHERE personnel_id=%s AND is_active=TRUE", (personnel_id,))
    if current:
        return current, None
    roster_number = allocate_roster_number()
    field_code = _random_field_code()
    card = fetch_one(
        """
        INSERT INTO battle_roster_cards (personnel_id,roster_number,field_code_hash)
        VALUES (%s,%s,%s)
        RETURNING *
        """,
        (personnel_id, roster_number, generate_password_hash(field_code)),
    )
    ensure_member_site_user(personnel_id, roster_number)
    return card, field_code


def issue_m16(personnel_id):
    ensure_standard_uniform(personnel_id)
    current = fetch_one(
        """
        SELECT wi.*, wih.issued_at, wih.id AS issue_id
        FROM weapon_issue_history wih JOIN weapon_inventory wi ON wi.id=wih.weapon_id
        WHERE wih.personnel_id=%s AND wih.is_current=TRUE LIMIT 1
        """,
        (personnel_id,),
    )
    if current:
        return current
    weapon = fetch_one("SELECT * FROM weapon_inventory WHERE status='AVAILABLE FOR ISSUE' ORDER BY created_at LIMIT 1")
    if not weapon:
        weapon = fetch_one(
            """
            INSERT INTO weapon_inventory (serial_number,rack_number,status,condition_state,last_inspected_at,last_cleaned_at)
            VALUES (%s,%s,'AVAILABLE FOR ISSUE','SERVICEABLE',NOW(),NOW()) RETURNING *
            """,
            (allocate_m16_serial(), allocate_rack_number()),
        )
    execute("UPDATE weapon_inventory SET status='ISSUED', updated_at=NOW() WHERE id=%s", (weapon["id"],))
    execute(
        """
        INSERT INTO weapon_issue_history (weapon_id,personnel_id,condition_at_issue)
        VALUES (%s,%s,%s)
        """,
        (weapon["id"], personnel_id, weapon.get("condition_state") or "SERVICEABLE"),
    )
    return fetch_one(
        """
        SELECT wi.*, wih.issued_at, wih.id AS issue_id
        FROM weapon_issue_history wih JOIN weapon_inventory wi ON wi.id=wih.weapon_id
        WHERE wih.personnel_id=%s AND wih.is_current=TRUE LIMIT 1
        """,
        (personnel_id,),
    )


def _last_duty_activity(personnel: dict | None):
    if not personnel:
        return None
    activity = fetch_one("SELECT MAX(activity_date) AS last_date FROM personnel_activity_credit WHERE personnel_id=%s AND credited=TRUE", (personnel["id"],))
    last_date = activity.get("last_date") if activity else None
    # Activity signals written by Battalion Clerk (voice/community presence) also
    # count toward keeping issued equipment from degrading for inactivity.
    for stamp_key in ("activity_last_duty_at", "activity_last_seen_at"):
        stamp = personnel.get(stamp_key)
        if stamp:
            stamp_date = stamp.date() if hasattr(stamp, "date") else stamp
            if not last_date or stamp_date > last_date:
                last_date = stamp_date
    return last_date or personnel.get("date_joined")


def derive_weapon_state(weapon: dict | None, personnel: dict | None):
    if not weapon: return None
    record=dict(weapon)
    last_duty=_last_duty_activity(personnel)
    inactive_days=max((date.today()-last_duty).days,0) if last_duty else 0
    absence_paused = authorized_absence_active(personnel) if personnel else False
    if absence_paused:
        inactive_days = 0
    state,pct=weapon_condition_from_rounds_and_time(record,personnel)
    since_clean=max(0,int(record.get("rounds_since_cleaning") or 0))
    if since_clean>=450: fouling="CLEANING REQUIRED"
    elif since_clean>=250: fouling="HEAVY"
    elif since_clean>=100: fouling="MODERATE"
    elif since_clean>0: fouling="LIGHT"
    else: fouling="CLEAN"
    t=inactivity_thresholds_for_person(personnel) if personnel else {"warning":7,"s1":14,"property":21,"command":30}
    if absence_paused: neglect="AUTHORIZED ABSENCE — CLOCK PAUSED"
    elif inactive_days>=t["command"]: neglect="S-4 ACCOUNTABILITY REQUIRED"
    elif inactive_days>=t["property"]: neglect="PROPERTY ACCOUNTABILITY REVIEW"
    elif inactive_days>=t["s1"]: neglect="INSPECTION / ACCOUNTABILITY DUE"
    elif inactive_days>=t["warning"]: neglect="NEGLECT WATCH"
    else: neglect="CURRENT"
    inspection=weapon_inspection_status(personnel["id"]) if personnel else None
    inspection_label="OVERDUE" if inspection and inspection.get("overdue") else "CURRENT"
    stages={"SERVICEABLE":0,"FOULED":2,"HEAVY FOULING":3,"CLEANING REQUIRED":3,"MAINTENANCE REQUIRED":4,"UNSERVICEABLE":5}
    record.update({"display_state":state,"display_condition_percent":pct,"dirt_stage":stages.get(state,0),"inactive_days":inactive_days,"last_duty_date":last_duty,"fouling_status":fouling,"cleanliness_status":"CLEAN" if since_clean==0 else "DIRTY","neglect_status":neglect,"inspection_status":inspection_label,"serviceability_status":state})
    return record

def current_weapon_for(personnel: dict | None):
    if not personnel:
        return None
    weapon = fetch_one(
        """
        SELECT wi.*, wih.issued_at, wih.id AS issue_id
        FROM weapon_issue_history wih JOIN weapon_inventory wi ON wi.id=wih.weapon_id
        WHERE wih.personnel_id=%s AND wih.is_current=TRUE LIMIT 1
        """,
        (personnel["id"],),
    )
    return derive_weapon_state(weapon, personnel)


def battle_roster_for(personnel: dict | None):
    if not personnel:
        return None
    return fetch_one("SELECT id,personnel_id,roster_number,issued_at,last_used_at,is_active FROM battle_roster_cards WHERE personnel_id=%s AND is_active=TRUE", (personnel["id"],))

@app.context_processor
def inject_globals():
    member_nco=False
    try:
        if session.get("user_id") and session.get("access_role") in {"member","nco","company_hq"}:
            lp=linked_personnel()
            member_nco=bool(lp and str(lp.get("rank_code") or "").upper() in NCO_RANKS)
    except Exception:
        member_nco=False
    return {
        "site_name": "5th Cavalry Regiment",
        "unit_name": "1st Battalion, 5th Cavalry Regiment",
        "today": date.today(),
        "member_nco": member_nco,
        "public_system_configured": database_ready(),
    }


COMMAND_ROLES = {"battalion_hq", "commander", "admin"}
NCO_RANKS = {"CPL", "SGT", "SSG", "SFC", "MSG", "1SG", "SGM"}


def staff_landing(role: str | None) -> str:
    return {
        "s1": "staff_action_center", "s2": "staff_action_center", "s3": "staff_action_center", "s4": "staff_action_center",
        "training": "staff_action_center", "battalion_hq": "staff_action_center",
        "commander": "staff_action_center", "admin": "staff_action_center", "nco": "my_soldier_record",
        "company_hq": "my_soldier_record",
    }.get(role or "", "staff_access")


def is_command() -> bool:
    return session.get("access_role") in COMMAND_ROLES


def member_is_nco(personnel=None) -> bool:
    personnel = personnel or linked_personnel()
    return bool(personnel and str(personnel.get("rank_code") or "").upper() in NCO_RANKS)


@app.before_request
def enforce_private_battalion_sections():
    """Keep public recruiting/unit-history pages open while enforcing staff lanes."""
    endpoint = request.endpoint or ""
    if endpoint == "static" or endpoint.startswith("internal_") or request.path.startswith("/internal/clerk/"):
        return None

    public_endpoints = {
        "home", "recruiting", "recruiting_status",
        "recruiting_discord_connect", "recruiting_discord_callback",
        "recruiting_discord_switch", "recruiting_case_discord_connect",
        "why_join", "about", "awards_decorations",
        "organization", "company", "platoon", "unit_history_page",
        "public_personnel", "public_operations", "public_readiness_report", "contact",
        "public_recruiting_needs",
        "login", "my_soldier_record", "staff_login", "staff_access", "logout", "health",
    }
    if endpoint in public_endpoints:
        return None

    if not session.get("user_id"):
        return redirect(url_for("home"))

    role = session.get("access_role", "member")
    if role in COMMAND_ROLES:
        return None

    member_allowed = {
        "my_soldier_record", "my_201_file", "battle_roster_card",
        "personnel_document", "personnel_document_preview",
        "notification_ack", "member_clean_weapon", "member_ribbon_toggle",
        "my_soldiers", "award_recommendation",
        "my_action_center", "my_journal",
        "my_career", "my_career_goals", "my_service_statistics",
        "my_weekly_report", "my_qualification_card",
        "my_weapon_service_history", "my_squad", "my_platoon_identity",
        "my_unit", "my_tour_book", "weapon_history",
    }
    staff_common = {
        "staff_action_center", "staff_personnel_snapshot", "staff_personnel_drawer",
        "smart_personnel_search_page", "personnel_compare_page",
        "logout", "home",
    }
    scoped = {
        "s1": staff_common | {
            "staff_batch_action", "staff_soldier_action", "staff_personnel_manage",
            "replacement_detachment", "replacement_quick_action", "replacement_batch_action",
            "personnel_action_quick", "s1", "personnel_office", "personnel_service_record",
            "personnel_document", "personnel_document_preview", "morning_report",
            "duty_status_action", "personnel_actions", "document_amendment",
            "personnel_lifecycle_action", "staff_workload_page", "award_recommendation",
        },
        "s2": staff_common | {"s2"},
        "s3": staff_common | {
            "staff_batch_action", "staff_soldier_action", "staff_personnel_manage",
            "personnel_action_quick", "s3", "operations", "operation_detail",
            "operation_schedule_action", "operation_attendance_action",
            "operation_close_action", "operation_delete_action", "operation_duplicate",
            "orders", "training_office_phase9", "training", "readiness",
            "personnel_actions", "operation_lifecycle_action", "staff_workload_page",
            "mos_proficiency_action", "instructor_qualification_action",
            "operation_duty_assignments_page", "operation_readiness_snapshot_action",
            "morning_report",
        },
        "training": staff_common | {
            "staff_batch_action", "staff_soldier_action", "staff_personnel_manage",
            "personnel_action_quick", "operations", "operation_detail",
            "training_office_phase9", "training", "readiness",
            "personnel_actions", "staff_workload_page", "morning_report",
        },
        "s4": staff_common | {
            "staff_batch_action", "staff_soldier_action", "staff_personnel_manage",
            "personnel_action_quick", "s4", "supply", "arms_room",
            "personnel_actions", "weapon_inspection_action",
        },
        "nco": member_allowed | {"my_soldiers"},
        "company_hq": member_allowed | {"my_soldiers"},
        "member": member_allowed,
    }
    allowed = scoped.get(role, {"logout", "home"})
    if endpoint not in allowed:
        flash("YOUR LOGIN IS RESTRICTED TO YOUR ASSIGNED BATTALION SECTION.", "warning")
        return redirect(url_for(staff_landing(role) if role != "member" else "my_soldier_record"))
    return None



def action_section_for_type(action_type: str) -> str:
    kind = str(action_type or "").upper()
    if kind in {"TRAINING","QUALIFICATION","MOS","OPERATION","AFTER ACTION"}: return "S-3"
    if kind in {"WEAPON","EQUIPMENT","SUPPLY","LOGISTICS","PROPERTY"}: return "S-4"
    if kind in {"COMMAND","PROMOTION BOARD","COMMAND REVIEW"}: return "HQ"
    return "S-1"


def open_personnel_action(personnel_id, action_type, subject, section=None, priority="ROUTINE", initiated_by=None, details=None, source_key=None, due_date=None):
    existing = fetch_one("SELECT * FROM personnel_actions WHERE source_key=%s", (source_key,)) if source_key else None
    if existing:
        return existing
    row=fetch_one(
        """INSERT INTO personnel_actions(personnel_id,action_type,subject,owning_section,status,priority,initiated_by,due_date,details_json,source_key)
           VALUES(%s,%s,%s,%s,'OPEN',%s,%s,%s,%s,%s) RETURNING *""",
        (personnel_id, str(action_type).upper(), subject, section or action_section_for_type(action_type), priority, initiated_by, due_date, Json(details or {}), source_key),
    )
    if row:
        staff_log(row.get("owning_section") or "HQ","ACTION OPENED",subject,initiated_by,personnel_id,details={"action_id":str(row['id']),"priority":priority})
    return row


def transition_personnel_action(action_id, new_status, actor=None, remarks=None, assigned_to=None, section=None):
    row = fetch_one("SELECT * FROM personnel_actions WHERE id=%s", (action_id,))
    if not row: return None
    old = row.get("status")
    closed = new_status in {"COMPLETE","CLOSED","APPROVED","DENIED","CANCELLED"}
    execute(
        """UPDATE personnel_actions SET status=%s,assigned_to=COALESCE(%s,assigned_to),owning_section=COALESCE(%s,owning_section),
           updated_at=NOW(),closed_at=CASE WHEN %s THEN NOW() ELSE NULL END WHERE id=%s""",
        (new_status, assigned_to, section, closed, action_id),
    )
    execute("INSERT INTO personnel_action_events(action_id,event_type,from_status,to_status,actor,remarks) VALUES(%s,'STATUS',%s,%s,%s,%s)",
            (action_id, old, new_status, actor, remarks))
    updated=fetch_one("SELECT * FROM personnel_actions WHERE id=%s", (action_id,))
    if updated:
        staff_log(updated.get("owning_section") or "HQ","ACTION ROUTED",f"{updated.get('subject')} — {old} → {new_status}",actor,updated.get("personnel_id"),details={"action_id":str(action_id),"remarks":remarks or ""})
    return updated


def section_action_counts(section=None):
    if section:
        return fetch_one("""SELECT COUNT(*) FILTER (WHERE status NOT IN ('COMPLETE','CLOSED','APPROVED','DENIED','CANCELLED')) AS open,
                         COUNT(*) FILTER (WHERE priority IN ('HIGH','URGENT') AND status NOT IN ('COMPLETE','CLOSED','APPROVED','DENIED','CANCELLED')) AS urgent
                         FROM personnel_actions WHERE owning_section=%s""", (section,)) or {"open":0,"urgent":0}
    return fetch_one("""SELECT COUNT(*) FILTER (WHERE status NOT IN ('COMPLETE','CLOSED','APPROVED','DENIED','CANCELLED')) AS open,
                     COUNT(*) FILTER (WHERE priority IN ('HIGH','URGENT') AND status NOT IN ('COMPLETE','CLOSED','APPROVED','DENIED','CANCELLED')) AS urgent
                     FROM personnel_actions""") or {"open":0,"urgent":0}


def soldier_action_items(personnel):
    if not personnel: return []
    pid=personnel["id"]
    items=[]
    progress=personnel_progress(pid)
    replacement=replacement_training_status(personnel)
    if not progress.get("rules_acknowledged_at"):
        items.append({"title":"Acknowledge Battalion Standing Orders","section":"S-1","status":"ACTION REQUIRED"})
    if not replacement.get("complete"):
        remaining=sum(1 for r in replacement.get("requirements",[]) if not r.get("complete"))
        items.append({"title":f"{replacement.get('program_title','Replacement Training')} — {remaining} requirement(s) remaining","section":"S-1 / S-3" if replacement.get("replacement_required") else "S-1","status":"IN PROCESSING"})
    weapon=current_weapon_for(personnel)
    if weapon and int(weapon.get("dirt_stage") or 0)>=3:
        items.append({"title":f"M16 {weapon.get('serial_number')} requires cleaning / maintenance","section":"S-4","status":"DUE"})
    for q in fetch_all("SELECT qualification_name,expires_at FROM qualifications WHERE personnel_id=%s AND expires_at BETWEEN CURRENT_DATE AND CURRENT_DATE + 14 ORDER BY expires_at",(pid,)):
        items.append({"title":f"{q['qualification_name']} expires {q['expires_at']}","section":"S-3","status":"EXPIRING"})
    elig=promotion_eligibility(personnel)
    for e in elig:
        if e.get("status") in {"ELIGIBLE FOR CONSIDERATION","RECOMMENDED FOR PROMOTION"}:
            items.append({"title":f"Promotion to {e.get('target')} — {e.get('status')}","section":"S-1 / HQ","status":e.get("status")})
    return items


def personnel_mos_for(personnel_id):
    return fetch_all("SELECT * FROM personnel_mos_records WHERE personnel_id=%s AND status='CURRENT' ORDER BY CASE mos_kind WHEN 'PRIMARY' THEN 0 ELSE 1 END,effective_date", (personnel_id,))


LIFECYCLE_STATES = {
    "PROSPECT", "REPLACEMENT", "IN PROCESSING", "REPLACEMENT TRAINING", "PRESENT FOR DUTY",
    "ACTIVE SERVICE", "AUTHORIZED LEAVE", "HOSPITAL", "WIA", "TEMPORARY DUTY",
    "DEROS PROCESSING", "SEPARATED", "ARCHIVED"
}

def staff_log(section, action_type, summary, actor=None, personnel_id=None, reference_number=None, details=None):
    execute("""INSERT INTO staff_duty_log(section,actor,personnel_id,action_type,summary,reference_number,details_json)
               VALUES(%s,%s,%s,%s,%s,%s,%s)""",
            (section,actor,personnel_id,action_type,summary,reference_number,Json(details or {})))

def battalion_history_entry(category,title,narrative="",personnel_id=None,operation_id=None,reference_number=None,visibility="STAFF",history_date=None):
    execute("""INSERT INTO battalion_history(history_date,category,title,narrative,personnel_id,operation_id,reference_number,visibility)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
            (history_date or date.today(),category,title,narrative,personnel_id,operation_id,reference_number,visibility))

def notify_soldier(personnel_id, section, title, message=None, *, notification_type="NOTICE", priority="ROUTINE", source_key=None, target_endpoint="my_soldier_record", target_anchor=None, expires_at=None):
    if source_key:
        row=fetch_one("SELECT * FROM soldier_notifications WHERE source_key=%s",(source_key,))
        if row: return row
    return fetch_one("""INSERT INTO soldier_notifications(personnel_id,section,notification_type,title,message,target_endpoint,target_anchor,source_key,priority,expires_at)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                     (personnel_id,section,notification_type,title,message,target_endpoint,target_anchor,source_key,priority,expires_at))

def current_notifications(personnel_id):
    return fetch_all("""SELECT * FROM soldier_notifications WHERE personnel_id=%s AND acknowledged_at IS NULL
                        AND (expires_at IS NULL OR expires_at>NOW()) ORDER BY CASE priority WHEN 'URGENT' THEN 0 WHEN 'HIGH' THEN 1 ELSE 2 END,created_at DESC""",(personnel_id,))

def weapon_inspection_status(personnel_id):
    weapon=fetch_one("""SELECT wi.*,wih.personnel_id FROM weapon_issue_history wih JOIN weapon_inventory wi ON wi.id=wih.weapon_id
                        WHERE wih.personnel_id=%s AND wih.is_current=TRUE LIMIT 1""",(personnel_id,))
    if not weapon: return None
    insp=fetch_one("SELECT * FROM weapon_inspections WHERE weapon_id=%s ORDER BY inspection_date DESC,created_at DESC LIMIT 1",(weapon["id"],))
    last=(insp or {}).get("inspection_date") or (weapon.get("last_inspected_at").date() if weapon.get("last_inspected_at") else None)
    due=(insp or {}).get("next_due_date") or ((last + timedelta(days=14)) if last else date.today())
    return {"weapon":weapon,"last":last,"due":due,"overdue":due < date.today(),"days":(due-date.today()).days}

def reconcile_lifecycle(personnel, authority="BATTALION SYSTEM"):
    if not personnel: return None
    p=dict(personnel)
    current=str(p.get("lifecycle_state") or "REPLACEMENT").upper()
    duty=str(p.get("duty_status") or "").upper()
    if p.get("archived"):
        target="ARCHIVED"
    elif p.get("separated_at"):
        target="SEPARATED"
    elif duty=="LEAVE": target="AUTHORIZED LEAVE"
    elif duty=="HOSPITAL": target="HOSPITAL"
    elif duty=="WIA": target="WIA"
    elif duty=="TEMPORARY DUTY": target="TEMPORARY DUTY"
    elif p.get("deros_date") and p.get("deros_date") <= date.today(): target="DEROS PROCESSING"
    else:
        repl=replacement_training_status(p)
        prog=personnel_progress(p["id"])
        if not prog.get("s1_onboarded_at"): target="IN PROCESSING"
        elif not p.get("platoon") or not p.get("squad"): target="IN PROCESSING"
        elif not repl.get("complete") and repl.get("replacement_required"): target="REPLACEMENT TRAINING"
        elif not repl.get("complete"): target="IN PROCESSING"
        else: target="PRESENT FOR DUTY"
    if target!=current:
        execute("UPDATE personnel SET lifecycle_state=%s,lifecycle_updated_at=NOW() WHERE id=%s",(target,p["id"]))
        write_service_entry(p["id"],"STATUS",f"PERSONNEL STATE — {target}",f"Lifecycle status changed from {current} to {target}.",authority,None,date.today())
        battalion_history_entry("PERSONNEL STATUS",f"{p.get('rank_code','')} {p.get('last_name','')} — {target}",f"Lifecycle changed from {current} to {target}.",p["id"])
    p["lifecycle_state"]=target
    return p

def refresh_member_notices(personnel):
    if not personnel: return []
    p=reconcile_lifecycle(personnel) or personnel
    pid=p["id"]
    progress=personnel_progress(pid)
    repl=replacement_training_status(p)
    if not progress.get("rules_acknowledged_at"):
        notify_soldier(pid,"S-1","Standing Orders require acknowledgement","Open Replacement Training and acknowledge the Battalion Standing Orders.",priority="HIGH",source_key=f"RULES:{pid}",target_anchor="replacement-training")
    if not repl.get("complete"):
        remaining=sum(1 for r in repl.get("requirements",[]) if not r.get("complete"))
        notify_soldier(pid,"S-1 / S-3",f"{repl.get('program_title','Replacement Training')} — {remaining} requirement(s) remaining","Complete the remaining battalion in-processing requirements.",source_key=f"ENTRY-PROCESSING:{pid}",target_anchor="replacement-training")
    inspection=weapon_inspection_status(pid)
    if inspection and inspection["overdue"]:
        notify_soldier(pid,"S-4",f"M16 inspection overdue — Serial {inspection['weapon']['serial_number']}",f"Weapon inspection was due {inspection['due']}.",priority="HIGH",source_key=f"WEAPON-INSP:{pid}:{inspection['due']}",target_anchor="weapon")
    for q in fetch_all("SELECT qualification_name,expires_at FROM qualifications WHERE personnel_id=%s AND expires_at BETWEEN CURRENT_DATE AND CURRENT_DATE + 14 ORDER BY expires_at",(pid,)):
        notify_soldier(pid,"S-3",f"{q['qualification_name']} expires {q['expires_at']}","Requalification should be coordinated with S-3.",source_key=f"QUAL-EXP:{pid}:{q['qualification_name']}:{q['expires_at']}",target_anchor="training")
    for e in promotion_eligibility(p):
        if e.get("status") in {"ELIGIBLE FOR CONSIDERATION","RECOMMENDED FOR PROMOTION"}:
            notify_soldier(pid,"S-1 / HQ",f"Promotion to {e.get('target')} — {e.get('status')}","Your promotion worksheet is ready for leadership review.",source_key=f"PROMO-ELIG:{pid}:{e.get('target')}:{e.get('status')}",target_anchor="promotion")
    return current_notifications(pid)

def soldier_next_step(personnel):
    p=reconcile_lifecycle(personnel) or personnel
    state=p.get("lifecycle_state")
    return {
        "IN PROCESSING":"Complete S-1 onboarding and unit assignment.",
        "REPLACEMENT TRAINING":"Complete remaining Replacement Training requirements.",
        "PRESENT FOR DUTY":"Maintain readiness, attend operations, and progress toward promotion.",
        "AUTHORIZED LEAVE":"Return on the approved date or coordinate an extension with S-1.",
        "HOSPITAL":"Await return-to-duty action from S-1.",
        "WIA":"Await medical/return-to-duty processing.",
        "TEMPORARY DUTY":"Complete temporary duty and return to battalion control.",
        "DEROS PROCESSING":"Coordinate tour extension or separation processing with S-1.",
        "SEPARATED":"Service record closed; property accountability must be complete.",
        "ARCHIVED":"Historical record retained by Headquarters."
    }.get(state,"Report to S-1 for personnel processing.")

def sync_access_from_appointments(personnel_id) -> None:
    """Set site access from the Soldier's highest current appointment.
    This is internal authorization plumbing; member-facing pages use Army terms.
    """
    from auth import ROLE_WEIGHT
    rows = fetch_all(
        """
        SELECT ac.access_role
        FROM personnel_appointments pa
        JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code
        WHERE pa.personnel_id=%s AND pa.is_current=TRUE AND ac.access_role IS NOT NULL
        """,
        (personnel_id,),
    )
    roles = [r["access_role"] for r in rows if r.get("access_role")]
    target = max(roles, key=lambda r: ROLE_WEIGHT.get(r, 0)) if roles else "member"
    link = fetch_one("SELECT user_id FROM user_personnel_links WHERE personnel_id=%s", (personnel_id,))
    if link:
        execute("UPDATE site_users SET access_role=%s,updated_at=NOW() WHERE id=%s", (target, link["user_id"]))
        if str(session.get("personnel_id") or "") == str(personnel_id):
            session["access_role"] = target


def process_rank_action(personnel_id, new_rank_code: str, effective_date=None, authority=None, order_number=None, remarks=None):
    person = fetch_one("SELECT * FROM personnel WHERE id=%s", (personnel_id,))
    rank = fetch_one("SELECT * FROM rank_catalog WHERE rank_code=%s AND is_active=TRUE", (new_rank_code,))
    if not person or not rank:
        raise ValueError("Personnel record or rank not found")
    old_rank = person.get("rank_code")
    eff = effective_date or date.today()
    order_number = order_number or _order_number("PROMOTION")
    execute("UPDATE personnel SET rank_code=%s,updated_at=NOW() WHERE id=%s", (new_rank_code, personnel_id))
    execute(
        """INSERT INTO promotion_history
        (personnel_id,old_rank_code,new_rank_code,effective_date,authority,order_number,remarks)
        VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (personnel_id, old_rank, new_rank_code, eff, authority, order_number, remarks),
    )
    action_word = "PROMOTED" if old_rank != new_rank_code else "RANK RECORDED"
    narrative = f"{action_word.title()} from {old_rank or 'NO PRIOR ENTRY'} to {new_rank_code} ({rank['rank_name']})."
    if remarks:
        narrative += f" {remarks}"
    write_service_entry(personnel_id, "RANK", action_word, narrative, authority, order_number, eff)
    create_personnel_order(personnel_id, "PROMOTION", "PROMOTION ORDERS", narrative, effective_date=eff, authority=authority, details={"old_rank":old_rank,"new_rank":new_rank_code}, source_key=f"PROMOTION:{personnel_id}:{new_rank_code}:{eff}", document_number=order_number)
    pa=open_personnel_action(personnel_id,"PROMOTION",f"Promotion to {new_rank_code}","S-1","ROUTINE",authority,{"old_rank":old_rank,"new_rank":new_rank_code},source_key=f"ACTION:PROMOTION:{personnel_id}:{new_rank_code}:{eff}")
    if pa: transition_personnel_action(pa["id"],"COMPLETE",authority,"Promotion order published and permanent record updated.")
    enqueue_discord_role_sync(personnel_id,f'RANK {old_rank}->{new_rank_code}')


def process_appointment_action(personnel_id, appointment_code: str, organization=None, status="PERMANENT", effective_date=None, authority=None, order_number=None, remarks=None, unit_node_id=None):
    appt = fetch_one("SELECT * FROM appointment_catalog WHERE appointment_code=%s AND is_active=TRUE", (appointment_code,))
    if not appt:
        raise ValueError("Appointment not found")
    eff = effective_date or date.today()
    order_number = order_number or _order_number("APPOINTMENT")
    if unit_node_id:
        node = unit_node(unit_node_id)
        organization = format_assignment_node(unit_node_id) if node else organization
    execute(
        """INSERT INTO personnel_appointments
        (personnel_id,appointment_code,unit_node_id,organization,appointment_status,effective_date,authority,order_number,remarks)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (personnel_id, appointment_code, unit_node_id, organization, status, eff, authority, order_number, remarks),
    )
    narrative = f"Appointed {appt['appointment_name']}"
    if organization:
        narrative += f", {organization}"
    narrative += f" ({status})."
    if remarks:
        narrative += f" {remarks}"
    write_service_entry(personnel_id, "APPOINTMENT", "APPOINTED", narrative, authority, order_number, eff)
    create_personnel_order(personnel_id, "APPOINTMENT", "APPOINTMENT ORDERS", narrative, effective_date=eff, authority=authority, details={"appointment":appt["appointment_name"],"organization":organization}, source_key=f"APPOINTMENT:{personnel_id}:{appointment_code}:{eff}", document_number=order_number)
    pa=open_personnel_action(personnel_id,"APPOINTMENT",f"Appointment — {appt['appointment_name']}","S-1","ROUTINE",authority,{"organization":organization},source_key=f"ACTION:APPOINTMENT:{personnel_id}:{appointment_code}:{eff}")
    if pa: transition_personnel_action(pa["id"],"COMPLETE",authority,"Appointment orders filed and access synchronized.")
    sync_access_from_appointments(personnel_id)
    enqueue_discord_role_sync(personnel_id,f'APPOINTMENT {appt["appointment_name"]}')


def relieve_appointment(appointment_id, ended_date=None, authority=None, order_number=None, remarks=None):
    row = fetch_one(
        """SELECT pa.*,ac.appointment_name FROM personnel_appointments pa
        JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code
        WHERE pa.id=%s AND pa.is_current=TRUE""",
        (appointment_id,),
    )
    if not row:
        raise ValueError("Current appointment not found")
    ended = ended_date or date.today()
    order_number = order_number or _order_number("APPOINTMENT")
    execute(
        "UPDATE personnel_appointments SET is_current=FALSE,ended_date=%s WHERE id=%s",
        (ended, appointment_id),
    )
    narrative = f"Relieved from appointment as {row['appointment_name']}"
    if row.get("organization"):
        narrative += f", {row['organization']}"
    narrative += "."
    if remarks:
        narrative += f" {remarks}"
    write_service_entry(row["personnel_id"], "APPOINTMENT", "RELIEVED FROM APPOINTMENT", narrative, authority, order_number, ended)
    create_personnel_order(row["personnel_id"], "APPOINTMENT", "RELIEF FROM APPOINTMENT ORDERS", narrative, effective_date=ended, authority=authority, details={"appointment":row["appointment_name"],"organization":row.get("organization"),"action":"RELIEF"}, source_key=f"APPOINTMENT-RELIEF:{appointment_id}:{ended}", document_number=order_number)
    sync_access_from_appointments(row["personnel_id"])
    enqueue_discord_role_sync(row["personnel_id"],f'APPOINTMENT RELIEF {row["appointment_name"]}')


def unit_node(node_id):
    if not node_id:
        return None
    return fetch_one("SELECT * FROM unit_nodes WHERE id=%s", (node_id,))


def unit_ancestry(node_id):
    """Return current node -> parent -> ... -> battalion."""
    if not node_id:
        return []
    return fetch_all(
        """
        WITH RECURSIVE tree AS (
            SELECT id,parent_id,unit_code,display_name,unit_type,sort_order,0 depth
            FROM unit_nodes WHERE id=%s
            UNION ALL
            SELECT u.id,u.parent_id,u.unit_code,u.display_name,u.unit_type,u.sort_order,t.depth+1
            FROM unit_nodes u JOIN tree t ON t.parent_id=u.id
        )
        SELECT * FROM tree ORDER BY depth
        """,
        (node_id,),
    )


def unit_descendant_ids(node_id):
    if not node_id:
        return []
    rows = fetch_all(
        """
        WITH RECURSIVE tree AS (
            SELECT id,parent_id FROM unit_nodes WHERE id=%s
            UNION ALL
            SELECT u.id,u.parent_id FROM unit_nodes u JOIN tree t ON u.parent_id=t.id
        )
        SELECT id FROM tree
        """,
        (node_id,),
    )
    return [r["id"] for r in rows]


def legacy_assignment_from_node(node_id):
    """Translate structured organization into legacy unit/platoon/squad labels."""
    ancestry = unit_ancestry(node_id)
    company = next((n for n in ancestry if str(n["unit_type"]).lower()=="company"), None)
    platoon = next((n for n in ancestry if str(n["unit_type"]).lower()=="platoon"), None)
    squad = next((n for n in ancestry if str(n["unit_type"]).lower()=="squad"), None)
    unit_code = "1-5 CAV"
    if company:
        code = str(company["unit_code"])
        if code.startswith("HHC"):
            unit_code = "HHC/1-5 CAV"
        elif code.startswith("CS-"):
            unit_code = "CS/1-5 CAV"
        else:
            unit_code = f"{code.split('-')[0]}/1-5 CAV"
    return {
        "unit_code": unit_code,
        "platoon": platoon["display_name"] if platoon else None,
        "squad": squad["display_name"] if squad else None,
    }


def format_assignment_node(node_id):
    ancestry = unit_ancestry(node_id)
    if not ancestry:
        return None
    visible = [n["display_name"] for n in reversed(ancestry)
               if n["unit_type"] not in {"Battalion"}]
    return " / ".join(visible)


def process_assignment_action(personnel_id, unit_node_id, duty_position=None,
                              effective_date=None, authority=None,
                              order_number=None, remarks=None):
    person = fetch_one("SELECT * FROM personnel WHERE id=%s", (personnel_id,))
    target = unit_node(unit_node_id)
    if not person or not target:
        raise ValueError("Personnel record or organization not found")

    eff = effective_date or date.today()
    order_number = order_number or _order_number("ASSIGNMENT")
    old_label = format_assignment_node(person.get("unit_node_id")) or (
        " / ".join(x for x in [person.get("unit_code"), person.get("platoon"), person.get("squad")] if x)
    )
    legacy = legacy_assignment_from_node(unit_node_id)
    new_duty = duty_position if duty_position is not None else person.get("duty_position")

    execute(
        """UPDATE assignment_history
           SET is_current=FALSE,ended_date=%s
           WHERE personnel_id=%s AND is_current=TRUE""",
        (eff, personnel_id),
    )
    execute(
        """UPDATE personnel
           SET unit_node_id=%s,unit_code=%s,platoon=%s,squad=%s,
               duty_position=%s,field_status='Assigned',updated_at=NOW()
           WHERE id=%s""",
        (unit_node_id, legacy["unit_code"], legacy["platoon"], legacy["squad"],
         new_duty, personnel_id),
    )
    execute(
        """INSERT INTO assignment_history
           (personnel_id,unit_node_id,unit_code,platoon,squad,duty_position,effective_date,is_current)
           VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE)""",
        (personnel_id, unit_node_id, legacy["unit_code"], legacy["platoon"],
         legacy["squad"], new_duty, eff),
    )

    new_label = format_assignment_node(unit_node_id) or target["display_name"]
    narrative = f"Reassigned from {old_label or 'Replacement / Unassigned'} to {new_label}"
    if new_duty:
        narrative += f"; duty: {new_duty}"
    narrative += "."
    if remarks:
        narrative += f" {remarks}"
    write_service_entry(personnel_id, "ASSIGNMENT", "REASSIGNED", narrative,
                        authority, order_number, eff)
    create_personnel_order(personnel_id, "ASSIGNMENT", "UNIT ASSIGNMENT ORDERS", narrative, effective_date=eff, authority=authority, details={"assignment":new_label,"duty_position":new_duty}, source_key=f"ASSIGNMENT:{personnel_id}:{unit_node_id}:{eff}", document_number=order_number)
    enqueue_discord_role_sync(personnel_id,f'ASSIGNMENT {old_label}->{new_label}')
    # Any assignment entry point may complete a Replacement Detachment workflow.
    # The helper is intentionally idempotent and releases only when every gate is met.
    try:
        case=fetch_one("SELECT id FROM recruiting_cases WHERE personnel_id=%s AND status IN ('REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING') LIMIT 1",(personnel_id,))
        if case and 'finalize_replacement_release' in globals():
            finalize_replacement_release(personnel_id,authority or 'BATTALION S-1')
    except Exception:
        log.exception('Replacement release check failed after assignment for %s',personnel_id)


def personnel_form_catalogs():
    """Authoritative dropdown values for staff personnel actions."""
    ranks=fetch_all("""SELECT rank_code,rank_name,pay_grade,precedence FROM rank_catalog WHERE is_active=TRUE ORDER BY precedence""")
    mos=fetch_all("""SELECT mos_code,mos_title,category,sort_order FROM battalion_mos_catalog WHERE is_active=TRUE ORDER BY category,sort_order,mos_code""")
    nodes=fetch_all("""SELECT id,parent_id,unit_code,display_name,unit_type,sort_order FROM unit_nodes WHERE is_active=TRUE ORDER BY CASE unit_type WHEN 'Battalion' THEN 0 WHEN 'Company' THEN 1 WHEN 'Headquarters' THEN 2 WHEN 'Section' THEN 3 WHEN 'Platoon' THEN 4 WHEN 'Squad' THEN 5 ELSE 9 END,sort_order,display_name""")
    assignments=[]
    for n in nodes:
        if str(n.get('unit_type') or '').lower()=='battalion':
            continue
        assignments.append({**n,'assignment_label':format_assignment_node(n['id']) or n['display_name']})
    billets=fetch_all("""SELECT billet_code,billet_title,preferred_mos_code,unit_code,sort_order,is_leadership FROM unit_billets WHERE is_active=TRUE ORDER BY unit_code,sort_order,billet_title""")
    appointments=fetch_all("""SELECT appointment_code,appointment_name,echelon,suggested_rank,sort_order FROM appointment_catalog WHERE is_active=TRUE ORDER BY sort_order,appointment_name""")
    duty={}
    for b in billets:
        name=(b.get('billet_title') or '').strip()
        if name: duty[name.upper()]={'value':name,'label':name,'mos_code':b.get('preferred_mos_code'),'source':'BILLET'}
    for m in mos:
        name=(m.get('mos_title') or '').strip()
        if name: duty.setdefault(name.upper(),{'value':name,'label':name,'mos_code':m.get('mos_code'),'source':'MOS'})
    for a in appointments:
        name=(a.get('appointment_name') or '').strip()
        if name: duty.setdefault(name.upper(),{'value':name,'label':name,'mos_code':None,'source':'APPOINTMENT'})
    return {'ranks':ranks,'mos_catalog':mos,'organization_nodes':nodes,'assignment_options':assignments,'duty_positions':sorted(duty.values(),key=lambda x:x['label'].upper()),'appointment_catalog':appointments,'billet_catalog':billets}


def validate_system_choice(value, rows, key):
    if value in (None,''): return None
    for row in rows:
        if str(row.get(key))==str(value): return row
    raise ValueError(f'Invalid system selection for {key}')


def file_primary_mos_change(personnel_id, mos_code, effective_date, authority, remarks=None):
    catalogs=personnel_form_catalogs(); mos=validate_system_choice((mos_code or '').upper(),catalogs['mos_catalog'],'mos_code')
    if not mos: raise ValueError('MOS selection required')
    current=fetch_one('SELECT mos_code FROM personnel WHERE id=%s',(personnel_id,))
    if not current: raise ValueError('Personnel record not found')
    if (current.get('mos_code') or '').upper()==mos['mos_code']: return
    execute("UPDATE personnel_mos_records SET status='SUPERSEDED' WHERE personnel_id=%s AND mos_kind='PRIMARY' AND status='CURRENT'",(personnel_id,))
    execute('UPDATE personnel SET mos_code=%s,updated_at=NOW() WHERE id=%s',(mos['mos_code'],personnel_id))
    execute("""INSERT INTO personnel_mos_records(personnel_id,mos_code,mos_title,mos_kind,status,effective_date,qualified_by,remarks) VALUES(%s,%s,%s,'PRIMARY','CURRENT',%s,%s,%s) ON CONFLICT(personnel_id,mos_code,mos_kind) DO UPDATE SET status='CURRENT',mos_title=EXCLUDED.mos_title,effective_date=EXCLUDED.effective_date,qualified_by=EXCLUDED.qualified_by,remarks=EXCLUDED.remarks""",(personnel_id,mos['mos_code'],mos['mos_title'],effective_date,authority,remarks))
    write_service_entry(personnel_id,'MOS',f"PRIMARY MOS — {mos['mos_code']}",f"{mos['mos_title']} recorded as the Soldier's primary battlefield MOS.",authority,None,effective_date if isinstance(effective_date,date) else date.today())
    enqueue_discord_role_sync(personnel_id,f'MOS {mos["mos_code"]}')



def _state_event_type_for_service_entry(entry_type, title=None):
    key=str(entry_type or '').upper().strip()
    title_key=str(title or '').upper()
    mapping={
        'ARRIVAL':'PERSONNEL_REPORTED','ASSIGNMENT':'PERSONNEL_ASSIGNED','RANK':'PROMOTED',
        'APPOINTMENT':'APPOINTMENT_STARTED','AWARD':'AWARD_GRANTED','TRAINING':'TRAINING_COMPLETED',
        'OPERATIONS':'OPERATION_CREDITED','CASUALTY':'WIA_RECORDED','MOS':'MOS_CHANGED',
        'EQUIPMENT':'EQUIPMENT_EVENT','COMMAND REMARK':'COMMAND_REMARK','ADMIN':'ADMIN_ACTION',
    }
    event=mapping.get(key,'SERVICE_RECORD_ENTRY')
    if event=='PROMOTED' and 'RANK RECORDED' in title_key:
        event='RANK_RECORDED'
    if key=='TRAINING' and 'QUALIFICATION' in title_key:
        event='QUALIFICATION_COMPLETED'
    return event


def emit_state_event(event_type, *, personnel_id=None, operation_id=None, weapon_id=None,
                     unit_node_id=None, effective_date=None, title=None, narrative=None,
                     reference_number=None, source_key=None, details=None):
    """Append one standardized battalion-state event. It never replaces authoritative records."""
    if not source_key:
        seed=f"{event_type}:{personnel_id}:{operation_id}:{weapon_id}:{effective_date}:{title}:{narrative}"
        source_key='STATE:'+str(abs(hash(seed)))
    execute("""INSERT INTO battalion_state_events
               (event_type,personnel_id,operation_id,weapon_id,unit_node_id,effective_date,title,narrative,reference_number,source_key,details_json)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(source_key) DO NOTHING""",
            (event_type,personnel_id,operation_id,weapon_id,unit_node_id,effective_date or date.today(),
             title or event_type.replace('_',' ').title(),narrative,reference_number,source_key,Json(details or {})))


def enqueue_discord_role_sync(personnel_id, reason):
    """Website is authoritative; Battalion Clerk consumes this queue and mirrors canonical Discord roles."""
    link=fetch_one("SELECT guild_id,discord_user_id FROM website_member_links WHERE personnel_id=%s",(str(personnel_id),))
    if not link:
        return None
    execute("""INSERT INTO discord_role_sync_queue(personnel_id,guild_id,discord_user_id,reason,status,requested_at)
               VALUES(%s,%s,%s,%s,'PENDING',NOW())
               ON CONFLICT(personnel_id) WHERE status='PENDING'
               DO UPDATE SET guild_id=EXCLUDED.guild_id,discord_user_id=EXCLUDED.discord_user_id,
                             reason=EXCLUDED.reason,requested_at=NOW(),error_text=NULL""",
            (personnel_id,link.get('guild_id'),link.get('discord_user_id'),reason))
    return link


def current_situation_snapshot(person):
    if not person:
        return {}
    p=soldier_view(person)
    pid=p['id']
    weapon=current_weapon_for(p)
    exp=member_combat_experience(pid)
    tour=member_tour_phase(p)
    stats=member_service_statistics(p)
    act=inactivity_snapshot(p)
    last_activity=act.get('days_since_activity') if isinstance(act,dict) else None
    mos=fetch_one("SELECT mos_title FROM battalion_mos_catalog WHERE mos_code=%s",(p.get('mos_code'),)) or {}
    return {
        'rank':p.get('rank_code'),'mos_code':p.get('mos_code'),'mos_title':mos.get('mos_title') or p.get('duty_position'),
        'mos':p.get('mos_code'),'assignment':' / '.join(x for x in [p.get('unit_code'),p.get('platoon'),p.get('squad')] if x),
        'duty':p.get('duty_position') or mos.get('mos_title') or p.get('mos_code'),
        'weapon':f"M16 #{weapon.get('serial_number')}" if weapon else 'NO M16 ISSUED',
        'readiness_percent':int(p.get('readiness_percent') or 0),'readiness_status':p.get('readiness_status') or 'NOT RATED',
        'tour_day':tour.get('tour_day') or 0,'days_to_deros':tour.get('days_to_deros'),'tour_phase':tour.get('phase'),
        'operations':stats.get('operations',0),'combat_experience':exp.get('level'),
        'last_activity_days':last_activity,'activity_state':act.get('state') if isinstance(act,dict) else None,
    }


def field_reputation(person):
    """Narrative descriptors only. They grant no rank, access, or automatic award."""
    if not person:
        return []
    p=soldier_view(person); pid=p['id']; tags=[]
    stats=member_service_statistics(p); exp=member_combat_experience(pid); tour=member_tour_phase(p)
    leadership=leadership_service_summary(pid)
    act=inactivity_snapshot(p)
    inactive_days=int((act or {}).get('days_since_activity') or 0) if isinstance(act,dict) else 0
    if stats.get('operations',0)>=5 and inactive_days<=7:
        tags.append({'code':'RELIABLE','label':'RELIABLE','detail':'Consistent recent activity and official operation participation.'})
    if exp.get('order',0)>=2:
        tags.append({'code':'COMBAT_EXPERIENCED','label':exp.get('level'),'detail':f"{exp.get('operations',0)} credited official operations."})
    if leadership.get('total_days',0)>=14 or combat_leadership_score(pid).get('operations_led',0)>=3:
        tags.append({'code':'FIELD_LEADER','label':'FIELD LEADER','detail':'Established leadership service in the field.'})
    weapon=current_weapon_for(p)
    if weapon:
        insp=weapon_inspection_status(pid)
        if str(weapon.get('serviceability_status') or '').upper()=='SERVICEABLE' and int(weapon.get('rounds_since_cleaning') or 0)<250 and not (insp or {}).get('overdue'):
            tags.append({'code':'WEAPONS_DISCIPLINED','label':'WEAPONS DISCIPLINED','detail':'Issued M16 is serviceable and maintained within battalion standards.'})
    instructed=int((fetch_one("SELECT COUNT(DISTINCT event_id) total FROM battalion_event_instructors WHERE personnel_id=%s",(pid,)) or {'total':0}).get('total') or 0)
    if instructed>=5:
        tags.append({'code':'TRAINING_CADRE','label':'TRAINING CADRE','detail':f'{instructed} official training events instructed.'})
    air_ops=int((fetch_one("""SELECT COUNT(DISTINCT op.operation_id) total FROM operation_participation op
                              JOIN operations o ON o.id=op.operation_id WHERE op.personnel_id=%s
                              AND (UPPER(COALESCE(o.operation_type,'')) LIKE '%%AIR%%' OR UPPER(COALESCE(o.title,'')) LIKE '%%AIR%%'
                                   OR UPPER(COALESCE(o.title,'')) LIKE '%%LZ %%' OR UPPER(COALESCE(o.mission,'')) LIKE '%%AIRMOBILE%%')
                              AND UPPER(COALESCE(op.attendance_status,'')) IN ('FULL CREDIT','CREDITED','COMPLETE','COMPLETED','PARTICIPATED')""",(pid,)) or {'total':0}).get('total') or 0)
    if air_ops>=3:
        tags.append({'code':'AIR_ASSAULT_VETERAN','label':'AIR ASSAULT VETERAN','detail':f'{air_ops} qualifying airmobile / landing-zone operations.'})
    if tour.get('phase') in {'SHORT TIMER','DEROS PENDING'}:
        tags.append({'code':'SHORT_TIMER','label':'SHORT TIMER','detail':f"{tour.get('days_to_deros') if tour.get('days_to_deros') is not None else 0} days to DEROS."})
    return tags


def active_service_timeline(personnel_id, limit=250):
    rows=fetch_all("""SELECT bse.*,o.operation_number,o.title AS operation_title,wi.serial_number AS weapon_serial
                       FROM battalion_state_events bse
                       LEFT JOIN operations o ON o.id=bse.operation_id
                       LEFT JOIN weapon_inventory wi ON wi.id=bse.weapon_id
                       WHERE bse.personnel_id=%s
                       ORDER BY bse.effective_date ASC,bse.created_at ASC LIMIT %s""",(personnel_id,limit))
    # If a very old record predates the state-event migration, preserve service-history visibility.
    if not rows:
        rows=fetch_all("""SELECT id,'SERVICE_RECORD_ENTRY' AS event_type,personnel_id,entry_date AS effective_date,
                           title,narrative,reference_number,created_at FROM personnel_service_history
                           WHERE personnel_id=%s ORDER BY entry_date ASC,created_at ASC LIMIT %s""",(personnel_id,limit))
    return rows


def weapon_personality(weapon_id):
    if not weapon_id:
        return None
    w=fetch_one("SELECT * FROM weapon_inventory WHERE id=%s",(weapon_id,))
    if not w:
        return None
    ops=int((fetch_one("SELECT COUNT(DISTINCT operation_id) total FROM weapon_round_events WHERE weapon_id=%s AND operation_id IS NOT NULL",(weapon_id,)) or {'total':0}).get('total') or 0)
    cleanings=int((fetch_one("SELECT COUNT(*) total FROM weapon_maintenance_log WHERE weapon_id=%s AND UPPER(action_type) LIKE '%%CLEAN%%'",(weapon_id,)) or {'total':0}).get('total') or 0)
    inspections=int((fetch_one("SELECT COUNT(*) total FROM weapon_inspections WHERE weapon_id=%s",(weapon_id,)) or {'total':0}).get('total') or 0)
    holders=fetch_all("""SELECT wih.personnel_id,wih.issued_at,wih.turned_in_at,p.rank_code,p.first_name,p.last_name,
                         COALESCE((SELECT SUM(wre.rounds_fired) FROM weapon_round_events wre
                                   WHERE wre.weapon_id=wih.weapon_id AND wre.personnel_id=wih.personnel_id
                                     AND wre.recorded_at::date>=wih.issued_at
                                     AND (wih.turned_in_at IS NULL OR wre.recorded_at::date<=wih.turned_in_at)),0) AS holder_rounds
                         FROM weapon_issue_history wih JOIN personnel p ON p.id=wih.personnel_id
                         WHERE wih.weapon_id=%s ORDER BY wih.issued_at ASC,wih.created_at ASC""",(weapon_id,))
    events=fetch_all("""SELECT wre.*,o.operation_number,o.title AS operation_title FROM weapon_round_events wre
                        LEFT JOIN operations o ON o.id=wre.operation_id WHERE wre.weapon_id=%s
                        ORDER BY wre.recorded_at DESC LIMIT 50""",(weapon_id,))
    maintenance=fetch_all("SELECT * FROM weapon_maintenance_log WHERE weapon_id=%s ORDER BY performed_at DESC LIMIT 40",(weapon_id,))
    return {'weapon':w,'operations_carried':ops,'cleanings':cleanings,'inspections':inspections,'holders':holders,'round_events':events,'maintenance':maintenance}


def award_recommendation_evidence(personnel_id):
    p=fetch_one("SELECT * FROM personnel WHERE id=%s",(personnel_id,))
    if not p: return {}
    stats=member_service_statistics(p)
    leadership=combat_leadership_score(personnel_id)
    citations=fetch_all("""SELECT pfc.*,pfc.citation_type AS title,pfc.citation_text AS narrative,o.operation_number,o.title AS operation_title FROM personnel_field_citations pfc
                            LEFT JOIN operations o ON o.id=pfc.operation_id WHERE pfc.personnel_id=%s
                            ORDER BY pfc.citation_date DESC,pfc.created_at DESC LIMIT 12""",(personnel_id,))
    previous=fetch_all("SELECT award_name,award_date,order_number FROM personnel_awards WHERE personnel_id=%s ORDER BY award_date DESC",(personnel_id,))
    return {'operations':stats.get('operations',0),'leadership_days':leadership.get('leadership_days',0),
            'training_instructed':leadership.get('training_conducted',0),'readiness':int(p.get('readiness_percent') or 0),'readiness_percent':int(p.get('readiness_percent') or 0),
            'field_citations':citations,'previous_awards':previous}


def promotion_board_packet(personnel_id):
    p=fetch_one("SELECT * FROM personnel WHERE id=%s",(personnel_id,))
    if not p: return {}
    last=fetch_one("SELECT effective_date FROM promotion_history WHERE personnel_id=%s ORDER BY effective_date DESC,created_at DESC LIMIT 1",(personnel_id,))
    start=(last or {}).get('effective_date') or p.get('date_joined') or date.today()
    tig=max(0,(date.today()-start).days) if isinstance(start,date) else 0
    stats=member_service_statistics(p); leadership=combat_leadership_score(personnel_id)
    quals=int((fetch_one("""SELECT (SELECT COUNT(*) FROM qualifications WHERE personnel_id=%s AND UPPER(status)<>'EXPIRED')+
                              (SELECT COUNT(*) FROM personnel_duty_qualifications WHERE personnel_id=%s AND UPPER(status)<>'EXPIRED') total""",(personnel_id,personnel_id)) or {'total':0}).get('total') or 0)
    training=replacement_training_status(p)
    weapon=current_weapon_for(p)
    weapon_ready=100 if weapon and str(weapon.get('serviceability_status') or '').upper()=='SERVICEABLE' else 0
    attendance=fetch_one("""SELECT COUNT(*) FILTER(WHERE credited=TRUE) credited,COUNT(*) total FROM personnel_activity_credit
                            WHERE personnel_id=%s AND activity_date>=CURRENT_DATE-INTERVAL '90 days'""",(personnel_id,)) or {'credited':0,'total':0}
    attendance_pct=round(int(attendance.get('credited') or 0)/max(1,int(attendance.get('total') or 0))*100) if int(attendance.get('total') or 0) else 0
    admin_notes=int((fetch_one("SELECT COUNT(*) total FROM personnel_service_history WHERE personnel_id=%s AND UPPER(entry_type) IN ('DISCIPLINARY','ADVERSE')",(personnel_id,)) or {'total':0}).get('total') or 0)
    rec=fetch_one("""SELECT * FROM personnel_recommendations WHERE personnel_id=%s AND UPPER(recommendation_type)='PROMOTION'
                     ORDER BY created_at DESC LIMIT 1""",(personnel_id,))
    eligibility=promotion_eligibility(p)
    eligible=any(x.get('eligible') for x in eligibility) if eligibility else False
    return {'personnel':p,'time_in_rank_days':tig,'operations':stats.get('operations',0),'training_complete':bool(training.get('complete')),
            'weapon_readiness':weapon_ready,'attendance_percent':attendance_pct,'leadership_duties':leadership.get('operations_led',0),
            'leadership_days':leadership.get('leadership_days',0),'qualifications':quals,'admin_adverse_notes':admin_notes,
            'recommendation':rec,'eligibility':eligibility,'eligible_for_consideration':eligible}


def most_served_with(personnel_id, limit=5):
    return member_buddy_history(personnel_id,limit)


def unit_cohesion_readonly(unit_node_id):
    node=unit_node(unit_node_id)
    if not node: return None
    ids=unit_descendant_ids(unit_node_id) or [unit_node_id]
    people=fetch_all("SELECT id,readiness_percent FROM personnel WHERE unit_node_id=ANY(%s) AND separated_at IS NULL AND archived=FALSE",(ids,))
    strength=len(people); pids=[x['id'] for x in people]
    readiness=round(sum(int(x.get('readiness_percent') or 0) for x in people)/strength) if strength else 0
    recent_ops=fetch_all("SELECT id FROM operations WHERE UPPER(COALESCE(status,'')) IN ('CLOSED','COMPLETE','COMPLETED') ORDER BY COALESCE(operation_date,CURRENT_DATE) DESC LIMIT 5")
    rates=[]
    for op in recent_ops:
        count=int((fetch_one("SELECT COUNT(DISTINCT personnel_id) total FROM operation_participation WHERE operation_id=%s AND personnel_id=ANY(%s) AND UPPER(COALESCE(attendance_status,'')) IN ('FULL CREDIT','CREDITED','COMPLETE','COMPLETED','PARTICIPATED','PRESENT')",(op['id'],pids)) or {'total':0}).get('total') or 0) if pids else 0
        rates.append(round(count/max(1,strength)*100))
    attendance=round(sum(rates)/len(rates)) if rates else 0
    training_rates=[]
    recent_training=fetch_all("SELECT id FROM battalion_events WHERE event_type='TRAINING' AND UPPER(COALESCE(status,'')) IN ('CLOSED','COMPLETE','COMPLETED') ORDER BY starts_at DESC LIMIT 5")
    for ev in recent_training:
        trained=int((fetch_one("SELECT COUNT(DISTINCT personnel_id) total FROM battalion_event_attendance WHERE event_id=%s AND personnel_id=ANY(%s) AND credited_at IS NOT NULL",(ev['id'],pids)) or {'total':0}).get('total') or 0) if pids else 0
        training_rates.append(round(trained/max(1,strength)*100))
    training_together=round(sum(training_rates)/len(training_rates)) if training_rates else 0
    leader=fetch_one("SELECT MIN(effective_date) effective_date FROM personnel_appointments WHERE unit_node_id=%s AND is_current=TRUE",(unit_node_id,))
    stable_days=max(0,(date.today()-leader['effective_date']).days) if leader and leader.get('effective_date') else 0
    stability=min(100,round(stable_days/30*100))
    together=(unit_experience(unit_node_id) or {}).get('operations_together',0)
    continuity=min(100,int(together or 0)*10)
    score=round(readiness*.20+attendance*.30+training_together*.10+stability*.20+continuity*.20)
    return {'unit':node,'cohesion':max(0,min(100,score)),'readiness':readiness,'attendance':attendance,'training_together':training_together,'leadership_stability':stability,'continuity':continuity,'strength':strength}


def unit_history_snapshot(unit_node_id):
    node=unit_node(unit_node_id)
    if not node: return None
    ids=unit_descendant_ids(unit_node_id) or [unit_node_id]
    people=fetch_all("SELECT id,rank_code,first_name,last_name,mos_code,duty_position,readiness_percent FROM personnel WHERE unit_node_id=ANY(%s) AND separated_at IS NULL AND archived=FALSE ORDER BY last_name,first_name",(ids,))
    ops=int((fetch_one("SELECT COUNT(DISTINCT operation_id) total FROM operation_participation WHERE unit_node_id=ANY(%s)",(ids,)) or {'total':0}).get('total') or 0)
    awards=int((fetch_one("""SELECT COUNT(*) total FROM personnel_awards pa JOIN personnel p ON p.id=pa.personnel_id WHERE p.unit_node_id=ANY(%s)""",(ids,)) or {'total':0}).get('total') or 0)
    readiness=round(sum(int(x.get('readiness_percent') or 0) for x in people)/len(people)) if people else 0
    leaders=fetch_all("""SELECT pa.*,ac.appointment_name,p.rank_code,p.first_name,p.last_name FROM personnel_appointments pa
                          JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code JOIN personnel p ON p.id=pa.personnel_id
                          WHERE pa.unit_node_id=%s ORDER BY pa.effective_date DESC,pa.created_at DESC LIMIT 30""",(unit_node_id,))
    command_history=fetch_all("""SELECT cch.*,po.rank_code AS outgoing_rank,po.last_name AS outgoing_last,pi.rank_code AS incoming_rank,pi.last_name AS incoming_last
                                   FROM command_change_history cch LEFT JOIN personnel po ON po.id=cch.outgoing_personnel_id
                                   LEFT JOIN personnel pi ON pi.id=cch.incoming_personnel_id
                                   WHERE cch.unit_code=%s ORDER BY cch.effective_date DESC,cch.created_at DESC LIMIT 20""",(node.get('unit_code'),))
    experience=unit_experience(unit_node_id)
    cohesion=unit_cohesion_readonly(unit_node_id)
    return {'unit':node,'current_members':people,'operations':ops,'awards':awards,'average_readiness':readiness,
            'leaders':leaders,'command_history':command_history,'experience':experience,'cohesion':cohesion}


def command_lineage():
    rows=fetch_all("""SELECT pa.unit_node_id,un.unit_code,un.display_name,un.unit_type,pa.appointment_code,ac.appointment_name,
                       p.rank_code,p.first_name,p.last_name,pa.effective_date,pa.ended_date,pa.is_current
                       FROM personnel_appointments pa JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code
                       JOIN personnel p ON p.id=pa.personnel_id LEFT JOIN unit_nodes un ON un.id=pa.unit_node_id
                       WHERE pa.appointment_code IN ('BN_CO','BN_XO','BN_SGM','CO_CO','CO_XO','CO_1SG','PL','PSG','SL','ASST_SL','FTL')
                       ORDER BY COALESCE(un.sort_order,0),ac.sort_order,pa.effective_date DESC""")
    return rows


def smart_personnel_search(query, limit=100):
    q=(query or '').strip().lower()
    if not q: return []
    special_words={'not','ready','inactive','inactivity','promotion','eligible','weapon','cleaning','days','day','company'}
    tokens=[x for x in re.findall(r'[a-z0-9-]+',q) if x not in special_words and not x.isdigit()]
    clauses=[]; params=[]
    for token in tokens:
        clauses.append("(LOWER(COALESCE(first_name,'')) LIKE %s OR LOWER(COALESCE(last_name,'')) LIKE %s OR LOWER(COALESCE(rank_code,'')) LIKE %s OR LOWER(COALESCE(mos_code,'')) LIKE %s OR LOWER(COALESCE(unit_code,'')) LIKE %s OR LOWER(COALESCE(platoon,'')) LIKE %s OR LOWER(COALESCE(squad,'')) LIKE %s OR LOWER(COALESCE(duty_position,'')) LIKE %s)")
        like=f'%{token}%'; params += [like]*8
    sql="SELECT * FROM personnel WHERE separated_at IS NULL AND archived=FALSE"
    if clauses: sql += ' AND ' + ' AND '.join(clauses)
    if 'not ready' in q: sql += " AND COALESCE(readiness_percent,0)<80"
    if 'inactive' in q or 'inactivity' in q:
        m=re.search(r'(?:inactive|inactivity)\s+(\d+)',q); days=int(m.group(1)) if m else 7
        sql += " AND COALESCE(activity_last_seen_at,activity_last_duty_at,created_at)<NOW()-(%s || ' days')::interval"; params.append(days)
    company_match=re.search(r'\b([abch])\s+company\b',q)
    if company_match:
        letter=company_match.group(1).upper()
        code='HHC' if letter=='H' else letter+'/'
        sql += " AND UPPER(COALESCE(unit_code,'')) LIKE %s"; params.append(code+'%')
    sql += " ORDER BY last_name,first_name LIMIT %s"; params.append(limit)
    rows=fetch_all(sql,tuple(params))
    if 'weapon cleaning' in q:
        rows=[r for r in rows if (lambda w: bool(w and int(w.get('rounds_since_cleaning') or 0)>=250))(current_weapon_for(r))]
    if 'promotion eligible' in q:
        filtered=[]
        for r in rows:
            try:
                if any(x.get('eligible') for x in promotion_eligibility(soldier_view(r))): filtered.append(r)
            except Exception: pass
        rows=filtered
    return rows


def comparison_snapshot(personnel_id):
    p=fetch_one("SELECT * FROM personnel WHERE id=%s",(personnel_id,))
    if not p: return None
    stats=member_service_statistics(p); leadership=combat_leadership_score(personnel_id)
    quals=int((fetch_one("""SELECT (SELECT COUNT(*) FROM qualifications WHERE personnel_id=%s AND UPPER(status)<>'EXPIRED')+
                         (SELECT COUNT(*) FROM personnel_duty_qualifications WHERE personnel_id=%s AND UPPER(status)<>'EXPIRED') total""",(personnel_id,personnel_id)) or {'total':0}).get('total') or 0)
    att=fetch_one("""SELECT COUNT(*) FILTER(WHERE UPPER(COALESCE(attendance_status,'')) IN ('FULL CREDIT','CREDITED','PARTICIPATED','PRESENT','COMPLETE','COMPLETED')) credited,COUNT(*) total
                     FROM operation_participation WHERE personnel_id=%s""",(personnel_id,)) or {'credited':0,'total':0}
    attendance_percent=round(100*int(att.get('credited') or 0)/max(1,int(att.get('total') or 0))) if int(att.get('total') or 0) else 0
    return {'personnel':p,'statistics':stats,'leadership':leadership,'qualifications':quals,
            'awards':stats.get('awards',0),'readiness':int(p.get('readiness_percent') or 0),'attendance_percent':attendance_percent,
            'tour':member_tour_phase(p),'experience':member_combat_experience(personnel_id)}


def operation_duty_suggestions(operation_id):
    op=operation_record(operation_id)
    if not op: return []
    expected=operation_expected_roster(op)
    assigned=fetch_all("SELECT * FROM operation_duty_assignments WHERE operation_id=%s",(operation_id,))
    assigned_ids={str(x['personnel_id']) for x in assigned}
    requirements=fetch_all("SELECT * FROM operation_role_requirements WHERE operation_id=%s ORDER BY duty_role",(operation_id,))
    if not requirements:
        requirements=[{'duty_role':'Platoon Leader','required_count':1,'preferred_mos_code':'11A'},
                      {'duty_role':'Platoon Sergeant','required_count':1,'preferred_mos_code':'11L'},
                      {'duty_role':'Squad Leader','required_count':3,'preferred_mos_code':'11L'},
                      {'duty_role':'RTO','required_count':3,'preferred_mos_code':'11N'},
                      {'duty_role':'Machine Gunner','required_count':3,'preferred_mos_code':'11M'}]
    results=[]
    rank_order={r['rank_code']:int(r['precedence']) for r in fetch_all("SELECT rank_code,precedence FROM rank_catalog")}
    expected_ids=[x['id'] for x in expected]
    load_rows=fetch_all("""SELECT oda.personnel_id,COUNT(*)::int AS total FROM operation_duty_assignments oda
                           JOIN operations o ON o.id=oda.operation_id
                           WHERE oda.personnel_id=ANY(%s) AND COALESCE(o.start_at,o.created_at)>=NOW()-INTERVAL '30 days'
                             AND (UPPER(COALESCE(oda.duty_role,'')) LIKE '%%LEADER%%' OR UPPER(COALESCE(oda.duty_role,'')) LIKE '%%SERGEANT%%' OR UPPER(COALESCE(oda.duty_role,'')) LIKE '%%COMMANDER%%')
                           GROUP BY oda.personnel_id""",(expected_ids,)) if expected_ids else []
    leadership_load={str(x['personnel_id']):int(x.get('total') or 0) for x in load_rows}
    for req in requirements:
        candidates=[]
        for p in expected:
            if str(p['id']) in assigned_ids: continue
            duty_state=str(p.get('duty_status') or '').upper()
            if duty_state in {'LEAVE','HOSPITAL','WIA','SEPARATED'}: continue
            score=0; reasons=[]
            if req.get('preferred_mos_code') and p.get('mos_code')==req.get('preferred_mos_code'): score+=50; reasons.append('preferred MOS')
            if str(req.get('duty_role') or '').lower() in str(p.get('duty_position') or '').lower(): score+=30; reasons.append('current billet match')
            score+=min(20,int(p.get('readiness_percent') or 0)//5)
            if duty_state in {'PRESENT FOR DUTY','FIELD DUTY','TRAINING','ATTACHED','TEMPORARY DUTY'}: score+=10; reasons.append('available for duty')
            if any(x in str(req.get('duty_role') or '').upper() for x in ['LEADER','SERGEANT','COMMANDER']):
                load=leadership_load.get(str(p['id']),0)
                score-=min(15,load*3)
                if load: reasons.append(f'{load} recent leadership assignment(s)')
            if req.get('minimum_rank_code') and rank_order.get(p.get('rank_code'),0)<rank_order.get(req.get('minimum_rank_code'),0): continue
            if req.get('qualification_code'):
                ok=fetch_one("""SELECT 1 FROM personnel_duty_qualifications pdq JOIN duty_qualification_types dqt ON dqt.id=pdq.qualification_type_id
                               WHERE pdq.personnel_id=%s AND dqt.code=%s AND UPPER(pdq.status)='QUALIFIED' AND (pdq.expiration_date IS NULL OR pdq.expiration_date>=CURRENT_DATE)""",(p['id'],req['qualification_code']))
                if not ok: continue
                score+=20; reasons.append('qualification current')
            candidates.append({'personnel':p,'score':score,'reasons':reasons})
        candidates.sort(key=lambda x:(-x['score'],x['personnel'].get('last_name') or ''))
        results.append({'requirement':req,'candidates':candidates[:8]})
    return results


def member_personal_action_center(person):
    if not person: return []
    p=soldier_view(person); pid=p['id']; items=[]
    insp=weapon_inspection_status(pid)
    if insp and insp.get('days') is not None and int(insp.get('days'))<=7:
        items.append({'section':'S-4','title':'M16 INSPECTION','detail':f"Due in {max(0,int(insp.get('days')))} day(s).",'priority':'HIGH' if insp.get('overdue') else 'WATCH','target':'my_soldier_record','anchor':'weapon'})
    exp=fetch_one("""SELECT MIN(expires_at) due FROM qualifications WHERE personnel_id=%s AND expires_at BETWEEN CURRENT_DATE AND CURRENT_DATE+30""",(pid,))
    dexp=fetch_one("""SELECT MIN(expiration_date) due FROM personnel_duty_qualifications WHERE personnel_id=%s AND expiration_date BETWEEN CURRENT_DATE AND CURRENT_DATE+30""",(pid,))
    dates=[x.get('due') for x in [exp or {},dexp or {}] if x.get('due')]
    if dates:
        due=min(dates); items.append({'section':'S-3','title':'QUALIFICATION EXPIRATION','detail':f'{(due-date.today()).days} days remaining.','priority':'WATCH','target':'my_soldier_record','anchor':'training'})
    next_op=fetch_one("SELECT operation_number,title,start_at FROM operations WHERE start_at>NOW() AND UPPER(COALESCE(status,'')) NOT IN ('CANCELLED','CLOSED') ORDER BY start_at LIMIT 1")
    if next_op:
        items.append({'section':'S-3','title':'UPCOMING OPERATION','detail':f"{next_op.get('operation_number') or ''} {next_op.get('title')} — {next_op.get('start_at')}",'priority':'ROUTINE','target':'my_soldier_record','anchor':'operations'})
    tour=member_tour_phase(p)
    if tour.get('days_to_deros') is not None:
        items.append({'section':'S-1','title':'DEROS','detail':f"{tour.get('days_to_deros')} days remaining in current tour.",'priority':'WATCH' if tour.get('days_to_deros')<=60 else 'ROUTINE','target':'my_201_file','anchor':'tour'})
    elig=promotion_eligibility(p)
    if elig:
        next_row=elig[0]
        missing=[x.get('label') for x in next_row.get('requirements',[]) if not x.get('complete')]
        if missing:
            items.append({'section':'S-1','title':'NEXT PROMOTION REQUIREMENT','detail':missing[0],'priority':'ROUTINE','target':'my_201_file','anchor':'promotion-eligibility'})
    return items


def next_recommended_action(person):
    items=member_personal_action_center(person)
    return items[0] if items else {'section':'HEADQUARTERS','title':'MAINTAIN READINESS','detail':'No immediate deficiency is on file. Continue unit participation and maintain current qualifications.','priority':'ROUTINE'}


def appointment_for_node(appointment_code, node_id=None):
    if node_id:
        row = fetch_one(
            """SELECT p.*,pa.appointment_status,pa.organization,
                      ac.appointment_name,un.display_name AS appointment_unit
               FROM personnel_appointments pa
               JOIN personnel p ON p.id=pa.personnel_id
               JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code
               LEFT JOIN unit_nodes un ON un.id=pa.unit_node_id
               WHERE pa.appointment_code=%s AND pa.is_current=TRUE
                 AND (pa.unit_node_id=%s OR pa.unit_node_id IS NULL)
               ORDER BY (pa.unit_node_id IS NOT NULL) DESC,pa.effective_date DESC
               LIMIT 1""",
            (appointment_code, node_id),
        )
    else:
        row = fetch_one(
            """SELECT p.*,pa.appointment_status,pa.organization,
                      ac.appointment_name,un.display_name AS appointment_unit
               FROM personnel_appointments pa
               JOIN personnel p ON p.id=pa.personnel_id
               JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code
               LEFT JOIN unit_nodes un ON un.id=pa.unit_node_id
               WHERE pa.appointment_code=%s AND pa.is_current=TRUE
               ORDER BY pa.effective_date DESC LIMIT 1""",
            (appointment_code,),
        )
    return row


def chain_of_command_for(personnel):
    if not personnel:
        return []
    ancestry = unit_ancestry(personnel.get("unit_node_id"))
    squad = next((n for n in ancestry if n["unit_type"]=="Squad"), None)
    platoon = next((n for n in ancestry if n["unit_type"]=="Platoon"), None)
    company = next((n for n in ancestry if n["unit_type"]=="Company"), None)

    chain = []
    checks = [
        ("Squad Leader","SL", squad),
        ("Platoon Sergeant","PSG", platoon),
        ("Platoon Leader","PL", platoon),
        ("First Sergeant","CO_1SG", company),
        ("Company Commander","CO_CO", company),
        ("Battalion Sergeant Major","BN_SGM", None),
        ("Battalion Commander","BN_CO", None),
    ]
    seen = set()
    for title, code, node in checks:
        leader = appointment_for_node(code, node["id"] if node else None)
        if leader and str(leader["id"]) != str(personnel["id"]) and str(leader["id"]) not in seen:
            leader["chain_title"] = title
            chain.append(leader)
            seen.add(str(leader["id"]))
    return chain


def scoped_personnel_for(personnel):
    """Personnel a leader should see on the My Soldiers page."""
    if not personnel:
        return [], None

    current_appts = fetch_all(
        """SELECT pa.*,ac.appointment_code,ac.appointment_name,ac.echelon
           FROM personnel_appointments pa
           JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code
           WHERE pa.personnel_id=%s AND pa.is_current=TRUE
           ORDER BY ac.sort_order""",
        (personnel["id"],),
    )

    scope_node = None
    # Prefer the most local command appointment.
    priority = ["SL","ASST_SL","FTL","PSG","PL","CO_1SG","CO_XO","CO_CO","BN_SGM","BN_XO","BN_CO"]
    for code in priority:
        ap = next((x for x in current_appts if x["appointment_code"]==code and x.get("unit_node_id")), None)
        if ap:
            scope_node = unit_node(ap["unit_node_id"])
            break

    if not scope_node and personnel.get("unit_node_id"):
        ancestry = unit_ancestry(personnel["unit_node_id"])
        if session.get("access_role") == "nco":
            scope_node = next((n for n in ancestry if n["unit_type"]=="Squad"), None)
        elif session.get("access_role") == "company_hq":
            scope_node = next((n for n in ancestry if n["unit_type"]=="Company"), None)
        elif session.get("access_role") == "battalion_hq":
            scope_node = next((n for n in ancestry if n["unit_type"]=="Battalion"), None)

    if not scope_node:
        return [], None

    ids = unit_descendant_ids(scope_node["id"])
    if not ids:
        return [], scope_node
    soldiers = fetch_all(
        """SELECT p.*,
                  wi.serial_number AS weapon_serial,
                  wi.condition_state AS weapon_condition,
                  wi.condition_percent AS weapon_percent
           FROM personnel p
           LEFT JOIN weapon_issue_history wih ON wih.personnel_id=p.id AND wih.is_current=TRUE
           LEFT JOIN weapon_inventory wi ON wi.id=wih.weapon_id
           WHERE p.unit_node_id = ANY(%s)
           ORDER BY p.platoon NULLS FIRST,p.squad NULLS FIRST,p.last_name""",
        (ids,),
    )
    return soldiers, scope_node


def authorized_absence_active(person):
    if not person:
        return False
    if str(person.get("duty_status") or "").upper() != "LEAVE":
        return False
    expected = person.get("loa_expected_return_date")
    if isinstance(expected, str):
        try: expected = date.fromisoformat(expected)
        except Exception: expected = None
    return expected is None or expected >= date.today()



def inactivity_thresholds_for_person(person):
    """Use the same guild thresholds as Battalion Clerk; defaults remain 7/14/21/30."""
    defaults={"warning":7,"s1":14,"property":21,"command":30}
    if not person or not person.get("id"): return defaults
    try:
        row=fetch_one("""SELECT COALESCE(c.inactivity_warning_days,7) warning,
                               COALESCE(c.inactivity_s1_days,14) s1,
                               COALESCE(c.inactivity_property_days,21) property,
                               COALESCE(c.inactivity_command_days,30) command
                        FROM website_member_links w
                        LEFT JOIN clerk_guild_settings c ON c.guild_id=w.guild_id::text
                        WHERE w.personnel_id=%s LIMIT 1""",(str(person["id"]),))
        if row:
            vals={k:int(row.get(k) or defaults[k]) for k in defaults}
            if vals["warning"] < vals["s1"] < vals["property"] < vals["command"]: return vals
    except Exception:
        pass
    return defaults

def activity_classification(person):
    """Battalion inactivity ladder based only on qualifying participation."""
    if authorized_absence_active(person): return "EXCUSED ABSENCE", 0
    stamp=person.get("activity_last_duty_at") or person.get("activity_last_seen_at") or person.get("created_at")
    if not stamp: return "CURRENT", 0
    if hasattr(stamp,"date"):
        delta=datetime.now(stamp.tzinfo)-stamp if getattr(stamp,"tzinfo",None) else datetime.now()-stamp
        days=max(0,delta.days)
    else: days=0
    t=inactivity_thresholds_for_person(person)
    if days<t["warning"]: return "CURRENT",days
    if days<t["s1"]: return "WATCH",days
    if days<t["property"]: return "DEFICIENT",days
    if days<t["command"]: return "INACTIVE",days
    return "COMMAND REVIEW",days


def inactivity_snapshot(person):
    state, days = activity_classification(person)
    stamp = person.get("activity_last_duty_at") or person.get("activity_last_seen_at") or person.get("created_at")
    source = "BATTALION RECORD"
    last_credit = fetch_one("""SELECT activity_date,source,duration_seconds FROM personnel_activity_credit
                             WHERE personnel_id=%s AND credited=TRUE ORDER BY activity_date DESC,created_at DESC LIMIT 1""", (person["id"],))
    if last_credit:
        source = last_credit.get("source") or source
        credit_date = last_credit.get("activity_date")
        if not stamp or (credit_date and hasattr(stamp, 'date') and credit_date >= stamp.date()):
            stamp = credit_date
    t=inactivity_thresholds_for_person(person)
    thresholds={"CURRENT":t["warning"],"WATCH":t["s1"],"DEFICIENT":t["property"],"INACTIVE":t["command"]}
    next_day = thresholds.get(state)
    next_label = {"CURRENT":"WATCH","WATCH":"DEFICIENT","DEFICIENT":"INACTIVE","INACTIVE":"COMMAND REVIEW"}.get(state)
    until = max(0,next_day-days) if next_day is not None else None
    latest_contact = fetch_one("SELECT * FROM inactivity_contact_log WHERE personnel_id=%s ORDER BY contacted_at DESC LIMIT 1", (person["id"],))
    return {"state":state,"days":days,"last_activity":stamp,"source":source,"next_label":next_label,"days_to_next":until,
            "excused":authorized_absence_active(person),"expected_return":person.get("loa_expected_return_date"),"latest_contact":latest_contact}


def tour_phase(person):
    deros = person.get("deros_date")
    if not deros:
        return "IN COUNTRY", None
    if isinstance(deros, str):
        try:
            deros = date.fromisoformat(deros)
        except Exception:
            return "IN COUNTRY", None
    remaining = (deros - date.today()).days
    if remaining < 0:
        return "TOUR COMPLETE", remaining
    if remaining <= 7:
        return "CLEARANCE PENDING", remaining
    if remaining <= 14:
        return "DEROS PROCESSING", remaining
    if remaining <= 30:
        return "SHORT-TIMER", remaining
    if remaining <= 60:
        return "DEROS FORECAST ENTERED", remaining
    return "IN COUNTRY", remaining




def readiness_score(person):
    """Calculate the Soldier's 100-point readiness score from current battalion records.

    Weighting:
      Activity / participation 30
      Training / qualifications 25
      Weapon / equipment 20
      Administrative readiness 15
      Official duty participation 10
    """
    pid = person.get("id")
    now = datetime.now(timezone.utc)
    breakdown = {}

    # 1. General activity / participation — Discord voice activity directly matters.
    stamp = person.get("activity_last_seen_at") or person.get("activity_last_duty_at") or person.get("created_at")
    if authorized_absence_active(person):
        inactive_days = 0
        activity_points = 30
        breakdown["activity"] = {"points":30,"max":30,"detail":"Authorized absence — inactivity penalties paused"}
        stamp = None
    if stamp and getattr(stamp, "tzinfo", None) is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    if not authorized_absence_active(person):
        inactive_days = max(0, (now - stamp).days) if stamp else 999
        t=inactivity_thresholds_for_person(person)
        if inactive_days < t["warning"]: activity_points=30
        elif inactive_days < t["s1"]: activity_points=22
        elif inactive_days < t["property"]: activity_points=12
        elif inactive_days < t["command"]: activity_points=5
        else: activity_points=0
        breakdown["activity"] = {"points":activity_points,"max":30,"detail":f"{inactive_days} day(s) since qualifying activity"}

    # 2. Training / qualifications. Initial processing is the foundation; current
    # qualifications add the remainder. Expired credentials immediately reduce readiness until renewed.
    training_points = 0
    try:
        sync_qualification_currency(pid)
        replacement = replacement_training_status(person)
        if replacement.get("complete"):
            training_points += 10
        current_quals = fetch_one("""SELECT COUNT(*) total FROM qualifications
                                    WHERE personnel_id=%s AND UPPER(status)='CURRENT'
                                      AND (expires_at IS NULL OR expires_at>=CURRENT_DATE)""", (pid,)) or {"total":0}
        duty_quals = fetch_one("""SELECT COUNT(*) total FROM personnel_duty_qualifications
                                 WHERE personnel_id=%s AND UPPER(status)='QUALIFIED'
                                   AND (expiration_date IS NULL OR expiration_date>=CURRENT_DATE)""", (pid,)) or {"total":0}
        qual_count = int(current_quals.get("total") or 0) + int(duty_quals.get("total") or 0)
        training_points += min(15, qual_count * 5)
        expired = fetch_one("""SELECT (SELECT COUNT(*) FROM qualifications WHERE personnel_id=%s AND UPPER(status)='EXPIRED') +
                                    (SELECT COUNT(*) FROM personnel_duty_qualifications WHERE personnel_id=%s AND UPPER(status)='EXPIRED') AS total""",(pid,pid)) or {"total":0}
        expired_count=int(expired.get("total") or 0)
        if expired_count:
            training_points=max(0,training_points-min(15,expired_count*5))
        breakdown["training"] = {"points":training_points,"max":25,"detail":f"{qual_count} current qualification(s) • {expired_count} expired"}
    except Exception:
        log.exception("Readiness training score failed for %s", pid)
        breakdown["training"] = {"points":training_points,"max":25,"detail":"training data unavailable"}

    # 3. Individual weapon / equipment readiness.
    weapon_points = 0
    weapon = fetch_one("""SELECT wi.* FROM weapon_issue_history wih
                          JOIN weapon_inventory wi ON wi.id=wih.weapon_id
                          WHERE wih.personnel_id=%s AND wih.is_current=TRUE LIMIT 1""", (pid,))
    if weapon:
        state = str(weapon.get("condition_state") or "SERVICEABLE").upper()
        pct = int(weapon.get("condition_percent") or 0)
        if state in {"SERVICEABLE","CLEAN"} and pct >= 80:
            weapon_points = 20
        elif state not in {"UNSERVICEABLE","MAINTENANCE"} and pct >= 70:
            weapon_points = 15
        elif state not in {"UNSERVICEABLE"} and pct >= 50:
            weapon_points = 8
    breakdown["weapon"] = {"points":weapon_points,"max":20,"detail":("No individual weapon issued" if not weapon else f"{weapon.get('condition_state') or 'SERVICEABLE'} — {int(weapon.get('condition_percent') or 0)}%") }

    # 4. Administrative readiness.
    admin_points = 15
    try:
        progress = personnel_progress(pid)
        if progress.get("promotion_hold"):
            admin_points -= 5
        if str(person.get("lifecycle_state") or "ACTIVE").upper() in {"SEPARATED","ARCHIVED"}:
            admin_points = 0
        else:
            missing = [k for k in ("rank_code","mos_code","unit_code") if not person.get(k)]
            if missing:
                admin_points -= min(6, len(missing)*2)
            if not person.get("platoon"):
                admin_points -= 2
            if not person.get("squad") and str(person.get("unit_code") or "").upper() not in {"HHC","HEADQUARTERS"}:
                admin_points -= 2
        admin_points = max(0, admin_points)
        breakdown["admin"] = {"points":admin_points,"max":15,"detail":"No administrative hold" if admin_points == 15 else "Administrative requirements reduce readiness"}
    except Exception:
        log.exception("Readiness admin score failed for %s", pid)
        breakdown["admin"] = {"points":admin_points,"max":15,"detail":"administrative data partially available"}

    # 5. Official duty participation — separate from general Discord activity.
    duty_points = 0
    try:
        duty = fetch_one("""SELECT COUNT(*) total FROM personnel_activity_credit
                             WHERE personnel_id=%s AND credited=TRUE
                               AND activity_date >= CURRENT_DATE - INTERVAL '30 days'
                               AND UPPER(source) IN ('BATTALION DUTY','OPERATION','TRAINING')""", (pid,)) or {"total":0}
        duty_count = int(duty.get("total") or 0)
        duty_points = 10 if duty_count >= 2 else (5 if duty_count == 1 else 0)
        breakdown["duty"] = {"points":duty_points,"max":10,"detail":f"{duty_count} credited official duty period(s) in last 30 days"}
    except Exception:
        log.exception("Readiness duty score failed for %s", pid)
        breakdown["duty"] = {"points":0,"max":10,"detail":"official duty data unavailable"}

    total = sum(v["points"] for v in breakdown.values())
    total = max(0, min(100, int(total)))
    status = "READY" if total >= 80 else ("LIMITED" if total >= 60 else "NOT READY")
    return total, status, breakdown


def sync_readiness(person):
    """Recalculate and persist readiness so 201 Files, promotion boards and rosters agree."""
    score, status, breakdown = readiness_score(person)
    if int(person.get("readiness_percent") or -1) != score or str(person.get("readiness_status") or "") != status:
        execute("UPDATE personnel SET readiness_percent=%s,readiness_status=%s,updated_at=NOW() WHERE id=%s", (score,status,person["id"]))
        person["readiness_percent"] = score
        person["readiness_status"] = status
    return score, status, breakdown

def soldier_readiness(person):
    """Compute a restrained staff readiness classification from existing records."""
    duty = str(person.get("duty_status") or "Present for Duty").upper()
    if duty == "LEAVE":
        activity, inactive_days = "AUTHORIZED ABSENCE", 0
    else:
        activity, inactive_days = activity_classification(person)
    weapon = fetch_one(
        """SELECT wi.* FROM weapon_issue_history wih
           JOIN weapon_inventory wi ON wi.id=wih.weapon_id
           WHERE wih.personnel_id=%s AND wih.is_current=TRUE LIMIT 1""",
        (person["id"],),
    )
    deficiencies = []
    if activity in {"DEFICIENT","INACTIVE","COMMAND REVIEW"}:
        deficiencies.append(("PERSONNEL", activity))
    if weapon:
        state = str(weapon.get("condition_state") or "SERVICEABLE").upper()
        pct = int(weapon.get("condition_percent") or 100)
        if state not in {"CLEAN","SERVICEABLE"} or pct < 70:
            deficiencies.append(("ARMS", state))
    else:
        # A replacement may legitimately be awaiting issue.
        if str(person.get("field_status") or "").upper() not in {"REPLACEMENT","UNASSIGNED"}:
            deficiencies.append(("ARMS", "NO INDIVIDUAL WEAPON ON RECORD"))

    if duty in {"HOSPITAL","WIA","AWOL","INACTIVE"}:
        deficiencies.append(("PERSONNEL", duty))
    try:
        _entry = replacement_training_status(person)
        if _entry.get("replacement_required") and not _entry.get("complete") and not person.get("separated_at"):
            deficiencies.append(("TRAINING","REPLACEMENT TRAINING INCOMPLETE"))
        progress=personnel_progress(person["id"])
        if progress.get("promotion_hold"):
            deficiencies.append(("ADMIN",progress.get("promotion_hold_reason") or "PROMOTION / ADMINISTRATIVE HOLD"))
        expired=fetch_one("SELECT COUNT(*) total FROM qualifications WHERE personnel_id=%s AND expires_at<CURRENT_DATE",(person["id"],)) or {"total":0}
        if int(expired.get("total") or 0)>0:
            deficiencies.append(("TRAINING",f"{expired['total']} EXPIRED QUALIFICATION(S)"))
    except Exception:
        log.exception("Readiness supplemental check failed for %s",person.get("id"))

    if any(x[1] in {"ADMINISTRATIVE REVIEW","UNSERVICEABLE","AWOL"} for x in deficiencies):
        overall = "NOT COMBAT EFFECTIVE"
    elif duty in {"HOSPITAL","WIA"}:
        overall = "LIMITED"
    elif deficiencies:
        overall = "COMBAT EFFECTIVE — DEFICIENCIES NOTED"
    else:
        overall = "COMBAT EFFECTIVE"
    score, score_status, score_breakdown = sync_readiness(person)
    return {"overall": overall, "activity": activity, "inactive_days": inactive_days,
            "weapon": weapon, "deficiencies": deficiencies, "percent": score,
            "score_status": score_status, "breakdown": score_breakdown}


def readiness_summary_for_unit(node_id=None):
    if node_id:
        ids = unit_descendant_ids(node_id)
        people = fetch_all("SELECT * FROM personnel WHERE unit_node_id = ANY(%s)", (ids,))
    else:
        people = fetch_all("SELECT * FROM personnel")
    summary = {"assigned":len(people),"present":0,"combat_effective":0,"limited":0,
               "inactive":0,"wia":0,"hospital":0,"leave":0,"replacements":0,
               "weapon_deficiencies":0,"personnel_deficiencies":0}
    detail = []
    for p in people:
        r = soldier_readiness(p)
        status = str(p.get("duty_status") or "Present for Duty").upper()
        if status in {"PRESENT FOR DUTY","FIELD DUTY","TRAINING","ATTACHED","TEMPORARY DUTY"}:
            summary["present"] += 1
        if status == "WIA": summary["wia"] += 1
        if status == "HOSPITAL": summary["hospital"] += 1
        if status == "LEAVE": summary["leave"] += 1
        if str(p.get("field_status") or "").upper() in {"REPLACEMENT","UNASSIGNED"}:
            summary["replacements"] += 1
        if r["activity"] in {"INACTIVE","ADMINISTRATIVE REVIEW"}:
            summary["inactive"] += 1
        if r["overall"].startswith("COMBAT EFFECTIVE"):
            summary["combat_effective"] += 1
        elif r["overall"] == "LIMITED":
            summary["limited"] += 1
        summary["weapon_deficiencies"] += sum(1 for c,_ in r["deficiencies"] if c=="ARMS")
        summary["personnel_deficiencies"] += sum(1 for c,_ in r["deficiencies"] if c=="PERSONNEL")
        detail.append((p,r))
    return summary, detail


def vacancy_report():
    """Detect key command vacancies from Phase 4 appointments + Phase 5 organization."""
    vacancies = []
    battalion_checks = [("BN_CO","Battalion Commander"),("BN_XO","Battalion Executive Officer"),("BN_SGM","Battalion Sergeant Major")]
    for code,title in battalion_checks:
        if not appointment_for_node(code):
            vacancies.append({"organization":"1ST BATTALION, 5TH CAVALRY","position":title,"severity":"CRITICAL" if code=="BN_CO" else "NOTICE"})
    companies = fetch_all("SELECT * FROM unit_nodes WHERE unit_type='Company' AND is_active=TRUE ORDER BY sort_order")
    for co in companies:
        if str(co["unit_code"]).startswith(("A-","B-","C-")):
            for code,title in [("CO_CO","Company Commander"),("CO_1SG","First Sergeant")]:
                if not appointment_for_node(code,co["id"]):
                    vacancies.append({"organization":co["display_name"],"position":title,"severity":"CRITICAL"})
            platoons = fetch_all("SELECT * FROM unit_nodes WHERE parent_id=%s AND unit_type='Platoon' AND is_active=TRUE ORDER BY sort_order",(co["id"],))
            for pl in platoons:
                for code,title in [("PL","Platoon Leader"),("PSG","Platoon Sergeant")]:
                    if not appointment_for_node(code,pl["id"]):
                        vacancies.append({"organization":f"{co['display_name']} / {pl['display_name']}","position":title,"severity":"NOTICE"})
                squads = fetch_all("SELECT * FROM unit_nodes WHERE parent_id=%s AND unit_type='Squad' AND is_active=TRUE ORDER BY sort_order",(pl["id"],))
                for sq in squads:
                    if not appointment_for_node("SL",sq["id"]):
                        vacancies.append({"organization":f"{co['display_name']} / {pl['display_name']} / {sq['display_name']}","position":"Squad Leader","severity":"NOTICE"})
    return vacancies


def deros_forecast(days=90):
    return fetch_all(
        """SELECT p.*, (p.deros_date - CURRENT_DATE) AS days_remaining
           FROM personnel p
           WHERE p.deros_date IS NOT NULL
             AND p.deros_date BETWEEN CURRENT_DATE AND CURRENT_DATE + %s
           ORDER BY p.deros_date""",
        (days,),
    )


def morning_report_data():
    companies = fetch_all("SELECT * FROM unit_nodes WHERE unit_type='Company' AND is_active=TRUE ORDER BY sort_order")
    company_rows = []
    for co in companies:
        summary,_ = readiness_summary_for_unit(co["id"])
        company_rows.append({"unit":co, "summary":summary})
    total,_ = readiness_summary_for_unit(None)
    return company_rows,total


def save_morning_report_snapshot(prepared_by=None):
    company_rows,total = morning_report_data()
    forecast = deros_forecast(30)
    readiness_row=fetch_one("""SELECT ROUND(COALESCE(AVG(readiness_percent),0))::int AS pct
                               FROM personnel WHERE archived=FALSE AND separated_at IS NULL""") or {"pct":0}
    weapon_row=fetch_one("""SELECT COUNT(*) FILTER(WHERE UPPER(COALESCE(wi.status,'SERVICEABLE'))<>'MAINTENANCE') AS good,
                                   COUNT(*) AS total
                            FROM weapon_issue_history wih JOIN weapon_inventory wi ON wi.id=wih.weapon_id
                            WHERE wih.is_current=TRUE""") or {"good":0,"total":0}
    qual_row=fetch_one("""SELECT COUNT(DISTINCT p.id) FILTER(WHERE q.personnel_id IS NOT NULL OR dq.personnel_id IS NOT NULL) AS current,
                                 COUNT(DISTINCT p.id) AS total
                          FROM personnel p
                          LEFT JOIN qualifications q ON q.personnel_id=p.id AND UPPER(q.status)='CURRENT' AND (q.expires_at IS NULL OR q.expires_at>=CURRENT_DATE)
                          LEFT JOIN personnel_duty_qualifications dq ON dq.personnel_id=p.id AND UPPER(dq.status)<>'EXPIRED' AND (dq.expiration_date IS NULL OR dq.expiration_date>=CURRENT_DATE)
                          WHERE p.archived=FALSE AND p.separated_at IS NULL""") or {"current":0,"total":0}
    last_op=fetch_one("SELECT id FROM operations WHERE UPPER(COALESCE(status,'')) IN ('CLOSED','COMPLETE','COMPLETED') ORDER BY COALESCE(start_at,created_at) DESC LIMIT 1")
    attendance_pct=0
    if last_op:
        a=fetch_one("""SELECT COUNT(*) FILTER(WHERE UPPER(COALESCE(attendance_status,'')) IN ('FULL CREDIT','CREDIT','PRESENT')) AS credited,
                              COUNT(*) AS total FROM operation_participation WHERE operation_id=%s""",(last_op['id'],)) or {"credited":0,"total":0}
        attendance_pct=round(100*int(a.get('credited') or 0)/max(1,int(a.get('total') or 0)))
    attention=int((fetch_one("SELECT COUNT(*) total FROM personnel_actions WHERE status NOT IN ('COMPLETE','CLOSED','DENIED') AND (priority IN ('URGENT','CRITICAL','HIGH') OR due_date<=CURRENT_DATE)") or {"total":0})['total'] or 0)
    weapon_pct=round(100*int(weapon_row.get('good') or 0)/max(1,int(weapon_row.get('total') or 0))) if int(weapon_row.get('total') or 0) else 100
    training_pct=round(100*int(qual_row.get('current') or 0)/max(1,int(qual_row.get('total') or 0))) if int(qual_row.get('total') or 0) else 0
    payload = {"companies":[{"unit_code":x["unit"]["unit_code"],"display_name":x["unit"]["display_name"],"summary":x["summary"]} for x in company_rows],"vacancies":vacancy_report()}
    existing=fetch_one("SELECT report_number FROM morning_report_snapshots WHERE report_date=CURRENT_DATE")
    report_number=(existing or {}).get('report_number') or _order_number('MORNING REPORT')
    execute("""INSERT INTO morning_report_snapshots
       (report_date,report_number,prepared_by,battalion_assigned,battalion_present,battalion_combat_effective,
        battalion_inactive,battalion_wia,battalion_leave,battalion_hospital,battalion_replacements,battalion_deros_30,
        readiness_percent,weapon_readiness_percent,training_current_percent,operation_attendance_percent,command_attention_count,data_json)
       VALUES(CURRENT_DATE,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
       ON CONFLICT(report_date) DO UPDATE SET prepared_by=EXCLUDED.prepared_by,battalion_assigned=EXCLUDED.battalion_assigned,
        battalion_present=EXCLUDED.battalion_present,battalion_combat_effective=EXCLUDED.battalion_combat_effective,
        battalion_inactive=EXCLUDED.battalion_inactive,battalion_wia=EXCLUDED.battalion_wia,battalion_leave=EXCLUDED.battalion_leave,
        battalion_hospital=EXCLUDED.battalion_hospital,battalion_replacements=EXCLUDED.battalion_replacements,battalion_deros_30=EXCLUDED.battalion_deros_30,
        readiness_percent=EXCLUDED.readiness_percent,weapon_readiness_percent=EXCLUDED.weapon_readiness_percent,
        training_current_percent=EXCLUDED.training_current_percent,operation_attendance_percent=EXCLUDED.operation_attendance_percent,
        command_attention_count=EXCLUDED.command_attention_count,data_json=EXCLUDED.data_json""",
        (report_number,prepared_by,total['assigned'],total['present'],total['combat_effective'],total['inactive'],total['wia'],total['leave'],total['hospital'],total['replacements'],len(forecast),
         int(readiness_row.get('pct') or 0),weapon_pct,training_pct,attendance_pct,attention,Json(payload)))
    emit_state_event('MORNING_REPORT_FILED',effective_date=date.today(),title=f'{report_number} — Morning Report',reference_number=report_number,
                     source_key=f'MR:{date.today().isoformat()}',details={'assigned':total['assigned'],'ready':int(readiness_row.get('pct') or 0)})


def weapon_condition_from_rounds_and_time(weapon, person=None):
    """Derive M16 condition from firing fouling plus unattended issued-weapon neglect.

    Rounds remain the primary fouling source. While a rifle remains issued, prolonged
    Soldier inactivity also adds a smaller environmental/neglect fouling load. A fresh
    cleaning or recent issue resets that inactivity clock so a rifle does not instantly
    become dirty again after S-4 services it.
    """
    since_clean=max(0,int(weapon.get("rounds_since_cleaning") or 0))
    neglect_days=0
    if person and not authorized_absence_active(person):
        stamps=[]
        for value in (person.get("activity_last_duty_at"),person.get("activity_last_seen_at"),weapon.get("last_cleaned_at"),weapon.get("_issued_at")):
            if not value:
                continue
            if isinstance(value,date) and not isinstance(value,datetime):
                value=datetime.combine(value,time.min,tzinfo=timezone.utc)
            elif getattr(value,"tzinfo",None) is None:
                value=value.replace(tzinfo=timezone.utc)
            stamps.append(value)
        if stamps:
            unattended_days=max(0,(datetime.now(timezone.utc)-max(stamps)).days)
            warning=inactivity_thresholds_for_person(person)["warning"]
            neglect_days=max(0,unattended_days-warning)

    # About 15 equivalent fouling rounds per unattended day after the inactivity warning.
    # This reaches visible FOULED status around the normal S-1 inactivity stage without
    # overwhelming real ammunition expenditure.
    neglect_equivalent=min(450,neglect_days*15)
    effective_fouling=since_clean+neglect_equivalent
    fouling_penalty=min(70,(since_clean//10)+(neglect_equivalent//10))
    score=max(0,100-fouling_penalty)
    if str(weapon.get("status") or "").upper()=="MAINTENANCE":
        score=min(score,30)
    if score<=15: state="UNSERVICEABLE"
    elif score<=30: state="MAINTENANCE REQUIRED"
    elif effective_fouling>=450: state="CLEANING REQUIRED"
    elif effective_fouling>=250: state="HEAVY FOULING"
    elif effective_fouling>=100: state="FOULED"
    else: state="SERVICEABLE"
    return state,score

def refresh_weapon_condition(weapon_id):
    weapon = fetch_one("SELECT * FROM weapon_inventory WHERE id=%s", (weapon_id,))
    if not weapon:
        return None
    issue = fetch_one("SELECT personnel_id,issued_at FROM weapon_issue_history WHERE weapon_id=%s AND is_current=TRUE", (weapon_id,))
    person = fetch_one("SELECT * FROM personnel WHERE id=%s", (issue["personnel_id"],)) if issue and issue.get("personnel_id") else None
    if issue and issue.get("issued_at"):
        weapon["_issued_at"]=issue["issued_at"]
    state, pct = weapon_condition_from_rounds_and_time(weapon, person)
    execute("UPDATE weapon_inventory SET condition_state=%s,condition_percent=%s,updated_at=NOW() WHERE id=%s",
            (state,pct,weapon_id))
    weapon["condition_state"], weapon["condition_percent"] = state, pct
    return weapon


def current_equipment_for(personnel_id):
    return fetch_all(
        """SELECT ei.*,sic.item_name,sic.category,sic.stock_number,eih.issued_at,eih.condition_at_issue
           FROM equipment_issue_history eih
           JOIN equipment_inventory ei ON ei.id=eih.equipment_id
           JOIN supply_item_catalog sic ON sic.item_code=ei.item_code
           WHERE eih.personnel_id=%s AND eih.is_current=TRUE
           ORDER BY sic.sort_order,sic.item_name""",
        (personnel_id,),
    )


def next_supply_request_number():
    seq = fetch_one("SELECT COUNT(*)+1 AS n FROM supply_requisitions WHERE submitted_at::date=CURRENT_DATE")
    return f"REQ-{date.today().strftime('%y%m%d')}-{int(seq['n'] or 1):03d}"


def issue_equipment_to_soldier(personnel_id,item_code,authority=None,remarks=None):
    item = fetch_one("SELECT * FROM supply_item_catalog WHERE item_code=%s AND is_active=TRUE",(item_code,))
    if not item:
        raise ValueError("Supply item not found")
    inventory = fetch_one("""SELECT * FROM equipment_inventory
                             WHERE item_code=%s AND status='AVAILABLE'
                             ORDER BY created_at,id LIMIT 1""",(item_code,))
    if not inventory:
        execute("""INSERT INTO equipment_inventory(item_code,status,condition_state,condition_percent)
                   VALUES(%s,'AVAILABLE',%s,100)""",(item_code,item["default_condition"]))
        inventory = fetch_one("""SELECT * FROM equipment_inventory
                                 WHERE item_code=%s AND status='AVAILABLE'
                                 ORDER BY created_at DESC LIMIT 1""",(item_code,))
    execute("""INSERT INTO equipment_issue_history
               (equipment_id,personnel_id,unit_node_id,condition_at_issue,issue_authority,remarks)
               SELECT %s,id,unit_node_id,%s,%s,%s FROM personnel WHERE id=%s""",
            (inventory["id"],inventory["condition_state"],authority,remarks,personnel_id))
    execute("UPDATE equipment_inventory SET status='ISSUED',updated_at=NOW() WHERE id=%s",(inventory["id"],))
    write_service_entry(personnel_id,"SUPPLY","EQUIPMENT ISSUED",
                        f"{item['item_name']} issued from S-4 supply.",authority,None,date.today())
    return inventory




def ensure_standard_uniform(personnel_id, authority="S-4 SUPPLY"):
    """Ensure every active Soldier has one standard Army Green service uniform on issue."""
    current = fetch_one(
        """SELECT eih.id,ei.id AS equipment_id,ei.item_code,ei.condition_state,eih.issued_at
           FROM equipment_issue_history eih
           JOIN equipment_inventory ei ON ei.id=eih.equipment_id
           WHERE eih.personnel_id=%s AND eih.is_current=TRUE AND ei.item_code='AG44'
           LIMIT 1""",
        (personnel_id,),
    )
    if current:
        return current
    try:
        issue_equipment_to_soldier(
            personnel_id,
            "AG44",
            authority,
            "Standard service uniform issue upon entry on battalion rolls.",
        )
    except ValueError:
        return None
    return fetch_one(
        """SELECT eih.id,ei.id AS equipment_id,ei.item_code,ei.condition_state,eih.issued_at
           FROM equipment_issue_history eih
           JOIN equipment_inventory ei ON ei.id=eih.equipment_id
           WHERE eih.personnel_id=%s AND eih.is_current=TRUE AND ei.item_code='AG44'
           LIMIT 1""",
        (personnel_id,),
    )


def turn_in_equipment(issue_id,authority=None,condition=None,remarks=None):
    row = fetch_one("""SELECT eih.*,ei.item_code,sic.item_name
                       FROM equipment_issue_history eih
                       JOIN equipment_inventory ei ON ei.id=eih.equipment_id
                       JOIN supply_item_catalog sic ON sic.item_code=ei.item_code
                       WHERE eih.id=%s AND eih.is_current=TRUE""",(issue_id,))
    if not row:
        raise ValueError("Current equipment issue not found")
    returned_condition = condition or "INSPECTION REQUIRED"
    execute("""UPDATE equipment_issue_history SET is_current=FALSE,returned_at=NOW(),
               condition_at_return=%s,turn_in_authority=%s,remarks=COALESCE(%s,remarks)
               WHERE id=%s""",(returned_condition,authority,remarks,issue_id))
    execute("""UPDATE equipment_inventory SET status='AVAILABLE',condition_state=%s,
               updated_at=NOW() WHERE id=%s""",(returned_condition,row["equipment_id"]))
    if row.get("personnel_id"):
        write_service_entry(row["personnel_id"],"SUPPLY","EQUIPMENT TURNED IN",
                            f"{row['item_name']} returned to S-4 supply; condition: {returned_condition}.",
                            authority,None,date.today())


def weapon_maintenance_action(weapon_id,action_type,personnel_id=None,performed_by=None,remarks=None):
    weapon = fetch_one("SELECT * FROM weapon_inventory WHERE id=%s",(weapon_id,))
    if not weapon:
        raise ValueError("Weapon not found")
    before = weapon.get("condition_state")
    action = action_type.upper()
    if action == "CLEANED":
        execute("""UPDATE weapon_inventory SET rounds_since_cleaning=0,last_cleaned_at=NOW(),
                   condition_percent=100,condition_state='SERVICEABLE',updated_at=NOW() WHERE id=%s""",
                (weapon_id,))
        refreshed = refresh_weapon_condition(weapon_id) or {}
        new_state = refreshed.get("condition_state") or "SERVICEABLE"
        new_pct = int(refreshed.get("condition_percent") or 100)
    elif action == "INSPECTED":
        new_state,new_pct = weapon_condition_from_rounds_and_time({**weapon,"condition_percent":max(int(weapon.get("condition_percent") or 0),85)}, None)
        if new_state in {"FIELD WORN","FOULED"}:
            new_state="SERVICEABLE"
            new_pct=max(new_pct,85)
        execute("""UPDATE weapon_inventory SET last_inspected_at=NOW(),condition_percent=%s,
                   condition_state=%s,updated_at=NOW() WHERE id=%s""",(new_pct,new_state,weapon_id))
    elif action == "MAINTENANCE COMPLETED":
        new_state,new_pct="SERVICEABLE",100
        execute("""UPDATE weapon_inventory SET condition_state=%s,condition_percent=%s,
                   last_inspected_at=NOW(),last_cleaned_at=NOW(),rounds_since_cleaning=0,
                   updated_at=NOW() WHERE id=%s""",(new_state,new_pct,weapon_id))
    elif action == "PLACED IN MAINTENANCE":
        new_state,new_pct="MAINTENANCE REQUIRED",min(int(weapon.get("condition_percent") or 100),30)
        execute("""UPDATE weapon_inventory SET condition_state=%s,condition_percent=%s,
                   status='MAINTENANCE',updated_at=NOW() WHERE id=%s""",(new_state,new_pct,weapon_id))
    else:
        raise ValueError("Unsupported maintenance action")

    execute("""INSERT INTO weapon_maintenance_log
               (weapon_id,personnel_id,action_type,condition_before,condition_after,
                rounds_at_action,performed_by,remarks)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
            (weapon_id,personnel_id,action,before,new_state,int(weapon.get("total_rounds") or 0),performed_by,remarks))
    if personnel_id:
        write_service_entry(personnel_id,"ARMS",action,
                            f"M16 serial {weapon.get('serial_number')} — {action.lower()}.",
                            performed_by,None,date.today())


def record_weapon_rounds(weapon_id,rounds,personnel_id=None,operation_id=None,source_type="MANUAL ENTRY",recorded_by=None,remarks=None):
    rounds=max(0,int(rounds))
    execute("""INSERT INTO weapon_round_events
               (weapon_id,personnel_id,operation_id,rounds_fired,source_type,recorded_by,remarks)
               VALUES(%s,%s,%s,%s,%s,%s,%s)""",
            (weapon_id,personnel_id,operation_id,rounds,source_type,recorded_by,remarks))
    execute("""UPDATE weapon_inventory SET total_rounds=COALESCE(total_rounds,0)+%s,
               rounds_since_cleaning=COALESCE(rounds_since_cleaning,0)+%s,
               last_fired_at=NOW(),updated_at=NOW() WHERE id=%s""",(rounds,rounds,weapon_id))
    refresh_weapon_condition(weapon_id)
    if personnel_id:
        op=operation_record(operation_id) if operation_id else None
        emit_state_event('WEAPON_ROUNDS_FIRED',personnel_id=personnel_id,operation_id=operation_id,weapon_id=weapon_id,
                         effective_date=date.today(),title='M16 AMMUNITION EXPENDITURE',
                         narrative=f"{rounds} rounds recorded" + (f" for {op.get('operation_number') or op.get('title')}" if op else '') + '.',
                         source_key=f"ROUND:{weapon_id}:{personnel_id}:{operation_id}:{source_type}:{rounds}:{datetime.now(timezone.utc).isoformat()}",
                         details={'rounds':rounds,'source_type':source_type,'recorded_by':recorded_by})


def operation_weapon_rounds_applied(operation_id, personnel_id, weapon_id=None):
    params=[operation_id,personnel_id]
    weapon_filter=""
    if weapon_id:
        weapon_filter=" AND weapon_id=%s"
        params.append(weapon_id)
    row=fetch_one("""SELECT COALESCE(SUM(rounds_fired),0) AS total
                     FROM weapon_round_events
                     WHERE operation_id=%s AND personnel_id=%s""" + weapon_filter,tuple(params))
    return int((row or {}).get("total") or 0)


def reconcile_operation_weapon_rounds(operation_id, personnel_id, expected_rounds,
                                      recorded_by="BATTALION CLERK", remarks=None):
    expected=max(0,int(expected_rounds or 0))
    weapon=fetch_one("""SELECT wi.id,wi.serial_number FROM weapon_issue_history wih
                        JOIN weapon_inventory wi ON wi.id=wih.weapon_id
                        WHERE wih.personnel_id=%s AND wih.is_current=TRUE
                        ORDER BY wih.issued_at DESC LIMIT 1""",(personnel_id,))
    if not weapon or expected<=0:
        return 0
    applied=operation_weapon_rounds_applied(operation_id,personnel_id,weapon["id"])
    delta=max(0,expected-applied)
    if delta:
        record_weapon_rounds(weapon["id"],delta,personnel_id,operation_id,
                             "OPERATION RECORD",recorded_by,
                             remarks or f"Automatic operation ammunition reconciliation; {delta} previously unapplied rounds filed.")
    return delta



def operation_round_target_for_time(event, qualifying_seconds):
    """Rounds that should exist in the weapon ledger for verified time already served.

    S-3's rounds_per_soldier is treated as the expected expenditure for the full
    scheduled operation. Battalion Clerk attendance chunks accrue toward it linearly.
    """
    total_rounds=max(0,int((event or {}).get("rounds_per_soldier") or 0))
    if total_rounds<=0:
        return 0
    start=(event or {}).get("starts_at"); end=(event or {}).get("ends_at")
    if start and end and end>start:
        duration=max(300,int((end-start).total_seconds()))
    else:
        duration=max(300,int((event or {}).get("credit_threshold_minutes") or 45)*60)
    served=max(0,min(int(qualifying_seconds or 0),duration))
    return min(total_rounds,int(round(total_rounds*(served/duration))))


def accrue_live_operation_weapon_rounds(event, personnel_id, qualifying_seconds, authority="BATTALION CLERK"):
    """Idempotently apply the time-proportional M16 expenditure during a live Operation."""
    if str((event or {}).get("event_type") or "").upper()!="OPERATION" or not (event or {}).get("operation_id"):
        return {"target":0,"applied":0}
    target=operation_round_target_for_time(event,qualifying_seconds)
    if target<=0:
        return {"target":0,"applied":0}
    applied=reconcile_operation_weapon_rounds(
        event["operation_id"],personnel_id,target,authority,
        f"Live operation ammunition accrual at {int(qualifying_seconds or 0)//60} verified minutes."
    )
    # If official participation already exists, keep its displayed expenditure in sync
    # with the actual weapon ledger as additional verified minutes accrue.
    execute("""UPDATE operation_participation SET rounds_expended=GREATEST(COALESCE(rounds_expended,0),%s)
               WHERE operation_id=%s AND personnel_id=%s""",
            (target,event["operation_id"],personnel_id))
    return {"target":target,"applied":applied}


def company_supply_readiness(unit_node_id):
    rows=fetch_all("""SELECT css.*,sic.item_name,sic.category,sic.default_unit
                      FROM company_supply_stock css
                      JOIN supply_item_catalog sic ON sic.item_code=css.item_code
                      WHERE css.unit_node_id=%s ORDER BY sic.category,sic.sort_order""",(unit_node_id,))
    for r in rows:
        q=int(r.get("quantity_on_hand") or 0); low=int(r.get("reorder_level") or 0)
        if q<=0: state="CRITICAL"
        elif low and q<=low: state="LOW"
        else: state="ADEQUATE"
        r["readiness_state"]=state
    return rows


def next_operation_number():
    """Issue the next standardized OP-YY-#### reference from the shared document sequence."""
    return _order_number("OPERATION")


def operation_expected_roster(op):
    """Return the formation expected to participate in an operation."""
    if not op:
        return []
    unit_id=op.get("formation_unit_node_id")
    if unit_id:
        # Include selected unit and immediate descendants by unit-code prefix fallback.
        unit=fetch_one("SELECT * FROM unit_nodes WHERE id=%s",(unit_id,))
        if unit:
            code=unit.get("unit_code") or ""
            return fetch_all("""SELECT * FROM personnel WHERE separated_at IS NULL AND archived=FALSE
                              AND (unit_node_id=%s OR unit_code=%s OR unit_code LIKE %s)
                              ORDER BY unit_code,platoon,squad,last_name,first_name""",
                             (unit_id,code,f"%{code}%"))
    return fetch_all("""SELECT * FROM personnel WHERE separated_at IS NULL AND archived=FALSE
                      ORDER BY unit_code,platoon,squad,last_name,first_name""")


def operation_live_event(operation_id):
    return fetch_one("""SELECT e.*,COUNT(a.id)::int AS tracked_count,
                      COALESCE(SUM(CASE WHEN a.credited_at IS NOT NULL THEN 1 ELSE 0 END),0)::int AS credited_count
                      FROM battalion_events e LEFT JOIN battalion_event_attendance a ON a.event_id=e.id
                      WHERE e.operation_id=%s GROUP BY e.id ORDER BY e.created_at DESC LIMIT 1""",(operation_id,))


def operation_live_attendance(operation_id):
    event=operation_live_event(operation_id)
    if not event:
        return event,[]
    rows=fetch_all("""SELECT a.*,p.rank_code,p.first_name,p.last_name,p.unit_code,p.platoon,p.squad,p.duty_position
                      FROM battalion_event_attendance a JOIN personnel p ON p.id=a.personnel_id
                      WHERE a.event_id=%s ORDER BY a.qualifying_seconds DESC,p.last_name,p.first_name""",(event["id"],))
    threshold=int(event.get("credit_threshold_minutes") or 45)
    for row in rows:
        mins=int(row.get("qualifying_seconds") or 0)//60
        row["minutes"]=mins
        row["minutes_remaining"]=max(0,threshold-mins)
        row["credit_state"]="FULL CREDIT" if row.get("credited_at") else ("EARNING CREDIT" if mins>0 else "NO CREDIT")
    return event,rows


def clerk_health_snapshot():
    row=fetch_one("SELECT * FROM clerk_runtime_health ORDER BY last_seen_at DESC LIMIT 1")
    if not row:
        return {"state":"UNKNOWN","last_seen_at":None,"voice_collector_running":False}
    age=(datetime.now(timezone.utc)-row["last_seen_at"]).total_seconds() if row.get("last_seen_at") else 999999
    row["state"]="CONNECTED" if age<=150 else "STALE"
    return row


def schedule_operation_event(op, authority=None):
    """Website-first schedule: create/update the shared Clerk event and credit-channel binding."""
    if not op or not op.get("start_at"):
        raise ValueError("Operation date/time is required before publishing.")
    duration=max(45,int(op.get("duration_minutes") or 90))
    threshold=max(5,min(duration,int(op.get("credit_threshold_minutes") or 45)))
    rounds=max(0,int(op.get("rounds_per_soldier") or 0))
    channel_id=op.get("credit_channel_id")
    channel_name=op.get("credit_channel_name") or "Operation"
    ends=op["start_at"]+timedelta(minutes=duration)
    external=f"website-operation:{op['id']}"
    event=fetch_one("""INSERT INTO battalion_events
        (external_event_id,event_type,title,starts_at,ends_at,channel_name,channel_id,operation_id,
         rounds_per_soldier,credit_threshold_minutes,reminder_minutes,status)
        VALUES(%s,'OPERATION',%s,%s,%s,%s,%s,%s,%s,%s,%s,'SCHEDULED')
        ON CONFLICT(external_event_id) DO UPDATE SET title=EXCLUDED.title,starts_at=EXCLUDED.starts_at,
          ends_at=EXCLUDED.ends_at,channel_name=EXCLUDED.channel_name,channel_id=EXCLUDED.channel_id,
          operation_id=EXCLUDED.operation_id,rounds_per_soldier=EXCLUDED.rounds_per_soldier,
          credit_threshold_minutes=EXCLUDED.credit_threshold_minutes,reminder_minutes=EXCLUDED.reminder_minutes,
          status='SCHEDULED' RETURNING *""",
        (external,op.get("title") or "Operation",op["start_at"],ends,channel_name,channel_id,op["id"],rounds,threshold,op.get("reminder_minutes") or "1440,120,30"))
    # Website selection becomes the active OPERATION duty binding. Battalion Clerk reloads it every minute.
    if channel_id:
        guild=(fetch_one("SELECT guild_id FROM clerk_guild_settings ORDER BY updated_at DESC LIMIT 1") or {}).get("guild_id")
        if not guild:
            guild=(fetch_one("SELECT guild_id FROM website_member_links WHERE guild_id IS NOT NULL LIMIT 1") or {}).get("guild_id")
        if guild:
            execute("""INSERT INTO clerk_duty_channels(guild_id,event_type,channel_id,channel_name)
                       VALUES(%s,'OPERATION',%s,%s)
                       ON CONFLICT(guild_id,event_type) DO UPDATE SET channel_id=EXCLUDED.channel_id,
                       channel_name=EXCLUDED.channel_name,updated_at=NOW()""",(guild,channel_id,channel_name))
    execute("""UPDATE operations SET status='SCHEDULED',lifecycle_status='PUBLISHED',publish_status='PUBLISHED',
              published_at=COALESCE(published_at,NOW()),clerk_event_id=%s,updated_at=NOW() WHERE id=%s""",(event["id"],op["id"]))
    # Immediate read-back guards the public schedule contract. If this row is future/active,
    # home() will read this same authoritative operations record without waiting for Clerk.
    visible=fetch_one("""SELECT id FROM operations WHERE id=%s AND start_at IS NOT NULL
                         AND UPPER(COALESCE(status,'')) IN ('SCHEDULED','ACTIVE')
                         AND (start_at + make_interval(mins => COALESCE(duration_minutes,90)))>NOW()""",(op['id'],))
    if not visible:
        log.warning('SCHEDULE CONTRACT: operation %s published but not currently homepage-visible',op['id'])
    return event


def operation_record(operation_id):
    return fetch_one("SELECT * FROM operations WHERE id=%s",(operation_id,))


def operation_participants(operation_id):
    return fetch_all(
        """SELECT op.*,p.rank_code,p.first_name,p.last_name,p.unit_code,p.platoon,p.squad,
                  p.duty_position,un.display_name AS assigned_unit
           FROM operation_participation op
           JOIN personnel p ON p.id=op.personnel_id
           LEFT JOIN unit_nodes un ON un.id=op.unit_node_id
           WHERE op.operation_id=%s
           ORDER BY p.unit_code,p.platoon NULLS FIRST,p.squad NULLS FIRST,p.last_name""",
        (operation_id,),
    )


def operation_units_for(operation_id):
    return fetch_all(
        """SELECT ou.*,un.display_name,un.unit_code
           FROM operation_units ou
           JOIN unit_nodes un ON un.id=ou.unit_node_id
           WHERE ou.operation_id=%s
           ORDER BY ou.is_primary DESC,un.sort_order,un.display_name""",
        (operation_id,),
    )


def file_operation_participation(operation_id, personnel_id, duty_role=None,
                                 attendance_status="PARTICIPATED", rounds_expended=0,
                                 casualty_status=None, remarks=None, credited_by=None):
    p = fetch_one("SELECT * FROM personnel WHERE id=%s",(personnel_id,))
    if not p:
        raise ValueError("Personnel record not found")
    execute(
        """INSERT INTO operation_participation
           (operation_id,personnel_id,unit_node_id,duty_role,attendance_status,
            rounds_expended,casualty_status,remarks,credited_by)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT(operation_id,personnel_id) DO UPDATE SET
             unit_node_id=EXCLUDED.unit_node_id,duty_role=EXCLUDED.duty_role,
             attendance_status=EXCLUDED.attendance_status,rounds_expended=EXCLUDED.rounds_expended,
             casualty_status=EXCLUDED.casualty_status,remarks=EXCLUDED.remarks,
             credited_by=EXCLUDED.credited_by,credited_at=NOW()""",
        (operation_id,personnel_id,p.get("unit_node_id"),duty_role or p.get("duty_position"),
         attendance_status,max(0,int(rounds_expended or 0)),casualty_status,remarks,credited_by),
    )
    op = operation_record(operation_id)
    title = op.get("title") if op else "OPERATION"
    write_service_entry(
        personnel_id,"OPERATIONS","OPERATION PARTICIPATION",
        f"Participated in {title}" + (f" as {duty_role or p.get('duty_position')}" if (duty_role or p.get("duty_position")) else "") + ".",
        credited_by,op.get("operation_number") if op else None,
        (op.get("start_at").date() if op and op.get("start_at") else date.today()),
    )

    # Reconcile with the actual weapon ledger; repeated calls only add missing rounds.
    if int(rounds_expended or 0) > 0:
        reconcile_operation_weapon_rounds(operation_id,personnel_id,int(rounds_expended),
                                          credited_by or "BATTALION CLERK",
                                          f"Round expenditure reconciled for {title}.")

    if casualty_status and casualty_status.upper() in {"WIA","KIA"}:
        execute(
            """INSERT INTO casualty_records
               (personnel_id,operation_id,casualty_type,effective_date,remarks)
               VALUES(%s,%s,%s,%s,%s)""",
            (personnel_id,operation_id,casualty_status.upper(),
             op.get("start_at").date() if op and op.get("start_at") else date.today(),remarks),
        )
        execute("UPDATE personnel SET duty_status=%s,updated_at=NOW() WHERE id=%s",
                (casualty_status.upper(),personnel_id))
        write_service_entry(
            personnel_id,"CASUALTY",casualty_status.upper(),
            f"Recorded {casualty_status.upper()} during {title}.",
            credited_by,op.get("operation_number") if op else None,
            op.get("start_at").date() if op and op.get("start_at") else date.today(),
        )


def operation_total_rounds(operation_id):
    row = fetch_one("SELECT COALESCE(SUM(rounds_expended),0) AS total FROM operation_participation WHERE operation_id=%s",(operation_id,))
    return int(row["total"] or 0) if row else 0


def complete_operation(operation_id, result=None, commander_remarks=None, prepared_by=None):
    op = operation_record(operation_id)
    if not op:
        raise ValueError("Operation not found")
    total_rounds = operation_total_rounds(operation_id)
    execute(
        """UPDATE operations SET status='COMPLETED',result=%s,commander_remarks=%s,
           completed_at=NOW() WHERE id=%s""",
        (result,commander_remarks,operation_id),
    )
    execute(
        """INSERT INTO after_action_reports
           (operation_id,objective,result,ammunition_expended,commander_remarks,prepared_by)
           VALUES(%s,%s,%s,%s,%s,%s)
           ON CONFLICT(operation_id) DO UPDATE SET
             result=EXCLUDED.result,ammunition_expended=EXCLUDED.ammunition_expended,
             commander_remarks=EXCLUDED.commander_remarks,prepared_by=EXCLUDED.prepared_by,
             filed_at=NOW()""",
        (operation_id,op.get("mission"),result,total_rounds,commander_remarks,prepared_by),
    )
    execute(
        """INSERT INTO operation_journal_entries(operation_id,title,body,created_by)
           VALUES(%s,%s,%s,%s)""",
        (operation_id,op.get("title") or op.get("operation_number") or "Completed Operation",
         result or commander_remarks,prepared_by),
    )



def member_operation_credit_ledger(personnel_id):
    """Member-facing proof of official operation credit, including Battalion Clerk attendance."""
    return fetch_all("""SELECT op.operation_id,o.operation_number,o.title,o.start_at,o.area_of_operations,o.result,
          op.duty_role,op.attendance_status,op.rounds_expended,op.credited_by,op.credited_at,
          a.qualifying_seconds,a.attendance_percent,a.attendance_grade,a.credited_at AS attendance_credited_at,
          e.id AS battalion_event_id
      FROM operation_participation op
      JOIN operations o ON o.id=op.operation_id
      LEFT JOIN LATERAL (
          SELECT bea.*,be.id AS matched_event_id
          FROM battalion_events be
          JOIN battalion_event_attendance bea ON bea.event_id=be.id AND bea.personnel_id=op.personnel_id
          WHERE be.operation_id=op.operation_id AND UPPER(be.event_type)='OPERATION'
          ORDER BY bea.credited_at DESC NULLS LAST,bea.updated_at DESC LIMIT 1
      ) a ON TRUE
      LEFT JOIN battalion_events e ON e.id=a.matched_event_id
      WHERE op.personnel_id=%s AND UPPER(COALESCE(op.attendance_status,'')) NOT IN ('ABSENT','NO CREDIT','PARTIAL / LATE')
      ORDER BY COALESCE(o.start_at,op.credited_at) DESC""",(personnel_id,))

def personal_operations(personnel_id):
    return fetch_all(
        """SELECT opart.*,o.title,o.operation_number,o.area_of_operations,o.start_at,o.result,o.status
           FROM operation_participation opart
           JOIN operations o ON o.id=opart.operation_id
           WHERE opart.personnel_id=%s
           ORDER BY COALESCE(o.start_at,opart.credited_at) DESC""",
        (personnel_id,),
    )


def personnel_progress(personnel_id):
    execute("INSERT INTO personnel_progress_control(personnel_id) VALUES(%s) ON CONFLICT(personnel_id) DO NOTHING", (personnel_id,))
    return fetch_one("SELECT * FROM personnel_progress_control WHERE personnel_id=%s", (personnel_id,)) or {}


def training_record(personnel_id, program_code):
    return fetch_one(
        """SELECT ptr.*,tpc.program_name,tpc.description
           FROM personnel_training_records ptr
           JOIN training_program_catalog tpc ON tpc.program_code=ptr.program_code
           WHERE ptr.personnel_id=%s AND ptr.program_code=%s""",
        (personnel_id, program_code),
    )


def training_program_complete(personnel_id, program_code):
    row = training_record(personnel_id, program_code)
    return bool(row and row.get("status") == "COMPLETE" and row.get("completed_at"))


def certify_training_program(personnel_id, program_code, authority, remarks=None, completed_at=None):
    program = fetch_one("SELECT * FROM training_program_catalog WHERE program_code=%s AND is_active=TRUE", (program_code,))
    if not program:
        raise ValueError("Training program not found")
    completed = completed_at or date.today()
    execute(
        """INSERT INTO personnel_training_records
           (personnel_id,program_code,status,started_at,completed_at,certified_by,remarks)
           VALUES(%s,%s,'COMPLETE',%s,%s,%s,%s)
           ON CONFLICT(personnel_id,program_code) DO UPDATE SET
             status='COMPLETE',completed_at=EXCLUDED.completed_at,
             certified_by=EXCLUDED.certified_by,remarks=EXCLUDED.remarks,updated_at=NOW()""",
        (personnel_id, program_code, completed, completed, authority, remarks),
    )
    existing = fetch_one(
        "SELECT 1 FROM personnel_service_history WHERE personnel_id=%s AND entry_type='TRAINING' AND title=%s LIMIT 1",
        (personnel_id, f"{program['program_name'].upper()} COMPLETE"),
    )
    if not existing:
        write_service_entry(personnel_id,"TRAINING",f"{program['program_name'].upper()} COMPLETE",f"Completed {program['program_name']}." + (f" {remarks}" if remarks else ""),authority,None,completed)
    create_personnel_order(personnel_id,"TRAINING","TRAINING COMPLETION ORDERS",f"The Soldier named herein has successfully completed {program['program_name']} and the completion is entered in the permanent training record.",effective_date=completed,authority=authority,details={"program_code":program_code,"program_name":program['program_name']},source_key=f"TRAINING:{personnel_id}:{program_code}")
    refreshed=fetch_one("SELECT * FROM personnel WHERE id=%s",(personnel_id,))
    if refreshed:
        sync_mos_proficiency(refreshed)
        sync_readiness(refreshed)
    return training_record(personnel_id, program_code)


def credited_operation_count(personnel_id):
    row = fetch_one(
        """SELECT COUNT(DISTINCT opart.operation_id) AS total
           FROM operation_participation opart
           JOIN operations o ON o.id=opart.operation_id
           WHERE opart.personnel_id=%s
             AND UPPER(COALESCE(opart.attendance_status,'')) IN ('FULL CREDIT','CREDITED','COMPLETE','COMPLETED','PARTICIPATED','PRESENT')
             AND UPPER(COALESCE(o.status,'ACTIVE')) <> 'CANCELLED'""",
        (personnel_id,),
    )
    return int((row or {}).get("total") or 0)


def _service_days(personnel):
    joined = personnel.get("date_joined") or personnel.get("roster_entered_at")
    if isinstance(joined, str):
        try: joined = date.fromisoformat(joined)
        except Exception: joined = None
    if not joined:
        return 0
    end = personnel.get("separated_at")
    if hasattr(end, "date"):
        end = end.date()
    elif isinstance(end, str):
        try: end = date.fromisoformat(end[:10])
        except Exception: end = None
    end = end or date.today()
    return max(0, (end - joined).days + 1)


def ribbon_progress_for(personnel_id, award_completed=True):
    """Return live ribbon progress from authoritative personnel/event records.

    Battalion Clerk only files facts (attendance, instructor assignment, conversion).
    This website function decides eligibility and files automatic ribbons idempotently.
    """
    person = fetch_one("SELECT * FROM personnel WHERE id=%s", (personnel_id,))
    if not person:
        return []
    operations = credited_operation_count(personnel_id)
    service_days = _service_days(person)
    instructor_row = fetch_one("""SELECT COUNT(DISTINCT bei.event_id) AS total
        FROM battalion_event_instructors bei
        JOIN battalion_events be ON be.id=bei.event_id
        WHERE bei.personnel_id=%s AND be.event_type='TRAINING' AND be.status='CLOSED'""", (personnel_id,)) or {}
    instructor_sessions = int(instructor_row.get('total') or 0)
    recruiter_row = fetch_one("""SELECT COUNT(*) AS total FROM recruiting_cases
        WHERE recruited_by_personnel_id=%s AND status='ENLISTED' AND personnel_id IS NOT NULL""", (personnel_id,)) or {}
    successful_recruits = int(recruiter_row.get('total') or 0)
    nco_days_row = fetch_one("""SELECT COALESCE(SUM(GREATEST(0,(COALESCE(ended_date,CURRENT_DATE)-effective_date)+1)),0) AS total
        FROM personnel_appointments
        WHERE personnel_id=%s AND appointment_code IN ('FTL','ASST_SL','SL','PSG')""", (personnel_id,)) or {}
    nco_days = int(nco_days_row.get('total') or 0)
    nco_events_row = fetch_one("""SELECT COUNT(DISTINCT a.event_id) AS total
        FROM battalion_event_attendance a
        JOIN battalion_events e ON e.id=a.event_id
        WHERE a.personnel_id=%s AND a.credited_at IS NOT NULL AND e.status='CLOSED'
          AND EXISTS (SELECT 1 FROM personnel_appointments pa
              WHERE pa.personnel_id=a.personnel_id AND pa.appointment_code IN ('FTL','ASST_SL','SL','PSG')
                AND e.starts_at::date >= pa.effective_date
                AND e.starts_at::date <= COALESCE(pa.ended_date,CURRENT_DATE))""", (personnel_id,)) or {}
    nco_events = int(nco_events_row.get('total') or 0)
    military = bool(fetch_one("SELECT 1 FROM personnel_military_service_verification WHERE personnel_id=%s AND verified=TRUE", (personnel_id,)))
    good_standing = not bool(person.get('administrative_review')) and not bool(person.get('separated_at'))

    progress = [
        {'code':'INSTRUCTOR','name':'Instructor Ribbon','current':instructor_sessions,'target':5,'detail':f'{instructor_sessions} / 5 completed instructional periods','complete':instructor_sessions >= 5},
        {'code':'NCO_LEADERSHIP','name':'NCO Leadership Ribbon','current':min(nco_days,30),'target':30,'secondary_current':nco_events,'secondary_target':3,'detail':f'{nco_days} / 30 qualifying days • {nco_events} / 3 official events','complete':nco_days >= 30 and nco_events >= 3},
        {'code':'RECRUITING','name':'Recruiting Ribbon','current':successful_recruits,'target':3,'detail':f'{successful_recruits} / 3 successful recruits','complete':successful_recruits >= 3},
        {'code':'COMBAT_INFANTRY','name':'Combat Infantry Ribbon','current':operations,'target':10,'detail':f'{operations} / 10 credited official operations','complete':operations >= 10},
        {'code':'CAMPAIGN','name':'Campaign Ribbon','current':0,'target':1,'detail':'Progress begins when Headquarters designates a battalion campaign.','complete':False,'pending_system':True},
        {'code':'GOOD_CONDUCT','name':'Good Conduct Ribbon','current':min(service_days,90) if good_standing else service_days,'target':90,'detail':(f'{service_days} / 90 qualifying service days' if good_standing else 'Eligibility suspended — personnel file is not currently in good standing.'),'complete':service_days >= 90 and good_standing},
        {'code':'TOUR_OF_DUTY','name':'Tour of Duty Ribbon','current':min(service_days,180),'target':180,'secondary_current':operations,'secondary_target':20,'detail':f'{service_days} / 180 service days • {operations} / 20 official operations','complete':service_days >= 180 and operations >= 20},
        {'code':'MILITARY_SERVICE','name':'Military Service Ribbon','current':1 if military else 0,'target':1,'detail':'Military service verified by Battalion Headquarters.' if military else 'Command verification required.','complete':military},
    ]
    earned = {r['ribbon_code'] for r in fetch_all("SELECT ribbon_code FROM personnel_ribbons WHERE personnel_id=%s", (personnel_id,))}
    if award_completed:
        for row in progress:
            if row['complete'] and row['code'] not in earned:
                execute("""INSERT INTO personnel_ribbons(personnel_id,ribbon_code,earned_at,source_type,source_reference,notes,is_worn)
                           VALUES(%s,%s,CURRENT_DATE,'AUTOMATIC','RIBBON ENGINE',%s,TRUE)
                           ON CONFLICT(personnel_id,ribbon_code) DO NOTHING""",
                        (personnel_id,row['code'],row['detail']))
                write_service_entry(personnel_id,'AWARD',row['name'].upper(),f"Automatically awarded after satisfying published ribbon requirements. {row['detail']}",'BATTALION CLERK / S-1')
                earned.add(row['code'])
    for row in progress:
        row['earned'] = row['code'] in earned
        base = max(1, int(row.get('target') or 1))
        row['percent'] = 100 if row['earned'] else min(100, int((int(row.get('current') or 0) / base) * 100))
    return progress


def automatic_ribbon_recheck(personnel_ids):
    for pid in {str(x) for x in personnel_ids if x}:
        try:
            ribbon_progress_for(pid, award_completed=True)
        except Exception as exc:
            log.warning('Automatic ribbon recheck failed for %s: %s', pid, exc)


def file_named_ribbon_award(personnel_id, award_name, award_date=None, source_reference=None, notes=None):
    """If a website-issued award matches a ribbon catalog entry, authorize it and wear it immediately."""
    catalog = fetch_one("""SELECT ribbon_code,ribbon_name FROM ribbon_catalog
                           WHERE is_active=TRUE AND LOWER(TRIM(ribbon_name))=LOWER(TRIM(%s)) LIMIT 1""", (award_name,))
    if not catalog:
        return False
    execute("""INSERT INTO personnel_ribbons(personnel_id,ribbon_code,earned_at,source_type,source_reference,notes,is_worn)
               VALUES(%s,%s,%s,'HEADQUARTERS',%s,%s,TRUE)
               ON CONFLICT(personnel_id,ribbon_code) DO UPDATE SET
                   is_worn=TRUE,
                   source_reference=COALESCE(EXCLUDED.source_reference,personnel_ribbons.source_reference),
                   notes=COALESCE(EXCLUDED.notes,personnel_ribbons.notes)""",
            (personnel_id, catalog['ribbon_code'], award_date or date.today(), source_reference, notes))
    return True


def earned_ribbons_for_display(personnel_id):
    rows = fetch_all("""SELECT pr.*,rc.ribbon_name,rc.requirement_text,rc.image_filename,rc.sort_order
                        FROM personnel_ribbons pr
                        JOIN ribbon_catalog rc ON rc.ribbon_code=pr.ribbon_code
                        WHERE pr.personnel_id=%s
                        ORDER BY rc.sort_order,pr.earned_at""", (personnel_id,))
    for row in rows:
        row['is_worn'] = bool(row.get('is_worn'))
    return rows


def build_ribbon_rows(ribbons, max_per_row=3):
    """Build a military-style ribbon rack: three ribbons per row, filled top row first."""
    ribbons = list(ribbons or [])
    if not ribbons:
        return []
    return [ribbons[idx:idx + max_per_row] for idx in range(0, len(ribbons), max_per_row)]


def worn_ribbon_rows(personnel_id):
    earned = earned_ribbons_for_display(personnel_id)
    worn = [row for row in earned if row.get('is_worn') and row.get('image_filename')]
    return build_ribbon_rows(worn), earned


def duty_qualification_count(personnel_id):
    row = fetch_one(
        "SELECT COUNT(*) AS total FROM personnel_duty_qualifications WHERE personnel_id=%s AND UPPER(status)='CURRENT'",
        (personnel_id,),
    )
    return int((row or {}).get("total") or 0)


def has_current_rifle_qualification(personnel_id):
    return bool(fetch_one(
        """SELECT 1 FROM qualifications
           WHERE personnel_id=%s AND UPPER(COALESCE(status,'CURRENT')) IN ('CURRENT','QUALIFIED','COMPLETE')
             AND (UPPER(qualification_code) LIKE '%%M16%%' OR UPPER(qualification_code) LIKE '%%RIFLE%%'
                  OR UPPER(qualification_name) LIKE '%%M16%%' OR UPPER(qualification_name) LIKE '%%RIFLE%%')
           LIMIT 1""",
        (personnel_id,),
    ))


def has_appointment_history(personnel_id, codes):
    if not codes:
        return False
    placeholders = ",".join(["%s"] * len(codes))
    return bool(fetch_one(
        f"SELECT 1 FROM personnel_appointments WHERE personnel_id=%s AND appointment_code IN ({placeholders}) LIMIT 1",
        (personnel_id, *codes),
    ))


def has_promotion_recommendation(personnel_id, target_rank):
    return bool(fetch_one(
        """SELECT 1 FROM personnel_recommendations
           WHERE personnel_id=%s AND UPPER(recommendation_type)='PROMOTION'
             AND UPPER(recommended_action) LIKE %s
             AND UPPER(status) NOT IN ('DENIED','REJECTED','CANCELLED')
           LIMIT 1""",
        (personnel_id, f"%{target_rank.upper()}%"),
    ))


def time_in_grade_days(personnel):
    row = fetch_one(
        """SELECT effective_date FROM promotion_history
           WHERE personnel_id=%s AND new_rank_code=%s
           ORDER BY effective_date DESC,created_at DESC LIMIT 1""",
        (personnel["id"], personnel.get("rank_code")),
    )
    start = (row or {}).get("effective_date") or personnel.get("date_joined") or date.today()
    return max((date.today() - start).days, 0)


def initial_entry_rank(personnel_id):
    """Return the rank the Soldier held when first entered on the battalion rolls."""
    row = fetch_one(
        """SELECT new_rank_code,effective_date FROM promotion_history
           WHERE personnel_id=%s AND old_rank_code IS NULL
           ORDER BY effective_date ASC,created_at ASC LIMIT 1""",
        (personnel_id,),
    )
    return (row or {}).get("new_rank_code")


def entry_processing_profile(personnel):
    """Separate initial battalion in-processing from PVT-only Replacement Training."""
    if not personnel:
        return {"initial_rank": None, "replacement_required": True, "program_code": "REPLACEMENT", "program_title": "Replacement Training"}
    rank = initial_entry_rank(personnel["id"]) or personnel.get("rank_code") or "PVT"
    replacement_required = rank == "PVT"
    return {
        "initial_rank": rank,
        "replacement_required": replacement_required,
        "program_code": "REPLACEMENT" if replacement_required else "INITIAL_INPROCESSING",
        "program_title": "Replacement Training" if replacement_required else "Initial Battalion In-Processing",
    }


def replacement_training_status(personnel):
    """Return controlled initial-processing / Replacement Training status.

    Assignment and PVT promotion gates are intentionally separate. Replacement
    Training is completed by staff certification after S-1 onboarding and the
    Soldier's Standing Orders acknowledgement; a Soldier does not need a squad,
    seven days, or an operation merely to finish in-processing.
    """
    if not personnel:
        return {"complete": False, "requirements": [], "replacement_required": True, "program_title": "Replacement Training", "initial_rank": None}
    pid=personnel['id']
    profile=entry_processing_profile(personnel)
    progress=personnel_progress(pid)
    record=training_record(pid,profile['program_code'])
    record_complete=bool(record and str(record.get('status') or '').upper() in {'COMPLETE','COMPLETED','CLOSED'})
    requirements=[
        {"code":"S1","label":"Onboarded — spoke with an S-1 representative","complete":bool(progress.get('s1_onboarded_at')),"detail":progress.get('s1_onboarded_at') or 'S-1 certification pending'},
        {"code":"RULES","label":"Community rules / Standing Orders acknowledged","complete":bool(progress.get('rules_acknowledged_at')),"detail":progress.get('rules_acknowledged_at') or 'Acknowledgement required'},
        {"code":"PROGRAM","label":profile['program_title']+" certified","complete":record_complete,"detail":(record or {}).get('completed_at') or 'Staff certification pending'},
    ]
    return {
        'complete':all(x['complete'] for x in requirements),
        'requirements':requirements,
        'record':record,
        'replacement_required':profile['replacement_required'],
        'program_code':profile['program_code'],
        'program_title':profile['program_title'],
        'initial_rank':profile['initial_rank'],
        'days_in_service':max((date.today()-(personnel.get('date_joined') or date.today())).days,0),
        'operations':credited_operation_count(pid),
    }


PROMOTION_PATHS = {
    "PVT": [
        {"target":"PFC","title":"PRIVATE FIRST CLASS","requirements":[("tig",7,"7 days time in grade"),("ops",1,"1 official operation"),("program","REPLACEMENT","Replacement Training complete")]},
    ],
    "PFC": [
        {"target":"CPL","title":"CORPORAL","requirements":[("tig",14,"14 days time in grade"),("ops",3,"3 official operations"),("program","COMBAT_ORIENTATION","Battalion Combat Orientation complete"),("rifle",True,"Current M16 / rifle qualification"),("readiness",80,"80% readiness"),("hold",False,"No promotion hold")]},
        {"target":"SP4","title":"SPECIALIST FOUR","requirements":[("tig",14,"14 days time in grade"),("ops",3,"3 official operations"),("program","REPLACEMENT","Replacement Training complete"),("dutyquals",2,"2 current duty qualifications"),("readiness",80,"80% readiness"),("hold",False,"No promotion hold")]},
    ],
    "CPL": [
        {"target":"SGT","title":"SERGEANT","requirements":[("tig",21,"21 days time in grade"),("ops",6,"6 official operations"),("dutyquals",1,"1 current duty qualification"),("program","SQUAD_LEADERSHIP","Squad Leadership Course complete"),("readiness",85,"85% readiness"),("recommendation",True,"NCO promotion recommendation"),("hold",False,"No promotion hold")]},
    ],
    "SGT": [
        {"target":"SSG","title":"STAFF SERGEANT","requirements":[("tig",30,"30 days time in grade"),("ops",10,"10 official operations"),("appointment",("SL","ASST_SL"),"Squad Leader / Assistant Squad Leader experience"),("dutyquals",2,"2 current duty qualifications"),("readiness",88,"88% readiness"),("recommendation",True,"PSG / 1SG promotion recommendation"),("hold",False,"No promotion hold")]},
    ],
    "SSG": [
        {"target":"SFC","title":"SERGEANT FIRST CLASS","requirements":[("tig",45,"45 days time in grade"),("ops",15,"15 official operations"),("program","PLATOON_LEADERSHIP","Platoon Leadership Course complete"),("appointment",("SL",),"Successful Squad Leader experience"),("readiness",90,"90% readiness"),("recommendation",True,"Command review / promotion recommendation"),("hold",False,"No promotion hold")]},
    ],
    "SFC": [
        {"target":"MSG","title":"MASTER SERGEANT","requirements":[("tig",60,"60 days time in grade"),("ops",20,"20 official operations"),("appointment",("PSG",),"Platoon-level leadership experience"),("dutyquals",3,"3 current duty qualifications"),("readiness",90,"90% readiness"),("recommendation",True,"Command promotion recommendation"),("hold",False,"No promotion hold")]},
    ],
    "MSG": [
        {"target":"1SG","title":"FIRST SERGEANT","appointment_based":True,"requirements":[("appointment",("CO_1SG",),"Selected / appointed as Company First Sergeant"),("recommendation",True,"Command selection recorded"),("hold",False,"No promotion hold")]},
    ],
    "1SG": [
        {"target":"SGM","title":"SERGEANT MAJOR","appointment_based":True,"requirements":[("appointment",("BN_SGM",),"Selected / appointed for battalion senior enlisted leadership"),("recommendation",True,"Battalion Commander approval / selection"),("hold",False,"No promotion hold")]},
    ],
    "SP4": [
        {"target":"SP5","title":"SPECIALIST FIVE","requirements":[("tig",30,"30 days time in grade"),("ops",7,"7 official operations"),("dutyquals",3,"3 current duty qualifications"),("readiness",85,"85% readiness"),("recommendation",True,"Specialist duty recommendation"),("hold",False,"No promotion hold")]},
    ],
    "SP5": [
        {"target":"SP6","title":"SPECIALIST SIX","requirements":[("tig",45,"45 days time in grade"),("ops",12,"12 official operations"),("dutyquals",4,"Advanced specialist qualifications"),("appointment",("TRNG_NCO","BN_CLERK","CO_CLERK","ARMORER","COMM_SGT"),"Instructor, staff, or specialist duty experience"),("readiness",88,"88% readiness"),("recommendation",True,"Command promotion recommendation"),("hold",False,"No promotion hold")]},
    ],
    "SP6": [
        {"target":"SP7","title":"SPECIALIST SEVEN","appointment_based":True,"requirements":[("tig",60,"60 days time in grade"),("ops",18,"18 official operations"),("dutyquals",4,"Multiple advanced duty qualifications"),("recommendation",True,"Senior technical billet / command selection"),("hold",False,"No promotion hold")]},
    ],
    "2LT": [
        {"target":"1LT","title":"FIRST LIEUTENANT","requirements":[("tig",30,"30 days time in grade"),("ops",5,"5 official operations"),("program","OFFICER_ORIENTATION","Officer Orientation complete"),("appointment",("PL",),"Successful platoon leadership experience"),("recommendation",True,"Command promotion recommendation"),("hold",False,"No promotion hold")]},
    ],
    "1LT": [
        {"target":"CPT","title":"CAPTAIN","requirements":[("tig",60,"60 days time in grade"),("ops",10,"10 official operations"),("program","COMPANY_LEADERSHIP","Company Leadership Course complete"),("appointment",("PL","CO_XO"),"Successful Platoon Leader / Company XO service"),("readiness",90,"90% readiness"),("recommendation",True,"Command promotion recommendation"),("hold",False,"No promotion hold")]},
    ],
    "CPT": [
        {"target":"MAJ","title":"MAJOR","appointment_based":True,"requirements":[("appointment",("CO_CO","S1","S2","S3","S4","ASST_S3"),"Successful company command or battalion staff service"),("recommendation",True,"Battalion Commander selection"),("hold",False,"No promotion hold")]},
    ],
    "MAJ": [
        {"target":"LTC","title":"LIEUTENANT COLONEL","appointment_based":True,"requirements":[("appointment",("BN_CO",),"Selected / appointed as Battalion Commander"),("recommendation",True,"Command appointment approval"),("hold",False,"No promotion hold")]},
    ],
}


def promotion_eligibility(personnel):
    if not personnel:
        return []
    pid = personnel["id"]
    progress = personnel_progress(pid)
    tig = time_in_grade_days(personnel)
    ops = credited_operation_count(pid)
    dqs = duty_qualification_count(pid)
    readiness = int(personnel.get('readiness_percent') or 0)
    results = []
    for path in PROMOTION_PATHS.get(personnel.get("rank_code"), []):
        items = []
        for req in path["requirements"]:
            kind, value, label = req
            if kind == "tig":
                complete, detail = tig >= value, f"{tig} / {value} days"
            elif kind == "ops":
                complete, detail = ops >= value, f"{ops} / {value} operations"
            elif kind == "program":
                complete = training_program_complete(pid, value)
                row = training_record(pid, value)
                detail = f"COMPLETE {row.get('completed_at')}" if complete and row else "NOT COMPLETE"
            elif kind == "rifle":
                complete, detail = has_current_rifle_qualification(pid), "CURRENT" if has_current_rifle_qualification(pid) else "NOT CURRENT"
            elif kind == "readiness":
                complete, detail = readiness >= value, f"{readiness}% / {value}%"
            elif kind == "dutyquals":
                complete, detail = dqs >= value, f"{dqs} / {value} current"
            elif kind == "appointment":
                complete, detail = has_appointment_history(pid, value), "ON FILE" if has_appointment_history(pid, value) else "NOT ON FILE"
            elif kind == "recommendation":
                complete = has_promotion_recommendation(pid, path["target"])
                detail = "ON FILE" if complete else "NOT SUBMITTED"
            elif kind == "hold":
                complete = not bool(progress.get("promotion_hold"))
                detail = "NONE" if complete else (progress.get("promotion_hold_reason") or "ACTIVE HOLD")
            else:
                complete, detail = False, "PENDING"
            items.append({"label": label, "complete": complete, "detail": detail, "kind": kind})
        eligible = all(x["complete"] for x in items)
        recommended = has_promotion_recommendation(pid, path["target"])
        status = "ELIGIBLE FOR CONSIDERATION" if eligible else "NOT ELIGIBLE"
        if eligible and recommended:
            status = "RECOMMENDED FOR PROMOTION"
        results.append({**path, "requirements": items, "eligible": eligible, "recommended": recommended, "status": status, "tig": tig, "operations": ops})
    return results


def duty_qualification_catalog():
    return fetch_all("""SELECT * FROM duty_qualification_types
                        WHERE is_active=TRUE ORDER BY sort_order,display_name""") if database_ready() else []


def personnel_duty_qualifications(personnel_id):
    return fetch_all(
        """SELECT pdq.*,dqt.code,dqt.display_name,dqt.battlefield_unit,
                  i.rank_code AS instructor_rank,i.first_name AS instructor_first,i.last_name AS instructor_last
           FROM personnel_duty_qualifications pdq
           JOIN duty_qualification_types dqt ON dqt.id=pdq.qualification_type_id
           LEFT JOIN personnel i ON i.id=pdq.instructor_personnel_id
           WHERE pdq.personnel_id=%s
           ORDER BY dqt.sort_order,dqt.display_name""",(personnel_id,))


def award_duty_qualification(personnel_id, qualification_type_id, instructor_id=None,
                             qualified_date=None, expiration_date=None, remarks=None, authority=None):
    q=fetch_one("SELECT * FROM duty_qualification_types WHERE id=%s",(qualification_type_id,))
    if not q: raise ValueError("Duty qualification not found")
    qdate=qualified_date or date.today()
    # Duty-role credentials are current for 90 days unless S-3 files a specific expiration.
    expiration_date = expiration_date or (qdate + timedelta(days=90))
    execute(
        """INSERT INTO personnel_duty_qualifications
           (personnel_id,qualification_type_id,status,qualified_date,expiration_date,
            instructor_personnel_id,remarks)
           VALUES(%s,%s,'QUALIFIED',%s,%s,%s,%s)
           ON CONFLICT(personnel_id,qualification_type_id) DO UPDATE SET
             status='QUALIFIED',qualified_date=EXCLUDED.qualified_date,
             expiration_date=EXCLUDED.expiration_date,
             instructor_personnel_id=EXCLUDED.instructor_personnel_id,
             remarks=EXCLUDED.remarks,updated_at=NOW()""",
        (personnel_id,qualification_type_id,qdate,expiration_date,instructor_id,remarks))
    write_service_entry(personnel_id,"TRAINING","DUTY QUALIFICATION",f"Qualified for HLL: Vietnam duty role: {q['display_name']}.",authority,q["code"],qdate)
    qdoc=create_personnel_order(personnel_id,"QUALIFICATION RECORD","QUALIFICATION RECORD",f"The Soldier named herein is qualified for duty as {q['display_name']} ({q['code']}).",effective_date=qdate,authority=authority,details={"qualification_code":q['code'],"qualification_name":q['display_name']},source_key=f"QUAL:{personnel_id}:{q['code']}:{qdate}")
    if qdoc and qdoc.get('document_number'):
        execute("UPDATE personnel_duty_qualifications SET qualification_number=%s WHERE personnel_id=%s AND qualification_type_id=%s",(qdoc['document_number'],personnel_id,qualification_type_id))
    refreshed=fetch_one("SELECT * FROM personnel WHERE id=%s",(personnel_id,))
    if refreshed:
        sync_mos_proficiency(refreshed)
        sync_readiness(refreshed)

def training_deficiencies():
    return training_deficiency_board()


def sync_qualification_currency(personnel_id=None):
    """Expire dated credentials automatically. Renewing the credential restores currency."""
    params=()
    person_filter=""
    if personnel_id:
        person_filter=" AND personnel_id=%s"
        params=(personnel_id,)
    execute(f"""UPDATE qualifications SET status='EXPIRED'
                WHERE expires_at IS NOT NULL AND expires_at<CURRENT_DATE
                  AND UPPER(status)<>'EXPIRED'{person_filter}""", params)
    execute(f"""UPDATE personnel_duty_qualifications SET status='EXPIRED',updated_at=NOW()
                WHERE expiration_date IS NOT NULL AND expiration_date<CURRENT_DATE
                  AND UPPER(status)<>'EXPIRED'{person_filter}""", params)
    execute(f"""UPDATE instructor_qualifications SET status='EXPIRED'
                WHERE expires_at IS NOT NULL AND expires_at<CURRENT_DATE
                  AND UPPER(status)<>'EXPIRED'{person_filter}""", params)


def leadership_service_summary(personnel_id):
    """Permanent leadership-service clock independent from rank."""
    rows=fetch_all("""SELECT pa.appointment_code,ac.appointment_name,pa.organization,pa.effective_date,pa.ended_date,pa.is_current,
                       GREATEST(0,(COALESCE(pa.ended_date,CURRENT_DATE)-pa.effective_date)+1)::int AS days_served
                FROM personnel_appointments pa JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code
                WHERE pa.personnel_id=%s AND pa.appointment_code IN ('FTL','ASST_SL','SL','PSG','PL','CO_1SG','CO_XO','CO_CO')
                ORDER BY pa.effective_date DESC,ac.sort_order""",(personnel_id,))
    totals={}
    for r in rows:
        code=r['appointment_code']; totals.setdefault(code,{'appointment_code':code,'appointment_name':r['appointment_name'],'days':0,'periods':0})
        totals[code]['days'] += int(r.get('days_served') or 0); totals[code]['periods'] += 1
    preferred=['FTL','ASST_SL','SL','PSG','PL','CO_1SG','CO_XO','CO_CO']
    total_rows=[totals[c] for c in preferred if c in totals]
    return {'history':rows,'totals':total_rows,'total_days':sum(x['days'] for x in total_rows)}



def combat_leadership_score(personnel_id):
    """Advisory 0-100 leadership score. It is not rank and never auto-promotes a Soldier."""
    person = fetch_one("SELECT * FROM personnel WHERE id=%s", (personnel_id,))
    if not person:
        return {"score":0,"rating":"NOT RATED","breakdown":{},"operations_led":0,"training_conducted":0,"assigned_readiness":0,"successful_assignments":0}

    service = leadership_service_summary(personnel_id)
    leadership_days = int(service.get("total_days") or 0)
    duty_points = min(30, leadership_days // 3)

    # Credit an operation as led when the filed duty role is a recognized leadership role.
    led = fetch_one("""SELECT COUNT(DISTINCT op.operation_id) total
        FROM operation_participation op
        WHERE op.personnel_id=%s AND UPPER(COALESCE(op.attendance_status,'')) IN ('FULL CREDIT','PARTICIPATED','PRESENT','CREDITED','COMPLETE','COMPLETED')
          AND (UPPER(COALESCE(op.duty_role,'')) LIKE '%%LEADER%%'
               OR UPPER(COALESCE(op.duty_role,'')) LIKE '%%SERGEANT%%'
               OR UPPER(COALESCE(op.duty_role,'')) LIKE '%%COMMANDER%%'
               OR UPPER(COALESCE(op.duty_role,'')) LIKE '%%PLATOON LEADER%%'
               OR UPPER(COALESCE(op.duty_role,'')) LIKE '%%SQUAD LEADER%%')""", (personnel_id,)) or {"total":0}
    operations_led = int(led.get("total") or 0)
    operation_points = min(25, operations_led * 5)

    tr = fetch_one("""SELECT COUNT(DISTINCT bei.event_id) total
        FROM battalion_event_instructors bei
        JOIN battalion_events be ON be.id=bei.event_id
        WHERE bei.personnel_id=%s AND UPPER(COALESCE(be.status,'')) IN ('CLOSED','COMPLETE','COMPLETED')""", (personnel_id,)) or {"total":0}
    training_conducted = int(tr.get("total") or 0)
    training_points = min(15, training_conducted * 3)

    # Current command scope determines the readiness of Soldiers presently entrusted to the leader.
    ap = fetch_one("""SELECT pa.unit_node_id FROM personnel_appointments pa
        WHERE pa.personnel_id=%s AND pa.is_current=TRUE AND pa.unit_node_id IS NOT NULL
          AND pa.appointment_code IN ('FTL','ASST_SL','SL','PSG','PL','CO_1SG','CO_XO','CO_CO','BN_SGM','BN_XO','BN_CO')
        ORDER BY CASE pa.appointment_code WHEN 'FTL' THEN 1 WHEN 'ASST_SL' THEN 2 WHEN 'SL' THEN 3 WHEN 'PSG' THEN 4 WHEN 'PL' THEN 5 WHEN 'CO_1SG' THEN 6 WHEN 'CO_XO' THEN 7 WHEN 'CO_CO' THEN 8 ELSE 9 END
        LIMIT 1""", (personnel_id,))
    assigned_readiness = 0
    assigned_count = 0
    if ap and ap.get('unit_node_id'):
        ids = unit_descendant_ids(ap['unit_node_id'])
        if ids:
            subordinates = fetch_all("SELECT * FROM personnel WHERE unit_node_id=ANY(%s) AND separated_at IS NULL AND archived=FALSE AND id<>%s", (ids, personnel_id))
            # Leadership scoring is read-only on page views; use the last authoritative readiness snapshot.
            scores=[int(sub.get('readiness_percent') or 0) for sub in subordinates]
            assigned_count=len(scores)
            assigned_readiness=round(sum(scores)/len(scores)) if scores else 0
    readiness_points = round(assigned_readiness * .20) if assigned_count else 0

    completed = fetch_one("""SELECT COUNT(*) total FROM personnel_appointments
        WHERE personnel_id=%s AND appointment_code IN ('FTL','ASST_SL','SL','PSG','PL','CO_1SG','CO_XO','CO_CO')
          AND is_current=FALSE AND ended_date IS NOT NULL""", (personnel_id,)) or {"total":0}
    successful_assignments = int(completed.get('total') or 0)
    assignment_points = min(10, successful_assignments * 2)

    score = max(0, min(100, duty_points + operation_points + training_points + readiness_points + assignment_points))
    if leadership_days < 7:
        rating='DEVELOPING'
    elif score >= 85:
        rating='DISTINGUISHED'
    elif score >= 70:
        rating='PROVEN'
    elif score >= 50:
        rating='ESTABLISHED'
    else:
        rating='DEVELOPING'
    return {
        'score':score,'rating':rating,'leadership_days':leadership_days,'operations_led':operations_led,
        'training_conducted':training_conducted,'assigned_readiness':assigned_readiness,'assigned_count':assigned_count,
        'successful_assignments':successful_assignments,
        'breakdown':{
            'Leadership Duty':{'points':duty_points,'max':30},
            'Operations Led':{'points':operation_points,'max':25},
            'Training Conducted':{'points':training_points,'max':15},
            'Assigned Soldier Readiness':{'points':readiness_points,'max':20},
            'Completed Leadership Assignments':{'points':assignment_points,'max':10},
        }
    }


def unit_cohesion(unit_node_id):
    """Derived cohesion for a squad/platoon. Built from activity, attendance, training, stability, qualifications and staffing."""
    node = unit_node(unit_node_id)
    if not node:
        return None
    ids = unit_descendant_ids(unit_node_id) or [unit_node_id]
    people = fetch_all("SELECT * FROM personnel WHERE unit_node_id=ANY(%s) AND separated_at IS NULL AND archived=FALSE", (ids,))
    assigned=len(people)

    auth = fetch_one("SELECT COALESCE(SUM(authorized_strength),0) total FROM unit_billets WHERE unit_node_id=ANY(%s) AND is_active=TRUE", (ids,)) or {'total':0}
    authorized=int(auth.get('total') or 0)
    if authorized <= 0:
        authorized = 9 if str(node.get('unit_type','')).lower()=='squad' else (36 if str(node.get('unit_type','')).lower()=='platoon' else max(assigned,1))
    staffing = min(100, round((assigned/authorized)*100)) if authorized else 0

    activity_values=[]; training_values=[]; qual_values=[]
    for person in people:
        _, _, b = sync_readiness(person)
        activity_values.append(round((int((b.get('Activity / Participation') or {}).get('points',0))/30)*100))
        training_values.append(round((int((b.get('Training & Qualifications') or {}).get('points',0))/25)*100))
        q=fetch_one("""SELECT COUNT(*) total FROM personnel_duty_qualifications WHERE personnel_id=%s AND UPPER(status)='QUALIFIED' AND (expiration_date IS NULL OR expiration_date>=CURRENT_DATE)""", (person['id'],)) or {'total':0}
        qual_values.append(100 if int(q.get('total') or 0)>0 else 0)
    activity=round(sum(activity_values)/len(activity_values)) if activity_values else 0
    training=round(sum(training_values)/len(training_values)) if training_values else 0
    qualifications=round(sum(qual_values)/len(qual_values)) if qual_values else 0

    recent_ops=fetch_all("""SELECT o.id FROM operations o WHERE UPPER(COALESCE(o.status,'')) IN ('CLOSED','COMPLETE','COMPLETED') ORDER BY COALESCE(o.operation_date,CURRENT_DATE) DESC LIMIT 5""")
    attendance_rates=[]
    if assigned:
        person_ids=[x['id'] for x in people]
        for op in recent_ops:
            row=fetch_one("SELECT COUNT(DISTINCT personnel_id) total FROM operation_participation WHERE operation_id=%s AND personnel_id=ANY(%s) AND UPPER(COALESCE(attendance_status,'')) IN ('FULL CREDIT','PARTICIPATED','PRESENT','CREDITED','COMPLETE','COMPLETED')", (op['id'],person_ids)) or {'total':0}
            attendance_rates.append(min(100,round(int(row.get('total') or 0)/assigned*100)))
    attendance=round(sum(attendance_rates)/len(attendance_rates)) if attendance_rates else 0

    leader_codes = ('SL',) if str(node.get('unit_type','')).lower()=='squad' else ('PSG','PL')
    placeholders=','.join(['%s']*len(leader_codes))
    leader=fetch_one(f"SELECT effective_date FROM personnel_appointments WHERE unit_node_id=%s AND is_current=TRUE AND appointment_code IN ({placeholders}) ORDER BY effective_date LIMIT 1", (unit_node_id,*leader_codes))
    leader_days=0
    if leader and leader.get('effective_date'):
        leader_days=max(0,(date.today()-leader['effective_date']).days+1)
    churn=fetch_one("""SELECT COUNT(*) total FROM assignment_history WHERE unit_node_id=ANY(%s) AND created_at >= NOW()-INTERVAL '30 days'""", (ids,)) or {'total':0}
    churn_count=int(churn.get('total') or 0)
    stability=max(0,min(100, round(min(30,leader_days)/30*100) - min(60,churn_count*10)))

    # Weighted to put actual participation and member currency ahead of simple headcount.
    score=round(activity*.20 + attendance*.20 + training*.20 + stability*.15 + qualifications*.10 + staffing*.15)
    return {'unit':node,'cohesion':max(0,min(100,score)),'assigned':assigned,'authorized':authorized,
            'activity':activity,'attendance':attendance,'training':training,'stability':stability,
            'qualifications':qualifications,'staffing':staffing,'churn_30d':churn_count,'leader_days':leader_days}


def unit_experience(unit_node_id):
    """Experience is earned by current members completing official operations together; it is not rank or readiness."""
    node=unit_node(unit_node_id)
    if not node:
        return None
    ids=unit_descendant_ids(unit_node_id) or [unit_node_id]
    people=fetch_all("SELECT id FROM personnel WHERE unit_node_id=ANY(%s) AND separated_at IS NULL AND archived=FALSE", (ids,))
    person_ids=[p['id'] for p in people]
    strength=len(person_ids)
    together=0
    if strength:
        rows=fetch_all("""SELECT op.operation_id,COUNT(DISTINCT op.personnel_id) participants
            FROM operation_participation op JOIN operations o ON o.id=op.operation_id
            WHERE op.personnel_id=ANY(%s) AND UPPER(COALESCE(o.status,'')) IN ('CLOSED','COMPLETE','COMPLETED')
              AND UPPER(COALESCE(op.attendance_status,'')) IN ('FULL CREDIT','PARTICIPATED','PRESENT','CREDITED','COMPLETE','COMPLETED')
            GROUP BY op.operation_id""", (person_ids,))
        minimum=max(2, (strength+1)//2) if strength>1 else 1
        together=sum(1 for r in rows if int(r.get('participants') or 0) >= minimum)
    if together >= 15:
        level='VETERAN'; order=4
    elif together >= 8:
        level='COMBAT TESTED'; order=3
    elif together >= 3:
        level='FIELD EXPERIENCED'; order=2
    else:
        level='NEWLY FORMED'; order=1
    next_text={1:'3 qualifying operations together',2:'8 qualifying operations together',3:'15 qualifying operations together',4:'MAXIMUM EXPERIENCE CLASSIFICATION'}[order]
    return {'unit':node,'level':level,'order':order,'operations_together':together,'next_requirement':next_text,'current_strength':strength}


def unit_performance_board(scope_node_id=None):
    """Return squad/platoon performance cards for command/readiness views."""
    params=[]
    where="WHERE is_active=TRUE AND LOWER(unit_type) IN ('squad','platoon')"
    if scope_node_id:
        ids=unit_descendant_ids(scope_node_id)
        where += " AND id=ANY(%s)"; params.append(ids)
    nodes=fetch_all(f"SELECT * FROM unit_nodes {where} ORDER BY sort_order,display_name", tuple(params))
    board=[]
    for n in nodes:
        c=unit_cohesion(n['id']); e=unit_experience(n['id'])
        if c and e: board.append({'node':n,'cohesion':c,'experience':e})
    return board

def _completed_training_count(personnel_id):
    row=fetch_one("SELECT COUNT(*) total FROM personnel_training_records WHERE personnel_id=%s AND UPPER(status)='COMPLETE'",(personnel_id,)) or {'total':0}
    return int(row.get('total') or 0)


def sync_mos_proficiency(person):
    """Experience-based MOS proficiency; never changes rank or command authority."""
    if not person or not person.get('id') or not person.get('mos_code'):
        return None
    pid=person['id']; mos=person['mos_code']
    title_row=fetch_one("SELECT mos_title FROM battalion_mos_catalog WHERE mos_code=%s",(mos,)) or {}
    base=(title_row.get('mos_title') or person.get('duty_position') or mos).strip()
    ops=credited_operation_count(pid)
    training=_completed_training_count(pid)
    qrow=fetch_one("""SELECT COUNT(*) total FROM personnel_duty_qualifications
                      WHERE personnel_id=%s AND UPPER(status)='QUALIFIED'
                        AND (expiration_date IS NULL OR expiration_date>=CURRENT_DATE)""",(pid,)) or {'total':0}
    quals=int(qrow.get('total') or 0)
    # Four proficiency grades. Senior is deliberately difficult and represents sustained experience.
    if ops>=20 and training>=3 and quals>=3:
        order=4; suffix='SENIOR'
    elif ops>=10 and training>=2 and quals>=2:
        order=3; suffix='I'
    elif ops>=5 and training>=1 and quals>=1:
        order=2; suffix='II'
    else:
        order=1; suffix='III'
    if mos=='11R':
        label={'III':'Rifleman III','II':'Rifleman II','I':'Rifleman I','SENIOR':'Senior Rifleman'}[suffix]
    else:
        label=(f"Senior {base}" if suffix=='SENIOR' else f"{base} {suffix}")
    current=fetch_one("SELECT * FROM personnel_mos_proficiency WHERE personnel_id=%s AND mos_code=%s",(pid,mos))
    if not current or int(current.get('proficiency_order') or 0)!=order or current.get('proficiency_level')!=label:
        execute("""INSERT INTO personnel_mos_proficiency(personnel_id,mos_code,proficiency_level,proficiency_order,effective_date,certified_by,remarks,is_current)
                   VALUES(%s,%s,%s,%s,CURRENT_DATE,'BATTALION TRAINING SYSTEM',%s,TRUE)
                   ON CONFLICT(personnel_id,mos_code) DO UPDATE SET proficiency_level=EXCLUDED.proficiency_level,
                     proficiency_order=EXCLUDED.proficiency_order,effective_date=CASE WHEN personnel_mos_proficiency.proficiency_order<>EXCLUDED.proficiency_order THEN CURRENT_DATE ELSE personnel_mos_proficiency.effective_date END,
                     certified_by=EXCLUDED.certified_by,remarks=EXCLUDED.remarks,is_current=TRUE""",
                (pid,mos,label,order,f"{ops} operations • {training} completed training programs • {quals} current duty qualifications"))
    return {'mos_code':mos,'mos_title':base,'level':label,'order':order,'operations':ops,'training':training,'qualifications':quals,
            'next_requirement':('MAXIMUM PROFICIENCY' if order==4 else ('20 operations / 3 training / 3 qualifications' if order==3 else ('10 operations / 2 training / 2 qualifications' if order==2 else '5 operations / 1 training / 1 qualification')))}


def required_training_programs_for(person):
    """Return the minimum S-3 schools used by the deficiency board."""
    rank=str(person.get('rank_code') or '').upper()
    required=[]
    # Every active Soldier needs battalion combat orientation once initial processing is complete.
    required.append(('COMBAT_ORIENTATION','Battalion Combat Orientation'))
    if rank in {'SGT','SSG','SFC','MSG','1SG','SGM'}:
        required.append(('SQUAD_LEADERSHIP','Squad Leadership Course'))
    if rank in {'SFC','MSG','1SG','SGM'}:
        required.append(('PLATOON_LEADERSHIP','Platoon Leadership Course'))
    if rank in {'2LT','1LT','CPT','MAJ','LTC'}:
        required.append(('OFFICER_ORIENTATION','Officer Orientation'))
    if rank in {'CPT','MAJ','LTC'}:
        required.append(('COMPANY_LEADERSHIP','Company Leadership Course'))
    return required


def training_deficiency_board():
    """S-3 board: missing required schools plus expired / soon-expiring credentials."""
    if not database_ready(): return []
    sync_qualification_currency()
    board=[]
    people=fetch_all("SELECT * FROM personnel WHERE separated_at IS NULL AND archived=FALSE ORDER BY last_name,first_name")
    for p in people:
        pid=p['id']; name=f"{p.get('rank_code') or ''} {p.get('last_name') or ''}, {p.get('first_name') or ''}".strip()
        for code,title in required_training_programs_for(p):
            ok=fetch_one("SELECT 1 FROM personnel_training_records WHERE personnel_id=%s AND program_code=%s AND UPPER(status)='COMPLETE'",(pid,code))
            if not ok:
                board.append({'personnel_id':pid,'rank_code':p.get('rank_code'),'first_name':p.get('first_name'),'last_name':p.get('last_name'),'unit_code':p.get('unit_code'),
                              'category':'MISSING SCHOOL','item':title,'due_date':None,'days_remaining':None,'severity':'HIGH'})
        quals=fetch_all("""SELECT qualification_name AS item,expires_at AS due_date,status FROM qualifications
                           WHERE personnel_id=%s AND expires_at IS NOT NULL AND expires_at<=CURRENT_DATE+INTERVAL '30 days'
                           UNION ALL
                           SELECT dqt.display_name,pdq.expiration_date,pdq.status FROM personnel_duty_qualifications pdq
                           JOIN duty_qualification_types dqt ON dqt.id=pdq.qualification_type_id
                           WHERE pdq.personnel_id=%s AND pdq.expiration_date IS NOT NULL AND pdq.expiration_date<=CURRENT_DATE+INTERVAL '30 days'""",(pid,pid))
        for q in quals:
            due=q.get('due_date'); days=(due-date.today()).days if due else None
            expired=(days is not None and days<0) or str(q.get('status') or '').upper()=='EXPIRED'
            board.append({'personnel_id':pid,'rank_code':p.get('rank_code'),'first_name':p.get('first_name'),'last_name':p.get('last_name'),'unit_code':p.get('unit_code'),
                          'category':'EXPIRED QUALIFICATION' if expired else 'EXPIRING QUALIFICATION','item':q.get('item'),'due_date':due,'days_remaining':days,
                          'severity':'CRITICAL' if expired else ('HIGH' if days is not None and days<=7 else 'WATCH')})
    order={'CRITICAL':0,'HIGH':1,'WATCH':2}
    board.sort(key=lambda x:(order.get(x['severity'],9),x.get('due_date') or date.max,str(x.get('last_name') or '')))
    return board


def member_combat_experience(personnel_id):
    ops=credited_operation_count(personnel_id)
    if ops>=15: level,order,next_requirement="VETERAN",4,"MAXIMUM FIELD EXPERIENCE CLASSIFICATION"
    elif ops>=8: level,order,next_requirement="COMBAT TESTED",3,"15 credited official operations"
    elif ops>=3: level,order,next_requirement="FIELD EXPERIENCED",2,"8 credited official operations"
    else: level,order,next_requirement="NEW ARRIVAL",1,"3 credited official operations"
    return {"level":level,"order":order,"operations":ops,"next_requirement":next_requirement}

def member_tour_phase(person):
    if not person: return {"phase":"NEW IN COUNTRY","tour_day":0,"days_to_deros":None,"progress":0}
    p=soldier_view(person); days=int(p.get("days_in_country") or 0); remaining=p.get("days_to_deros")
    lifecycle=str(p.get("lifecycle_state") or "").upper()
    if lifecycle in {"SEPARATED","ARCHIVED","TOUR COMPLETE"}: phase="TOUR COMPLETE"
    elif p.get("deros_date") and p.get("deros_date")<=date.today(): phase="DEROS PENDING"
    elif remaining is not None and remaining<=14: phase="DEROS PENDING"
    elif remaining is not None and remaining<=60: phase="SHORT TIMER"
    elif days>=120: phase="MID-TOUR"
    elif days>=30: phase="ESTABLISHED"
    else: phase="NEW IN COUNTRY"
    total=max(1,(p["deros_date"]-p["rvn_arrival_date"]).days) if p.get("rvn_arrival_date") and p.get("deros_date") else None
    progress=min(100,round(days/total*100)) if total else min(100,round(days/180*100))
    return {"phase":phase,"tour_day":days+1 if p.get("rvn_arrival_date") else 0,"days_to_deros":remaining,"progress":progress}


def ensure_tour_completion_summary(personnel_id, authority="S-1 PERSONNEL"):
    person=fetch_one("SELECT * FROM personnel WHERE id=%s",(personnel_id,))
    if not person: return None
    stats=member_service_statistics(person)
    tour_no=int(person.get("tour_number") or 1)
    source=f"TOUR-SUMMARY:{personnel_id}:{tour_no}"
    title=f"TOUR OF DUTY COMPLETE — TOUR {tour_no}"
    narrative=(f"{stats.get('days_in_battalion',0)} days in battalion service; "
               f"{stats.get('operations',0)} credited operations; {stats.get('training',0)} training completions; "
               f"{stats.get('qualifications',0)} qualifications; {stats.get('awards',0)} awards; "
               f"{stats.get('rounds_fired',0)} recorded rounds fired.")
    row=fetch_one("""INSERT INTO soldier_tour_book(personnel_id,entry_type,entry_date,title,narrative,source_key)
                     VALUES(%s,'TOUR SUMMARY',CURRENT_DATE,%s,%s,%s)
                     ON CONFLICT(source_key) DO UPDATE SET narrative=EXCLUDED.narrative
                     RETURNING *""",(personnel_id,title,narrative,source))
    emit_state_event('TOUR_COMPLETED',personnel_id=personnel_id,effective_date=date.today(),title=title,
                     narrative=narrative,source_key=source,details=stats)
    return row

def member_assignment_history(personnel_id):
    return fetch_all("""SELECT ah.*,un.display_name AS unit_name FROM assignment_history ah
        LEFT JOIN unit_nodes un ON un.id=ah.unit_node_id WHERE ah.personnel_id=%s
        ORDER BY ah.effective_date ASC,ah.created_at ASC""",(personnel_id,))

def member_service_statistics(person):
    if not person: return {}
    pid=person["id"]; joined=person.get("date_joined") or person.get("created_at")
    if hasattr(joined,"date"): joined=joined.date()
    days=max(0,(date.today()-joined).days)+1 if joined else 0
    ops=credited_operation_count(pid)
    training=int((fetch_one("SELECT COUNT(*) total FROM personnel_training_records WHERE personnel_id=%s AND UPPER(status)='COMPLETE'",(pid,)) or {"total":0}).get("total") or 0)
    leadership=leadership_service_summary(pid)
    quals=int((fetch_one("""SELECT (SELECT COUNT(*) FROM qualifications WHERE personnel_id=%s AND UPPER(status)<>'EXPIRED') +
      (SELECT COUNT(*) FROM personnel_duty_qualifications WHERE personnel_id=%s AND UPPER(status)<>'EXPIRED') AS total""",(pid,pid)) or {"total":0}).get("total") or 0)
    awards=int((fetch_one("SELECT COUNT(*) total FROM personnel_awards WHERE personnel_id=%s",(pid,)) or {"total":0}).get("total") or 0)
    rounds=int((fetch_one("SELECT COALESCE(SUM(rounds_expended),0) total FROM operation_participation WHERE personnel_id=%s",(pid,)) or {"total":0}).get("total") or 0)
    cleanings=int((fetch_one("SELECT COUNT(*) total FROM weapon_maintenance_log WHERE personnel_id=%s AND UPPER(action_type) LIKE '%%CLEAN%%'",(pid,)) or {"total":0}).get("total") or 0)
    formations={(r.get("unit_code"),r.get("platoon"),r.get("squad")) for r in member_assignment_history(pid)}
    tour=member_tour_phase(person)
    return {"days_in_battalion":days,"operations":ops,"training_events":training,"leadership_days":leadership.get("total_days",0),
            "qualifications":quals,"awards":awards,"rounds_fired":rounds,"m16_cleanings":cleanings,
            "formations_served":len(formations),"current_tour_day":tour.get("tour_day",0)}

def member_buddy_history(personnel_id,limit=8):
    return fetch_all("""SELECT p.id,p.rank_code,p.first_name,p.last_name,p.unit_code,COUNT(DISTINCT mine.operation_id) AS shared_operations
      FROM operation_participation mine JOIN operation_participation theirs ON theirs.operation_id=mine.operation_id AND theirs.personnel_id<>mine.personnel_id
      JOIN personnel p ON p.id=theirs.personnel_id WHERE mine.personnel_id=%s
      AND UPPER(COALESCE(mine.attendance_status,'')) IN ('FULL CREDIT','PARTICIPATED','PRESENT','CREDITED','COMPLETE','COMPLETED')
      AND UPPER(COALESCE(theirs.attendance_status,'')) IN ('FULL CREDIT','PARTICIPATED','PRESENT','CREDITED','COMPLETE','COMPLETED')
      GROUP BY p.id,p.rank_code,p.first_name,p.last_name,p.unit_code ORDER BY shared_operations DESC,p.last_name,p.first_name LIMIT %s""",(personnel_id,limit))

def member_weekly_report(person):
    if not person: return {}
    pid=person["id"]; p=soldier_view(person); readiness=soldier_readiness(p); weapon=current_weapon_for(p); activity=inactivity_snapshot(p)
    qualrow=fetch_one("""SELECT (SELECT COUNT(*) FROM qualifications WHERE personnel_id=%s AND UPPER(status)<>'EXPIRED') +
      (SELECT COUNT(*) FROM personnel_duty_qualifications WHERE personnel_id=%s AND UPPER(status)<>'EXPIRED') AS current_count,
      (SELECT COUNT(*) FROM qualifications WHERE personnel_id=%s AND expires_at BETWEEN CURRENT_DATE AND CURRENT_DATE+14) +
      (SELECT COUNT(*) FROM personnel_duty_qualifications WHERE personnel_id=%s AND expiration_date BETWEEN CURRENT_DATE AND CURRENT_DATE+14) AS expiring_count""",(pid,pid,pid,pid)) or {"current_count":0,"expiring_count":0}
    ops=int((fetch_one("""SELECT COUNT(DISTINCT operation_id) total FROM operation_participation WHERE personnel_id=%s AND credited_at>=NOW()-INTERVAL '7 days'
      AND UPPER(COALESCE(attendance_status,'')) IN ('FULL CREDIT','PARTICIPATED','PRESENT','CREDITED','COMPLETE','COMPLETED')""",(pid,)) or {"total":0}).get("total") or 0)
    readiness_pct=readiness.get("percent") if isinstance(readiness,dict) else None
    if readiness_pct is None: readiness_pct=p.get("readiness_percent",0)
    return {"period_start":date.today()-timedelta(days=6),"period_end":date.today(),"activity":activity.get("state") or activity.get("label") or "CURRENT",
            "readiness":readiness_pct,"weapon":weapon.get("serviceability_status") if weapon else "NO WEAPON ISSUED",
            "weapon_fouling":weapon.get("fouling_status") if weapon else None,"qual_current":int(qualrow.get("current_count") or 0),
            "qual_expiring":int(qualrow.get("expiring_count") or 0),"operations_this_week":ops}

def member_formation_snapshot(person,unit_type):
    if not person or not person.get("unit_node_id"): return None
    target=unit_node(person["unit_node_id"]); desired=unit_type.lower()
    while target and str(target.get("unit_type") or "").lower()!=desired:
        target=unit_node(target["parent_id"]) if target.get("parent_id") else None
    if not target: return None
    ids=unit_descendant_ids(target["id"]) or [target["id"]]
    roster=fetch_all("""SELECT id,rank_code,first_name,last_name,mos_code,duty_position,unit_code,platoon,squad,readiness_percent
      FROM personnel WHERE unit_node_id=ANY(%s) AND separated_at IS NULL AND archived=FALSE ORDER BY last_name,first_name""",(ids,))
    leaders=fetch_all("""SELECT pa.appointment_code,ac.appointment_name,p.rank_code,p.first_name,p.last_name FROM personnel_appointments pa
      JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code JOIN personnel p ON p.id=pa.personnel_id
      WHERE pa.unit_node_id=%s AND pa.is_current=TRUE ORDER BY ac.sort_order""",(target["id"],))
    return {"unit":target,"roster":roster,"leaders":leaders,"cohesion":unit_cohesion(target["id"]),"experience":unit_experience(target["id"]),"identity":unit_identity(target["id"])}

def member_career_milestones(person):
    if not person: return []
    pid=person["id"]; out=[]; mos=sync_mos_proficiency(person); exp=member_combat_experience(pid)
    if mos and mos.get("next_requirement")!="MAXIMUM PROFICIENCY": out.append({"title":f"Advance beyond {mos.get('level')}","detail":mos.get("next_requirement"),"section":"MOS PROFICIENCY"})
    if exp.get("order",0)<4: out.append({"title":f"Advance beyond {exp.get('level')}","detail":exp.get("next_requirement"),"section":"FIELD EXPERIENCE"})
    elig=promotion_eligibility(person)
    if elig:
        e=elig[0]
        if e.get("target"):
            unmet=[x for x in e.get("requirements",[]) if not x.get("complete")]
            detail=" • ".join(str(x.get("label") or x.get("detail") or "") for x in unmet[:2]) or e.get("status","UNDER REVIEW")
            out.append({"title":f"Promotion consideration — {e.get('target')}","detail":detail,"section":"CAREER"})
    insp=weapon_inspection_status(pid)
    if insp: out.append({"title":"M16 inspection overdue" if insp.get("overdue") else "Next M16 inspection","detail":"Report to S-4 for inspection." if insp.get("overdue") else f"{insp.get('days')} day(s) • due {insp.get('due')}","section":"S-4"})
    return out[:5]


SERVICE_GOALS = {
    "TEAM_LEADER": {"label":"Become Team Leader","kind":"leadership"},
    "SENIOR_RIFLEMAN": {"label":"Earn Senior Rifleman","kind":"mos"},
    "RADIO_QUAL": {"label":"Complete Radio Qualification","kind":"qualification"},
    "READINESS_90": {"label":"Reach 90% Readiness","kind":"readiness"},
    "FIRST_TOUR": {"label":"Complete First Tour","kind":"tour"},
}

def member_formation_legacy(personnel_id):
    """Summarize each distinct historical company/platoon/squad assignment."""
    rows=member_assignment_history(personnel_id)
    groups={}
    for r in rows:
        key=(r.get("unit_code"),r.get("platoon"),r.get("squad"),r.get("unit_node_id"))
        g=groups.setdefault(key,{
            "unit_code":r.get("unit_code"),"platoon":r.get("platoon"),"squad":r.get("squad"),
            "unit_node_id":r.get("unit_node_id"),"unit_name":r.get("unit_name"),
            "first_date":r.get("effective_date"),"last_date":r.get("ended_date"),"days":0,"operations":0
        })
        start=r.get("effective_date") or date.today()
        end=r.get("ended_date") or date.today()
        if hasattr(start,"date"): start=start.date()
        if hasattr(end,"date"): end=end.date()
        if end>=start:
            g["days"] += (end-start).days + 1
        if g["first_date"] is None or (r.get("effective_date") and r.get("effective_date")<g["first_date"]):
            g["first_date"]=r.get("effective_date")
        if r.get("ended_date") is None:
            g["last_date"]=None
        elif g["last_date"] is not None and r.get("ended_date")>g["last_date"]:
            g["last_date"]=r.get("ended_date")
    # operation counts by matching the formation fields recorded on participation
    for g in groups.values():
        if g.get("unit_node_id"):
            row=fetch_one("""SELECT COUNT(DISTINCT op.operation_id) total
              FROM operation_participation op
              WHERE op.personnel_id=%s AND op.unit_node_id=%s
                AND UPPER(COALESCE(op.attendance_status,'')) IN ('PARTICIPATED','PRESENT','CREDITED','COMPLETE','COMPLETED','FULL CREDIT')""",
              (personnel_id,g["unit_node_id"]))
        else:
            row={"total":0}
        g["operations"]=int((row or {"total":0}).get("total") or 0)
    out=list(groups.values())
    out.sort(key=lambda x:(x["first_date"] or date.min))
    return out

def member_promotion_readiness(person):
    """Convert promotion requirements into member-facing category percentages."""
    result={"target":None,"overall":0,"time":0,"training":0,"operations":0,"leadership":0,"readiness":0,"status":"NOT TRACKED"}
    elig=promotion_eligibility(person)
    if not elig:
        return result
    row=elig[0]
    result["target"]=row.get("target")
    result["status"]=row.get("status") or "UNDER REVIEW"
    reqs=row.get("requirements") or []
    cats={"time":[],"training":[],"operations":[],"leadership":[],"readiness":[]}
    for r in reqs:
        label=str(r.get("label") or r.get("detail") or "").lower()
        pct=100 if r.get("complete") else int(r.get("progress_percent") or 0)
        if any(x in label for x in ["day","time","tig","service"]): cats["time"].append(pct)
        elif any(x in label for x in ["school","training","qualif"]): cats["training"].append(pct)
        elif any(x in label for x in ["operation","event","attendance"]): cats["operations"].append(pct)
        elif any(x in label for x in ["leader","leadership","team leader","squad leader"]): cats["leadership"].append(pct)
        elif "readiness" in label: cats["readiness"].append(pct)
    # Backfill directly from live data when a category is absent.
    result["time"]=round(sum(cats["time"])/len(cats["time"])) if cats["time"] else min(100,round((int((soldier_view(person).get("days_in_country") or 0))/90)*100))
    result["training"]=round(sum(cats["training"])/len(cats["training"])) if cats["training"] else min(100,round((member_service_statistics(person).get("qualifications",0)/3)*100))
    result["operations"]=round(sum(cats["operations"])/len(cats["operations"])) if cats["operations"] else min(100,round((credited_operation_count(person["id"])/8)*100))
    result["leadership"]=round(sum(cats["leadership"])/len(cats["leadership"])) if cats["leadership"] else min(100,round((leadership_service_summary(person["id"]).get("total_days",0)/30)*100))
    live_readiness=int((soldier_readiness(soldier_view(person)) or {}).get("percent") or person.get("readiness_percent") or 0)
    result["readiness"]=round(sum(cats["readiness"])/len(cats["readiness"])) if cats["readiness"] else live_readiness
    result["overall"]=round(sum([result["time"],result["training"],result["operations"],result["leadership"],result["readiness"]])/5)
    return result

def unit_identity(unit_node_id):
    return fetch_one("""SELECT uis.*,un.display_name FROM unit_identity_settings uis
      JOIN unit_nodes un ON un.id=uis.unit_node_id WHERE uis.unit_node_id=%s""",(unit_node_id,)) or \
           {"unit_node_id":unit_node_id,"nickname":None,"call_sign":None}

def goal_progress(person, goal_code):
    pid=person["id"]; p=soldier_view(person)
    if goal_code=="TEAM_LEADER":
        held=fetch_one("""SELECT COUNT(*) total FROM personnel_appointments
          WHERE personnel_id=%s AND appointment_code IN ('TL','FTL','TEAM_LEADER')""",(pid,))
        if int((held or {"total":0}).get("total") or 0)>0: return {"percent":100,"detail":"Team Leader appointment earned.","complete":True}
        score=combat_leadership_score(pid)
        pct=min(95,round((int(score.get("score") or 0)/70)*100))
        return {"percent":pct,"detail":"Build leadership service, training, and operational experience.","complete":False}
    if goal_code=="SENIOR_RIFLEMAN":
        mos=sync_mos_proficiency(person) or {}
        level=str(mos.get("level") or mos.get("proficiency_level") or "")
        order={"RIFLEMAN III":25,"RIFLEMAN II":50,"RIFLEMAN I":75,"SENIOR RIFLEMAN":100}.get(level.upper(),0)
        return {"percent":order,"detail":mos.get("next_requirement") or "Continue Rifleman progression.","complete":order>=100}
    if goal_code=="RADIO_QUAL":
        q=fetch_one("""SELECT COUNT(*) total FROM personnel_duty_qualifications pdq
          JOIN duty_qualification_types dqt ON dqt.id=pdq.qualification_type_id
          WHERE pdq.personnel_id=%s AND UPPER(pdq.status)<>'EXPIRED'
          AND (UPPER(dqt.code) LIKE '%%PRC%%' OR UPPER(dqt.display_name) LIKE '%%RADIO%%')""",(pid,))
        done=int((q or {"total":0}).get("total") or 0)>0
        return {"percent":100 if done else 0,"detail":"Radio qualification current." if done else "Complete the AN/PRC-25 Radio qualification.","complete":done}
    if goal_code=="READINESS_90":
        rd=int((soldier_readiness(p) or {}).get("percent") or p.get("readiness_percent") or 0)
        return {"percent":min(100,round(rd/90*100)),"detail":f"Current readiness {rd}% / goal 90%.","complete":rd>=90}
    if goal_code=="FIRST_TOUR":
        t=member_tour_phase(person)
        if t.get("phase")=="DEROS PENDING" and t.get("days_to_deros") is not None and t.get("days_to_deros")<=0:
            return {"percent":100,"detail":"First tour complete.","complete":True}
        return {"percent":int(t.get("progress") or 0),"detail":f"Tour day {t.get('tour_day')}.","complete":False}
    return {"percent":0,"detail":"Goal tracking unavailable.","complete":False}

def member_service_goals(person):
    rows=fetch_all("SELECT * FROM member_service_goals WHERE personnel_id=%s AND is_active=TRUE ORDER BY created_at",(person["id"],))
    out=[]
    for r in rows:
        gp=goal_progress(person,r["goal_code"]); r=dict(r); r.update(gp); out.append(r)
        if gp["complete"] and not r.get("completed_at"):
            execute("UPDATE member_service_goals SET completed_at=NOW() WHERE id=%s",(r["id"],))
    return out

def where_you_stand(person):
    p=soldier_view(person); pid=p["id"]; tour=member_tour_phase(p); promo=member_promotion_readiness(p); mos=sync_mos_proficiency(p) or {}; exp=member_combat_experience(pid)
    snap=member_formation_snapshot(p,"squad")
    cohesion=(snap.get("cohesion") or {}).get("cohesion") if snap else None
    return {
      "tour":int(tour.get("progress") or 0),
      "readiness":int((soldier_readiness(p) or {}).get("percent") or p.get("readiness_percent") or 0),
      "promotion_readiness":promo.get("overall",0),
      "mos_proficiency":mos.get("level") or mos.get("proficiency_level") or p.get("mos_code"),
      "experience":exp.get("level"),
      "squad_cohesion":cohesion
    }

def member_career_context(person):
    if not person: return {}
    p=soldier_view(person); pid=p["id"]
    leadership_service=leadership_service_summary(pid)
    leadership_score=combat_leadership_score(pid)
    return {"career_stats":member_service_statistics(p),"combat_experience":member_combat_experience(pid),"career_tour":member_tour_phase(p),
      "career_milestones":member_career_milestones(p),"leadership_service":leadership_service,"leadership_score":leadership_score,
      "assignment_history_full":member_assignment_history(pid),"buddy_history":member_buddy_history(pid),"weekly_report":member_weekly_report(p),
      "squad_snapshot":member_formation_snapshot(p,"squad"),
      "where_you_stand":{"tour":member_tour_phase(p).get("progress",0),
                         "readiness":int(p.get("readiness_percent") or 0),
                         "promotion_readiness":0,
                         "mos_proficiency":(current_mos_proficiency(p) or {}).get("proficiency_level") or p.get("mos_code"),
                         "experience":member_combat_experience(pid).get("level"),
                         "squad_cohesion":None}}

def qualification_card_record(personnel_id,source,record_id):
    if source=="duty":
        row=fetch_one("""SELECT pdq.id,pdq.status,pdq.qualified_date AS earned_at,pdq.expiration_date AS expires_at,pdq.score_text,pdq.remarks,
          dqt.code AS qualification_code,dqt.display_name AS qualification_name,i.rank_code AS instructor_rank,i.first_name AS instructor_first,i.last_name AS instructor_last
          FROM personnel_duty_qualifications pdq JOIN duty_qualification_types dqt ON dqt.id=pdq.qualification_type_id
          LEFT JOIN personnel i ON i.id=pdq.instructor_personnel_id WHERE pdq.id=%s AND pdq.personnel_id=%s""",(record_id,personnel_id))
        if row:
            row["approving_instructor_display"]=" ".join(x for x in [row.get("instructor_rank"),row.get("instructor_first"),row.get("instructor_last")] if x) or "BATTALION TRAINING OFFICE"
        return row
    row=fetch_one("SELECT * FROM qualifications WHERE id=%s AND personnel_id=%s",(record_id,personnel_id))
    if row: row["approving_instructor_display"]=row.get("approving_instructor") or "BATTALION TRAINING OFFICE"
    return row

def linked_personnel():
    cache_key=(session.get("personnel_id"),session.get("user_id"))
    if getattr(g,"_linked_personnel_key",None)==cache_key and hasattr(g,"_linked_personnel_value"):
        return g._linked_personnel_value
    if session.get("personnel_id"):
        value=fetch_one("SELECT * FROM personnel WHERE id=%s", (session["personnel_id"],))
    elif not session.get("user_id"):
        value=None
    else:
        value=fetch_one(
            """
            SELECT p.* FROM personnel p
            JOIN user_personnel_links upl ON upl.personnel_id=p.id
            WHERE upl.user_id=%s
            """,
            (session["user_id"],),
        )
    g._linked_personnel_key=cache_key
    g._linked_personnel_value=value
    return value


def soldier_view(personnel: dict | None) -> dict | None:
    """Presentation-only derived tour fields; stored records remain untouched."""
    if not personnel:
        return None
    record = dict(personnel)
    arrival = record.get("rvn_arrival_date")
    deros = record.get("deros_date")
    record["days_in_country"] = max((date.today() - arrival).days, 0) if arrival else None
    record["days_to_deros"] = max((deros - date.today()).days, 0) if deros else None

    loa_start = record.get("loa_start_date")
    loa_expected = record.get("loa_expected_return_date")
    loa_actual = record.get("loa_actual_return_date")
    duty_status = str(record.get("duty_status") or "PRESENT FOR DUTY").upper()
    record["loa_active"] = duty_status == "LEAVE"
    if loa_start:
        end_date = loa_actual or date.today()
        record["loa_days_absent"] = max((end_date - loa_start).days, 0)
    else:
        record["loa_days_absent"] = None
    record["loa_days_remaining"] = max((loa_expected - date.today()).days, 0) if record["loa_active"] and loa_expected else None
    if record["loa_active"]:
        record["personnel_status_label"] = "ON AUTHORIZED LEAVE"
        record["personnel_status_note"] = f"EXPECTED RETURN {loa_expected}" if loa_expected else "RETURN DATE NOT FILED"
    elif loa_actual and loa_start:
        record["personnel_status_label"] = "RETURNED TO DUTY"
        record["personnel_status_note"] = f"LAST LEAVE {loa_start} TO {loa_actual}"
    else:
        record["personnel_status_label"] = record.get("duty_status") or "PRESENT FOR DUTY"
        record["personnel_status_note"] = record.get("readiness_status") or "ACTIVE IN BATTALION RECORDS"

    days = record["days_in_country"]
    remaining = record["days_to_deros"]
    if deros and deros <= date.today():
        phase = "TOUR COMPLETE"
    elif remaining is not None and remaining <= 30:
        phase = "SHORT-TIMER"
    elif days is None:
        phase = "NEW ARRIVAL"
    elif days < 30:
        phase = "NEW ARRIVAL"
    elif days < 90:
        phase = "IN COUNTRY"
    elif days < 180:
        phase = "FIELD EXPERIENCED"
    else:
        phase = "VETERAN"
    record["tour_phase"] = phase
    return record


def voice_record_for(personnel: dict | None):
    if not personnel:
        return None, None
    discord = fetch_one(
        """
        SELECT dm.guild_id, dm.discord_user_id, dm.username, dm.display_name, dm.updated_at AS last_seen_at
        FROM website_member_links wml
        JOIN discord_members dm ON dm.guild_id=wml.guild_id AND dm.discord_user_id=wml.discord_user_id
        WHERE wml.personnel_id=%s
        LIMIT 1
        """,
        (str(personnel["id"]),),
    )
    voice = None
    if discord:
        voice = fetch_one(
            """
            SELECT COUNT(*) AS sessions,
                   COALESCE(SUM(duration_seconds),0) AS total_seconds,
                   MAX(ended_at) AS last_voice_activity
            FROM voice_sessions
            WHERE guild_id=%s AND discord_user_id=%s
            """,
            (discord["guild_id"], discord["discord_user_id"]),
        )
    return discord, voice


def public_scheduled_operations(limit=5):
    """Authoritative public schedule with legacy-schema fallback.

    Website S-3 operations are primary. The Clerk event mirror is supplemental.
    This helper is deliberately defensive because the public homepage must survive
    partially migrated/older Railway databases.
    """
    try:
        return fetch_all("""
            WITH website_schedule AS (
              SELECT o.id::text AS source_id,o.id AS operation_id,o.title,
                     COALESCE(o.operation_type,'OFFICIAL OPERATION') AS event_type,
                     o.start_at AS starts_at,
                     (o.start_at + make_interval(mins => COALESCE(o.duration_minutes,90))) AS ends_at,
                     'WEBSITE' AS schedule_source,COALESCE(o.area_of_operations,o.location) AS area_of_operations,
                     o.operation_number
              FROM operations o
              WHERE o.start_at IS NOT NULL
                AND UPPER(COALESCE(o.status,'')) IN ('SCHEDULED','ACTIVE')
                AND UPPER(COALESCE(o.lifecycle_status,'PUBLISHED')) NOT IN ('CLOSED','COMPLETED','CANCELLED','CANCELED','ARCHIVED')
                AND (o.start_at + make_interval(mins => COALESCE(o.duration_minutes,90))) > NOW()
            ), clerk_only AS (
              SELECT e.id::text AS source_id,e.operation_id,e.title,
                     COALESCE(e.event_type,'BATTALION DUTY') AS event_type,e.starts_at,e.ends_at,
                     'CLERK' AS schedule_source,NULL::text AS area_of_operations,NULL::text AS operation_number
              FROM battalion_events e
              WHERE UPPER(COALESCE(e.status,'')) IN ('SCHEDULED','ACTIVE') AND e.ends_at>NOW()
                AND (e.operation_id IS NULL OR NOT EXISTS (SELECT 1 FROM website_schedule w WHERE w.operation_id=e.operation_id))
            )
            SELECT source_id,operation_id,title,event_type,starts_at,ends_at,schedule_source,area_of_operations,operation_number
            FROM (SELECT * FROM website_schedule UNION ALL SELECT * FROM clerk_only) schedule
            ORDER BY starts_at ASC LIMIT %s
        """,(int(limit),))
    except Exception:
        log.exception("Full public schedule query failed; using compatibility schedule query")

    # Compatibility path uses only long-established operation columns. It intentionally
    # avoids newer lifecycle/AO/Clerk-link fields and still keeps website scheduling visible.
    try:
        return fetch_all("""
            SELECT o.id::text AS source_id,o.id AS operation_id,o.title,
                   'OFFICIAL OPERATION'::text AS event_type,o.start_at AS starts_at,
                   (o.start_at + INTERVAL '90 minutes') AS ends_at,
                   'WEBSITE'::text AS schedule_source,NULL::text AS area_of_operations,
                   NULL::text AS operation_number
            FROM operations o
            WHERE o.start_at IS NOT NULL
              AND UPPER(COALESCE(o.status,'')) IN ('SCHEDULED','ACTIVE')
              AND (o.start_at + INTERVAL '90 minutes') > NOW()
            ORDER BY o.start_at ASC LIMIT %s
        """,(int(limit),))
    except Exception:
        log.exception("Compatibility public schedule query also failed")
        return []



def _public_home_safe(label, loader, default):
    """Public homepage data must never be allowed to take down the recruiting front door."""
    try:
        value = loader()
        return default if value is None else value
    except Exception:
        log.exception("Public homepage optional data failed: %s", label)
        return default


def public_recruiting_snapshot():
    """Aggregate only public-safe recruiting pipeline counts."""
    row = _public_home_safe("recruiting pipeline", lambda: fetch_one("""
        SELECT
          COUNT(*) FILTER (WHERE status NOT IN ('DENIED','CLOSED','ENLISTED')) AS applications_pending,
          COUNT(*) FILTER (WHERE status IN ('SUBMITTED','DISCORD_VERIFIED','PENDING_COMMAND','MORE_INFO_REQUIRED')) AS command_review,
          COUNT(*) FILTER (WHERE status IN ('APPROVED_AWAITING_DISCORD','REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING')) AS processing
        FROM recruiting_cases
    """), {}) or {}
    ready = 0
    try:
        rows = replacement_detachment_rows()
        ready = sum(1 for x in rows if str(x.get("replacement_stage") or "").upper() == "READY FOR ASSIGNMENT")
    except Exception:
        log.exception("Public ready-for-assignment count unavailable")
    return {
        "applications_pending": int(row.get("applications_pending") or 0),
        "command_review": int(row.get("command_review") or 0),
        "processing": int(row.get("processing") or 0),
        "ready_assignment": int(ready or 0),
    }


def public_company_strength():
    """Current company strength from structured unit assignments, with legacy fallback.
    Authorized strength is shown only when a real unit_billets total exists.
    """
    try:
        nodes=fetch_all("""SELECT id,parent_id,unit_code,display_name,unit_type,sort_order
                           FROM unit_nodes WHERE is_active=TRUE ORDER BY sort_order,display_name""") or []
        roster=public_active_roster()
        node_map,_=_public_node_maps(nodes)
        counts={}
        company_nodes=[n for n in nodes if str(n.get("unit_type") or "").lower()=="company"]
        for co in company_nodes:
            counts[str(co.get("id"))]={"company":str(co.get("display_name") or co.get("unit_code") or "COMPANY").upper(),
                                       "unit_code":co.get("unit_code"),"current":0,"authorized":0,
                                       "sort_order":int(co.get("sort_order") or 0)}
        for person in roster:
            company=_public_ancestor_node(person.get("unit_node_id"),"Company",node_map)
            if not company:
                company=_public_legacy_node_for_person(person,"Company",nodes)
            if company and str(company.get("id")) in counts:
                counts[str(company.get("id"))]["current"]+=1

        # Existing billet catalog uses legacy display unit codes; map it back to companies.
        try:
            billet_rows=fetch_all("""SELECT unit_code,COALESCE(SUM(authorized_strength),0)::int AS authorized
                                     FROM unit_billets WHERE is_active=TRUE GROUP BY unit_code""") or []
            for b in billet_rows:
                u=str(b.get("unit_code") or "").upper()
                target=None
                if u.startswith("A/"): target=next((x for x in counts.values() if str(x.get("unit_code") or "").upper()=="A-1-5"),None)
                elif u.startswith("B/"): target=next((x for x in counts.values() if str(x.get("unit_code") or "").upper()=="B-1-5"),None)
                elif u.startswith("C/"): target=next((x for x in counts.values() if str(x.get("unit_code") or "").upper()=="C-1-5"),None)
                elif u.startswith("HHC") or u.startswith("HQ"): target=next((x for x in counts.values() if str(x.get("unit_code") or "").upper()=="HHC-1-5"),None)
                if target:
                    target["authorized"]+=int(b.get("authorized") or 0)
        except Exception:
            log.exception("Public authorized company strength unavailable")

        return sorted(counts.values(),key=lambda x:(x.get("sort_order",0),x.get("company","")))
    except Exception:
        log.exception("Structured public company strength unavailable; using legacy compatibility")
        rows=_public_home_safe("legacy company strength",lambda: fetch_all("""
            SELECT COALESCE(unit_code,'UNASSIGNED') company,COUNT(*)::int current,0::int authorized
            FROM personnel WHERE separated_at IS NULL AND COALESCE(archived,FALSE)=FALSE
            GROUP BY COALESCE(unit_code,'UNASSIGNED') ORDER BY company
        """),[])
        return rows


def public_readiness_snapshot():
    active = _public_home_safe(
        "active strength for readiness",
        lambda: int(((fetch_one("SELECT COUNT(*) total FROM personnel WHERE separated_at IS NULL AND archived=FALSE") or {}).get("total") or 0)),
        0,
    )
    combat = _public_home_safe(
        "combat readiness",
        lambda: int(round(float(((fetch_one("SELECT COALESCE(AVG(readiness_percent),0) pct FROM personnel WHERE separated_at IS NULL AND archived=FALSE") or {}).get("pct") or 0)))),
        0,
    )
    weapon = _public_home_safe(
        "weapon readiness",
        lambda: int(round(float(((fetch_one("""
            SELECT COALESCE(AVG(w.condition_percent),0) pct
            FROM weapon_issue_history h JOIN weapon_inventory w ON w.id=h.weapon_id
            WHERE h.is_current=TRUE
        """) or {}).get("pct") or 0)))),
        0,
    )
    equipment = _public_home_safe(
        "equipment readiness",
        lambda: int(round(float(((fetch_one("""
            SELECT COALESCE(AVG(condition_percent),0) pct
            FROM equipment_issues
            WHERE UPPER(COALESCE(status,'')) NOT IN ('TURNED IN','LOST','RETURNED')
        """) or {}).get("pct") or 0)))),
        weapon,
    )
    qualified = _public_home_safe(
        "qualification rate",
        lambda: int(round(100 * int(((fetch_one("""
            SELECT COUNT(DISTINCT personnel_id) total FROM qualifications
            WHERE UPPER(COALESCE(status,''))='CURRENT'
        """) or {}).get("total") or 0)) / max(1, active))),
        0,
    )
    trained = _public_home_safe(
        "training completion",
        lambda: int(round(100 * int(((fetch_one("""
            SELECT COUNT(DISTINCT personnel_id) total FROM personnel_training_records
            WHERE UPPER(COALESCE(status,''))='COMPLETE' OR completed_at IS NOT NULL
        """) or {}).get("total") or 0)) / max(1, active))),
        0,
    )
    return {
        "weapon": max(0,min(100,weapon)),
        "equipment": max(0,min(100,equipment)),
        "qualification": max(0,min(100,qualified)),
        "training": max(0,min(100,trained)),
        "combat": max(0,min(100,combat)),
    }


def public_recent_achievements():
    """Public aggregate counts only; no command notes or private records."""
    return {
        "promotions": _public_home_safe("recent promotions", lambda: int(((fetch_one("""
            SELECT COUNT(*) total FROM promotion_history
            WHERE effective_date >= CURRENT_DATE - 30
        """) or {}).get("total") or 0)), 0),
        "awards": _public_home_safe("recent awards", lambda: int(((fetch_one("""
            SELECT COUNT(*) total FROM personnel_ribbons
            WHERE earned_at >= CURRENT_DATE - INTERVAL '30 days'
        """) or {}).get("total") or 0)), 0),
        "schools": _public_home_safe("recent schools", lambda: int(((fetch_one("""
            SELECT COUNT(*) total FROM personnel_training_records
            WHERE completed_at >= CURRENT_DATE - INTERVAL '30 days'
        """) or {}).get("total") or 0)), 0),
        "qualifications": _public_home_safe("recent qualifications", lambda: int(((fetch_one("""
            SELECT COUNT(*) total FROM qualifications
            WHERE earned_at >= CURRENT_DATE - INTERVAL '30 days'
        """) or {}).get("total") or 0)), 0),
        "operations": _public_home_safe("recent completed operations", lambda: int(((fetch_one("""
            SELECT COUNT(*) total FROM operations
            WHERE COALESCE(completed_at,operation_date::timestamptz) >= NOW() - INTERVAL '30 days'
              AND UPPER(COALESCE(status,'')) IN ('CLOSED','COMPLETE','COMPLETED')
        """) or {}).get("total") or 0)), 0),
    }


def public_award_preview(limit=4):
    return _public_home_safe("award preview", lambda: fetch_all("""
        SELECT ribbon_code,ribbon_name,image_filename,requirement_text
        FROM ribbon_catalog WHERE is_active=TRUE
        ORDER BY sort_order LIMIT %s
    """, (int(limit),)), [])


def public_strength_snapshot():
    active = _public_home_safe("public active strength", lambda: int(((fetch_one("""
        SELECT COUNT(*) total FROM personnel
        WHERE separated_at IS NULL AND COALESCE(archived,FALSE)=FALSE
    """) or {}).get("total") or 0)), 0)
    assigned = _public_home_safe("public assigned strength", lambda: int(((fetch_one("""
        SELECT COUNT(*) total FROM personnel
        WHERE separated_at IS NULL AND COALESCE(archived,FALSE)=FALSE
          AND (
            UPPER(COALESCE(field_status,''))='ASSIGNED'
            OR (
              unit_node_id IS NOT NULL
              AND UPPER(COALESCE(unit_code,'')) NOT IN ('REPLACEMENT DETACHMENT','REPLACEMENT','UNASSIGNED')
            )
          )
    """) or {}).get("total") or 0)), active)
    combat_ready = _public_home_safe("public combat ready strength", lambda: int(((fetch_one("""
        SELECT COUNT(*) total FROM personnel
        WHERE separated_at IS NULL AND COALESCE(archived,FALSE)=FALSE
          AND COALESCE(readiness_percent,0)>=80
    """) or {}).get("total") or 0)), 0)
    return {"assigned":assigned,"in_country":active,"combat_ready":combat_ready}


def public_front_snapshot():
    return {
        "recruiting": public_recruiting_snapshot(),
        "strength": public_strength_snapshot(),
        "companies": public_company_strength(),
        "readiness": public_readiness_snapshot(),
        "achievements": public_recent_achievements(),
        "awards": public_award_preview(4),
    }


def public_headquarters_feed(limit=8):
    """Public-safe proof-of-life feed assembled from existing records.

    Presentation only: this never writes records and it never exposes staff remarks,
    citations, private notes, Discord IDs, or credentials. Each source fails soft so
    one legacy table shape cannot take down the public homepage.
    """
    events=[]
    try:
        for r in fetch_all("""
            SELECT ph.effective_date AS event_date,ph.new_rank_code,p.last_name
            FROM promotion_history ph JOIN personnel p ON p.id=ph.personnel_id
            WHERE p.separated_at IS NULL AND COALESCE(p.archived,FALSE)=FALSE
            ORDER BY ph.effective_date DESC,ph.created_at DESC LIMIT 8
        """):
            events.append({"date":r.get("event_date"),"kind":"PROMOTION",
                           "text":f"{r.get('new_rank_code') or 'SOLDIER'} {(r.get('last_name') or '').upper()} PROMOTED"})
    except Exception:
        log.exception("Public HQ feed promotion source failed")
    try:
        for r in fetch_all("""
            SELECT pa.award_date AS event_date,pa.award_name,p.rank_code,p.last_name
            FROM personnel_awards pa JOIN personnel p ON p.id=pa.personnel_id
            WHERE p.separated_at IS NULL AND COALESCE(p.archived,FALSE)=FALSE
            ORDER BY pa.award_date DESC,pa.id DESC LIMIT 8
        """):
            who=f"{r.get('rank_code') or ''} {(r.get('last_name') or '').upper()}".strip()
            events.append({"date":r.get("event_date"),"kind":"AWARD",
                           "text":f"{r.get('award_name') or 'AWARD'} FILED FOR {who}"})
    except Exception:
        log.exception("Public HQ feed award source failed")
    try:
        for r in fetch_all("""
            SELECT ah.effective_date AS event_date,p.rank_code,p.last_name,ah.unit_code,ah.platoon,ah.squad,ah.duty_position
            FROM assignment_history ah JOIN personnel p ON p.id=ah.personnel_id
            WHERE ah.is_current=TRUE AND p.separated_at IS NULL AND COALESCE(p.archived,FALSE)=FALSE
            ORDER BY ah.effective_date DESC,ah.created_at DESC LIMIT 8
        """):
            who=f"{r.get('rank_code') or ''} {(r.get('last_name') or '').upper()}".strip()
            where=' / '.join([str(x) for x in [r.get('unit_code'),r.get('platoon'),r.get('squad')] if x])
            events.append({"date":r.get("event_date"),"kind":"ASSIGNMENT",
                           "text":f"{who} ASSIGNED {where or (r.get('duty_position') or 'TO FORMATION')}"})
    except Exception:
        log.exception("Public HQ feed assignment source failed")
    try:
        for r in fetch_all("""
            SELECT COALESCE(completed_at,operation_date::timestamptz,start_at,created_at) AS event_date,
                   operation_number,operation_code,title,result
            FROM operations
            WHERE completed_at IS NOT NULL OR UPPER(COALESCE(status,'')) IN ('CLOSED','COMPLETE','COMPLETED','AAR FILED')
            ORDER BY COALESCE(completed_at,operation_date::timestamptz,start_at,created_at) DESC NULLS LAST LIMIT 8
        """):
            ref=r.get('operation_number') or r.get('operation_code') or 'OPERATION'
            title=r.get('title') or 'OFFICIAL OPERATION'
            events.append({"date":r.get("event_date"),"kind":"OPERATION",
                           "text":f"{ref} — {title} COMPLETED"})
    except Exception:
        log.exception("Public HQ feed operation source failed")

    def _key(e):
        v=e.get('date')
        if isinstance(v, datetime): return v.replace(tzinfo=None) if v.tzinfo else v
        if isinstance(v, date): return datetime.combine(v, datetime.min.time())
        try: return datetime.fromisoformat(str(v).replace('Z','+00:00')).replace(tzinfo=None)
        except Exception: return datetime.min
    events.sort(key=_key,reverse=True)
    return events[:max(1,int(limit or 8))]


@app.get("/")
def home():
    if not database_ready():
        return render_template("setup.html")

    scheduled_operations = _public_home_safe("scheduled operations", lambda: public_scheduled_operations(5), [])
    incoming_orders = _public_home_safe(
        "incoming orders",
        lambda: fetch_all("""SELECT pd.document_number,pd.title,pd.effective_date,p.rank_code,p.last_name
                           FROM personnel_documents pd LEFT JOIN personnel p ON p.id=pd.personnel_id
                           ORDER BY pd.created_at DESC LIMIT 4"""),
        [],
    )
    newest_arrivals = _public_home_safe(
        "newest arrivals",
        lambda: fetch_all("""SELECT rank_code,first_name,last_name,unit_code,platoon,squad,date_joined,duty_status
                           FROM personnel WHERE separated_at IS NULL AND archived=FALSE
                           ORDER BY COALESCE(roster_entered_at,created_at) DESC LIMIT 5"""),
        [],
    )
    public_strength = _public_home_safe(
        "public strength",
        lambda: int(((fetch_one("SELECT COUNT(*) AS total FROM personnel WHERE separated_at IS NULL AND archived=FALSE") or {}).get("total") or 0)),
        0,
    )
    operations_completed = _public_home_safe(
        "operations completed",
        lambda: int(((fetch_one("SELECT COUNT(*) AS total FROM operations WHERE UPPER(COALESCE(status,'')) IN ('CLOSED','COMPLETE','COMPLETED')") or {}).get("total") or 0)),
        0,
    )
    recruiting_needs = _public_home_safe("recruiting needs", mos_recruiting_needs, [])
    snapshot = _public_home_safe("public battalion snapshot", public_front_snapshot, {
        "recruiting":{"applications_pending":0,"command_review":0,"processing":0,"ready_assignment":0},
        "strength":{"assigned":0,"in_country":0,"combat_ready":0},
        "companies":[],"readiness":{"weapon":0,"equipment":0,"qualification":0,"training":0,"combat":0},
        "achievements":{"promotions":0,"awards":0,"schools":0,"qualifications":0,"operations":0},
        "awards":[],
    })
    headquarters_feed = _public_home_safe("public headquarters feed", lambda: public_headquarters_feed(8), [])

    return render_template(
        "home.html",
        recruiting_needs=recruiting_needs,
        scheduled_operations=scheduled_operations,
        incoming_orders=incoming_orders,
        newest_arrivals=newest_arrivals,
        public_strength=public_strength,
        replacement_count=int(snapshot["recruiting"].get("processing") or 0),
        operations_completed=operations_completed,
        public_snapshot=snapshot,
        headquarters_feed=headquarters_feed,
    )


@app.get("/personnel-system")
def public_personnel():
    # Retired public Personnel page; keep legacy URL safe for old bookmarks.
    return redirect(url_for("organization"))


@app.get("/field-operations")
def public_operations():
    if not database_ready():
        return render_template("setup.html")
    upcoming = _public_home_safe("public operations", lambda: public_scheduled_operations(12), [])
    def _completed_public_operations():
        # S-3 history spans both website Operations and older Clerk battalion_events.
        try:
            rows = fetch_all("""
                SELECT o.id,o.operation_number,o.operation_code,o.title,o.operation_type,
                       COALESCE(o.completed_at,o.operation_date::timestamptz,o.start_at,o.created_at) completed_at,
                       o.result,o.status,o.lifecycle_status
                FROM operations o
                WHERE o.completed_at IS NOT NULL
                   OR UPPER(COALESCE(o.status,'')) IN ('CLOSED','COMPLETE','COMPLETED','AAR FILED')
                   OR UPPER(COALESCE(o.lifecycle_status,'')) IN ('CLOSED','COMPLETE','COMPLETED','AAR FILED')
                   OR UPPER(COALESCE(o.publish_status,''))='CLOSED'
                ORDER BY COALESCE(o.completed_at,o.operation_date::timestamptz,o.start_at,o.created_at) DESC NULLS LAST
                LIMIT 40
            """)
        except Exception:
            log.exception("Public completed Operations extended query failed; using compatibility history query.")
            rows = fetch_all("""
                SELECT id,operation_number,operation_code,title,operation_type,
                       COALESCE(completed_at,operation_date::timestamptz,created_at) completed_at,result,status
                FROM operations
                WHERE completed_at IS NOT NULL
                   OR UPPER(COALESCE(status,'')) IN ('CLOSED','COMPLETE','COMPLETED')
                ORDER BY COALESCE(completed_at,operation_date::timestamptz,created_at) DESC NULLS LAST LIMIT 40
            """)
        try:
            event_rows = fetch_all("""
                SELECT be.id,be.operation_id,be.external_event_id,be.title,be.event_type,
                       be.ends_at AS completed_at,be.status
                FROM battalion_events be
                WHERE be.event_type='OPERATION'
                  AND (UPPER(COALESCE(be.status,''))='CLOSED' OR be.ends_at < NOW())
                ORDER BY be.ends_at DESC LIMIT 40
            """)
            linked={str(r.get('id')) for r in rows if r.get('id')}
            for e in event_rows:
                if e.get('operation_id') and str(e.get('operation_id')) in linked:
                    continue
                rows.append({
                    'operation_number': e.get('external_event_id') or 'OPERATION',
                    'operation_code': e.get('external_event_id'),
                    'title': e.get('title') or 'Official Operation',
                    'operation_type': e.get('event_type') or 'OPERATION',
                    'completed_at': e.get('completed_at'),
                    'result': None,
                    'status': e.get('status') or 'CLOSED',
                })
        except Exception:
            log.exception("Legacy Battalion Event history could not be added to public Operations history.")
        rows.sort(key=lambda r: r.get('completed_at') or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return rows[:24]
    completed = _public_home_safe("completed operations public", _completed_public_operations, [])
    return render_template("public_operations.html", upcoming=upcoming, completed=completed)


@app.get("/readiness-report")
def public_readiness_report():
    if not database_ready():
        return render_template("setup.html")
    return render_template("public_readiness.html", readiness=public_readiness_snapshot())


@app.get("/contact")
def contact():
    return render_template("contact.html", discord_invite_url=CONFIG.discord_invite_url)


@app.route("/login", methods=["GET", "POST"])
@app.route("/report-for-duty", methods=["GET", "POST"])
def login():
    if not database_ready():
        return render_template("setup.html")
    if request.method == "POST":
        if request.form.get("action") == "acknowledge_rules" and session.get("access_role") == "member" and session.get("user_id"):
            p = linked_personnel()
            if not p:
                abort(403)
            progress = personnel_progress(p["id"])
            if not progress.get("rules_acknowledged_at"):
                execute("UPDATE personnel_progress_control SET rules_acknowledged_at=NOW(),rules_acknowledged_by=%s,updated_at=NOW() WHERE personnel_id=%s",
                        (f"{p.get('rank_code') or ''} {p.get('last_name') or ''}".strip(), p["id"]))
                write_service_entry(p["id"], "ADMIN", "BATTALION STANDING ORDERS ACKNOWLEDGED",
                                    "Soldier acknowledged that the battalion community rules and standing orders were read and understood.",
                                    f"{p.get('rank_code') or ''} {p.get('last_name') or ''}".strip())
                flash("BATTALION STANDING ORDERS ACKNOWLEDGED AND FILED IN YOUR 201 RECORD.", "success")
            replacement_training_status(soldier_view(p))
            if 'finalize_replacement_release' in globals():
                try: finalize_replacement_release(p["id"], f"{p.get('rank_code') or ''} {p.get('last_name') or ''}".strip() or "SOLDIER")
                except Exception: log.exception("Replacement release check failed after Standing Orders acknowledgement for %s",p["id"])
            return redirect(url_for("my_soldier_record"))
        roster_number = request.form.get("roster_number", "").strip().upper()
        field_code = request.form.get("field_code", "").strip().upper()
        try:
            card = fetch_one(
                """
                SELECT brc.*, p.first_name, p.last_name, p.rank_code, upl.user_id
                FROM battle_roster_cards brc
                JOIN personnel p ON p.id=brc.personnel_id
                LEFT JOIN user_personnel_links upl ON upl.personnel_id=p.id
                WHERE UPPER(brc.roster_number)=UPPER(%s) AND brc.is_active=TRUE
                """,
                (roster_number,),
            )
            valid = bool(card and card.get("field_code_hash") and check_password_hash(card["field_code_hash"], field_code))
        except Exception:
            log.exception("Member Access credential lookup failed")
            flash("MEMBER ACCESS IS TEMPORARILY UNAVAILABLE. HEADQUARTERS HAS LOGGED THE FAILURE.", "danger")
            return render_template("member_login.html"), 503
        if not valid:
            flash("BATTLE ROSTER CREDENTIALS COULD NOT BE VERIFIED. STAFF CREDENTIALS MUST USE STAFF ACCESS.", "danger")
        else:
            try:
                user_id = card.get("user_id") or ensure_member_site_user(card["personnel_id"], card["roster_number"])
                session.clear()
                session["user_id"] = str(user_id)
                session["personnel_id"] = str(card["personnel_id"])
                session["username"] = card["roster_number"]
                session["access_role"] = "member"
                execute("UPDATE battle_roster_cards SET last_used_at=NOW() WHERE id=%s", (card["id"],))
                flash(f"DUTY STATUS CONFIRMED — {card['rank_code']} {card['last_name'].upper()}.", "success")
                return redirect(url_for("my_soldier_record"))
            except Exception:
                log.exception("Member Access session provisioning failed for roster %s", roster_number)
                session.clear()
                flash("YOUR CREDENTIALS WERE VERIFIED, BUT THE SOLDIER SESSION COULD NOT BE OPENED. HEADQUARTERS HAS LOGGED THE FAILURE.", "danger")
    return render_template("member_login.html")


def safe_member_panel(label, default, func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception:
        ref=secrets.token_hex(4).upper()
        log.exception("MEMBER PANEL FAILURE [%s] %s",ref,label)
        return default

def current_mos_proficiency(person):
    if not person or not person.get("id") or not person.get("mos_code"):
        return None
    return fetch_one("""SELECT pmp.*,bmc.mos_title
                        FROM personnel_mos_proficiency pmp
                        LEFT JOIN battalion_mos_catalog bmc ON bmc.mos_code=pmp.mos_code
                        WHERE pmp.personnel_id=%s AND pmp.mos_code=%s AND pmp.is_current=TRUE
                        LIMIT 1""",(person["id"],person["mos_code"]))

def member_record_context(personnel):
    if not personnel:
        return {
            "personnel": None, "roster_card": None, "weapon": None, "uniform_issue": None,
            "awards": [], "assignments": [], "appointments": [], "qualifications": [],
            "duty_quals": [], "personal_ops": [], "operation_credit_ledger": [], "service_history": [], "current_orders": [],
            "chain_of_command": [], "replacement_training": {"complete":False,"requirements":[]},
            "promotion_eligibility": [], "training_programs": [], "progress_control": {},
            "can_recommend_awards": False, "award_candidates": [], "award_recommendations": [], "documents": [], "action_items": [], "notifications": [], "next_step": "Report to S-1.", "weapon_inspection": None, "mos_records": [], "timeline": [], "current_story": {}, "mos_proficiency": [], "instructor_quals": [], "leadership_records": [], "acting_appointments": [], "tour_book_preview": [], "recognitions": [], "ribbon_progress": [], "earned_ribbons": [], "uniform_ribbon_rows": [], "career_stats": {}, "combat_experience": {}, "career_tour": {}, "career_milestones": [], "assignment_history_full": [], "buddy_history": [], "weekly_report": {}, "squad_snapshot": None, "where_you_stand": {}, "record_warning": None, "record_error_reference": None,
        }
    personnel = soldier_view(personnel)
    pid = personnel["id"]

    # Normal page views are read-oriented. Readiness/lifecycle/MOS/qualification
    # synchronization is handled by personnel actions and Battalion Clerk events,
    # not by every browser click.
    career_context = safe_member_panel("CAREER CONTEXT", {}, member_career_context, personnel)
    where_you_stand = career_context.get("where_you_stand") or {
        "tour": 0, "readiness": int(personnel.get("readiness_percent") or 0),
        "promotion_readiness": 0, "mos_proficiency": personnel.get("mos_code") or "NOT RATED",
        "experience": "NOT RATED", "squad_cohesion": None,
    }
    leadership_score = career_context.get("leadership_score") or safe_member_panel(
        "LEADERSHIP SCORE", {"score":0,"rating":"NOT RATED","breakdown":{}}, combat_leadership_score, pid
    )
    mos_proficiency = safe_member_panel("MOS PROFICIENCY", None, current_mos_proficiency, personnel)
    weapon = safe_member_panel("CURRENT WEAPON", None, current_weapon_for, personnel)

    return {
        "personnel": personnel,
        "roster_card": battle_roster_for(personnel),
        "weapon": weapon,
        "uniform_issue": fetch_one(
            """SELECT eih.issued_at,ei.condition_state,sic.item_name
               FROM equipment_issue_history eih
               JOIN equipment_inventory ei ON ei.id=eih.equipment_id
               JOIN supply_item_catalog sic ON sic.item_code=ei.item_code
               WHERE eih.personnel_id=%s AND eih.is_current=TRUE AND ei.item_code='AG44' LIMIT 1""",
            (pid,),
        ),
        "awards": fetch_all(
            """SELECT pa.*, pr.id AS ribbon_id, pr.is_worn, rc.ribbon_code, rc.ribbon_name
               FROM personnel_awards pa
               LEFT JOIN ribbon_catalog rc ON LOWER(TRIM(rc.ribbon_name))=LOWER(TRIM(pa.award_name))
               LEFT JOIN personnel_ribbons pr ON pr.personnel_id=pa.personnel_id AND pr.ribbon_code=rc.ribbon_code
               WHERE pa.personnel_id=%s
               ORDER BY pa.award_date DESC
               LIMIT 20""", (pid,)
        ),
        "assignments": fetch_all("SELECT * FROM assignment_history WHERE personnel_id=%s ORDER BY effective_date DESC,created_at DESC LIMIT 20", (pid,)),
        "appointments": fetch_all(
            """SELECT pa.*,ac.appointment_name FROM personnel_appointments pa
               JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code
               WHERE pa.personnel_id=%s ORDER BY pa.effective_date DESC,pa.created_at DESC LIMIT 20""",
            (pid,),
        ),
        "qualifications": fetch_all("SELECT * FROM qualifications WHERE personnel_id=%s ORDER BY expires_at NULLS LAST,qualification_name LIMIT 20", (pid,)),
        "duty_quals": personnel_duty_qualifications(pid),
        "personal_ops": personal_operations(pid),
        "operation_credit_ledger": member_operation_credit_ledger(pid),
        "service_history": fetch_all("SELECT * FROM personnel_service_history WHERE personnel_id=%s ORDER BY entry_date DESC,created_at DESC LIMIT 30", (pid,)),
        "documents": fetch_all("SELECT * FROM personnel_documents WHERE personnel_id=%s ORDER BY effective_date DESC,created_at DESC LIMIT 40", (pid,)),
        "current_orders": fetch_all(
            """SELECT * FROM operations
               WHERE status NOT IN ('CLOSED','COMPLETE','COMPLETED','CANCELLED')
               ORDER BY operation_date NULLS LAST, created_at DESC LIMIT 8"""
        ),
        "chain_of_command": safe_member_panel("CHAIN OF COMMAND", [], chain_of_command_for, personnel),
        "replacement_training": safe_member_panel("REPLACEMENT TRAINING", {"complete":False,"requirements":[]}, replacement_training_status, personnel),
        "promotion_eligibility": safe_member_panel("PROMOTION ELIGIBILITY", [], promotion_eligibility, personnel),
        "training_programs": fetch_all("SELECT * FROM training_program_catalog WHERE is_active=TRUE ORDER BY sort_order"),
        "progress_control": safe_member_panel("PROGRESS CONTROL", {}, personnel_progress, pid),
        "can_recommend_awards": member_is_nco(personnel),
        "award_candidates": fetch_all("SELECT id,rank_code,last_name,first_name,unit_code,platoon,squad FROM personnel ORDER BY unit_code,last_name,first_name") if member_is_nco(personnel) else [],
        "award_recommendations": fetch_all(
            """SELECT pr.*,p.rank_code,p.last_name,p.first_name FROM personnel_recommendations pr
               JOIN personnel p ON p.id=pr.personnel_id
               WHERE pr.recommending_personnel_id=%s AND UPPER(pr.recommendation_type)='AWARD'
               ORDER BY pr.created_at DESC LIMIT 12""", (pid,)
        ) if member_is_nco(personnel) else [],
        "action_items": safe_member_panel("ACTION ITEMS", [], soldier_action_items, personnel),
        "notifications": safe_member_panel("NOTIFICATIONS", [], current_notifications, pid),
        "next_step": safe_member_panel("NEXT STEP", "Report to S-1.", soldier_next_step, personnel),
        "weapon_inspection": safe_member_panel("WEAPON INSPECTION", None, weapon_inspection_status, pid),
        "mos_records": safe_member_panel("MOS RECORDS", [], personnel_mos_for, pid),
        "timeline": fetch_all("SELECT * FROM personnel_service_history WHERE personnel_id=%s ORDER BY entry_date DESC,created_at DESC LIMIT 80", (pid,)),
        "current_story": safe_member_panel("CURRENT STORY", {}, soldier_current_story, personnel),
        "mos_proficiency": fetch_all("SELECT pmp.*,bmc.mos_title FROM personnel_mos_proficiency pmp JOIN battalion_mos_catalog bmc ON bmc.mos_code=pmp.mos_code WHERE pmp.personnel_id=%s AND pmp.is_current=TRUE ORDER BY pmp.proficiency_order DESC,bmc.sort_order",(pid,)),
        "instructor_quals": fetch_all("SELECT * FROM instructor_qualifications WHERE personnel_id=%s AND status='CURRENT' ORDER BY effective_date DESC",(pid,)),
        "leadership_score": leadership_score,
        "leadership_records": fetch_all("SELECT * FROM leadership_performance_records WHERE personnel_id=%s ORDER BY record_date DESC,created_at DESC LIMIT 12",(pid,)),
        "acting_appointments": fetch_all("SELECT * FROM acting_appointments WHERE personnel_id=%s AND is_current=TRUE ORDER BY effective_date DESC",(pid,)),
        "tour_book_preview": fetch_all("SELECT * FROM soldier_tour_book WHERE personnel_id=%s ORDER BY entry_date DESC,created_at DESC LIMIT 8",(pid,)),
        "recognitions": fetch_all("SELECT * FROM soldier_recognitions WHERE personnel_id=%s ORDER BY effective_date DESC",(pid,)),
        "ribbon_progress": safe_member_panel("RIBBON PROGRESS", [], ribbon_progress_for, pid, award_completed=False),
        "earned_ribbons": safe_member_panel("EARNED RIBBONS", [], lambda: worn_ribbon_rows(pid)[1]),
        "uniform_ribbon_rows": safe_member_panel("UNIFORM RIBBONS", [], lambda: worn_ribbon_rows(pid)[0]),
        "current_situation": safe_member_panel("CURRENT SITUATION", {}, current_situation_snapshot, personnel),
        "field_reputation": safe_member_panel("FIELD REPUTATION", [], field_reputation, personnel),
        "personal_action_center": safe_member_panel("PERSONAL ACTION CENTER", [], member_personal_action_center, personnel),
        "recommended_action": safe_member_panel("NEXT RECOMMENDED ACTION", {"title":"MAINTAIN READINESS","detail":"No immediate deficiency on file."}, next_recommended_action, personnel),
        "most_served_with": safe_member_panel("MOST SERVED WITH", [], most_served_with, pid, 5),
        **career_context,
        "where_you_stand": where_you_stand,
    }



def member_record_fallback_context(personnel, error_reference=None):
    """Minimal, hard-safe Soldier Record context that does not depend on extended career modules."""
    p=dict(personnel or {})
    # Populate stable display defaults without running the full soldier_view/reconcile pipeline.
    p.setdefault("rank_code","")
    p.setdefault("first_name","")
    p.setdefault("last_name","")
    p.setdefault("unit_code","")
    p.setdefault("platoon",None)
    p.setdefault("squad",None)
    p.setdefault("mos_code","")
    p.setdefault("duty_position",None)
    p.setdefault("readiness_percent",0)
    p.setdefault("readiness_status","UNKNOWN")
    p.setdefault("lifecycle_state",p.get("personnel_status") or "ACTIVE")
    p.setdefault("personnel_status_label",p.get("personnel_status") or "ACTIVE")
    p.setdefault("tour_phase","")
    p.setdefault("loa_active",False)
    p.setdefault("deros_date",None)
    p.setdefault("service_number",None)

    roster=None
    weapon=None
    try:
        roster=fetch_one("SELECT * FROM battle_roster_cards WHERE personnel_id=%s AND is_active=TRUE ORDER BY issued_at DESC LIMIT 1",(p.get("id"),))
    except Exception:
        log.exception("MEMBER RECORD FALLBACK ROSTER LOOKUP FAILED [%s]",error_reference)
    try:
        weapon=fetch_one("""SELECT wi.*,wih.issued_at FROM weapon_issue_history wih
                            JOIN weapon_inventory wi ON wi.id=wih.weapon_id
                            WHERE wih.personnel_id=%s AND wih.is_current=TRUE
                            ORDER BY wih.issued_at DESC LIMIT 1""",(p.get("id"),))
    except Exception:
        log.exception("MEMBER RECORD FALLBACK WEAPON LOOKUP FAILED [%s]",error_reference)

    return {
        "personnel":p,
        "roster_card":roster,
        "weapon":weapon,
        "record_error_reference":error_reference,
        "record_warning":"The extended Soldier Record encountered a server-side data error. Your core personnel record is available below while Headquarters logs the failing module.",
    }


@app.route("/my-soldier-record", methods=["GET", "POST"])
def my_soldier_record():
    if not database_ready():
        return render_template("setup.html")
    if request.method == "POST":
        if request.form.get("action") == "acknowledge_rules" and session.get("user_id"):
            p = linked_personnel()
            if not p:
                abort(403)
            progress = personnel_progress(p["id"])
            if not progress.get("rules_acknowledged_at"):
                authority = f"{p.get('rank_code') or ''} {p.get('last_name') or ''}".strip()
                execute("UPDATE personnel_progress_control SET rules_acknowledged_at=NOW(),rules_acknowledged_by=%s,updated_at=NOW() WHERE personnel_id=%s", (authority,p["id"]))
                write_service_entry(p["id"],"ADMIN","BATTALION STANDING ORDERS ACKNOWLEDGED","Soldier acknowledged that the battalion community rules and standing orders were read and understood.",authority)
                flash("BATTALION STANDING ORDERS ACKNOWLEDGED AND FILED IN YOUR 201 RECORD.", "success")
            replacement_training_status(soldier_view(p))
            if 'finalize_replacement_release' in globals():
                try: finalize_replacement_release(p["id"], f"{p.get('rank_code') or ''} {p.get('last_name') or ''}".strip() or "SOLDIER")
                except Exception: log.exception("Replacement release check failed after Standing Orders acknowledgement for %s",p["id"])
            return redirect(url_for("my_soldier_record"))
        roster_number = request.form.get("roster_number", "").strip().upper()
        field_code = request.form.get("field_code", "").strip().upper()
        card = fetch_one(
            """SELECT brc.*,p.first_name,p.last_name,p.rank_code,upl.user_id
               FROM battle_roster_cards brc
               JOIN personnel p ON p.id=brc.personnel_id
               LEFT JOIN user_personnel_links upl ON upl.personnel_id=p.id
               WHERE UPPER(brc.roster_number)=UPPER(%s) AND brc.is_active=TRUE""",
            (roster_number,),
        )
        if not card or not check_password_hash(card["field_code_hash"], field_code):
            flash("SOLDIER RECORD CREDENTIALS COULD NOT BE VERIFIED.", "danger")
            return render_template("member_login.html")
        user_id = card.get("user_id") or ensure_member_site_user(card["personnel_id"], card["roster_number"])
        session.clear()
        session["user_id"] = str(user_id)
        session["personnel_id"] = str(card["personnel_id"])
        session["username"] = card["roster_number"]
        session["access_role"] = "member"
        execute("UPDATE battle_roster_cards SET last_used_at=NOW() WHERE id=%s", (card["id"],))
        flash(f"SOLDIER RECORD OPENED — {card['rank_code']} {card['last_name'].upper()}.", "success")
        return redirect(url_for("my_soldier_record"))

    if session.get("access_role") == "member" and session.get("user_id"):
        error_reference=None
        try:
            personnel=linked_personnel()
        except Exception:
            error_reference=secrets.token_hex(4).upper()
            log.exception("MEMBER RECORD LINK LOOKUP FAILURE [%s] session_user=%s",error_reference,session.get("user_id"))
            return render_template("member_record_core.html", personnel=None, roster_card=None, weapon=None,
                                   record_error_reference=error_reference,
                                   record_warning="Your login was accepted, but the personnel-link lookup failed. Headquarters has logged this reference.")

        if personnel:
            try:
                context=member_record_context(personnel)
                return render_template("member_record.html", **context)
            except Exception:
                error_reference=secrets.token_hex(4).upper()
                log.exception("MEMBER RECORD FULL RENDER FAILURE [%s] personnel=%s",error_reference,personnel.get("id"))
                try:
                    context=member_record_fallback_context(personnel,error_reference)
                    return render_template("member_record_core.html",**context)
                except Exception:
                    log.exception("MEMBER RECORD HARD FALLBACK FAILURE [%s] personnel=%s",error_reference,personnel.get("id"))
                    return render_template("member_record_core.html",personnel=dict(personnel),roster_card=None,weapon=None,
                                           record_error_reference=error_reference,
                                           record_warning="Your Soldier Record encountered a server-side data error. The core identity record is being displayed while Headquarters logs the failure.")
    return render_template("member_login.html")


def _staff_login_response():
    if not database_ready():
        return render_template("setup.html")
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        try:
            user = fetch_one(
                "SELECT * FROM site_users WHERE username=%s AND is_active=TRUE AND access_role<>'member'",
                (username,),
            )
        except Exception:
            log.exception("Staff Access credential lookup failed")
            flash("STAFF ACCESS IS TEMPORARILY UNAVAILABLE. HEADQUARTERS HAS LOGGED THE FAILURE.", "danger")
            return render_template("staff_access.html"), 503
        if not user or not user.get("password_hash") or not check_password_hash(user["password_hash"], password):
            flash("STAFF CREDENTIALS COULD NOT BE VERIFIED.", "danger")
        else:
            session.clear()
            session["user_id"] = str(user["id"])
            session["username"] = user["username"]
            session["access_role"] = user["access_role"]
            return redirect(url_for(staff_landing(user["access_role"])))
    return render_template("staff_access.html")


@app.route("/staff-login", methods=["GET", "POST"])
def staff_login():
    """Dedicated Headquarters staff authentication portal."""
    return _staff_login_response()


@app.route("/staff-access", methods=["GET", "POST"])
def staff_access():
    """Legacy staff-access URL retained for bookmarks and older templates."""
    if request.method == "POST":
        return _staff_login_response()
    return redirect(url_for("staff_login"))


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


STAFF_ROLES={"s1","s2","s3","s4","training","battalion_hq","commander","admin"}

def _staff_section(role=None):
    role=role or session.get("access_role")
    return {"s1":"S-1","s2":"S-2","s3":"S-3","training":"S-3","s4":"S-4"}.get(role)

def _staff_can(*roles):
    role=session.get("access_role")
    return role in {"battalion_hq","commander","admin"} or role in set(roles)

def _staff_search_rows(query,limit=18):
    return smart_personnel_search(query,limit=limit) if (query or '').strip() else []

def _replacement_safe_rows(sql, params=(), label="Replacement secondary query"):
    try:
        return fetch_all(sql, params)
    except Exception:
        log.exception("%s failed; Replacement Detachment will continue with available data.", label)
        return []


def _replacement_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
        except Exception:
            try:
                return date.fromisoformat(str(value)[:10])
            except Exception:
                pass
    return date.today()



def permanent_formation_state(person):
    """Resolve whether a Soldier has a real permanent formation assignment.

    A battalion/root placeholder is never enough. Line Soldiers must resolve to a
    Squad. Headquarters/Section nodes are valid permanent assignments because
    those billets do not belong to a rifle squad. Legacy company/platoon/squad
    fields remain supported when no structured node exists.
    """
    person=dict(person or {})
    node_id=person.get('unit_node_id')
    chain=[]
    seen=set()
    node_map=getattr(g,'_replacement_unit_node_map',None)
    if node_map is None:
        node_map={str(n['id']):n for n in fetch_all("SELECT id,parent_id,unit_code,display_name,unit_type FROM unit_nodes")}
        g._replacement_unit_node_map=node_map
    while node_id and str(node_id) not in seen:
        seen.add(str(node_id))
        node=node_map.get(str(node_id))
        if not node:
            break
        chain.append(node)
        node_id=node.get('parent_id')
    by_type={str(n.get('unit_type') or '').upper():n for n in chain}
    assigned_node=chain[0] if chain else None
    assigned_type=str((assigned_node or {}).get('unit_type') or '').upper()
    company=by_type.get('COMPANY')
    platoon=by_type.get('PLATOON')
    squad=by_type.get('SQUAD')
    headquarters=by_type.get('HEADQUARTERS')
    section=by_type.get('SECTION')

    legacy_unit=str(person.get('unit_code') or '').strip()
    legacy_company=bool(legacy_unit and legacy_unit.upper() not in {'1-5 CAV','1-5-CAV','1ST BATTALION, 5TH CAVALRY REGIMENT','REPLACEMENT DETACHMENT'})
    company_ok=bool(company) or legacy_company
    platoon_ok=bool(platoon) or bool(str(person.get('platoon') or '').strip())
    squad_ok=bool(squad) or bool(str(person.get('squad') or '').strip())

    # A structured HQ/Section assignment is a valid permanent destination.
    special_ok=bool(assigned_node and assigned_type in {'HEADQUARTERS','SECTION'})
    # A line assignment is complete only when the hierarchy reaches a squad.
    line_ok=bool(squad) or bool(company_ok and platoon_ok and squad_ok)
    field_assigned=str(person.get('field_status') or '').upper()=='ASSIGNED'
    complete=bool(field_assigned and (special_ok or line_ok))
    return {
        'complete':complete,
        'company_ok':company_ok,
        'platoon_ok':platoon_ok or special_ok,
        'squad_ok':squad_ok or special_ok,
        'special_assignment':special_ok,
        'assigned_type':assigned_type,
        'company':company,
        'platoon':platoon,
        'squad':squad,
        'assigned_node':assigned_node,
    }


def _replacement_program_complete(personnel_id, program_code):
    row=fetch_one("SELECT status,completed_at FROM personnel_training_records WHERE personnel_id=%s AND program_code=%s",(personnel_id,program_code))
    return bool(row and str(row.get('status') or '').upper() in {'COMPLETE','COMPLETED','CLOSED'}), row


def _replacement_release_requirements(personnel_id):
    person=fetch_one("SELECT * FROM personnel WHERE id=%s AND archived=FALSE AND separated_at IS NULL",(personnel_id,))
    if not person:
        return None, []
    progress=personnel_progress(personnel_id)
    formation=permanent_formation_state(person)
    profile=entry_processing_profile(person)
    program_ok,training_row=_replacement_program_complete(personnel_id,profile['program_code'])
    weapon=fetch_one("SELECT wi.serial_number FROM weapon_issue_history wih JOIN weapon_inventory wi ON wi.id=wih.weapon_id WHERE wih.personnel_id=%s AND wih.is_current=TRUE LIMIT 1",(personnel_id,))
    link=fetch_one("SELECT 1 AS ok FROM website_member_links WHERE personnel_id=%s LIMIT 1",(personnel_id,))
    mos=str(person.get('mos_code') or '').strip().upper()
    # Resolve the authoritative MOS catalog inside this helper.  The previous
    # Replacement release gate referenced `valid_mos` from another function's
    # local scope, which raised NameError immediately after S-1 filed a
    # formation assignment.
    valid_mos={str(r.get('mos_code') or '').strip().upper() for r in fetch_all(
        "SELECT mos_code FROM battalion_mos_catalog WHERE is_active=TRUE"
    )}
    mos_ok=bool(mos and mos not in {'00R','00','PENDING','UNASSIGNED'} and mos in valid_mos)
    requirements=[
        ('Discord / communications linked',bool(link)),
        ('S-1 onboarding complete',bool(progress.get('s1_onboarded_at'))),
        ('Standing orders acknowledged',bool(progress.get('rules_acknowledged_at'))),
        ('Primary MOS assigned',mos_ok),
        ('M16 issued',bool(weapon)),
        (profile['program_title']+' complete',program_ok),
        ('Permanent formation assigned',bool(formation.get('complete'))),
    ]
    return {'person':person,'progress':progress,'formation':formation,'profile':profile,'training_row':training_row,'weapon':weapon,'link':link,'mos_ok':mos_ok},requirements


def finalize_replacement_release(personnel_id, authority='BATTALION S-1'):
    """Release a Soldier only when the full Replacement Detachment checklist is complete."""
    state,requirements=_replacement_release_requirements(personnel_id)
    if not state:
        return False,['Personnel record not found']
    missing=[label for label,ok in requirements if not ok]
    if missing:
        return False,missing
    person=state['person']
    formation=state['formation']
    execute("UPDATE personnel SET field_status='Assigned',duty_status='PRESENT FOR DUTY',updated_at=NOW() WHERE id=%s",(personnel_id,))
    for a in fetch_all("SELECT * FROM personnel_actions WHERE personnel_id=%s AND status NOT IN ('COMPLETE','CLOSED','DENIED')",(personnel_id,)):
        subj=str(a.get('subject') or '').upper()
        src=str(a.get('source_key') or '')
        if ('IN-PROCESS' in subj or 'ONBOARD' in subj or 'REPLACEMENT TRAINING' in subj or src.startswith('REPLACEMENT-INPROCESS:') or src.startswith('REPLACEMENT-TRAINING:')):
            transition_personnel_action(a['id'],'COMPLETE',authority,'Replacement Detachment processing complete.')
    case=fetch_one("SELECT * FROM recruiting_cases WHERE personnel_id=%s AND status IN ('REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING') ORDER BY approved_at DESC NULLS LAST LIMIT 1",(personnel_id,))
    movement_number=None
    if case:
        refreshed=fetch_one("SELECT * FROM personnel WHERE id=%s",(personnel_id,)) or person
        destination=' / '.join(x for x in [refreshed.get('unit_code'),refreshed.get('platoon'),refreshed.get('squad')] if x)
        doc=create_personnel_order(personnel_id,'REPLACEMENT','MOVEMENT ORDERS — REPLACEMENT DETACHMENT',
            f"The Soldier named herein is released from the 1/5 Cavalry Replacement Detachment and directed to report to {destination} for duty.",
            effective_date=date.today(),authority=authority,details={'recruiting_case':case.get('case_number'),'destination':destination},
            source_key=f"REPLACEMENT-MOVEMENT:{case['id']}")
        movement_number=(doc or {}).get('document_number') if doc else None
        execute("""UPDATE recruiting_cases SET status='ENLISTED',movement_order_number=COALESCE(%s,movement_order_number),
                   movement_order_filed_at=CASE WHEN %s IS NOT NULL THEN NOW() ELSE movement_order_filed_at END,
                   movement_unit_code=COALESCE(%s,movement_unit_code),updated_at=NOW() WHERE id=%s""",
                (movement_number,movement_number,refreshed.get('unit_code'),case['id']))
        if case.get('recruited_by_personnel_id'):
            automatic_ribbon_recheck([case['recruited_by_personnel_id']])
    write_service_entry(personnel_id,'ADMIN','REPLACEMENT DETACHMENT PROCESSING COMPLETE','Released from Replacement Detachment administrative processing to permanent formation.',authority,movement_number,date.today())
    enqueue_discord_role_sync(personnel_id,'REPLACEMENT RELEASED TO PERMANENT FORMATION')
    try:
        refreshed=fetch_one("SELECT * FROM personnel WHERE id=%s",(personnel_id,))
        reconcile_lifecycle(soldier_view(refreshed),authority)
    except Exception:
        log.exception('Lifecycle refresh failed after Replacement release for %s',personnel_id)
    return True,[]

def replacement_detachment_rows():
    """Return the active Replacement Detachment pipeline without mutating Soldier records.

    A Soldier stays in the detachment only while they lack a permanent Company/HQ
    destination. Remaining onboarding or training items follow them as ordinary
    S-1/Training suspense after assignment. The status is derived from authoritative personnel,
    training, property, Discord-link and action data rather than a duplicate roster.
    """
    cached=getattr(g,'_replacement_detachment_rows',None)
    if cached is not None:
        return cached
    people=fetch_all("""SELECT p.*,un.unit_type AS assigned_unit_type,un.display_name AS assigned_unit_name
                        FROM personnel p LEFT JOIN unit_nodes un ON un.id=p.unit_node_id
                        WHERE p.archived=FALSE AND p.separated_at IS NULL
                        ORDER BY COALESCE(p.roster_entered_at,p.created_at),p.last_name,p.first_name""")
    if not people:
        return []
    ids=[str(x['id']) for x in people]
    progress={str(r['personnel_id']):r for r in _replacement_safe_rows("SELECT * FROM personnel_progress_control", label="Replacement progress query")}
    links={str(r['personnel_id']) for r in _replacement_safe_rows("SELECT personnel_id FROM website_member_links", label="Replacement Discord link query")}
    weapons={str(r['personnel_id']):r for r in _replacement_safe_rows("""SELECT wih.personnel_id,wi.serial_number,wi.rack_number,wi.condition_state
                                                            FROM weapon_issue_history wih JOIN weapon_inventory wi ON wi.id=wih.weapon_id
                                                            WHERE wih.is_current=TRUE""", label="Replacement M16 query")}
    training={(str(r['personnel_id']),str(r['program_code'])):r for r in _replacement_safe_rows("""SELECT ptr.* FROM personnel_training_records ptr
                                                                                         WHERE ptr.program_code IN ('REPLACEMENT','INITIAL_INPROCESSING')""", label="Replacement training query")}
    action_rows=_replacement_safe_rows("""SELECT * FROM personnel_actions
                             WHERE status NOT IN ('COMPLETE','CLOSED','DENIED')
                             ORDER BY created_at""", label="Replacement action query")
    action_map={}
    for a in action_rows:
        if a.get('personnel_id'):
            action_map.setdefault(str(a['personnel_id']),[]).append(a)
    op_counts={str(r['personnel_id']):int(r.get('total') or 0) for r in _replacement_safe_rows("""SELECT personnel_id,COUNT(*) AS total FROM operation_participation
                                                                                     WHERE UPPER(COALESCE(attendance_status,''))='FULL CREDIT' GROUP BY personnel_id""", label="Replacement operation-credit query")}
    valid_mos={str(r.get('mos_code') or '').upper() for r in _replacement_safe_rows("SELECT mos_code FROM battalion_mos_catalog WHERE is_active=TRUE",label="Replacement MOS catalog query")}
    out=[]
    for person in people:
        pid=str(person['id']); pr=progress.get(pid) or {}; open_actions=action_map.get(pid,[])
        
        try:
            profile_rank=(initial_entry_rank(person['id']) or person.get('rank_code') or 'PVT')
        except Exception:
            log.exception('Initial entry rank lookup failed for %s; using current rank.', pid)
            profile_rank=(person.get('rank_code') or 'PVT')
        program='REPLACEMENT' if profile_rank=='PVT' else 'INITIAL_INPROCESSING'
        tr=training.get((pid,program))
        unit_type=str(person.get('assigned_unit_type') or '').upper()
        formation=permanent_formation_state(person)
        formation_complete=bool(formation.get('complete'))
        # Replacement Detachment is driven by actual permanent assignment, not by a
        # generic unit_node_id. Battalion/root placeholders therefore stay visible.
        replacement_status=str(person.get('field_status') or '').upper() in {'REPLACEMENT','UNASSIGNED','PROCESSING','REPLACEMENT DEPOT'}
        lifecycle_replacement=str(person.get('lifecycle_state') or '').upper() in {'REPLACEMENT','IN PROCESSING','REPLACEMENT TRAINING'}
        active_initial_training=bool(tr and str(tr.get('status') or '').upper() not in {'COMPLETE','COMPLETED','CLOSED'})
        company_ok=bool(formation.get('company_ok'))
        platoon_ok=bool(formation.get('platoon_ok'))
        squad_ok=bool(formation.get('squad_ok'))
        mos=str(person.get('mos_code') or '').strip().upper()
        mos_ok=bool(mos and mos not in {'00R','00','PENDING','UNASSIGNED'} and fetch_one("SELECT 1 AS ok FROM battalion_mos_catalog WHERE mos_code=%s AND is_active=TRUE",(mos,)))
        weapon=weapons.get(pid)
        rules_ok=bool(pr.get('rules_acknowledged_at'))
        s1_ok=bool(pr.get('s1_onboarded_at'))
        training_ok=bool(tr and str(tr.get('status') or '').upper() in {'COMPLETE','COMPLETED','CLOSED'})
        discord_ok=pid in links
        assignment_pending=next((a for a in open_actions if str(a.get('action_type') or '').upper() in {'ASSIGNMENT','TRANSFER'}),None)
        inprocess_action=next((a for a in open_actions if 'IN-PROCESS' in str(a.get('subject') or '').upper() or str(a.get('source_key') or '').startswith('REPLACEMENT-INPROCESS:')),None)
        hold=next((a for a in open_actions if 'HOLD' in str(a.get('subject') or '').upper() or str(a.get('action_type') or '').upper()=='ADMIN HOLD'),None)
        # Replacement Detachment is an UNASSIGNED holding roster.  The moment a
        # Soldier has a real permanent Company/HQ destination, remove them from
        # this roster even if onboarding, Standing Orders, training, or lower-
        # echelon assignment follow-up is still outstanding.  Those remaining
        # items continue through normal S-1/Training suspense and the 201 File.
        permanent_destination=bool(formation.get('company_ok') or formation.get('special_assignment'))
        if permanent_destination:
            continue
        candidate=replacement_status or lifecycle_replacement or not permanent_destination or bool(inprocess_action) or active_initial_training
        if not candidate:
            continue
        checklist=[
            {'code':'201','label':'201 File established','complete':True,'detail':person.get('service_number') or 'FILE OPEN'},
            {'code':'DISCORD','label':'Discord / communications linked','complete':discord_ok,'detail':'LINKED' if discord_ok else 'LINK REQUIRED'},
            {'code':'S1','label':'S-1 onboarding complete','complete':s1_ok,'detail':pr.get('s1_onboarded_at') or 'S-1 ACTION REQUIRED'},
            {'code':'MOS','label':'MOS assigned','complete':mos_ok,'detail':person.get('mos_code') or 'MOS REQUIRED'},
            {'code':'M16','label':'M16 issued','complete':bool(weapon),'detail':f"M16 {weapon.get('serial_number')}" if weapon else 'ISSUE REQUIRED'},
            {'code':'ORDERS','label':'Standing orders acknowledged','complete':rules_ok,'detail':pr.get('rules_acknowledged_at') or 'SOLDIER ACKNOWLEDGMENT REQUIRED'},
            {'code':'FORMATION','label':'Permanent formation assigned','complete':formation_complete,'detail':' / '.join(x for x in [person.get('unit_code'),person.get('platoon'),person.get('squad')] if x) if formation_complete else 'COMPANY / PLATOON / SQUAD REQUIRED'},
            {'code':'TRAINING','label':'Initial processing / Replacement Training complete','complete':training_ok,'detail':str(tr.get('completed_at')) if training_ok else f'{program.replace("_"," ")} INCOMPLETE'},
        ]
        completed=sum(1 for x in checklist if x['complete']); total=len(checklist)
        if hold:
            stage='HOLD'; next_action='COMMAND / S-1 HOLD REVIEW'; next_code='OPEN_ACTION'
        elif assignment_pending:
            stage='ASSIGNMENT PENDING'; next_action='REVIEW / FILE ASSIGNMENT'; next_code='ASSIGN_FORMATION'
        elif not s1_ok:
            stage='NEW ARRIVALS' if (date.today()-_replacement_date(person.get('date_joined') or person.get('roster_entered_at') or person.get('created_at'))).days<=2 and not inprocess_action else 'IN-PROCESSING'
            if not inprocess_action:
                next_action='BEGIN IN-PROCESSING'; next_code='BEGIN_INPROCESSING'
            else:
                next_action='COMPLETE S-1 ONBOARDING'; next_code='COMPLETE_S1'
        elif not discord_ok:
            stage='IN-PROCESSING'; next_action='LINK DISCORD / COMMUNICATIONS'; next_code='OPEN_201'
        elif not mos_ok:
            stage='IN-PROCESSING'; next_action='ASSIGN MOS'; next_code='ASSIGN_MOS'
        elif not weapon:
            stage='IN-PROCESSING'; next_action='ISSUE M16'; next_code='ISSUE_M16'
        elif not rules_ok:
            stage='IN-PROCESSING'; next_action='ORDERS ACKNOWLEDGMENT'; next_code='SEND_REMINDER'
        elif not training_ok:
            stage='TRAINING'; next_action=f"COMPLETE {program.replace('_',' ')}"; next_code='COMPLETE_TRAINING'
        elif not formation_complete:
            stage='READY FOR ASSIGNMENT'; next_action='ASSIGN FORMATION'; next_code='ASSIGN_FORMATION'
        else:
            stage='ASSIGNED'; next_action='RELEASE TO UNIT'; next_code='RELEASE'
        # Keep the pipeline focused: fully complete Soldiers automatically disappear.
        if stage=='ASSIGNED':
            continue
        out.append({'person':soldier_view(person),'stage':stage,'next_action':next_action,'next_code':next_code,
                    'checklist':checklist,'completed':completed,'total':total,'weapon':weapon,'progress':pr,
                    'open_actions':open_actions,'assignment_pending':assignment_pending,'inprocess_action':inprocess_action,'hold_action':hold,
                    'formation_complete':formation_complete,'company_ok':company_ok,'platoon_ok':platoon_ok,'squad_ok':squad_ok,
                    'program_code':program,'training_record':tr})
    g._replacement_detachment_rows=out
    return out


def replacement_detachment_counts(rows=None):
    rows=rows if rows is not None else replacement_detachment_rows()
    labels=['NEW ARRIVALS','IN-PROCESSING','TRAINING','READY FOR ASSIGNMENT','ASSIGNMENT PENDING','HOLD']
    return {label:sum(1 for r in rows if r['stage']==label) for label in labels}


def personnel_exception_rows(limit=100):
    """S-1 integrity exceptions that are actionable rather than merely informational."""
    rows=[]
    people=fetch_all("""SELECT p.*,un.unit_type AS assigned_unit_type FROM personnel p
                        LEFT JOIN unit_nodes un ON un.id=p.unit_node_id
                        WHERE p.archived=FALSE AND p.separated_at IS NULL ORDER BY p.last_name,p.first_name""")
    current_weapons={str(r['personnel_id']) for r in _replacement_safe_rows("SELECT personnel_id FROM weapon_issue_history WHERE is_current=TRUE", label="Personnel exception M16 query")}
    links={str(r['personnel_id']) for r in _replacement_safe_rows("SELECT personnel_id FROM website_member_links", label="Replacement Discord link query")}
    sync={str(r['personnel_id']):r for r in _replacement_safe_rows("""SELECT DISTINCT ON (personnel_id) personnel_id,status,error_text,requested_at
                                                          FROM discord_role_sync_queue ORDER BY personnel_id,requested_at DESC""", label="Personnel exception Discord sync query")}
    team_leaders={str(r['personnel_id']) for r in _replacement_safe_rows("""SELECT personnel_id FROM personnel_appointments
                                                                  WHERE is_current=TRUE AND ended_date IS NULL
                                                                    AND appointment_code IN ('TL','TEAM_LEADER','ASST_SL')""", label="Personnel exception appointment query")}
    for p in people:
        pid=str(p['id']); name=f"{p.get('rank_code') or ''} {p.get('last_name') or ''}".strip()
        if p.get('squad') and not p.get('platoon'):
            rows.append({'person':p,'type':'ASSIGNMENT EXCEPTION','severity':'red','detail':'Squad is filed but platoon is blank.'})
        # Once a Soldier is assigned to a Company they leave Replacement Detachment.
        # Surface any unfinished lower-echelon placement here instead of keeping
        # them on the Replacement roster.
        try:
            _formation=permanent_formation_state(p)
        except Exception:
            _formation={}
        if _formation.get('company_ok') and not _formation.get('special_assignment') and (not _formation.get('platoon_ok') or not _formation.get('squad_ok')):
            missing=[]
            if not _formation.get('platoon_ok'): missing.append('platoon')
            if not _formation.get('squad_ok'): missing.append('squad')
            rows.append({'person':p,'type':'ASSIGNMENT FOLLOW-UP','severity':'amber','detail':'Company assignment filed; '+ ' and '.join(missing) +' assignment still requires S-1 action.'})
        if pid in team_leaders and not p.get('squad'):
            rows.append({'person':p,'type':'ROLE EXCEPTION','severity':'red','detail':'Active Team Leader / assistant leadership appointment has no squad assignment.'})
        if str(p.get('field_status') or '').upper()=='ASSIGNED' and pid not in current_weapons:
            rows.append({'person':p,'type':'PROPERTY EXCEPTION','severity':'amber','detail':'Active assigned Soldier has no current M16 issue.'})
        if pid not in links:
            rows.append({'person':p,'type':'DISCORD EXCEPTION','severity':'amber','detail':'No linked Discord communications record.'})
        sq=sync.get(pid)
        if sq and str(sq.get('status') or '').upper()=='FAILED':
            rows.append({'person':p,'type':'DISCORD ROLE EXCEPTION','severity':'red','detail':sq.get('error_text') or 'Latest Discord role synchronization failed.'})
        if str(p.get('field_status') or '').upper()!='ASSIGNED' and (date.today()-_replacement_date(p.get('date_joined') or p.get('roster_entered_at') or p.get('created_at'))).days>=7:
            rows.append({'person':p,'type':'PROCESSING EXCEPTION','severity':'amber','detail':'Soldier has remained in Replacement Detachment for 7+ days.'})
        if len(rows)>=limit: break
    return rows[:limit]


def s1_priority_work(rows=None,limit=12):
    rows=rows if rows is not None else replacement_detachment_rows()
    priority_order={'HOLD':0,'ASSIGNMENT PENDING':1,'READY FOR ASSIGNMENT':2,'IN-PROCESSING':3,'NEW ARRIVALS':4,'TRAINING':5}
    ordered=sorted(rows,key=lambda r:(priority_order.get(r['stage'],9),r['person'].get('date_joined') or date.today(),r['person'].get('last_name') or ''))
    return ordered[:limit]


def _staff_brief(role):
    section=_staff_section(role)
    personnel_total=int((fetch_one("SELECT COUNT(*) total FROM personnel WHERE archived=FALSE AND separated_at IS NULL") or {"total":0})["total"] or 0)
    ready=int((fetch_one("SELECT COUNT(*) total FROM personnel WHERE archived=FALSE AND separated_at IS NULL AND readiness_percent>=80") or {"total":0})["total"] or 0)
    replacements=int((fetch_one("SELECT COUNT(*) total FROM recruiting_cases WHERE status IN ('SUBMITTED','DISCORD_VERIFIED','PENDING_COMMAND','MORE_INFO_REQUIRED','APPROVED_AWAITING_DISCORD','REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING')") or {"total":0})["total"] or 0)
    replacement_detachment=0
    if role in {'s1','battalion_hq','commander','admin'}:
        try:
            replacement_detachment=len(replacement_detachment_rows())
        except Exception:
            log.exception('Replacement Detachment count failed while building staff brief.')
    recruit_review=int((fetch_one("SELECT COUNT(*) total FROM recruiting_cases WHERE status IN ('SUBMITTED','DISCORD_VERIFIED','PENDING_COMMAND','MORE_INFO_REQUIRED')") or {"total":0})["total"] or 0)
    qual_expiring=int((fetch_one("SELECT COUNT(*) total FROM qualifications WHERE status='CURRENT' AND expires_at BETWEEN CURRENT_DATE AND CURRENT_DATE+30") or {"total":0})["total"] or 0)
    weapons_due=int((fetch_one("""SELECT COUNT(*) total FROM weapon_issue_history wih JOIN weapon_inventory wi ON wi.id=wih.weapon_id
                                   WHERE wih.is_current=TRUE AND (wi.last_inspected_at IS NULL OR wi.last_inspected_at<NOW()-INTERVAL '14 days')""") or {"total":0})["total"] or 0)
    inactivity7=int((fetch_one("""SELECT COUNT(*) total FROM personnel WHERE archived=FALSE AND separated_at IS NULL
                                   AND COALESCE(activity_last_seen_at,created_at)<NOW()-INTERVAL '7 days'""") or {"total":0})["total"] or 0)
    action_where="" if not section else " AND owning_section=%s"
    params=() if not section else (section,)
    open_actions=int((fetch_one(f"SELECT COUNT(*) total FROM personnel_actions WHERE status NOT IN ('COMPLETE','CLOSED','DENIED'){action_where}",params) or {"total":0})["total"] or 0)
    overdue=int((fetch_one(f"SELECT COUNT(*) total FROM personnel_actions WHERE status NOT IN ('COMPLETE','CLOSED','DENIED') AND due_date<CURRENT_DATE{action_where}",params) or {"total":0})["total"] or 0)
    next_op=fetch_one("""SELECT * FROM operations WHERE operation_date>=CURRENT_DATE AND UPPER(COALESCE(status,'')) NOT IN ('CLOSED','CANCELLED')
                         ORDER BY operation_date,created_at LIMIT 1""")
    return {"personnel":personnel_total,"ready":ready,"replacements":replacements,"recruit_review":recruit_review,
            "qual_expiring":qual_expiring,"weapons_due":weapons_due,"inactivity7":inactivity7,"open_actions":open_actions,
            "overdue":overdue,"next_op":next_op,"replacement_detachment":replacement_detachment,"readiness_pct":round((ready/personnel_total)*100) if personnel_total else 0}

def _staff_attention_items(role):
    section=_staff_section(role)
    items=[]
    if role in {"battalion_hq","commander","admin","s1"}:
        c=int((fetch_one("SELECT COUNT(*) total FROM recruiting_cases WHERE status IN ('SUBMITTED','DISCORD_VERIFIED','PENDING_COMMAND','MORE_INFO_REQUIRED')") or {"total":0})["total"] or 0)
        if c: items.append({"level":"red","count":c,"title":"Recruit applications awaiting review","endpoint":"recruiting_control","detail":"Command / S-1 decision required"})
        try:
            rc=len(replacement_detachment_rows())
        except Exception:
            log.exception('Replacement Detachment attention item failed; continuing Action Center load.')
            rc=0
        if rc: items.append({"level":"amber","count":rc,"title":"Replacement Detachment requires processing","endpoint":"replacement_detachment","detail":"Click a Soldier and complete the next required S-1 action"})
    if role in {"battalion_hq","commander","admin","s3","training"}:
        c=int((fetch_one("SELECT COUNT(*) total FROM qualifications WHERE status='CURRENT' AND expires_at BETWEEN CURRENT_DATE AND CURRENT_DATE+30") or {"total":0})["total"] or 0)
        if c: items.append({"level":"amber","count":c,"title":"Qualifications expiring within 30 days","endpoint":"training","detail":"Schedule requalification"})
    if role in {"battalion_hq","commander","admin","s4"}:
        c=int((fetch_one("""SELECT COUNT(*) total FROM weapon_issue_history wih JOIN weapon_inventory wi ON wi.id=wih.weapon_id
                              WHERE wih.is_current=TRUE AND (wi.last_inspected_at IS NULL OR wi.last_inspected_at<NOW()-INTERVAL '14 days')""") or {"total":0})["total"] or 0)
        if c: items.append({"level":"amber","count":c,"title":"M16 inspections due","endpoint":"arms_room","detail":"S-4 inspection action required"})
    action_where="" if not section else " AND owning_section=%s"
    params=() if not section else (section,)
    c=int((fetch_one(f"SELECT COUNT(*) total FROM personnel_actions WHERE status NOT IN ('COMPLETE','CLOSED','DENIED') AND due_date<CURRENT_DATE{action_where}",params) or {"total":0})["total"] or 0)
    if c: items.append({"level":"red","count":c,"title":"Overdue staff actions","endpoint":"personnel_actions","detail":"Resolve or reroute overdue suspense"})
    if role in {"battalion_hq","commander","admin","s1"}:
        c=int((fetch_one("""SELECT COUNT(*) total FROM personnel WHERE archived=FALSE AND separated_at IS NULL
                              AND COALESCE(activity_last_seen_at,created_at)<NOW()-INTERVAL '7 days'""") or {"total":0})["total"] or 0)
        if c: items.append({"level":"amber","count":c,"title":"Personnel inactivity watch","endpoint":"personnel_office","detail":"7+ days since recorded activity"})
    return items

def staff_suspense_summary():
    """Role-aware suspense counts for the Staff Action Center."""
    section=_staff_section()
    section_sql="" if not section else " AND owning_section=%s"
    params=() if not section else (section,)
    base="status NOT IN (\'COMPLETE\',\'CLOSED\',\'DENIED\') AND due_date IS NOT NULL"

    due_today=fetch_one(
        f"SELECT COUNT(*) AS total FROM personnel_actions WHERE {base} AND due_date=CURRENT_DATE{section_sql}",
        params,
    ) or {"total":0}
    due_7=fetch_one(
        f"SELECT COUNT(*) AS total FROM personnel_actions WHERE {base} AND due_date>CURRENT_DATE AND due_date<=CURRENT_DATE+7{section_sql}",
        params,
    ) or {"total":0}
    overdue=fetch_one(
        f"SELECT COUNT(*) AS total FROM personnel_actions WHERE {base} AND due_date<CURRENT_DATE{section_sql}",
        params,
    ) or {"total":0}
    return {
        "due_today":int(due_today.get("total") or 0),
        "due_7":int(due_7.get("total") or 0),
        "overdue":int(overdue.get("total") or 0),
    }


@app.get('/staff')
@login_required
def staff_action_center():
    role=session.get('access_role')
    if role not in STAFF_ROLES: abort(403)
    q=(request.args.get('q') or '').strip()
    section=_staff_section(role)
    brief=_staff_brief(role)
    attention=_staff_attention_items(role)
    search_rows=_staff_search_rows(q) if q else []
    where="" if not section else " WHERE section=%s"
    params=() if not section else (section,)
    recent=fetch_all(f"SELECT * FROM staff_duty_log{where} ORDER BY created_at DESC LIMIT 12",params)
    action_where="" if not section else " AND pa.owning_section=%s"
    action_params=() if not section else (section,)
    open_rows=fetch_all(f"""SELECT pa.*,p.rank_code,p.first_name,p.last_name,p.unit_code FROM personnel_actions pa
                            LEFT JOIN personnel p ON p.id=pa.personnel_id
                            WHERE pa.status NOT IN ('COMPLETE','CLOSED','DENIED'){action_where}
                            ORDER BY CASE pa.priority WHEN 'URGENT' THEN 0 WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 ELSE 2 END,
                                     pa.due_date NULLS LAST,pa.created_at DESC LIMIT 8""",action_params)
    personnel_choices=fetch_all("SELECT id,rank_code,last_name,first_name,unit_code FROM personnel WHERE archived=FALSE AND separated_at IS NULL ORDER BY last_name,first_name")
    watchlist=fetch_all("""SELECT cw.*,p.rank_code,p.first_name,p.last_name,p.unit_code FROM command_watchlist cw
                          JOIN personnel p ON p.id=cw.personnel_id WHERE cw.resolved_at IS NULL ORDER BY cw.created_at DESC LIMIT 8""") if role in {'battalion_hq','commander','admin'} else []
    suspense_summary=staff_suspense_summary()
    replacement_rows=[]
    personnel_exceptions=[]
    if role in {'s1','battalion_hq','commander','admin'}:
        try:
            replacement_rows=replacement_detachment_rows()
        except Exception:
            log.exception('Replacement Detachment summary failed; Action Center will remain available.')
        try:
            personnel_exceptions=personnel_exception_rows(20)
        except Exception:
            log.exception('Personnel exception scan failed; Action Center will remain available.')
    return render_template('staff_action_center.html',role=role,section=section,brief=brief,attention=attention,
                           search_query=q,search_rows=search_rows,recent_actions=recent,open_actions=open_rows,
                           personnel_choices=personnel_choices,command_watchlist=watchlist,suspense_summary=suspense_summary,
                           replacement_rows=replacement_rows,priority_work=s1_priority_work(replacement_rows) if replacement_rows else [],
                           personnel_exceptions=personnel_exceptions)

@app.get('/staff/personnel/<personnel_id>')
@login_required
def staff_personnel_snapshot(personnel_id):
    role=session.get('access_role')
    if role not in STAFF_ROLES: abort(403)
    p=fetch_one("SELECT * FROM personnel WHERE id=%s",(personnel_id,))
    if not p: abort(404)
    p=soldier_view(p)
    weapon=current_weapon_for(p)
    quals=fetch_all("SELECT * FROM qualifications WHERE personnel_id=%s ORDER BY expires_at NULLS LAST,qualification_name LIMIT 10",(personnel_id,))
    recent=fetch_all("SELECT * FROM personnel_service_history WHERE personnel_id=%s ORDER BY entry_date DESC,created_at DESC LIMIT 10",(personnel_id,))
    return render_template('staff_personnel_snapshot.html',personnel=p,weapon=weapon,qualifications=quals,recent=recent,role=role)

@app.get('/staff/personnel/<personnel_id>/drawer')
@login_required
def staff_personnel_drawer(personnel_id):
    role=session.get('access_role')
    if role not in STAFF_ROLES: abort(403)
    person=fetch_one("SELECT * FROM personnel WHERE id=%s AND archived=FALSE AND separated_at IS NULL",(personnel_id,))
    if not person: abort(404)
    person=soldier_view(person)
    catalogs=personnel_form_catalogs()
    weapon=current_weapon_for(person)
    actions=fetch_all("""SELECT * FROM personnel_actions WHERE personnel_id=%s AND status NOT IN ('COMPLETE','CLOSED','DENIED')
                         ORDER BY CASE priority WHEN 'URGENT' THEN 0 WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 ELSE 2 END,due_date NULLS LAST,created_at""",(personnel_id,))
    try:
        replacement=next((r for r in replacement_detachment_rows() if str(r['person']['id'])==str(personnel_id)),None)
    except Exception:
        log.exception('Replacement detail failed for Personnel Quick File %s; drawer will remain available.', personnel_id)
        replacement=None
    return render_template('_staff_personnel_drawer.html',personnel=person,weapon=weapon,actions=actions,replacement=replacement,
                           catalogs=catalogs,role=role,authority=session.get('display_name') or session.get('username') or role.upper(),today=date.today())


@app.get('/staff/replacement-detachment')
@login_required
def replacement_detachment():
    if session.get('access_role') not in {'s1','battalion_hq','commander','admin'}: abort(403)
    try:
        rows=replacement_detachment_rows()
    except Exception:
        log.exception('Replacement Detachment core roster failed; rendering an empty recoverable workspace.')
        rows=[]
        flash('REPLACEMENT ROSTER COULD NOT LOAD ALL PERSONNEL DATA. SECONDARY RECORDS MAY STILL BE MIGRATING.','warning')
    counts=replacement_detachment_counts(rows)
    focus=request.args.get('focus') or None
    try:
        exceptions=personnel_exception_rows()
    except Exception:
        log.exception('Replacement Detachment exception scan failed; continuing.')
        exceptions=[]
    try:
        catalogs=personnel_form_catalogs()
    except Exception:
        log.exception('Replacement Detachment catalogs failed; continuing with roster-only view.')
        catalogs={'assignment_options':[],'mos_catalog':[],'duty_positions':[]}
    return render_template('replacement_detachment.html',rows=rows,counts=counts,priority_work=s1_priority_work(rows),
                           exceptions=exceptions,catalogs=catalogs,focus=focus,
                           authority=session.get('display_name') or session.get('username') or session.get('access_role').upper())


@app.post('/staff/replacement-detachment/<personnel_id>/action')
@login_required
def replacement_quick_action(personnel_id):
    role=session.get('access_role')
    if role not in {'s1','battalion_hq','commander','admin'}: abort(403)
    person=fetch_one("SELECT * FROM personnel WHERE id=%s AND archived=FALSE AND separated_at IS NULL",(personnel_id,))
    if not person: abort(404)
    action=(request.form.get('quick_action') or '').upper()
    authority=session.get('display_name') or session.get('username') or role.upper()
    if action=='BEGIN_INPROCESSING':
        execute("INSERT INTO personnel_progress_control(personnel_id) VALUES(%s) ON CONFLICT(personnel_id) DO NOTHING",(personnel_id,))
        program='REPLACEMENT' if (initial_entry_rank(personnel_id) or person.get('rank_code') or 'PVT')=='PVT' else 'INITIAL_INPROCESSING'
        execute("""INSERT INTO personnel_training_records(personnel_id,program_code,status,started_at)
                   VALUES(%s,%s,'IN PROGRESS',CURRENT_DATE) ON CONFLICT(personnel_id,program_code) DO NOTHING""",(personnel_id,program))
        pa=open_personnel_action(personnel_id,'PERSONNEL','Initial S-1 In-Processing','S-1','HIGH',authority,
                                 {'workflow':'REPLACEMENT DETACHMENT'},source_key=f'REPLACEMENT-INPROCESS:{personnel_id}',due_date=date.today()+timedelta(days=3))
        if pa:
            transition_personnel_action(pa['id'],pa.get('status') or 'OPEN',authority,'In-processing accepted by S-1.',assigned_to=authority,section='S-1')
        write_service_entry(personnel_id,'ADMIN','IN-PROCESSING OPENED','S-1 opened the Soldier for Replacement Detachment in-processing.',authority)
        flash('IN-PROCESSING OPENED AND ASSIGNED TO THIS S-1 CLERK.','success')
    elif action=='COMPLETE_S1':
        execute("INSERT INTO personnel_progress_control(personnel_id) VALUES(%s) ON CONFLICT(personnel_id) DO NOTHING",(personnel_id,))
        execute("UPDATE personnel_progress_control SET s1_onboarded_at=COALESCE(s1_onboarded_at,NOW()),s1_onboarded_by=COALESCE(s1_onboarded_by,%s),updated_at=NOW() WHERE personnel_id=%s",(authority,personnel_id))
        write_service_entry(personnel_id,'ADMIN','S-1 ONBOARDING COMPLETE','Completed initial S-1 personnel interview and Replacement Detachment in-processing review.',authority)
        for pa in fetch_all("""SELECT * FROM personnel_actions WHERE personnel_id=%s
                               AND status NOT IN ('COMPLETE','CLOSED','DENIED')
                               AND owning_section='S-1'
                               AND (subject ILIKE '%%IN-PROCESS%%' OR source_key=%s)""",
                            (personnel_id,f'REPLACEMENT-INPROCESS:{personnel_id}')):
            transition_personnel_action(pa['id'],'COMPLETE',authority,'S-1 onboarding requirements completed.')
        try: finalize_replacement_release(personnel_id,authority)
        except Exception: log.exception('Replacement release check failed after S-1 onboarding for %s',personnel_id)
        flash('S-1 ONBOARDING COMPLETED.','success')
    elif action=='ISSUE_M16':
        before=fetch_one("SELECT wi.serial_number FROM weapon_issue_history wih JOIN weapon_inventory wi ON wi.id=wih.weapon_id WHERE wih.personnel_id=%s AND wih.is_current=TRUE",(personnel_id,))
        weapon=issue_m16(personnel_id)
        if not before and weapon:
            write_service_entry(personnel_id,'EQUIPMENT','INDIVIDUAL WEAPON ISSUED',f"Issued U.S. Rifle, 5.56-MM, M16, Serial No. {weapon['serial_number']}, Rack No. {weapon['rack_number']}.",authority)
        try: finalize_replacement_release(personnel_id,authority)
        except Exception: log.exception('Replacement release check failed after M16 issue for %s',personnel_id)
        flash(f"M16 {weapon.get('serial_number') if weapon else ''} ISSUE CHECK COMPLETE.",'success')
    elif action=='COMPLETE_TRAINING':
        profile=entry_processing_profile(person)
        progress=personnel_progress(personnel_id)
        if not progress.get('s1_onboarded_at') or not progress.get('rules_acknowledged_at'):
            flash('COMPLETE S-1 ONBOARDING AND STANDING ORDERS ACKNOWLEDGMENT BEFORE CERTIFYING INITIAL TRAINING.','warning')
        else:
            certify_training_program(personnel_id,profile['program_code'],authority,'Certified from Replacement Detachment processing.')
            for a in fetch_all("SELECT * FROM personnel_actions WHERE personnel_id=%s AND status NOT IN ('COMPLETE','CLOSED','DENIED') AND action_type='TRAINING'",(personnel_id,)):
                if 'REPLACEMENT' in str(a.get('subject') or '').upper() or 'INITIAL' in str(a.get('subject') or '').upper():
                    transition_personnel_action(a['id'],'COMPLETE',authority,'Initial training / in-processing certified.')
            try: finalize_replacement_release(personnel_id,authority)
            except Exception: log.exception('Replacement release check failed after training certification for %s',personnel_id)
            flash(f"{profile['program_title'].upper()} CERTIFIED.",'success')
    elif action=='SEND_REMINDER':
        notify_soldier(personnel_id,'S-1','Replacement Detachment action required','Open your Soldier Record and complete the outstanding in-processing requirement.',priority='HIGH',source_key=f'REPLACEMENT-REMINDER:{personnel_id}:{date.today()}',target_anchor='replacement-training')
        flash('SOLDIER REMINDER FILED.','success')
    elif action=='ASSIGN_MOS':
        catalogs=personnel_form_catalogs(); mos=validate_system_choice((request.form.get('mos_code') or '').upper(),catalogs['mos_catalog'],'mos_code')
        if not mos: abort(400)
        file_primary_mos_change(personnel_id,mos['mos_code'],date.today(),authority,request.form.get('remarks') or None)
        flash('PRIMARY MOS FILED.','success')
    elif action=='ASSIGN_FORMATION':
        catalogs=personnel_form_catalogs()
        node=validate_system_choice(request.form.get('unit_node_id'),catalogs['assignment_options'],'id')
        duty=validate_system_choice(request.form.get('duty_position'),catalogs['duty_positions'],'value')
        mos=validate_system_choice((request.form.get('mos_code') or person.get('mos_code') or '').upper(),catalogs['mos_catalog'],'mos_code')
        if not node or not duty or not mos:
            flash('SELECT FORMATION, MOS, AND DUTY POSITION FROM THE AUTHORIZED LISTS.','danger')
        else:
            file_primary_mos_change(personnel_id,mos['mos_code'],date.today(),authority,request.form.get('remarks') or None)
            process_assignment_action(personnel_id,node['id'],duty['value'],date.today(),authority,None,request.form.get('remarks') or None)
            # Close the initial S-1 assignment suspense if one exists.
            for a in fetch_all("SELECT * FROM personnel_actions WHERE personnel_id=%s AND status NOT IN ('COMPLETE','CLOSED','DENIED') AND action_type IN ('ASSIGNMENT','TRANSFER')",(personnel_id,)):
                transition_personnel_action(a['id'],'COMPLETE',authority,'Permanent formation assignment filed.')
            try:
                released,missing=finalize_replacement_release(personnel_id,authority)
            except Exception:
                log.exception('Replacement release evaluation failed after Quick Action assignment for %s',personnel_id)
                released=False
                missing=['Release checklist could not be fully evaluated; assignment was filed successfully']
            if released:
                flash('PERMANENT FORMATION ASSIGNMENT FILED — SOLDIER RELEASED FROM REPLACEMENT DETACHMENT.','success')
            else:
                flash('PERMANENT FORMATION ASSIGNMENT FILED; REMAINING REPLACEMENT ACTIONS: '+', '.join(missing),'warning')
    elif action=='OPEN_WORKFLOW':
        kind=(request.form.get('action_type') or 'PERSONNEL').upper(); subject=(request.form.get('subject') or f'{kind} ACTION').strip()
        section=action_section_for_type(kind) or 'S-1'
        pa=open_personnel_action(personnel_id,kind,subject,section,request.form.get('priority') or 'ROUTINE',authority,
                                 {'remarks':request.form.get('remarks') or '','replacement_detachment':True},due_date=request.form.get('due_date') or None)
        if pa and request.form.get('claim')=='YES': transition_personnel_action(pa['id'],pa.get('status') or 'OPEN',authority,'Action claimed from Replacement Detachment.',assigned_to=authority,section=section)
        flash(f'{kind} ACTION OPENED.','success')
    elif action=='COMPLETE_INPROCESSING':
        released,missing=finalize_replacement_release(personnel_id,authority)
        if not released:
            flash('CANNOT RELEASE SOLDIER — OUTSTANDING: '+', '.join(missing),'warning')
        else:
            flash('SOLDIER RELEASED FROM REPLACEMENT DETACHMENT AND RECRUITING CASE CLOSED AS ENLISTED.','success')
    else:
        abort(400)
    return redirect(request.form.get('return_to') or url_for('replacement_detachment',focus=personnel_id))


@app.post('/staff/replacement-detachment/batch')
@login_required
def replacement_batch_action():
    """Apply safe routine S-1 actions to several replacement Soldiers at once.

    Permanent assignment changes remain structured and explicit; no promotions or
    awards are silently filed through batch processing.
    """
    role=session.get('access_role')
    if role not in {'s1','battalion_hq','commander','admin'}: abort(403)
    ids=[x for x in request.form.getlist('personnel_ids') if x][:75]
    if not ids:
        flash('SELECT AT LEAST ONE REPLACEMENT SOLDIER.','warning')
        return redirect(url_for('replacement_detachment'))
    action=(request.form.get('batch_action') or '').upper()
    authority=session.get('display_name') or session.get('username') or role.upper()
    processed=0; skipped=0
    for pid in ids:
        person=fetch_one("SELECT * FROM personnel WHERE id=%s AND archived=FALSE AND separated_at IS NULL",(pid,))
        if not person:
            skipped+=1; continue
        current=next((r for r in replacement_detachment_rows() if str(r['person']['id'])==str(pid)),None)
        if not current:
            skipped+=1; continue
        if action=='BEGIN_INPROCESSING':
            execute("INSERT INTO personnel_progress_control(personnel_id) VALUES(%s) ON CONFLICT(personnel_id) DO NOTHING",(pid,))
            program='REPLACEMENT' if (initial_entry_rank(pid) or person.get('rank_code') or 'PVT')=='PVT' else 'INITIAL_INPROCESSING'
            execute("""INSERT INTO personnel_training_records(personnel_id,program_code,status,started_at)
                       VALUES(%s,%s,'IN PROGRESS',CURRENT_DATE) ON CONFLICT(personnel_id,program_code) DO NOTHING""",(pid,program))
            pa=open_personnel_action(pid,'PERSONNEL','Initial S-1 In-Processing','S-1','HIGH',authority,
                                     {'workflow':'REPLACEMENT DETACHMENT','batch':True},source_key=f'REPLACEMENT-INPROCESS:{pid}',due_date=date.today()+timedelta(days=3))
            if pa:
                transition_personnel_action(pa['id'],pa.get('status') or 'OPEN',authority,'Batch in-processing accepted by S-1.',assigned_to=authority,section='S-1')
            processed+=1
        elif action=='COMPLETE_S1':
            execute("INSERT INTO personnel_progress_control(personnel_id) VALUES(%s) ON CONFLICT(personnel_id) DO NOTHING",(pid,))
            execute("UPDATE personnel_progress_control SET s1_onboarded_at=COALESCE(s1_onboarded_at,NOW()),s1_onboarded_by=COALESCE(s1_onboarded_by,%s),updated_at=NOW() WHERE personnel_id=%s",(authority,pid))
            write_service_entry(pid,'ADMIN','S-1 ONBOARDING COMPLETE','Completed initial S-1 review through Replacement Detachment batch processing.',authority)
            for pa in fetch_all("""SELECT * FROM personnel_actions WHERE personnel_id=%s
                                   AND status NOT IN ('COMPLETE','CLOSED','DENIED')
                                   AND owning_section='S-1'
                                   AND (subject ILIKE '%%IN-PROCESS%%' OR source_key=%s)""",
                                (pid,f'REPLACEMENT-INPROCESS:{pid}')):
                transition_personnel_action(pa['id'],'COMPLETE',authority,'S-1 onboarding requirements completed through batch processing.')
            try: finalize_replacement_release(pid,authority)
            except Exception: log.exception('Replacement release check failed after batch S-1 completion for %s',pid)
            processed+=1
        elif action=='ISSUE_M16':
            before=fetch_one("SELECT wi.id FROM weapon_issue_history wih JOIN weapon_inventory wi ON wi.id=wih.weapon_id WHERE wih.personnel_id=%s AND wih.is_current=TRUE",(pid,))
            weapon=issue_m16(pid)
            if weapon:
                if not before:
                    write_service_entry(pid,'EQUIPMENT','INDIVIDUAL WEAPON ISSUED',f"Issued U.S. Rifle, 5.56-MM, M16, Serial No. {weapon['serial_number']}, Rack No. {weapon['rack_number']}.",authority)
                try: finalize_replacement_release(pid,authority)
                except Exception: log.exception('Replacement release check failed after batch M16 issue for %s',pid)
                processed+=1
            else: skipped+=1
        elif action=='SEND_REMINDER':
            notify_soldier(pid,'S-1','Replacement Detachment action required','Open your Soldier Record and complete the outstanding in-processing requirement.',priority='HIGH',source_key=f'REPLACEMENT-REMINDER:{pid}:{date.today()}',target_anchor='replacement-training')
            processed+=1
        elif action=='OPEN_TRAINING_ACTION':
            open_personnel_action(pid,'TRAINING','Replacement Training Required','S-3','ROUTINE',authority,
                                  {'workflow':'REPLACEMENT DETACHMENT','batch':True},source_key=f'REPLACEMENT-TRAINING-ACTION:{pid}')
            processed+=1
        elif action=='COMPLETE_TRAINING':
            profile=entry_processing_profile(person)
            progress=personnel_progress(pid)
            if progress.get('s1_onboarded_at') and progress.get('rules_acknowledged_at'):
                certify_training_program(pid,profile['program_code'],authority,'Certified through Replacement Detachment batch processing.')
                for pa in fetch_all("""SELECT * FROM personnel_actions WHERE personnel_id=%s
                                       AND status NOT IN ('COMPLETE','CLOSED','DENIED')
                                       AND action_type='TRAINING'""",(pid,)):
                    if 'REPLACEMENT' in str(pa.get('subject') or '').upper() or 'INITIAL' in str(pa.get('subject') or '').upper():
                        transition_personnel_action(pa['id'],'COMPLETE',authority,'Initial training completed through batch processing.')
                try: finalize_replacement_release(pid,authority)
                except Exception: log.exception('Replacement release check failed after batch training for %s',pid)
                processed+=1
            else:
                skipped+=1
        elif action=='SET_HOLD':
            note=(request.form.get('remarks') or 'S-1 administrative hold').strip()
            open_personnel_action(pid,'ADMIN HOLD','Replacement Detachment Hold','S-1','HIGH',authority,
                                  {'remarks':note,'workflow':'REPLACEMENT DETACHMENT'},source_key=f'REPLACEMENT-HOLD:{pid}')
            processed+=1
        else:
            abort(400)
    staff_log('S-1','REPLACEMENT BATCH ACTION',f'{action} — {processed} Soldier(s)',authority,
              details={'processed':processed,'skipped':skipped,'action':action})
    flash(f'BATCH ACTION COMPLETE — {processed} PROCESSED'+(f'; {skipped} SKIPPED.' if skipped else '.'),'success')
    return redirect(url_for('replacement_detachment'))


@app.post('/staff/personnel-action/<action_id>/quick')
@login_required
def personnel_action_quick(action_id):
    role=session.get('access_role')
    if role not in {'s1','s3','s4','training','battalion_hq','commander','admin'}: abort(403)
    row=fetch_one("SELECT * FROM personnel_actions WHERE id=%s",(action_id,))
    if not row: abort(404)
    section=_staff_section(role)
    if section and row.get('owning_section') not in {section,'HQ'}: abort(403)
    authority=session.get('display_name') or session.get('username') or role.upper()
    action=(request.form.get('quick_action') or '').upper()
    if action=='CLAIM':
        transition_personnel_action(row['id'],row.get('status') or 'OPEN',authority,'Action claimed.',assigned_to=authority,section=row.get('owning_section'))
        flash('ACTION CLAIMED.','success')
    elif action=='COMPLETE':
        transition_personnel_action(row['id'],'COMPLETE',authority,request.form.get('remarks') or 'Action completed from quick control.')
        flash('ACTION COMPLETED AND FILED IN THE AUDIT LEDGER.','success')
    elif action=='IN_REVIEW':
        transition_personnel_action(row['id'],'IN REVIEW',authority,request.form.get('remarks') or 'Action moved to review.',assigned_to=authority,section=row.get('owning_section'))
        flash('ACTION MOVED TO IN REVIEW.','success')
    else: abort(400)
    return redirect(request.form.get('return_to') or request.referrer or url_for('staff_action_center'))


@app.post('/staff/batch-action')
@login_required
def staff_batch_action():
    role=session.get('access_role')
    if role not in STAFF_ROLES-{"s2"}: abort(403)
    ids=[x for x in request.form.getlist('personnel_ids') if x]
    if not ids:
        flash('SELECT AT LEAST ONE SOLDIER BEFORE FILING A BATCH ACTION.','warning')
        return redirect(request.referrer or url_for('staff_action_center'))
    kind=(request.form.get('action_type') or 'PERSONNEL').upper()
    subject=(request.form.get('subject') or f'BATCH {kind} ACTION').strip()
    remarks=(request.form.get('remarks') or '').strip()
    section=(request.form.get('owning_section') or _staff_section(role) or action_section_for_type(kind) or 'HQ').upper()
    if role not in {'battalion_hq','commander','admin'}:
        allowed={'s1':'S-1','s3':'S-3','training':'S-3','s4':'S-4'}.get(role)
        if allowed and section not in {allowed,'HQ'}: section=allowed
    authority=session.get('display_name') or session.get('username') or role.upper()
    created=0
    for pid in ids[:75]:
        p=fetch_one('SELECT id FROM personnel WHERE id=%s AND archived=FALSE AND separated_at IS NULL',(pid,))
        if not p: continue
        open_personnel_action(pid,kind,subject,section,'ROUTINE',authority,{'remarks':remarks,'batch':True})
        created+=1
    staff_log(section,'BATCH ACTION',f'{subject} — {created} Soldier(s)',authority,details={'count':created,'action_type':kind})
    flash(f'BATCH ACTION FILED FOR {created} SOLDIER(S).','success')
    return redirect(request.referrer or url_for('staff_action_center'))


@app.get('/staff/personnel-search')
@login_required
def smart_personnel_search_page():
    if session.get('access_role') not in STAFF_ROLES: abort(403)
    q=(request.args.get('q') or '').strip()
    rows=smart_personnel_search(q,150) if q else []
    return render_template('smart_personnel_search.html',query=q,rows=rows)


@app.get('/staff/personnel-compare')
@login_required
def personnel_compare_page():
    if session.get('access_role') not in STAFF_ROLES: abort(403)
    a=request.args.get('a'); b=request.args.get('b')
    choices=fetch_all("SELECT id,rank_code,first_name,last_name,unit_code FROM personnel WHERE separated_at IS NULL AND archived=FALSE ORDER BY last_name,first_name")
    left=comparison_snapshot(a) if a else None; right=comparison_snapshot(b) if b else None
    return render_template('personnel_compare.html',choices=choices,left=left,right=right,a=a,b=b)


@app.get('/unit/<unit_node_id>/history')
def unit_history_page(unit_node_id):
    snap=None
    try:
        snap=unit_history_snapshot(unit_node_id) if database_ready() else None
    except Exception:
        app.logger.exception("Public unit history unavailable")
    if not snap: abort(404)
    return render_template('unit_history.html',snapshot=snap)


@app.get('/headquarters/leadership-lineage')
@login_required
def leadership_lineage_page():
    if session.get('access_role') not in STAFF_ROLES: abort(403)
    changes=fetch_all("""SELECT cch.*,po.rank_code AS outgoing_rank,po.last_name AS outgoing_last,pi.rank_code AS incoming_rank,pi.last_name AS incoming_last
                           FROM command_change_history cch LEFT JOIN personnel po ON po.id=cch.outgoing_personnel_id LEFT JOIN personnel pi ON pi.id=cch.incoming_personnel_id
                           ORDER BY cch.effective_date DESC,cch.created_at DESC""")
    return render_template('leadership_lineage.html',rows=command_lineage(),command_changes=changes)


@app.get('/my-action-center')
@login_required
def my_action_center():
    person=linked_personnel()
    if not person: return redirect(url_for('login'))
    p=soldier_view(person)
    return render_template('member_action_center.html',personnel=p,items=member_personal_action_center(p),recommended=next_recommended_action(p),situation=current_situation_snapshot(p),reputation=field_reputation(p))


@app.route('/my-journal',methods=['GET','POST'])
@login_required
def my_journal():
    person=linked_personnel()
    if not person: abort(403)
    pid=person['id']
    if request.method=='POST':
        title=(request.form.get('title') or '').strip(); body=(request.form.get('body') or '').strip(); visibility=(request.form.get('visibility') or 'PRIVATE').upper(); op_id=request.form.get('operation_id') or None
        if visibility not in {'PRIVATE','UNIT'}: abort(400)
        if not title or not body:
            flash('TITLE AND JOURNAL ENTRY ARE REQUIRED.','danger')
        else:
            execute("INSERT INTO soldier_journal_entries(personnel_id,operation_id,entry_date,title,body,visibility) VALUES(%s,%s,%s,%s,%s,%s)",(pid,op_id,request.form.get('entry_date') or date.today(),title,body,visibility))
            if visibility=='UNIT':
                execute("""INSERT INTO soldier_tour_book(personnel_id,entry_date,entry_type,title,narrative,operation_id,source_key)
                           VALUES(%s,%s,'FIELD JOURNAL',%s,%s,%s,%s) ON CONFLICT(source_key) DO NOTHING""",
                        (pid,request.form.get('entry_date') or date.today(),title,body,op_id,f'JOURNAL:{pid}:{title}:{request.form.get("entry_date") or date.today()}'))
            flash('FIELD JOURNAL ENTRY FILED.','success')
        return redirect(url_for('my_journal'))
    rows=fetch_all("""SELECT sje.*,o.operation_number,o.title AS operation_title FROM soldier_journal_entries sje
                       LEFT JOIN operations o ON o.id=sje.operation_id WHERE sje.personnel_id=%s ORDER BY sje.entry_date DESC,sje.created_at DESC""",(pid,))
    ops=fetch_all("SELECT id,operation_number,title FROM operations ORDER BY COALESCE(start_at,created_at) DESC LIMIT 100")
    return render_template('member_journal.html',personnel=soldier_view(person),entries=rows,operations=ops)


@app.post('/personnel/<personnel_id>/watchlist/resolve')
@login_required
def resolve_watchlist(personnel_id):
    if session.get('access_role') not in {'battalion_hq','commander','admin'}: abort(403)
    wt=request.form.get('watch_type'); actor=session.get('display_name') or session.get('username') or 'COMMAND'
    execute("UPDATE command_watchlist SET resolved_at=NOW(),resolved_by=%s WHERE personnel_id=%s AND watch_type=%s AND resolved_at IS NULL",(actor,personnel_id,wt))
    flash('COMMAND WATCHLIST ENTRY RESOLVED.','success')
    return redirect(request.referrer or url_for('personnel_service_record',personnel_id=personnel_id))


@app.get('/staff/watchlist')
@login_required
def command_watchlist_page():
    if session.get('access_role') not in {'battalion_hq','commander','admin'}: abort(403)
    rows=fetch_all("""SELECT cw.*,p.rank_code,p.first_name,p.last_name,p.unit_code,p.platoon,p.squad,p.readiness_percent
                      FROM command_watchlist cw JOIN personnel p ON p.id=cw.personnel_id
                      WHERE cw.resolved_at IS NULL ORDER BY cw.watch_type,cw.created_at DESC""")
    return render_template('command_watchlist.html',rows=rows)


@app.get('/internal/clerk/role-sync/pending')
def clerk_role_sync_pending():
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    guild_id=request.args.get('guild_id')
    params=[]; where="WHERE q.status='PENDING'"
    if guild_id:
        where += ' AND q.guild_id=%s'; params.append(guild_id)
    rows=fetch_all(f"""SELECT q.id,q.personnel_id,q.guild_id,q.discord_user_id,q.reason,q.requested_at,
                        p.rank_code,p.mos_code,p.unit_code,p.platoon,p.squad
                        FROM discord_role_sync_queue q JOIN personnel p ON p.id=q.personnel_id
                        {where} ORDER BY q.requested_at LIMIT 50""",tuple(params))
    out=[]
    for r in rows:
        snap=canonical_personnel_snapshot(r['personnel_id']) or {}
        appts=fetch_all("""SELECT ac.appointment_name FROM personnel_appointments pa JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code
                           WHERE pa.personnel_id=%s AND pa.is_current=TRUE""",(r['personnel_id'],))
        item={**r,**snap}
        item['queue_id']=str(r['id']); item['linked']=True
        item['appointment_roles']=[x.get('appointment_name') for x in appts if x.get('appointment_name')]
        out.append(item)
    return {'ok':True,'items':out}


@app.post('/internal/clerk/role-sync/<queue_id>/complete')
def clerk_role_sync_complete(queue_id):
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    payload=request.get_json(silent=True) or {}; ok=bool(payload.get('ok',True)); err=payload.get('error')
    execute("UPDATE discord_role_sync_queue SET status=%s,processed_at=NOW(),error_text=%s WHERE id=%s",('COMPLETE' if ok else 'FAILED',err,queue_id))
    return {'ok':True}


@app.get('/internal/clerk/member-reminders')
def clerk_member_reminders():
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    rows=fetch_all("""SELECT p.id,p.rank_code,p.first_name,p.last_name,p.deros_date,p.unit_code,wml.guild_id,wml.discord_user_id
                      FROM personnel p JOIN website_member_links wml ON wml.personnel_id=p.id::text
                      WHERE p.separated_at IS NULL AND p.archived=FALSE""")
    reminders=[]
    for p in rows:
        pid=p['id']; insp=weapon_inspection_status(pid)
        if insp and insp.get('days') is not None and int(insp.get('days')) in {3,1,0}:
            reminders.append({'personnel_id':str(pid),'guild_id':p['guild_id'],'discord_user_id':p['discord_user_id'],'type':'M16_INSPECTION','stage':str(insp.get('days')),'reminder_key':f"M16:{insp.get('days')}",'message':f"**1/5 CAV — S-4 NOTICE**\nYour assigned M16 inspection is due in {max(0,int(insp.get('days')))} day(s)."})
        q=fetch_one("""SELECT MIN(due) due FROM (
                         SELECT expires_at due FROM qualifications WHERE personnel_id=%s AND expires_at IS NOT NULL
                         UNION ALL SELECT expiration_date due FROM personnel_duty_qualifications WHERE personnel_id=%s AND expiration_date IS NOT NULL) x
                         WHERE due BETWEEN CURRENT_DATE AND CURRENT_DATE+INTERVAL '7 days'""",(pid,pid))
        if q and q.get('due'):
            days=(q['due']-date.today()).days
            if days in {7,3,1,0}: reminders.append({'personnel_id':str(pid),'guild_id':p['guild_id'],'discord_user_id':p['discord_user_id'],'type':'QUALIFICATION_EXPIRATION','stage':str(days),'reminder_key':f"QUAL:{q.get('due')}:{days}",'message':f"**1/5 CAV — TRAINING NOTICE**\nA qualification on your Soldier Record expires in {days} day(s)."})
    return {'ok':True,'reminders':reminders}

@app.get("/dashboard")
@login_required
def dashboard():
    personnel = soldier_view(linked_personnel())
    upcoming = fetch_all("SELECT * FROM operations ORDER BY operation_date NULLS LAST, created_at DESC LIMIT 5")
    qualifications = []
    equipment = []
    service_history = []
    roster_card = None
    weapon = None
    if personnel:
        qualifications = fetch_all("SELECT * FROM qualifications WHERE personnel_id=%s ORDER BY expires_at NULLS LAST, qualification_name LIMIT 8", (personnel["id"],))
        equipment = fetch_all("SELECT * FROM equipment_issues WHERE personnel_id=%s ORDER BY item_type,nomenclature LIMIT 8", (personnel["id"],))
        service_history = fetch_all("SELECT * FROM personnel_service_history WHERE personnel_id=%s ORDER BY entry_date DESC, created_at DESC LIMIT 8", (personnel["id"],))
        roster_card = battle_roster_for(personnel)
        weapon = current_weapon_for(personnel)
    return render_template("dashboard.html", personnel=personnel, upcoming=upcoming, qualifications=qualifications, equipment=equipment, service_history=service_history, roster_card=roster_card, weapon=weapon)



def personnel_record_context(personnel):
    """Build the complete read-only 201 File / Service Record view."""
    if not personnel:
        return {
            "personnel": None, "qualifications": [], "equipment": [], "awards": [],
            "activity": [], "service_history": [], "assignments": [], "promotions": [],
            "appointments": [], "roster_card": None, "weapon": None,
            "chain_of_command": [], "readiness": {}, "tour_phase_record": ("NO RECORD", None),
            "duty_quals": [], "personal_ops": [], "issued_equipment": [],
            "replacement_training": {"complete":False,"requirements":[]}, "promotion_eligibility": [], "documents": [],
            "leadership_service": {"history":[],"totals":[],"total_days":0}, "leadership_score": {"score":0,"rating":"NOT RATED","breakdown":{}}, "mos_proficiency": None,
            "current_situation": {}, "field_reputation": [], "service_timeline": [], "weapon_personality": None,
            "award_evidence": {}, "promotion_packet": {}, "most_served_with": [], "member_action_center": [],
            "next_recommended_action": {}, "journal_entries": [], "command_watchlist": [],
        }

    personnel = soldier_view(personnel)
    pid = personnel["id"]
    # 201 File GET is read-oriented. Background/event actions own synchronization;
    # opening a personnel jacket must not trigger a chain of database writes.
    mos_proficiency = current_mos_proficiency(personnel)
    leadership_service = leadership_service_summary(pid)
    leadership_score = combat_leadership_score(pid)
    weapon = current_weapon_for(personnel)
    current_situation = current_situation_snapshot(personnel)
    reputation = field_reputation(personnel)
    service_timeline = active_service_timeline(pid)
    weapon_story = weapon_personality(weapon["id"]) if weapon else None
    award_evidence = award_recommendation_evidence(pid)
    promotion_packet = promotion_board_packet(pid)
    served_with = most_served_with(pid,5)
    member_actions = member_personal_action_center(personnel)
    recommended_action = next_recommended_action(personnel)
    journal_entries = fetch_all("""SELECT sje.*,o.operation_number,o.title AS operation_title FROM soldier_journal_entries sje
                                  LEFT JOIN operations o ON o.id=sje.operation_id
                                  WHERE sje.personnel_id=%s AND sje.visibility='UNIT'
                                  ORDER BY sje.entry_date DESC,sje.created_at DESC LIMIT 40""",(pid,))
    watchlist = fetch_all("SELECT * FROM command_watchlist WHERE personnel_id=%s AND resolved_at IS NULL ORDER BY created_at DESC",(pid,))

    promotions = fetch_all(
        """SELECT ph.*,rc.rank_name,rc.pay_grade
           FROM promotion_history ph
           LEFT JOIN rank_catalog rc ON rc.rank_code=ph.new_rank_code
           WHERE ph.personnel_id=%s
           ORDER BY ph.effective_date DESC,ph.created_at DESC""", (pid,)
    )
    appointments = fetch_all(
        """SELECT pa.*,ac.appointment_name
           FROM personnel_appointments pa
           JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code
           WHERE pa.personnel_id=%s
           ORDER BY pa.effective_date DESC,pa.created_at DESC""", (pid,)
    )
    return {
        "personnel": personnel,
        "qualifications": fetch_all("SELECT * FROM qualifications WHERE personnel_id=%s ORDER BY qualification_name", (pid,)),
        "equipment": fetch_all("SELECT * FROM equipment_issues WHERE personnel_id=%s ORDER BY item_type,nomenclature", (pid,)),
        "awards": fetch_all("SELECT * FROM personnel_awards WHERE personnel_id=%s ORDER BY award_date DESC", (pid,)),
        "award_catalog": fetch_all("SELECT ribbon_code,ribbon_name FROM ribbon_catalog WHERE is_active=TRUE ORDER BY sort_order,ribbon_name"),
        "activity": fetch_all("SELECT * FROM personnel_activity_credit WHERE personnel_id=%s ORDER BY activity_date DESC,created_at DESC LIMIT 100", (pid,)),
        "service_history": fetch_all("SELECT * FROM personnel_service_history WHERE personnel_id=%s ORDER BY entry_date DESC,created_at DESC LIMIT 150", (pid,)),
        "documents": fetch_all("SELECT * FROM personnel_documents WHERE personnel_id=%s ORDER BY effective_date DESC,created_at DESC LIMIT 100", (pid,)),
        "assignments": fetch_all("SELECT * FROM assignment_history WHERE personnel_id=%s ORDER BY effective_date DESC,created_at DESC", (pid,)),
        "promotions": promotions, "appointments": appointments,
        "roster_card": battle_roster_for(personnel), "weapon": weapon,
        "chain_of_command": chain_of_command_for(personnel),
        "readiness": soldier_readiness(personnel), "tour_phase_record": tour_phase(personnel),
        "inactivity": inactivity_snapshot(personnel),
        "inactivity_contacts": fetch_all("SELECT * FROM inactivity_contact_log WHERE personnel_id=%s ORDER BY contacted_at DESC LIMIT 12", (pid,)),
        "duty_quals": personnel_duty_qualifications(pid), "personal_ops": personal_operations(pid),
        "issued_equipment": current_equipment_for(pid),
        "replacement_training": replacement_training_status(personnel),
        "promotion_eligibility": promotion_eligibility(personnel),
        "leadership_service": leadership_service,
        "leadership_score": leadership_score,
        "mos_proficiency": mos_proficiency,
        "current_situation": current_situation,
        "field_reputation": reputation,
        "service_timeline": service_timeline,
        "weapon_personality": weapon_story,
        "award_evidence": award_evidence,
        "promotion_packet": promotion_packet,
        "most_served_with": served_with,
        "member_action_center": member_actions,
        "next_recommended_action": recommended_action,
        "journal_entries": journal_entries,
        "command_watchlist": watchlist,
        "uniform_issue": fetch_one(
            """SELECT eih.issued_at,ei.condition_state,sic.item_name
               FROM equipment_issue_history eih
               JOIN equipment_inventory ei ON ei.id=eih.equipment_id
               JOIN supply_item_catalog sic ON sic.item_code=ei.item_code
               WHERE eih.personnel_id=%s AND eih.is_current=TRUE AND ei.item_code='AG44'
               LIMIT 1""",
            (pid,),
        ),
        "earned_ribbons": worn_ribbon_rows(pid)[1],
        "uniform_ribbon_rows": worn_ribbon_rows(pid)[0],
        "personal_action_center": member_personal_action_center(personnel),
        "recommended_action": next_recommended_action(personnel),
        "field_reputation": field_reputation(personnel),
        "current_situation": current_situation_snapshot(personnel),
        "most_served_with": most_served_with(pid,5),
    }


@app.get("/personnel/<personnel_id>")
def personnel_service_record(personnel_id):
    if not database_ready(): abort(404)
    person = fetch_one("SELECT * FROM personnel WHERE id=%s", (personnel_id,))
    if not person: abort(404)
    return render_template("personnel_file.html", **personnel_record_context(person))


@app.get("/my-201-file")
@login_required
def my_201_file():
    return render_template("personnel_file.html", **personnel_record_context(linked_personnel()))



@app.post('/my-ribbons/<ribbon_id>/toggle')
@login_required
def member_ribbon_toggle(ribbon_id):
    personnel = linked_personnel()
    if not personnel:
        abort(403)
    ribbon = fetch_one("""SELECT pr.*,rc.ribbon_name,rc.image_filename FROM personnel_ribbons pr
                          JOIN ribbon_catalog rc ON rc.ribbon_code=pr.ribbon_code
                          WHERE pr.id=%s AND pr.personnel_id=%s""", (ribbon_id, personnel['id']))
    if not ribbon:
        abort(404)
    action = (request.form.get('action') or '').upper()
    if action not in {'WEAR', 'TAKE_OFF'}:
        abort(400)
    wear_flag = action == 'WEAR'
    execute("UPDATE personnel_ribbons SET is_worn=%s WHERE id=%s", (wear_flag, ribbon_id))
    flash(f"{ribbon['ribbon_name'].upper()} {'MARKED WORN' if wear_flag else 'REMOVED FROM UNIFORM'}.", 'success')
    target = request.form.get('return_to') or 'my_soldier_record'
    if target not in {'my_soldier_record','my_201_file'}:
        target='my_soldier_record'
    anchor = (request.form.get('anchor') or 'ribbons').strip().replace('#','')
    return redirect(url_for(target) + f'#{anchor}')

@app.get("/battle-roster-card")
@login_required
def battle_roster_card():
    personnel = soldier_view(linked_personnel())
    return render_template("battle_roster_card.html", personnel=personnel, roster_card=battle_roster_for(personnel))


_PUBLIC_DUTY_APPOINTMENT_ALIASES = {
    "BN_CO": {"battalion commander", "battalion commanding officer"},
    "BN_XO": {"battalion executive officer"},
    "BN_SGM": {"battalion sergeant major", "sergeant major"},
    "CO_CO": {"company commander", "company commanding officer", "commanding officer"},
    "CO_XO": {"company executive officer", "executive officer"},
    "CO_1SG": {"company first sergeant", "first sergeant"},
    "PL": {"platoon leader"},
    "PSG": {"platoon sergeant"},
    "PLT_RTO": {"platoon rto", "platoon radioman", "radio telephone operator"},
    "SL": {"squad leader"},
    "ASST_SL": {"assistant squad leader", "assistant squad leader"},
    "FTL": {"team leader", "fire team leader"},
}
_PUBLIC_APPOINTMENT_ECHELON = {
    "BN_CO":"Battalion","BN_XO":"Battalion","BN_SGM":"Battalion",
    "CO_CO":"Company","CO_XO":"Company","CO_1SG":"Company",
    "PL":"Platoon","PSG":"Platoon","PLT_RTO":"Platoon",
    "SL":"Squad","ASST_SL":"Squad","FTL":"Squad",
}
_PUBLIC_APPOINTMENT_NAMES = {
    "BN_CO":"Battalion Commander","BN_XO":"Battalion Executive Officer","BN_SGM":"Battalion Sergeant Major",
    "CO_CO":"Company Commander","CO_XO":"Company Executive Officer","CO_1SG":"Company First Sergeant",
    "PL":"Platoon Leader","PSG":"Platoon Sergeant","PLT_RTO":"Platoon RTO",
    "SL":"Squad Leader","ASST_SL":"Assistant Squad Leader","FTL":"Team Leader",
}

def _public_norm_duty(value):
    return re.sub(r"[^a-z0-9]+"," ",str(value or "").strip().lower()).strip()

def _public_node_maps(nodes):
    by_id={str(n.get("id")):n for n in (nodes or []) if n.get("id") is not None}
    children={}
    for n in nodes or []:
        children.setdefault(str(n.get("parent_id") or "ROOT"),[]).append(n)
    return by_id,children

def _public_ancestor_node(node_id, desired_type, node_map):
    desired=str(desired_type or "").lower()
    seen=set()
    current=node_map.get(str(node_id)) if node_id else None
    while current and str(current.get("id")) not in seen:
        seen.add(str(current.get("id")))
        if str(current.get("unit_type") or "").lower()==desired:
            return current
        current=node_map.get(str(current.get("parent_id"))) if current.get("parent_id") else None
    return None

def _public_legacy_node_for_person(person, desired_type, nodes):
    """Compatibility only for personnel created before structured unit_node_id assignments."""
    desired=str(desired_type or "").lower()
    unit=str(person.get("unit_code") or "").upper()
    platoon=str(person.get("platoon") or "").upper()
    squad=str(person.get("squad") or "").upper()
    company_letter=""
    m=re.search(r"\b([ABC])\b",unit)
    if m: company_letter=m.group(1)
    if "HHC" in unit or unit.startswith("HQ"):
        company_letter="HHC"
    company=None
    for n in nodes or []:
        if str(n.get("unit_type") or "").lower()!="company":
            continue
        name=str(n.get("display_name") or "").upper()
        code=str(n.get("unit_code") or "").upper()
        if company_letter=="HHC" and ("HEADQUARTERS" in name or code.startswith("HHC")):
            company=n; break
        if company_letter and (name.startswith(company_letter+" COMPANY") or code.startswith(company_letter+"-")):
            company=n; break
    if desired=="company":
        return company
    if not company:
        return None
    platoon_node=None
    for n in nodes or []:
        if str(n.get("parent_id"))!=str(company.get("id")) or str(n.get("unit_type") or "").lower()!="platoon":
            continue
        if platoon and (platoon in str(n.get("display_name") or "").upper() or platoon in str(n.get("unit_code") or "").upper()):
            platoon_node=n; break
    if desired=="platoon":
        return platoon_node
    if desired=="squad" and platoon_node:
        for n in nodes or []:
            if str(n.get("parent_id"))!=str(platoon_node.get("id")) or str(n.get("unit_type") or "").lower()!="squad":
                continue
            if squad and (squad in str(n.get("display_name") or "").upper() or squad in str(n.get("unit_code") or "").upper()):
                return n
    return None

def public_active_roster():
    """Public-safe active personnel records with structured assignment data."""
    try:
        return fetch_all("""
            SELECT p.id,p.rank_code,p.last_name,p.first_name,p.unit_code,p.platoon,p.squad,
                   p.duty_position,p.field_status,p.unit_node_id,p.readiness_percent,
                   COALESCE(rc.precedence,0) AS rank_precedence
            FROM personnel p
            LEFT JOIN rank_catalog rc ON rc.rank_code=p.rank_code
            WHERE p.separated_at IS NULL AND COALESCE(p.archived,FALSE)=FALSE
            ORDER BY COALESCE(rc.precedence,0) DESC,p.last_name,p.first_name
        """) or []
    except Exception:
        log.exception("Public active roster primary query unavailable")
        try:
            return fetch_all("""
                SELECT p.id,p.rank_code,p.last_name,p.first_name,p.unit_code,p.platoon,p.squad,
                       p.duty_position,p.field_status,p.unit_node_id,p.readiness_percent,
                       0 AS rank_precedence
                FROM personnel p
                WHERE p.separated_at IS NULL
                ORDER BY p.last_name,p.first_name
            """) or []
        except Exception:
            log.exception("Public active roster compatibility query unavailable")
            return []

def public_effective_appointments(nodes, roster, formal_rows):
    """Resolve public leadership from formal appointments first, then actual current duty assignments.
    This prevents assigned leaders from appearing VACANT merely because an older record was never
    migrated into personnel_appointments.
    """
    node_map,_=_public_node_maps(nodes)
    roster_by_id={str(p.get("id")):p for p in (roster or [])}
    resolved=[]
    occupied=set()

    # Normalize formal current appointments, including older rows with no unit_node_id.
    for raw in formal_rows or []:
        row=dict(raw)
        code=str(row.get("appointment_code") or "").upper()
        echelon=row.get("echelon") or _PUBLIC_APPOINTMENT_ECHELON.get(code)
        if not row.get("unit_node_id") and echelon:
            person=roster_by_id.get(str(row.get("personnel_id")))
            if person:
                target=_public_ancestor_node(person.get("unit_node_id"),echelon,node_map) or _public_legacy_node_for_person(person,echelon,nodes)
                if target:
                    row["unit_node_id"]=target.get("id")
        target_key=str(row.get("unit_node_id") or ("BATTALION" if str(echelon).lower()=="battalion" else ""))
        occupied.add((code,target_key))
        resolved.append(row)

    # Current personnel assignment/duty is authoritative fallback for visible unit leadership.
    for person in roster or []:
        duty=_public_norm_duty(person.get("duty_position"))
        if not duty:
            continue
        for code,aliases in _PUBLIC_DUTY_APPOINTMENT_ALIASES.items():
            if duty not in aliases:
                continue
            echelon=_PUBLIC_APPOINTMENT_ECHELON[code]
            target=_public_ancestor_node(person.get("unit_node_id"),echelon,node_map) or _public_legacy_node_for_person(person,echelon,nodes)
            if not target and echelon=="Battalion":
                target=next((n for n in nodes or [] if str(n.get("unit_type") or "").lower()=="battalion"),None)
            if not target:
                continue
            key=(code,str(target.get("id")))
            if key in occupied:
                continue
            resolved.append({
                "personnel_id":person.get("id"),
                "unit_node_id":target.get("id"),
                "appointment_status":"ASSIGNMENT",
                "appointment_name":_PUBLIC_APPOINTMENT_NAMES.get(code,person.get("duty_position")),
                "appointment_code":code,
                "echelon":echelon,
                "rank_code":person.get("rank_code"),
                "first_name":person.get("first_name"),
                "last_name":person.get("last_name"),
                "source":"CURRENT ASSIGNMENT",
            })
            occupied.add(key)
    return resolved

def public_leadership_for_node(appointment_code, unit_node_id, nodes=None, roster=None, formal_rows=None):
    nodes=nodes if nodes is not None else (fetch_all("SELECT id,parent_id,unit_code,display_name,unit_type,sort_order FROM unit_nodes WHERE is_active=TRUE ORDER BY sort_order,display_name") or [])
    roster=roster if roster is not None else public_active_roster()
    if formal_rows is None:
        try:
            formal_rows=fetch_all("""
                SELECT pa.personnel_id,pa.unit_node_id,pa.appointment_status,pa.organization,
                       ac.appointment_name,ac.appointment_code,ac.echelon,
                       p.rank_code,p.first_name,p.last_name
                FROM personnel_appointments pa
                JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code
                JOIN personnel p ON p.id=pa.personnel_id
                WHERE pa.is_current=TRUE AND p.separated_at IS NULL AND COALESCE(p.archived,FALSE)=FALSE
                ORDER BY ac.sort_order
            """) or []
        except Exception:
            formal_rows=[]
    rows=public_effective_appointments(nodes,roster,formal_rows)
    target=str(unit_node_id)
    return next((r for r in rows if str(r.get("appointment_code") or "").upper()==str(appointment_code).upper()
                 and str(r.get("unit_node_id"))==target),None)

@app.get("/battalion")
@app.get("/organization")
def organization():
    nodes=[]; roster=[]; formal_appointments=[]; appointments=[]
    if database_ready():
        try:
            nodes=fetch_all("""SELECT id,parent_id,unit_code,display_name,unit_type,sort_order
                               FROM unit_nodes WHERE is_active=TRUE ORDER BY sort_order,display_name""") or []
        except Exception:
            log.exception("Public organization unit structure unavailable")
        roster=public_active_roster()
        try:
            formal_appointments=fetch_all("""
                SELECT pa.personnel_id,pa.unit_node_id,pa.appointment_status,pa.organization,
                       ac.appointment_name,ac.appointment_code,ac.echelon,
                       p.rank_code,p.first_name,p.last_name
                FROM personnel_appointments pa
                JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code
                JOIN personnel p ON p.id=pa.personnel_id
                WHERE pa.is_current=TRUE AND p.separated_at IS NULL AND COALESCE(p.archived,FALSE)=FALSE
                ORDER BY ac.sort_order
            """) or []
        except Exception:
            log.exception("Public organization formal appointment data unavailable")
            formal_appointments=[]
        appointments=public_effective_appointments(nodes,roster,formal_appointments)

    appt_map={}
    for row in appointments:
        if row.get("personnel_id") is not None:
            appt_map.setdefault(str(row.get("personnel_id")),[]).append(row)
    node_map,children=_public_node_maps(nodes)
    return render_template("organization.html",nodes=nodes,roster=roster,children=children,
                           node_map=node_map,appt_map=appt_map,appointments=appointments)


@app.get("/company/<unit_code>")
def company(unit_code: str):
    unit=None; roster=[]; platoons=[]; leadership={}; appointment_map={}
    try:
        unit=fetch_one("SELECT * FROM unit_nodes WHERE unit_code=%s AND is_active=TRUE",(unit_code,)) if database_ready() else None
        if not unit:
            return render_template("company.html",unit=None,roster=[],platoons=[],leadership={},appointment_map={}),404
        nodes=fetch_all("""SELECT id,parent_id,unit_code,display_name,unit_type,sort_order
                           FROM unit_nodes WHERE is_active=TRUE ORDER BY sort_order,display_name""") or []
        all_roster=public_active_roster()
        ids=set(str(x) for x in (unit_descendant_ids(unit["id"]) or [unit["id"]]))
        roster=[p for p in all_roster if str(p.get("unit_node_id")) in ids]
        if not roster:
            # Legacy compatibility for personnel assigned before structured nodes were introduced.
            prefix={"A-1-5":"A/","B-1-5":"B/","C-1-5":"C/","HHC-1-5":"HHC"}.get(str(unit_code).upper())
            if prefix:
                roster=[p for p in all_roster if str(p.get("unit_code") or "").upper().startswith(prefix)]
        platoons=[n for n in nodes if str(n.get("parent_id"))==str(unit["id"]) and str(n.get("unit_type") or "").lower()=="platoon"]
        try:
            formal=fetch_all("""
                SELECT pa.personnel_id,pa.unit_node_id,pa.appointment_status,pa.organization,
                       ac.appointment_name,ac.appointment_code,ac.echelon,
                       p.rank_code,p.first_name,p.last_name
                FROM personnel_appointments pa
                JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code
                JOIN personnel p ON p.id=pa.personnel_id
                WHERE pa.is_current=TRUE AND p.separated_at IS NULL AND COALESCE(p.archived,FALSE)=FALSE
                ORDER BY ac.sort_order
            """) or []
        except Exception:
            formal=[]
        effective=public_effective_appointments(nodes,all_roster,formal)
        for ap in effective:
            if str(ap.get("personnel_id")) in {str(p.get("id")) for p in roster}:
                appointment_map.setdefault(str(ap.get("personnel_id")),[]).append(ap)
        leadership={
            "co":next((r for r in effective if r.get("appointment_code")=="CO_CO" and str(r.get("unit_node_id"))==str(unit["id"])),None),
            "xo":next((r for r in effective if r.get("appointment_code")=="CO_XO" and str(r.get("unit_node_id"))==str(unit["id"])),None),
            "first_sergeant":next((r for r in effective if r.get("appointment_code")=="CO_1SG" and str(r.get("unit_node_id"))==str(unit["id"])),None),
        }
    except Exception:
        app.logger.exception("Public company page recovery")
    return render_template("company.html",unit=unit,roster=roster,platoons=platoons,leadership=leadership,appointment_map=appointment_map)

@app.get("/platoon/<unit_code>")
def platoon(unit_code: str):
    unit=None; squads=[]; roster=[]; leadership={}
    try:
        unit=fetch_one("SELECT * FROM unit_nodes WHERE unit_code=%s AND unit_type='Platoon' AND is_active=TRUE",(unit_code,)) if database_ready() else None
        if not unit:
            return render_template("platoon.html",unit=None,squads=[],roster=[],leadership={}),404
        nodes=fetch_all("""SELECT id,parent_id,unit_code,display_name,unit_type,sort_order
                           FROM unit_nodes WHERE is_active=TRUE ORDER BY sort_order,display_name""") or []
        all_roster=public_active_roster()
        ids=set(str(x) for x in (unit_descendant_ids(unit["id"]) or [unit["id"]]))
        roster=[p for p in all_roster if str(p.get("unit_node_id")) in ids]
        squads=[n for n in nodes if str(n.get("parent_id"))==str(unit["id"]) and str(n.get("unit_type") or "").lower()=="squad"]
        try:
            formal=fetch_all("""
                SELECT pa.personnel_id,pa.unit_node_id,pa.appointment_status,pa.organization,
                       ac.appointment_name,ac.appointment_code,ac.echelon,
                       p.rank_code,p.first_name,p.last_name
                FROM personnel_appointments pa
                JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code
                JOIN personnel p ON p.id=pa.personnel_id
                WHERE pa.is_current=TRUE AND p.separated_at IS NULL AND COALESCE(p.archived,FALSE)=FALSE
                ORDER BY ac.sort_order
            """) or []
        except Exception:
            formal=[]
        effective=public_effective_appointments(nodes,all_roster,formal)
        leadership={
            "leader":next((r for r in effective if r.get("appointment_code")=="PL" and str(r.get("unit_node_id"))==str(unit["id"])),None),
            "sergeant":next((r for r in effective if r.get("appointment_code")=="PSG" and str(r.get("unit_node_id"))==str(unit["id"])),None),
            "rto":next((r for r in effective if r.get("appointment_code")=="PLT_RTO" and str(r.get("unit_node_id"))==str(unit["id"])),None),
        }
    except Exception:
        app.logger.exception("Public platoon page recovery")
    return render_template("platoon.html",unit=unit,squads=squads,roster=roster,leadership=leadership)

@app.route("/my-soldiers", methods=["GET","POST"])
@login_required
def my_soldiers():
    personnel = soldier_view(linked_personnel())
    if not personnel or not member_is_nco(personnel): abort(403)
    soldiers, scope = scoped_personnel_for(personnel)
    if request.method=="POST":
        target_id=request.form.get("personnel_id")
        target=next((x for x in soldiers if str(x.get("id"))==str(target_id)),None)
        if not target: abort(403)
        target_rank=(request.form.get("target_rank") or "").upper()
        justification=(request.form.get("justification") or "").strip()
        if not target_rank or not justification: abort(400)
        execute("""INSERT INTO personnel_recommendations(personnel_id,recommendation_type,recommended_action,justification,promotion_narrative,recommending_personnel_id,status)
                   VALUES(%s,'PROMOTION',%s,%s,%s,%s,'PENDING')""",(target_id,f"PROMOTION TO {target_rank}",justification,justification,personnel["id"]))
        open_personnel_action(target_id,"PROMOTION",f"Promotion recommendation — {target_rank}","S-1","HIGH",f"{personnel.get('rank_code','')} {personnel.get('last_name','')}",{"target_rank":target_rank,"justification":justification},source_key=f"PROMO-REC:{target_id}:{target_rank}:{date.today()}")
        notify_soldier(target_id,"S-1 / HQ",f"Promotion recommendation submitted — {target_rank}","Your NCO has forwarded a promotion recommendation for staff review.",source_key=f"PROMO-REC-NOTICE:{target_id}:{target_rank}:{date.today()}",target_anchor="promotion")
        staff_log("S-1","PROMOTION RECOMMENDATION",f"{personnel.get('rank_code','')} {personnel.get('last_name','')} recommended {target.get('rank_code','')} {target.get('last_name','')} for {target_rank}",f"{personnel.get('rank_code','')} {personnel.get('last_name','')}",target_id)
        flash("PROMOTION RECOMMENDATION FORWARDED TO S-1.","success")
        return redirect(url_for("my_soldiers"))
    enriched=[]
    for srow in soldiers:
        sv=soldier_view(srow)
        readiness=soldier_readiness(sv)
        replacement=replacement_training_status(sv)
        elig=promotion_eligibility(sv)
        inspection=weapon_inspection_status(sv["id"])
        enriched.append({"person":sv,"readiness":readiness,"replacement":replacement,"eligibility":elig,"inspection":inspection})
    return render_template("my_soldiers.html", personnel=personnel, soldiers=enriched, scope=scope)




@app.route("/operations", methods=["GET","POST"])
def operations():
    if request.method == "POST":
        if not session.get("user_id"):
            return redirect(url_for("report_for_duty"))
        role=session.get("access_role")
        if role not in {"s3","company_hq","battalion_hq","commander","admin"}:
            abort(403)
        action=request.form.get("action")
        authority=session.get("display_name") or session.get("username") or "S-3 OPERATIONS"
        if action=="create_operation":
            opnum=(request.form.get("operation_number") or "").strip() or next_operation_number()
            start_at=request.form.get("start_at") or None
            duration=max(45,int(request.form.get("duration_minutes") or 90))
            threshold=max(5,min(duration,int(request.form.get("credit_threshold_minutes") or 45)))
            rounds=max(0,min(1000,int(request.form.get("rounds_per_soldier") or 180)))
            channel_id=(request.form.get("credit_channel_id") or "").strip() or None
            channel_directory=fetch_one("SELECT channel_name FROM discord_channel_directory WHERE channel_id=%s AND active=TRUE",(channel_id,)) if channel_id else None
            selected_channel_name=(channel_directory or {}).get("channel_name") or request.form.get("credit_channel_name") or "Operation Voice"
            op=fetch_one(
                """INSERT INTO operations
                   (operation_code,title,operation_number,operation_type,area_of_operations,commander,h_hour,
                    situation,mission,execution,service_support,command_signal,status,start_at,operation_date,
                    duration_minutes,credit_threshold_minutes,rounds_per_soldier,credit_channel_id,credit_channel_name,
                    reminder_minutes,formation_scope,formation_unit_node_id,publish_status)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PLANNING',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'DRAFT') RETURNING *""",
                (opnum,request.form.get("title"),opnum,request.form.get("operation_type") or "OFFICIAL OPERATION",
                 request.form.get("area_of_operations"),request.form.get("commander"),request.form.get("h_hour"),
                 request.form.get("situation"),request.form.get("mission"),request.form.get("execution"),
                 request.form.get("service_support"),request.form.get("command_signal"),start_at,
                 (start_at or "")[:10] or None,duration,threshold,rounds,channel_id,
                 selected_channel_name,
                 request.form.get("reminder_minutes") or "1440,120,30",request.form.get("formation_scope") or "BATTALION",
                 request.form.get("formation_unit_node_id") or None),
            )
            if request.form.get("publish_now") == "YES":
                schedule_operation_event(op,authority)
                flash(f"{opnum} PUBLISHED. BATTALION CLERK CREDIT TRACKING IS SCHEDULED.","success")
            else:
                flash(f"{opnum} FILED AS A DRAFT IN S-3 OPERATIONS.","success")
        elif action=="assign_unit":
            execute(
                """INSERT INTO operation_units(operation_id,unit_node_id,task,is_primary)
                   VALUES(%s,%s,%s,%s)
                   ON CONFLICT(operation_id,unit_node_id) DO UPDATE SET
                     task=EXCLUDED.task,is_primary=EXCLUDED.is_primary""",
                (request.form.get("operation_id"),request.form.get("unit_node_id"),
                 request.form.get("task"),bool(request.form.get("is_primary"))),
            )
            flash("UNIT TASKING ENTERED ON THE OPERATION ORDER.","success")
        elif action=="credit_participant":
            file_operation_participation(
                request.form.get("operation_id"),request.form.get("personnel_id"),
                request.form.get("duty_role") or None,request.form.get("attendance_status") or "PARTICIPATED",
                request.form.get("rounds_expended") or 0,request.form.get("casualty_status") or None,
                request.form.get("remarks") or None,authority,
            )
            flash("PARTICIPATION ENTERED IN THE SOLDIER'S COMBAT OPERATIONS JOURNAL.","success")
        elif action=="file_aar":
            complete_operation(
                request.form.get("operation_id"),request.form.get("result") or None,
                request.form.get("commander_remarks") or None,authority,
            )
            flash("AFTER ACTION REPORT FILED. OPERATION MOVED TO THE JOURNAL.","success")
        elif action=="recommend":
            recommendation_type = request.form.get("recommendation_type") or "PERSONNEL ACTION"
            recommendation_status = "PENDING_S1" if recommendation_type.upper() == "AWARD" else "PENDING"
            execute(
                """INSERT INTO personnel_recommendations
                   (personnel_id,operation_id,recommendation_type,recommended_action,justification,
                    recommending_personnel_id,status)
                   VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (request.form.get("personnel_id"),request.form.get("operation_id") or None,
                 recommendation_type, request.form.get("recommended_action"),request.form.get("justification"),
                 (linked_personnel() or {}).get("id"), recommendation_status),
            )
            flash("PERSONNEL ACTION RECOMMENDATION FORWARDED.","success")
        return redirect(url_for("operations"))

    current=fetch_all(
        """SELECT * FROM operations
           WHERE UPPER(COALESCE(status,'')) NOT IN ('COMPLETED','CLOSED','CANCELLED','ARCHIVED')
             AND UPPER(COALESCE(lifecycle_status,'PLANNING')) NOT IN ('CLOSED','COMPLETED','CANCELLED','ARCHIVED')
           ORDER BY CASE WHEN start_at IS NULL THEN 1 ELSE 0 END,start_at ASC,created_at DESC"""
    ) if database_ready() else []
    completed=fetch_all(
        """SELECT o.*,aar.ammunition_expended,aar.filed_at
           FROM operations o LEFT JOIN after_action_reports aar ON aar.operation_id=o.id
           WHERE UPPER(COALESCE(o.status,'')) IN ('COMPLETED','CLOSED')
              OR UPPER(COALESCE(o.lifecycle_status,'')) IN ('COMPLETED','CLOSED')
           ORDER BY COALESCE(o.completed_at,o.start_at,o.created_at) DESC LIMIT 100"""
    ) if database_ready() else []
    personnel_list=fetch_all("SELECT * FROM personnel ORDER BY last_name") if database_ready() else []
    units=fetch_all("SELECT * FROM unit_nodes WHERE is_active=TRUE ORDER BY sort_order,display_name") if database_ready() else []
    all_ops=current+completed
    participants={str(o["id"]):operation_participants(o["id"]) for o in all_ops}
    op_units={str(o["id"]):operation_units_for(o["id"]) for o in all_ops}
    aars={}
    for o in completed:
        aars[str(o["id"])]=fetch_one("SELECT * FROM after_action_reports WHERE operation_id=%s",(o["id"],))
    recommendations=fetch_all(
        """SELECT pr.*,p.rank_code,p.last_name,p.first_name,o.title AS operation_title
           FROM personnel_recommendations pr
           JOIN personnel p ON p.id=pr.personnel_id
           LEFT JOIN operations o ON o.id=pr.operation_id
           ORDER BY pr.created_at DESC LIMIT 50"""
    ) if database_ready() else []
    operation_channel=fetch_one("SELECT * FROM clerk_duty_channels WHERE event_type='OPERATION' ORDER BY updated_at DESC LIMIT 1") if database_ready() else None
    discord_voice_channels=fetch_all("SELECT * FROM discord_channel_directory WHERE active=TRUE AND channel_type='VOICE' ORDER BY category_name NULLS FIRST,channel_name") if database_ready() else []
    live_map={}
    for o in current:
        event,attendance=operation_live_attendance(o["id"])
        expected=operation_expected_roster(o)
        live_map[str(o["id"])]= {"event":event,"attendance":attendance,"expected":len(expected),
                                 "ready":sum(1 for p in expected if int(p.get("readiness_percent") or 0)>=80)}
    return render_template("operations.html",current=current,completed=completed,
                           personnel_list=personnel_list,units=units,
                           participants=participants,op_units=op_units,aars=aars,
                           recommendations=recommendations,operation_channel=operation_channel,
                           discord_voice_channels=discord_voice_channels,live_map=live_map,clerk_health=clerk_health_snapshot())


@app.get("/operations/<operation_id>")
def operation_detail(operation_id):
    op=operation_record(operation_id)
    if not op:
        abort(404)
    participants=operation_participants(operation_id)
    units=operation_units_for(operation_id)
    aar=fetch_one("SELECT * FROM after_action_reports WHERE operation_id=%s",(operation_id,))
    journal=fetch_all("SELECT * FROM operation_journal_entries WHERE operation_id=%s ORDER BY entry_date,created_at",(operation_id,))
    photos=fetch_all("SELECT * FROM operation_photographs WHERE operation_id=%s ORDER BY sort_order,uploaded_at",(operation_id,))
    live_event,live_attendance=operation_live_attendance(operation_id)
    expected=operation_expected_roster(op)
    discord_voice_channels=fetch_all("SELECT * FROM discord_channel_directory WHERE active=TRUE AND channel_type='VOICE' ORDER BY category_name NULLS FIRST,channel_name") if database_ready() else []
    return render_template("operation_detail.html",op=op,participants=participants,units=units,aar=aar,journal=journal,photos=photos,
                           live_event=live_event,live_attendance=live_attendance,expected_roster=expected,
                           duty_suggestions=operation_duty_suggestions(operation_id),discord_voice_channels=discord_voice_channels,
                           clerk_health=clerk_health_snapshot())


@app.post("/operations/<operation_id>/schedule")
@login_required
def operation_schedule_action(operation_id):
    if session.get("access_role") not in {"s3","battalion_hq","commander","admin"}: abort(403)
    op=operation_record(operation_id)
    if not op: abort(404)
    start_at=request.form.get("start_at") or op.get("start_at")
    duration=max(45,int(request.form.get("duration_minutes") or op.get("duration_minutes") or 90))
    threshold=max(5,min(duration,int(request.form.get("credit_threshold_minutes") or op.get("credit_threshold_minutes") or 45)))
    rounds=max(0,min(1000,int(request.form.get("rounds_per_soldier") or op.get("rounds_per_soldier") or 180)))
    selected_channel_id=request.form.get("credit_channel_id") or op.get("credit_channel_id")
    channel_directory=fetch_one("SELECT channel_name FROM discord_channel_directory WHERE channel_id=%s AND active=TRUE",(selected_channel_id,)) if selected_channel_id else None
    selected_channel_name=(channel_directory or {}).get("channel_name") or request.form.get("credit_channel_name") or op.get("credit_channel_name") or "Operation Voice"
    execute("""UPDATE operations SET start_at=%s,operation_date=%s,duration_minutes=%s,credit_threshold_minutes=%s,
              rounds_per_soldier=%s,credit_channel_id=%s,credit_channel_name=%s,reminder_minutes=%s,
              formation_scope=%s,formation_unit_node_id=%s,updated_at=NOW() WHERE id=%s""",
            (start_at,(str(start_at)[:10] if start_at else None),duration,threshold,rounds,
             selected_channel_id,
             selected_channel_name,
             request.form.get("reminder_minutes") or op.get("reminder_minutes") or "1440,120,30",
             request.form.get("formation_scope") or op.get("formation_scope") or "BATTALION",
             request.form.get("formation_unit_node_id") or op.get("formation_unit_node_id"),operation_id))
    op=operation_record(operation_id)
    event=schedule_operation_event(op,session.get("display_name") or session.get("username") or "S-3")
    staff_log("S-3","OPERATION PUBLISHED",f"{op.get('operation_number')} — {op.get('title')}",session.get("display_name") or session.get("username"),details={"event_id":str(event['id'])})
    flash("OPERATION PUBLISHED TO BATTALION CLERK. VOICE CREDIT TRACKING IS SCHEDULED.","success")
    return redirect(url_for("operation_detail",operation_id=operation_id))


@app.post("/operations/<operation_id>/attendance-action")
@login_required
def operation_attendance_action(operation_id):
    if session.get("access_role") not in {"s3","battalion_hq","commander","admin"}: abort(403)
    event=operation_live_event(operation_id)
    if not event:
        flash("NO BATTALION CLERK DUTY EVENT IS LINKED TO THIS OPERATION.","danger")
        return redirect(url_for("operation_detail",operation_id=operation_id))
    pid=request.form.get("personnel_id")
    action=(request.form.get("action") or "").upper()
    threshold=int(event.get("credit_threshold_minutes") or 45)
    authority=session.get("display_name") or session.get("username") or "S-3"
    if action in {"GRANT_CREDIT","ADJUST_MINUTES","DENY_CREDIT","EXCUSED"}:
        current=fetch_one("SELECT * FROM battalion_event_attendance WHERE event_id=%s AND personnel_id=%s",(event["id"],pid))
        seconds=int((current or {}).get("qualifying_seconds") or 0)
        if action=="GRANT_CREDIT": seconds=max(seconds,threshold*60)
        elif action=="ADJUST_MINUTES": seconds=max(0,int(request.form.get("minutes") or 0)*60)
        credited_at=datetime.now(timezone.utc) if (action=="GRANT_CREDIT" or (action=="ADJUST_MINUTES" and seconds>=threshold*60)) else None
        if action in {"DENY_CREDIT","EXCUSED"}: credited_at=None
        execute("""INSERT INTO battalion_event_attendance(event_id,personnel_id,qualifying_seconds,first_seen_at,last_seen_at,credited_at,source_reference)
                   VALUES(%s,%s,%s,NOW(),NOW(),%s,%s)
                   ON CONFLICT(event_id,personnel_id) DO UPDATE SET qualifying_seconds=EXCLUDED.qualifying_seconds,
                   credited_at=EXCLUDED.credited_at,last_seen_at=NOW(),source_reference=EXCLUDED.source_reference,updated_at=NOW()""",
                (event["id"],pid,seconds,credited_at,f"MANUAL S-3 OVERRIDE — {authority}"))
        if credited_at:
            _credit_scheduled_duty(event,pid,0,f"MANUAL:{operation_id}:{pid}")
        if action in {"DENY_CREDIT","EXCUSED"}:
            execute("UPDATE personnel_activity_credit SET credited=FALSE WHERE personnel_id=%s AND source_reference=%s",(pid,str(event["id"])))
            execute("DELETE FROM operation_participation WHERE operation_id=%s AND personnel_id=%s",(operation_id,pid))
        staff_log("S-3","ATTENDANCE OVERRIDE",f"{action} — operation {operation_id}",authority,details={"personnel_id":pid,"minutes":seconds//60})
        flash("S-3 ATTENDANCE OVERRIDE FILED.","success")
    elif action=="UPDATE_ROLE":
        role=(request.form.get("duty_role") or "").strip()
        if role:
            person=fetch_one("SELECT unit_node_id FROM personnel WHERE id=%s",(pid,))
            execute("""INSERT INTO operation_participation(operation_id,personnel_id,unit_node_id,duty_role,attendance_status,rounds_expended,credited_by)
                       VALUES(%s,%s,%s,%s,'ASSIGNED',0,%s)
                       ON CONFLICT(operation_id,personnel_id) DO UPDATE SET duty_role=EXCLUDED.duty_role""",
                    (operation_id,pid,(person or {}).get("unit_node_id"),role,authority))
            flash("OPERATION DUTY ROLE UPDATED.","success")
    elif action=="ROUND_OVERRIDE":
        rounds=max(0,min(1000,int(request.form.get("rounds") or 0)))
        row=fetch_one("SELECT * FROM operation_participation WHERE operation_id=%s AND personnel_id=%s",(operation_id,pid))
        if row and row.get("attendance_status")=='FULL CREDIT':
            execute("UPDATE operation_participation SET rounds_expended=%s,remarks=%s WHERE id=%s",(rounds,f"Manual S-3 ammunition override — {authority}",row["id"]))
            reconcile_operation_weapon_rounds(operation_id,pid,rounds,authority,"Manual S-3 ammunition override.")
            flash("SOLDIER AMMUNITION EXPENDITURE RECONCILED.","success")
        else:
            flash("ROUND OVERRIDE REQUIRES AN EXISTING FULL-CREDIT OPERATION RECORD.","warning")
    return redirect(url_for("operation_detail",operation_id=operation_id))


def operation_credit_cascade(operation_id, personnel_id, rounds, authority="BATTALION CLERK"):
    op=fetch_one("SELECT * FROM operations WHERE id=%s",(operation_id,)) or {}
    person=fetch_one("SELECT * FROM personnel WHERE id=%s",(personnel_id,))
    if not person: return
    ref=f"OPCREDIT:{operation_id}:{personnel_id}"
    emit_state_event('OPERATION_CREDITED',personnel_id=personnel_id,operation_id=operation_id,
                     unit_node_id=person.get('unit_node_id'),effective_date=(op.get('operation_date') or date.today()),
                     title=f"{op.get('operation_number') or 'OPERATION'} — {op.get('title') or 'Operation Credit'}",
                     narrative=f"Official operation credit filed; {int(rounds or 0)} recorded rounds.",reference_number=op.get('operation_number'),
                     source_key=ref,details={'rounds':int(rounds or 0)})
    execute("""INSERT INTO soldier_tour_book(personnel_id,entry_type,entry_date,title,narrative,operation_id,source_key)
               VALUES(%s,'OPERATION',%s,%s,%s,%s,%s) ON CONFLICT(source_key) DO NOTHING""",
            (personnel_id,op.get('operation_date') or date.today(),f"{op.get('operation_number') or 'OPERATION'} — {op.get('title') or 'Operation'}",
             f"Official Battalion Clerk operation credit filed; {int(rounds or 0)} recorded rounds.",operation_id,ref))
    try: sync_readiness(person)
    except Exception: log.exception('OPERATION CASCADE READINESS FAILED personnel=%s',personnel_id)
    try: ribbon_progress_for(personnel_id,award_completed=True)
    except Exception: log.exception('OPERATION CASCADE RIBBON PROGRESS FAILED personnel=%s',personnel_id)

def finalize_operation_event(event_id, authority="BATTALION CLERK", result=None, remarks=None):
    event=fetch_one("SELECT * FROM battalion_events WHERE id=%s",(event_id,))
    if not event: raise ValueError("event not found")
    threshold=int(event.get("credit_threshold_minutes") or 45)
    execute("UPDATE battalion_events SET status='CLOSED',ends_at=LEAST(ends_at,NOW()) WHERE id=%s",(event_id,))
    attendance=fetch_all("SELECT a.*,p.rank_code,p.first_name,p.last_name FROM battalion_event_attendance a JOIN personnel p ON p.id=a.personnel_id WHERE a.event_id=%s",(event_id,))
    rounds=max(0,int(event.get("rounds_per_soldier") or 0)); repaired=0; credited=0
    operation_id=event.get("operation_id")
    if str(event.get("event_type") or '').upper()=="OPERATION" and operation_id:
        for a in attendance:
            secs=int(a.get("qualifying_seconds") or 0)
            if secs < threshold*60 and not a.get("credited_at"): continue
            credited+=1
            prior=fetch_one("SELECT id,rounds_expended FROM operation_participation WHERE operation_id=%s AND personnel_id=%s",(operation_id,a["personnel_id"]))
            timed_rounds=operation_round_target_for_time(event,secs)
            expected=max(timed_rounds,int((prior or {}).get("rounds_expended") or 0))
            if prior:
                execute("UPDATE operation_participation SET attendance_status='FULL CREDIT',rounds_expended=%s,remarks=%s,credited_by=%s,credited_at=NOW() WHERE operation_id=%s AND personnel_id=%s",
                        (expected,f"Operation closeout: {secs//60} verified minutes.",authority,operation_id,a["personnel_id"]))
                repaired += reconcile_operation_weapon_rounds(operation_id,a["personnel_id"],expected,authority,f"Operation closeout reconciliation for {event.get('title')}.")
            else:
                before=operation_weapon_rounds_applied(operation_id,a["personnel_id"])
                file_operation_participation(operation_id,a["personnel_id"],attendance_status="FULL CREDIT",rounds_expended=expected,remarks=f"Operation closeout: {secs//60} verified minutes.",credited_by=authority)
                after=operation_weapon_rounds_applied(operation_id,a["personnel_id"])
                repaired += max(0,after-before)
        # One closeout advances every dependent Soldier system exactly once through idempotent source keys.
        credited_rows=fetch_all("""SELECT personnel_id,rounds_expended FROM operation_participation
                                  WHERE operation_id=%s AND UPPER(COALESCE(attendance_status,''))='FULL CREDIT'""",(operation_id,))
        for cr in credited_rows:
            operation_credit_cascade(operation_id,cr['personnel_id'],cr.get('rounds_expended') or 0,authority)
        complete_operation(operation_id,result,remarks,authority)
        execute("UPDATE operations SET lifecycle_status='CLOSED',publish_status='CLOSED',updated_at=NOW() WHERE id=%s",(operation_id,))
    participated=sum(1 for row in attendance if int(row.get("qualifying_seconds") or 0)>=min(1200,max(300,threshold*30)))
    return {"tracked":len(attendance),"participated":participated,"credited":credited,"weapon_rounds_applied":repaired,"threshold":threshold}


@app.post("/operations/<operation_id>/close")
@login_required
def operation_close_action(operation_id):
    if session.get("access_role") not in {"s3","battalion_hq","commander","admin"}: abort(403)
    event=operation_live_event(operation_id)
    if not event:
        flash("NO ACTIVE BATTALION CLERK EVENT IS LINKED TO THIS OPERATION.","danger")
        return redirect(url_for("operation_detail",operation_id=operation_id))
    result=finalize_operation_event(event["id"],session.get("display_name") or session.get("username") or "S-3",request.form.get("result") or None,request.form.get("commander_remarks") or None)
    flash(f"OPERATION CLOSED — {result['credited']} SOLDIER(S) CREDITED; {result['weapon_rounds_applied']} M16 ROUNDS RECONCILED.","success")
    return redirect(url_for("operation_detail",operation_id=operation_id))


@app.post("/operations/<operation_id>/delete")
@login_required
def operation_delete_action(operation_id):
    if session.get("access_role") not in {"s3","battalion_hq","commander","admin"}: abort(403)
    op=operation_record(operation_id)
    if not op: abort(404)
    # Protect actual service history. A scheduled/planning operation can be removed;
    # once official credit/rounds/AAR exist, S-3 must close or cancel instead.
    credited=int((fetch_one("""SELECT COUNT(*) AS total FROM operation_participation
                              WHERE operation_id=%s AND (UPPER(COALESCE(attendance_status,''))='FULL CREDIT' OR COALESCE(rounds_expended,0)>0)""",(operation_id,)) or {'total':0})['total'] or 0)
    aar=fetch_one("SELECT id FROM after_action_reports WHERE operation_id=%s",(operation_id,))
    if credited or aar or str(op.get('status') or '').upper() in {'COMPLETED','CLOSED'}:
        flash('THIS OPERATION HAS FILED SERVICE HISTORY AND CANNOT BE DELETED. USE CLOSE / CANCEL SO THE RECORD REMAINS AUDITABLE.','warning')
        return redirect(url_for('operation_detail',operation_id=operation_id))
    authority=session.get('display_name') or session.get('username') or 'S-3'
    # Remove the linked Clerk event first so reminders/voice tracking stop immediately.
    execute("DELETE FROM battalion_events WHERE operation_id=%s OR id=%s",(operation_id,op.get('clerk_event_id')))
    execute("DELETE FROM operations WHERE id=%s",(operation_id,))
    staff_log('S-3','OPERATION DELETED',f"{op.get('operation_number') or op.get('operation_code')} — {op.get('title')}",authority,
              details={'operation_id':str(operation_id),'reason':request.form.get('reason') or 'S-3 scheduled operation removed'})
    flash('SCHEDULED OPERATION DELETED. CLERK TRACKING / REMINDERS REMOVED AND THE HOMEPAGE SCHEDULE UPDATED.','success')
    return redirect(url_for('operations'))


@app.post("/operations/<operation_id>/duplicate")
@login_required
def operation_duplicate(operation_id):
    if session.get("access_role") not in {"s3","battalion_hq","commander","admin"}: abort(403)
    op=operation_record(operation_id)
    if not op: abort(404)
    opnum=next_operation_number()
    copy=fetch_one("""INSERT INTO operations(operation_code,title,operation_number,operation_type,area_of_operations,commander,h_hour,
               situation,mission,execution,service_support,command_signal,status,duration_minutes,credit_threshold_minutes,rounds_per_soldier,
               credit_channel_id,credit_channel_name,reminder_minutes,formation_scope,formation_unit_node_id,publish_status)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PLANNING',%s,%s,%s,%s,%s,%s,%s,%s,'DRAFT') RETURNING id""",
              (opnum,f"{op.get('title')} — COPY",opnum,op.get('operation_type'),op.get('area_of_operations'),op.get('commander'),op.get('h_hour'),op.get('situation'),op.get('mission'),op.get('execution'),op.get('service_support'),op.get('command_signal'),op.get('duration_minutes') or 90,op.get('credit_threshold_minutes') or 45,op.get('rounds_per_soldier') or 180,op.get('credit_channel_id'),op.get('credit_channel_name'),op.get('reminder_minutes') or '1440,120,30',op.get('formation_scope') or 'BATTALION',op.get('formation_unit_node_id')))
    flash("OPERATION DUPLICATED AS A NEW DRAFT. SET THE NEW DATE/TIME AND PUBLISH WHEN READY.","success")
    return redirect(url_for("operation_detail",operation_id=copy["id"]))



@app.route("/training-office", methods=["GET","POST"])
def training_office_phase9():
    if request.method=="POST":
        if not session.get("user_id"): return redirect(url_for("report_for_duty"))
        role=session.get("access_role")
        action=request.form.get("action")
        authority=session.get("display_name") or session.get("username") or "S-3 TRAINING"
        if action=="award_qualification":
            if role not in {"s3","training","battalion_hq","commander","admin"}: abort(403)
            award_duty_qualification(
                request.form.get("personnel_id"),request.form.get("qualification_type_id"),
                request.form.get("instructor_personnel_id") or None,
                request.form.get("qualified_date") or date.today(),
                request.form.get("expiration_date") or None,
                request.form.get("remarks") or None,authority)
            flash("DUTY QUALIFICATION ENTERED IN THE SOLDIER'S TRAINING RECORD.","success")
        elif action=="certify_training_program":
            if role not in {"s3","training","battalion_hq","commander","admin"}: abort(403)
            certify_training_program(
                request.form.get("personnel_id"), request.form.get("program_code"),
                request.form.get("authority") or authority, request.form.get("remarks") or None,
                request.form.get("completed_at") or date.today())
            flash("TRAINING PROGRAM COMPLETION FILED IN THE SOLDIER'S 201 RECORD.", "success")
        elif action=="request_training":
            p=linked_personnel()
            if not p: abort(403)
            execute("""INSERT INTO training_requests(personnel_id,qualification_type_id,request_type,remarks)
                       VALUES(%s,%s,'DUTY QUALIFICATION',%s)""",
                    (p["id"],request.form.get("qualification_type_id"),request.form.get("remarks")))
            open_personnel_action(p["id"],"TRAINING","Training request","S-3","ROUTINE",f"{p.get('rank_code','')} {p.get('last_name','')}",{"remarks":request.form.get("remarks") or ""})
            flash("REQUEST FOR TRAINING FORWARDED TO THE TRAINING OFFICE.","success")
        elif action=="mos_record":
            if role not in {"s3","battalion_hq","commander","admin"}: abort(403)
            pid=request.form.get("personnel_id")
            code=(request.form.get("mos_code") or "").strip().upper()
            title=(request.form.get("mos_title") or code).strip()
            kind=(request.form.get("mos_kind") or "SECONDARY").upper()
            if kind not in {"PRIMARY","SECONDARY","ADDITIONAL"} or not code: abort(400)
            if kind=="PRIMARY":
                execute("UPDATE personnel_mos_records SET status='SUPERSEDED' WHERE personnel_id=%s AND mos_kind='PRIMARY' AND status='CURRENT'",(pid,))
                execute("UPDATE personnel SET mos_code=%s,updated_at=NOW() WHERE id=%s",(code,pid))
            execute("""INSERT INTO personnel_mos_records(personnel_id,mos_code,mos_title,mos_kind,effective_date,qualified_by,remarks)
                       VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(personnel_id,mos_code,mos_kind) DO UPDATE SET status='CURRENT',mos_title=EXCLUDED.mos_title,effective_date=EXCLUDED.effective_date,qualified_by=EXCLUDED.qualified_by,remarks=EXCLUDED.remarks""",
                    (pid,code,title,kind,request.form.get("effective_date") or date.today(),authority,request.form.get("remarks") or None))
            write_service_entry(pid,"MOS",f"{kind} MOS — {code}",f"{title} recorded as {kind.lower()} battlefield MOS.",authority,None,date.today())
            create_personnel_order(pid,"QUALIFICATION","MILITARY OCCUPATIONAL SPECIALTY ORDERS",f"The Soldier named herein is awarded/recorded with {kind.lower()} MOS {code} — {title}.",effective_date=request.form.get("effective_date") or date.today(),authority=authority,details={"mos_code":code,"mos_title":title,"mos_kind":kind},source_key=f"MOS:{pid}:{kind}:{code}")
            pa=open_personnel_action(pid,"MOS",f"{kind} MOS {code} — {title}","S-3","ROUTINE",authority,{"mos_code":code,"mos_title":title,"mos_kind":kind})
            if pa: transition_personnel_action(pa["id"],"COMPLETE",authority,"MOS record updated.")
            flash("BATTLEFIELD MOS RECORDED IN THE SOLDIER'S SERVICE RECORD.","success")
        return redirect(url_for("training_office_phase9"))
    catalog=duty_qualification_catalog()
    personnel_list=fetch_all("SELECT * FROM personnel ORDER BY last_name,first_name") if database_ready() else []
    if database_ready(): sync_qualification_currency()
    records=fetch_all(
        """SELECT pdq.*,p.rank_code,p.first_name,p.last_name,p.unit_code,
                  dqt.display_name,dqt.battlefield_unit,dqt.code,
                  i.rank_code AS instructor_rank,i.last_name AS instructor_last
           FROM personnel_duty_qualifications pdq
           JOIN personnel p ON p.id=pdq.personnel_id
           JOIN duty_qualification_types dqt ON dqt.id=pdq.qualification_type_id
           LEFT JOIN personnel i ON i.id=pdq.instructor_personnel_id
           ORDER BY p.last_name,dqt.sort_order""") if database_ready() else []
    requests=fetch_all(
        """SELECT tr.*,p.rank_code,p.first_name,p.last_name,dqt.display_name
           FROM training_requests tr JOIN personnel p ON p.id=tr.personnel_id
           LEFT JOIN duty_qualification_types dqt ON dqt.id=tr.qualification_type_id
           ORDER BY tr.requested_at DESC""") if database_ready() else []
    mine=[]
    lp=linked_personnel()
    if lp: mine=personnel_duty_qualifications(lp["id"])
    training_programs = fetch_all("SELECT * FROM training_program_catalog WHERE is_active=TRUE ORDER BY sort_order") if database_ready() else []
    return render_template("training_office.html",catalog=catalog,personnel_list=personnel_list,
                           records=records,requests=requests,mine=mine,deficiencies=training_deficiencies(),training_programs=training_programs)

@app.route("/supply", methods=["GET","POST"])
def supply():
    personnel = soldier_view(linked_personnel()) if database_ready() else None
    if request.method == "POST":
        if not session.get("user_id"):
            return redirect(url_for("login", next=url_for("supply")))
        role=session.get("access_role")
        action=request.form.get("action")
        authority=session.get("display_name") or session.get("username") or "S-4 SUPPLY"
        if action in {"issue_equipment","turn_in_equipment","weapon_maintenance","record_rounds","stock_adjust","requisition_action"} and role not in {"s4","company_hq","battalion_hq","commander","admin"}:
            abort(403)
        if action=="issue_equipment":
            issue_equipment_to_soldier(request.form.get("personnel_id"),request.form.get("item_code"),authority,request.form.get("remarks"))
            flash("EQUIPMENT ISSUE ENTERED ON THE SOLDIER'S PROPERTY RECORD.","success")
        elif action=="turn_in_equipment":
            turn_in_equipment(request.form.get("issue_id"),authority,request.form.get("condition_state"),request.form.get("remarks"))
            flash("PROPERTY TURN-IN ENTERED.","success")
        elif action=="weapon_maintenance":
            weapon_maintenance_action(request.form.get("weapon_id"),request.form.get("maintenance_action"),
                                      request.form.get("personnel_id") or None,authority,request.form.get("remarks"))
            flash("WEAPON MAINTENANCE ACTION FILED.","success")
        elif action=="record_rounds":
            record_weapon_rounds(request.form.get("weapon_id"),request.form.get("rounds_fired") or 0,
                                 request.form.get("personnel_id") or None,None,
                                 request.form.get("source_type") or "MANUAL ENTRY",authority,request.form.get("remarks"))
            flash("ROUND EXPENDITURE ENTERED ON THE WEAPON RECORD.","success")
        elif action=="stock_adjust":
            execute("""INSERT INTO company_supply_stock(unit_node_id,item_code,quantity_on_hand,reorder_level)
                       VALUES(%s,%s,%s,%s)
                       ON CONFLICT(unit_node_id,item_code) DO UPDATE SET
                       quantity_on_hand=EXCLUDED.quantity_on_hand,reorder_level=EXCLUDED.reorder_level,last_updated_at=NOW()""",
                    (request.form.get("unit_node_id"),request.form.get("item_code"),
                     int(request.form.get("quantity_on_hand") or 0),int(request.form.get("reorder_level") or 0)))
            flash("COMPANY SUPPLY STOCK UPDATED.","success")
        elif action=="submit_requisition":
            req=next_supply_request_number()
            execute("""INSERT INTO supply_requisitions
                       (request_number,requesting_unit_node_id,requested_by_personnel_id,item_code,
                        quantity_requested,priority,reason)
                       VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                    (req,request.form.get("unit_node_id") or None,
                     personnel["id"] if personnel else None,request.form.get("item_code"),
                     int(request.form.get("quantity_requested") or 1),
                     request.form.get("priority") or "ROUTINE",request.form.get("reason")))
            flash(f"SUPPLY REQUISITION {req} SUBMITTED.","success")
        elif action=="requisition_action":
            status=request.form.get("status")
            if status=="APPROVED":
                execute("UPDATE supply_requisitions SET status='APPROVED',approved_at=NOW(),approved_by=%s WHERE id=%s",(authority,request.form.get("requisition_id")))
            elif status=="FILLED":
                execute("UPDATE supply_requisitions SET status='FILLED',filled_at=NOW(),approved_by=COALESCE(approved_by,%s) WHERE id=%s",(authority,request.form.get("requisition_id")))
            elif status=="DENIED":
                execute("UPDATE supply_requisitions SET status='DENIED',approved_at=NOW(),approved_by=%s WHERE id=%s",(authority,request.form.get("requisition_id")))
            flash("REQUISITION STATUS UPDATED.","success")
        return redirect(url_for("supply"))

    weapon = current_weapon_for(personnel) if personnel else None
    if weapon:
        refresh_weapon_condition(weapon["id"])
        weapon = current_weapon_for(personnel)
    issued_equipment=current_equipment_for(personnel["id"]) if personnel else []
    catalog=fetch_all("SELECT * FROM supply_item_catalog WHERE is_active=TRUE ORDER BY category,sort_order") if database_ready() else []
    personnel_list=fetch_all("SELECT id,rank_code,last_name,first_name,unit_code,platoon,squad FROM personnel ORDER BY last_name") if database_ready() else []
    weapons=fetch_all("""SELECT wi.*,p.rank_code,p.last_name,p.first_name,wih.personnel_id
                         FROM weapon_inventory wi
                         LEFT JOIN weapon_issue_history wih ON wih.weapon_id=wi.id AND wih.is_current=TRUE
                         LEFT JOIN personnel p ON p.id=wih.personnel_id
                         ORDER BY wi.rack_number NULLS LAST,wi.serial_number""") if database_ready() else []
    for w in weapons:
        state,pct=weapon_condition_from_rounds_and_time(w,fetch_one("SELECT * FROM personnel WHERE id=%s",(w["personnel_id"],)) if w.get("personnel_id") else None)
        w["computed_state"],w["computed_percent"]=state,pct
    equipment_issues=fetch_all("""SELECT eih.id AS issue_id,eih.personnel_id,eih.issued_at,
                                  ei.id AS equipment_id,ei.serial_number,ei.rack_number,ei.condition_state,
                                  sic.item_name,sic.item_code,p.rank_code,p.last_name,p.first_name
                                  FROM equipment_issue_history eih
                                  JOIN equipment_inventory ei ON ei.id=eih.equipment_id
                                  JOIN supply_item_catalog sic ON sic.item_code=ei.item_code
                                  LEFT JOIN personnel p ON p.id=eih.personnel_id
                                  WHERE eih.is_current=TRUE ORDER BY sic.sort_order,p.last_name""") if database_ready() else []
    companies=fetch_all("SELECT * FROM unit_nodes WHERE unit_type='Company' AND is_active=TRUE ORDER BY sort_order") if database_ready() else []
    stock={str(c["id"]):company_supply_readiness(c["id"]) for c in companies}
    requisitions=fetch_all("""SELECT sr.*,sic.item_name,un.display_name AS requesting_unit
                              FROM supply_requisitions sr JOIN supply_item_catalog sic ON sic.item_code=sr.item_code
                              LEFT JOIN unit_nodes un ON un.id=sr.requesting_unit_node_id
                              ORDER BY CASE sr.status WHEN 'SUBMITTED' THEN 0 WHEN 'APPROVED' THEN 1 ELSE 2 END,sr.submitted_at DESC LIMIT 100""") if database_ready() else []
    maintenance_log=fetch_all("""SELECT wml.*,wi.serial_number,wi.rack_number,p.rank_code,p.last_name
                                 FROM weapon_maintenance_log wml JOIN weapon_inventory wi ON wi.id=wml.weapon_id
                                 LEFT JOIN personnel p ON p.id=wml.personnel_id
                                 ORDER BY wml.performed_at DESC LIMIT 40""") if database_ready() else []
    return render_template("supply.html",personnel=personnel,weapon=weapon,issued_equipment=issued_equipment,
                           catalog=catalog,personnel_list=personnel_list,weapons=weapons,
                           equipment_issues=equipment_issues,companies=companies,stock=stock,
                           requisitions=requisitions,maintenance_log=maintenance_log)


@app.get("/arms-room")
@login_required
def arms_room():
    return redirect(url_for("supply"))


@app.route("/morning-report", methods=["GET","POST"])
def morning_report():
    role=session.get("access_role")
    if role not in {"s1","s3","training","battalion_hq","commander","admin"}:
        abort(403)
    if request.method == "POST":
        if not session.get("user_id"):
            return redirect(url_for("report_for_duty"))
        save_morning_report_snapshot(session.get("display_name") or session.get("username") or "BATTALION CLERK")
        flash("MORNING REPORT FILED IN THE BATTALION ARCHIVE.", "success")
        return redirect(url_for("morning_report"))
    company_rows,total = morning_report_data() if database_ready() else ([],{})
    forecast = deros_forecast(90) if database_ready() else []
    vacancies = vacancy_report() if database_ready() else []
    snapshots = fetch_all("SELECT * FROM morning_report_snapshots ORDER BY report_date DESC LIMIT 90") if database_ready() else []
    return render_template("morning_report.html", company_rows=company_rows,total=total,
                           forecast=forecast,vacancies=vacancies,snapshots=snapshots,
                           report_date=date.today())


@app.get("/readiness")
@login_required
def readiness():
    personnel = soldier_view(linked_personnel())
    individual = soldier_readiness(personnel) if personnel else None
    soldiers,scope = scoped_personnel_for(personnel)
    scoped = []
    for s in soldiers:
        scoped.append({"person":s,"readiness":soldier_readiness(s)})
    performance_scope = scope["id"] if scope else (personnel.get("unit_node_id") if personnel else None)
    performance_board = unit_performance_board(performance_scope) if database_ready() and performance_scope else []
    leadership_score = combat_leadership_score(personnel["id"]) if personnel else None
    return render_template("readiness.html",personnel=personnel,individual=individual,
                           scope=scope,scoped=scoped,performance_board=performance_board,leadership_score=leadership_score)


@app.post("/duty-status/<personnel_id>")
@login_required
def duty_status_action(personnel_id):
    if session.get("access_role") not in {"nco","company_hq","battalion_hq","s1","commander","admin"}:
        abort(403)
    status = (request.form.get("duty_status") or "PRESENT FOR DUTY").upper()
    allowed = {"PRESENT FOR DUTY","FIELD DUTY","TRAINING","ATTACHED","TEMPORARY DUTY",
               "HOSPITAL","WIA","LEAVE","AWOL","INACTIVE","REPLACEMENT — UNASSIGNED","DEROS PENDING"}
    if status not in allowed:
        abort(400)
    authority = session.get("display_name") or session.get("username")
    remarks = request.form.get("remarks")
    prior = fetch_one("SELECT duty_status, loa_start_date, loa_expected_return_date, loa_actual_return_date FROM personnel WHERE id=%s", (personnel_id,)) or {}

    execute("UPDATE duty_status_history SET is_current=FALSE,ended_at=NOW() WHERE personnel_id=%s AND is_current=TRUE",(personnel_id,))
    execute("INSERT INTO duty_status_history(personnel_id,duty_status,authority,remarks) VALUES(%s,%s,%s,%s)",
            (personnel_id,status,authority,remarks))

    if status == "LEAVE":
        loa_start = request.form.get("loa_start_date") or date.today()
        loa_expected = request.form.get("loa_expected_return_date") or None
        execute(
            "UPDATE personnel SET duty_status=%s, loa_start_date=%s, loa_expected_return_date=%s, loa_actual_return_date=NULL, loa_remarks=%s, updated_at=NOW() WHERE id=%s",
            (status, loa_start, loa_expected, remarks, personnel_id),
        )
        narrative = f"Placed on authorized leave. Leave began {loa_start}."
        if loa_expected:
            narrative += f" Expected return {loa_expected}."
        if remarks:
            narrative += f" Remarks: {remarks}"
        write_service_entry(personnel_id,"LEAVE","PLACED ON AUTHORIZED LEAVE", narrative, authority, None, date.today())
        create_personnel_order(personnel_id,"LEAVE","LEAVE ORDERS",narrative,effective_date=date.today(),authority=authority,details={"expected_return":str(loa_expected) if loa_expected else None},source_key=f"LEAVE:{personnel_id}:{loa_start}")
        pa=open_personnel_action(personnel_id,"LEAVE","Authorized leave","S-1","ROUTINE",authority,{"start":str(loa_start),"expected_return":str(loa_expected) if loa_expected else None},source_key=f"ACTION:LEAVE:{personnel_id}:{loa_start}")
        if pa: transition_personnel_action(pa["id"],"COMPLETE",authority,"Leave order issued; activity penalties suspended during authorized absence.")
    else:
        returning_from_leave = str(prior.get("duty_status") or "").upper() == "LEAVE"
        if returning_from_leave:
            loa_return = request.form.get("loa_return_date") or date.today()
            execute(
                "UPDATE personnel SET duty_status=%s, loa_actual_return_date=%s, updated_at=NOW() WHERE id=%s",
                (status, loa_return, personnel_id),
            )
            narrative = f"Returned to duty on {loa_return}."
            if prior.get("loa_start_date"):
                narrative = f"Returned to duty from leave. Absence began {prior.get('loa_start_date')} and concluded {loa_return}."
            if remarks:
                narrative += f" Remarks: {remarks}"
            write_service_entry(personnel_id,"LEAVE","RETURNED TO DUTY", narrative, authority, None, date.today())
            create_personnel_order(personnel_id,"RETURN","RETURN TO DUTY ORDERS",narrative,effective_date=date.today(),authority=authority,source_key=f"RETURN:{personnel_id}:{loa_return}")
            pa=open_personnel_action(personnel_id,"LEAVE","Return to duty","S-1","ROUTINE",authority,{"return_date":str(loa_return)},source_key=f"ACTION:RETURN:{personnel_id}:{loa_return}")
            if pa: transition_personnel_action(pa["id"],"COMPLETE",authority,"Return-to-duty order filed.")
        else:
            execute("UPDATE personnel SET duty_status=%s,updated_at=NOW() WHERE id=%s",(status,personnel_id))
            write_service_entry(personnel_id,"DUTY STATUS",status,
                            f"Duty status changed to {status}.",authority,
                            None,date.today())
    flash("DUTY STATUS ENTERED ON PERSONNEL RECORD.", "success")
    return redirect(request.referrer or url_for("morning_report"))


@app.get("/personnel")
def personnel_office():
    personnel = soldier_view(linked_personnel()) if database_ready() else None
    roster = fetch_all(
        """SELECT id,rank_code,last_name,first_name,unit_code,platoon,squad,
                  duty_position,field_status,readiness_status,readiness_percent,mos_code
           FROM personnel WHERE archived=FALSE AND separated_at IS NULL
           ORDER BY unit_code,platoon NULLS FIRST,squad NULLS FIRST,last_name,first_name
           LIMIT 250"""
    ) if database_ready() else []
    return render_template("personnel.html", personnel=personnel, roster=roster)




@app.get("/training")
def training():
    personnel = soldier_view(linked_personnel()) if database_ready() else None
    qualifications = []
    duty_qualifications = []
    catalog = duty_qualification_catalog() if database_ready() else []
    if personnel:
        qualifications = fetch_all(
            "SELECT * FROM qualifications WHERE personnel_id=%s ORDER BY qualification_name",
            (personnel["id"],),
        )
        duty_qualifications = personnel_duty_qualifications(personnel["id"])
    return render_template(
        "training.html", personnel=personnel, qualifications=qualifications,
        duty_qualifications=duty_qualifications, catalog=catalog,
    )



def _document_display_fields(doc):
    """Resolve immutable display fields for official personnel paperwork.

    Personnel is the source of truth for current state, while document details/history
    preserve what was true when an order was issued.
    """
    record = dict(doc or {})
    details = record.get("details_json") or {}
    if not isinstance(details, dict):
        details = {}

    # Replacement/Vietnam orders should show the entry grade and MOS captured when
    # the document was issued, not a later promotion or MOS change.
    display_rank = details.get("rank") or record.get("rank_code") or "PVT"
    display_mos = details.get("mos_code") or record.get("mos_code") or "—"
    display_roster = details.get("battle_roster_number") or record.get("roster_number") or record.get("service_number") or "PENDING"

    rank_issue_date = details.get("rank_issue_date")
    if rank_issue_date:
        try:
            rank_issue_date = date.fromisoformat(str(rank_issue_date)[:10])
        except Exception:
            rank_issue_date = None
    if not rank_issue_date and record.get("personnel_id"):
        rank_row = fetch_one(
            """SELECT effective_date FROM promotion_history
               WHERE personnel_id=%s AND new_rank_code=%s
               ORDER BY effective_date ASC,created_at ASC LIMIT 1""",
            (record.get("personnel_id"), display_rank),
        )
        rank_issue_date = (rank_row or {}).get("effective_date")
    rank_issue_date = rank_issue_date or record.get("effective_date")

    record["display_rank"] = display_rank
    record["display_mos"] = display_mos
    record["display_roster"] = display_roster
    record["rank_issue_date"] = rank_issue_date
    return record


@app.get("/document/<document_id>")
@login_required
def personnel_document(document_id):
    doc = fetch_one("""SELECT d.*,p.rank_code,p.first_name,p.last_name,p.service_number,p.mos_code,p.duty_position,p.unit_code,p.platoon,p.squad,
                              br.roster_number
                       FROM personnel_documents d JOIN personnel p ON p.id=d.personnel_id
                       LEFT JOIN battle_roster_cards br ON br.personnel_id=p.id AND br.is_active=TRUE
                       WHERE d.id=%s""", (document_id,))
    if not doc:
        abort(404)
    if session.get("access_role") not in (COMMAND_ROLES | {"s1"}) and str(session.get("personnel_id") or "") != str(doc["personnel_id"]):
        abort(403)
    doc = _document_display_fields(doc)
    return render_template("personnel_document.html", doc=doc)


@app.get("/document/<document_id>/preview.svg")
@login_required
def personnel_document_preview(document_id):
    doc = fetch_one("""SELECT d.*,p.rank_code,p.first_name,p.last_name,p.mos_code,p.service_number,br.roster_number FROM personnel_documents d
                       JOIN personnel p ON p.id=d.personnel_id
                       LEFT JOIN battle_roster_cards br ON br.personnel_id=p.id AND br.is_active=TRUE
                       WHERE d.id=%s""", (document_id,))
    if not doc:
        abort(404)
    if session.get("access_role") not in (COMMAND_ROLES | {"s1"}) and str(session.get("personnel_id") or "") != str(doc["personnel_id"]):
        abort(403)
    import html, base64
    doc = _document_display_fields(doc)
    if doc.get("document_type") == "REPLACEMENT":
        grade=html.escape(str(doc.get("display_rank") or "PVT")); mos=html.escape(str(doc.get("display_mos") or "—"))
        name=html.escape(f"{doc.get('last_name','').upper()}, {doc.get('first_name','')[:1].upper()}.")
        roster=html.escape(str(doc.get("display_roster") or "PENDING"))
        eff=doc.get("rank_issue_date") or doc.get("effective_date"); dtext=eff.strftime("%d %B %Y") if eff else ""
        try:
            template_path = Path(app.root_path) / "static" / "art" / "vietnam-orders-template-transparent.webp"
            data_uri = "data:image/webp;base64," + base64.b64encode(template_path.read_bytes()).decode("ascii")
        except Exception:
            data_uri = ""
        number=html.escape(str(doc["document_number"]))
        routing=eff.strftime("%d %b %y").upper() if eff else ""
        svg=("<svg xmlns='http://www.w3.org/2000/svg' width='1576' height='998' viewBox='0 0 1576 998'>"
             f"<image href='{data_uri}' x='0' y='0' width='1576' height='998'/>"
             "<g font-family='Courier New,monospace' fill='#241e16' font-weight='700'>"
             f"<text x='300' y='274' font-size='16' text-anchor='middle'>{number}</text>"
             f"<text x='650' y='274' font-size='16' text-anchor='middle'>{html.escape(dtext)}</text>"
             "<text x='225' y='392' font-size='14' text-anchor='middle'>GRADE</text>"
             "<text x='330' y='392' font-size='14' text-anchor='middle'>MOS</text>"
             "<text x='500' y='392' font-size='14' text-anchor='middle'>NAME</text>"
             "<text x='700' y='392' font-size='14' text-anchor='middle'>RA NUMBER</text>"
             f"<text x='225' y='419' font-size='17' text-anchor='middle'>{grade}</text>"
             f"<text x='330' y='419' font-size='17' text-anchor='middle'>{mos}</text>"
             f"<text x='500' y='419' font-size='16' text-anchor='middle'>{name}</text>"
             f"<text x='700' y='419' font-size='16' text-anchor='middle'>{roster}</text>"
             f"<text x='955' y='214' font-size='15' text-anchor='middle'>{number}</text>"
             f"<text x='1190' y='244' font-size='15' text-anchor='middle'>{html.escape(dtext)}</text>"
             "<text x='878' y='350' font-size='12' text-anchor='middle'>GRADE</text>"
             "<text x='963' y='350' font-size='12' text-anchor='middle'>MOS</text>"
             "<text x='1080' y='350' font-size='12' text-anchor='middle'>NAME</text>"
             "<text x='1240' y='350' font-size='12' text-anchor='middle'>RA NUMBER</text>"
             f"<text x='878' y='375' font-size='15' text-anchor='middle'>{grade}</text>"
             f"<text x='963' y='375' font-size='15' text-anchor='middle'>{mos}</text>"
             f"<text x='1080' y='375' font-size='14' text-anchor='middle'>{name}</text>"
             f"<text x='1240' y='375' font-size='14' text-anchor='middle'>{roster}</text>"
             f"<text x='1088' y='704' font-size='15' text-anchor='middle' transform='rotate(4 1088 704)'>{html.escape(routing)}</text>"
             "</g></svg>")
        return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control":"no-store"})
    title = html.escape(str(doc["title"]))[:60]
    name = html.escape(f"{doc.get('rank_code') or ''} {doc['first_name']} {doc['last_name']}")[:55]
    number = html.escape(str(doc["document_number"]))
    svg = ("<svg xmlns='http://www.w3.org/2000/svg' width='900' height='1160'>"
           "<rect width='100%' height='100%' fill='#d9cfad'/><rect x='42' y='42' width='816' height='1076' fill='#eee5c9' stroke='#4b4433' stroke-width='3'/>"
           "<text x='450' y='110' text-anchor='middle' font-family='monospace' font-size='24' font-weight='bold'>HEADQUARTERS</text>"
           "<text x='450' y='145' text-anchor='middle' font-family='monospace' font-size='22'>1ST BATTALION, 5TH CAVALRY REGIMENT</text>"
           f"<text x='80' y='220' font-family='monospace' font-size='20'>{number}</text>"
           f"<text x='450' y='300' text-anchor='middle' font-family='monospace' font-size='30' font-weight='bold'>{title}</text>"
           f"<text x='90' y='390' font-family='monospace' font-size='24'>{name}</text>"
           f"<text x='90' y='445' font-family='monospace' font-size='19'>EFFECTIVE: {doc['effective_date']}</text>"
           "<line x1='90' y1='485' x2='810' y2='485' stroke='#4b4433'/><text x='90' y='550' font-family='monospace' font-size='18'>OFFICIAL PERSONNEL ORDER — OPEN FOR COMPLETE TEXT</text>"
           "<text x='450' y='1030' text-anchor='middle' font-family='monospace' font-size='20' font-weight='bold'>BY ORDER OF THE BATTALION COMMANDER</text></svg>")
    return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control":"no-store"})


@app.get("/internal/clerk/orders/pending")
def internal_clerk_orders_pending():
    if not _clerk_authorized():
        return {"ok": False}, 401
    guild_id = request.args.get("guild_id")
    rows = fetch_all("""SELECT d.id,d.document_type,d.document_number,d.title,d.effective_date,d.body_text,d.authority,
                               p.rank_code,p.first_name,p.last_name,p.unit_code,p.platoon,p.squad
                        FROM personnel_documents d JOIN personnel p ON p.id=d.personnel_id
                        WHERE d.discord_posted_at IS NULL AND (d.source_guild_id=%s OR d.source_guild_id IS NULL)
                        ORDER BY d.created_at LIMIT 25""", (guild_id,))
    return {"ok": True, "orders": [{**row,"id":str(row["id"]),"effective_date":str(row["effective_date"])} for row in rows]}


@app.post("/internal/clerk/orders/<document_id>/posted")
def internal_clerk_order_posted(document_id):
    if not _clerk_authorized():
        return {"ok": False}, 401
    execute("UPDATE personnel_documents SET discord_posted_at=NOW() WHERE id=%s", (document_id,))
    return {"ok": True}

@app.get("/orders")
def orders():
    ops = fetch_all("SELECT * FROM operations ORDER BY operation_date DESC NULLS LAST, created_at DESC") if database_ready() else []
    return render_template("orders.html", operations=ops)


@app.get("/why-join-us")
def why_join():
    return render_template("why_join.html")




@app.get("/1-5-awards-and-decorations")
def awards_decorations():
    ribbons=[]
    try:
        if database_ready():
            ribbons = fetch_all("""SELECT ribbon_code,ribbon_name,automation_mode,requirement_text,sort_order,image_filename
                                   FROM ribbon_catalog WHERE is_active=TRUE ORDER BY sort_order,ribbon_name""")
    except Exception:
        app.logger.exception("Public awards catalog unavailable")
    return render_template("awards_decorations.html", ribbons=ribbons, medals=[])

@app.get("/about-1-5-cav")
def about():
    # Retired standalone 5th Cavalry page; keep legacy URL safe.
    return redirect(url_for("organization"))


def _recruit_case_number() -> str:
    year = date.today().strftime("%y")
    for _ in range(50):
        candidate = f"RC-{year}-{_random_digits(4)}"
        if not fetch_one("SELECT 1 FROM recruiting_cases WHERE case_number=%s", (candidate,)):
            return candidate
    raise RuntimeError("Could not allocate recruiting case number")


def _oauth_fernet() -> Fernet:
    # Derive a stable encryption key from the existing website secret so OAuth
    # refresh/access tokens are never stored in plaintext in PostgreSQL.
    digest = hashlib.sha256(CONFIG.secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _oauth_encrypt(value: str | None) -> str | None:
    if not value:
        return None
    return _oauth_fernet().encrypt(value.encode("utf-8")).decode("ascii")


def _oauth_decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _oauth_fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return None


def _discord_refresh_oauth(case: dict) -> dict:
    refresh_token = _oauth_decrypt(case.get("discord_oauth_refresh_token_enc"))
    if not refresh_token:
        raise RuntimeError("Discord authorization must be renewed before automatic server join.")
    body = urllib.parse.urlencode({
        "client_id": CONFIG.discord_client_id,
        "client_secret": CONFIG.discord_client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://discord.com/api/oauth2/token", data=body,
        headers={"Content-Type":"application/x-www-form-urlencoded","User-Agent":"1-5-Cav-Recruiting/1.0"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            token_data=json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail=exc.read().decode("utf-8","replace")[:500]
        log.warning("Discord OAuth refresh failed: %s %s", exc.code, detail)
        raise RuntimeError("Discord authorization refresh failed.") from exc
    access=token_data.get("access_token")
    if not access:
        raise RuntimeError("Discord did not return a refreshed access token.")
    expires_at=datetime.now(timezone.utc)+timedelta(seconds=max(60,int(token_data.get("expires_in") or 604800)))
    execute("""UPDATE recruiting_cases SET discord_oauth_access_token_enc=%s,discord_oauth_refresh_token_enc=%s,
               discord_oauth_expires_at=%s,discord_oauth_scope=%s,updated_at=NOW() WHERE id=%s""",
            (_oauth_encrypt(access),_oauth_encrypt(token_data.get("refresh_token") or refresh_token),expires_at,token_data.get("scope"),case["id"]))
    return {"access_token":access,"expires_at":expires_at,"scope":token_data.get("scope") or ""}


def _discord_oauth_ready() -> bool:
    return bool(CONFIG.discord_client_id and CONFIG.discord_client_secret)


def _discord_oauth_redirect_uri() -> str:
    return CONFIG.discord_oauth_redirect_uri or url_for("recruiting_discord_callback", _external=True, _scheme="https")


def _discord_oauth_exchange(code: str) -> dict:
    redirect_uri = _discord_oauth_redirect_uri()
    body = urllib.parse.urlencode({
        "client_id": CONFIG.discord_client_id,
        "client_secret": CONFIG.discord_client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }).encode("utf-8")
    token_req = urllib.request.Request(
        "https://discord.com/api/oauth2/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "1-5-Cav-Recruiting/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(token_req, timeout=15) as resp:
            token_data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        log.warning("Discord OAuth token exchange failed: %s %s", exc.code, detail)
        raise RuntimeError("Discord authorization could not be completed.") from exc
    access_token = token_data.get("access_token")
    if not access_token:
        raise RuntimeError("Discord did not return an access token.")
    user_req = urllib.request.Request(
        "https://discord.com/api/users/@me",
        headers={"Authorization": f"Bearer {access_token}", "User-Agent": "1-5-Cav-Recruiting/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(user_req, timeout=15) as resp:
            raw_user = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        log.warning("Discord OAuth user lookup failed: %s %s", exc.code, detail)
        raise RuntimeError("Discord identity could not be read.") from exc
    return {
        "id": str(raw_user.get("id") or ""),
        "username": str(raw_user.get("username") or ""),
        "global_name": str(raw_user.get("global_name") or ""),
        "avatar": str(raw_user.get("avatar") or ""),
        "access_token": access_token,
        "refresh_token": token_data.get("refresh_token"),
        "expires_in": int(token_data.get("expires_in") or 604800),
        "scope": str(token_data.get("scope") or ""),
    }


def _recruit_oauth_return_url(default_endpoint="recruiting"):
    token=session.get("recruit_oauth_case_token")
    if token:
        return url_for("recruiting_status",token=token)
    return url_for(default_endpoint)


@app.get("/recruiting/status/<token>/discord/connect")
def recruiting_case_discord_connect(token):
    case=fetch_one("SELECT id,status FROM recruiting_cases WHERE public_token=%s",(token,))
    if not case:
        abort(404)
    if case.get("status") in {"DENIED","CLOSED","ENLISTED"}:
        flash("THIS RECRUITING CASE IS ALREADY CLOSED.","warning")
        return redirect(url_for("recruiting_status",token=token))
    session["recruit_oauth_case_token"]=token
    return redirect(url_for("recruiting_discord_connect"))


@app.get("/recruiting/discord/connect")
def recruiting_discord_connect():
    if not _discord_oauth_ready():
        flash("DISCORD IDENTITY VERIFICATION IS NOT CONFIGURED YET. CONTACT BATTALION STAFF.", "danger")
        return redirect(url_for("recruiting"))
    state = secrets.token_urlsafe(32)
    session["discord_oauth_state"] = state
    params = {
        "response_type": "code",
        "client_id": CONFIG.discord_client_id,
        "scope": "identify guilds.join",
        "state": state,
        "redirect_uri": _discord_oauth_redirect_uri(),
        "prompt": "consent",
    }
    return redirect("https://discord.com/oauth2/authorize?" + urllib.parse.urlencode(params))


@app.get("/recruiting/discord/callback")
def recruiting_discord_callback():
    expected = session.pop("discord_oauth_state", None)
    returned = request.args.get("state")
    if not expected or not returned or not secrets.compare_digest(expected, returned):
        flash("DISCORD VERIFICATION SESSION EXPIRED. PLEASE TRY AGAIN.", "danger")
        return redirect(url_for("recruiting"))
    if request.args.get("error"):
        flash("DISCORD IDENTITY VERIFICATION WAS CANCELLED.", "warning")
        return redirect(url_for("recruiting"))
    code = request.args.get("code", "").strip()
    if not code:
        flash("DISCORD DID NOT RETURN AN AUTHORIZATION CODE.", "danger")
        return redirect(url_for("recruiting"))
    try:
        identity = _discord_oauth_exchange(code)
    except Exception as exc:
        log.warning("Recruit Discord OAuth callback failed: %s", exc)
        flash("DISCORD IDENTITY VERIFICATION FAILED. PLEASE TRY AGAIN.", "danger")
        return redirect(url_for("recruiting"))
    if not identity.get("id") or not identity.get("username"):
        flash("DISCORD IDENTITY COULD NOT BE VERIFIED.", "danger")
        return redirect(url_for("recruiting"))
    existing_case_token=session.pop("recruit_oauth_case_token",None)
    if existing_case_token:
        case=fetch_one("SELECT * FROM recruiting_cases WHERE public_token=%s",(existing_case_token,))
        if not case:
            flash("THE RECRUITING CASE COULD NOT BE FOUND.","danger")
            return redirect(url_for("recruiting"))
        discord_user_id=int(identity["id"])
        conflict=fetch_one("""SELECT case_number,public_token FROM recruiting_cases
                              WHERE discord_user_id=%s AND id<>%s
                                AND status NOT IN ('DENIED','CLOSED','ENLISTED')
                              ORDER BY created_at DESC LIMIT 1""",(discord_user_id,case["id"]))
        if conflict:
            flash(f"THAT DISCORD ACCOUNT IS ALREADY ATTACHED TO ACTIVE CASE {conflict['case_number']}.","danger")
            return redirect(url_for("recruiting_status",token=existing_case_token))
        next_status='REPLACEMENT_DEPOT' if case.get('status')=='APPROVED_AWAITING_DISCORD' else case.get('status') or 'PENDING_COMMAND'
        expires_at=datetime.now(timezone.utc)+timedelta(seconds=max(60,int(identity.get("expires_in") or 604800)))
        execute("""UPDATE recruiting_cases SET discord_username_input=%s,discord_user_id=%s,discord_verified_username=%s,
                 discord_avatar_hash=%s,discord_oauth_linked_at=NOW(),status=%s,
                 discord_oauth_access_token_enc=%s,discord_oauth_refresh_token_enc=%s,discord_oauth_expires_at=%s,discord_oauth_scope=%s,
                 replacement_depot_entered_at=CASE WHEN %s='REPLACEMENT_DEPOT' THEN COALESCE(replacement_depot_entered_at,NOW()) ELSE replacement_depot_entered_at END,
                 updated_at=NOW() WHERE id=%s""",
                (identity.get("username"),discord_user_id,identity.get("global_name") or identity.get("username"),identity.get("avatar") or None,next_status,
                 _oauth_encrypt(identity.get("access_token")),_oauth_encrypt(identity.get("refresh_token")),expires_at,identity.get("scope"),
                 next_status,case["id"]))
        if next_status=='REPLACEMENT_DEPOT':
            flash("DISCORD IDENTITY VERIFIED. BATTALION CLERK WILL ADD YOU TO THE SERVER AND OPEN YOUR REPLACEMENT 201 FILE AUTOMATICALLY.","success")
        else:
            flash("DISCORD IDENTITY VERIFIED AND ATTACHED TO YOUR EXISTING APPLICATION.","success")
        return redirect(url_for("recruiting_status",token=existing_case_token))
    session["recruit_discord_identity"] = {
        "id":identity.get("id"),"username":identity.get("username"),"global_name":identity.get("global_name"),"avatar":identity.get("avatar"),
        "access_token_enc":_oauth_encrypt(identity.get("access_token")),"refresh_token_enc":_oauth_encrypt(identity.get("refresh_token")),
        "expires_in":identity.get("expires_in"),"scope":identity.get("scope")
    }
    flash("DISCORD IDENTITY VERIFIED. COMPLETE AND SUBMIT YOUR ENLISTMENT APPLICATION.", "success")
    return redirect(url_for("recruiting") + "#application")


@app.get("/recruiting/discord/switch")
def recruiting_discord_switch():
    session.pop("recruit_discord_identity", None)
    return redirect(url_for("recruiting_discord_connect"))


@app.route("/recruiting", methods=["GET", "POST"])
def recruiting():
    identity = session.get("recruit_discord_identity") or {}
    recruiting_counts = _public_home_safe("recruiting page counts", public_recruiting_snapshot, {
        "applications_pending":0,"command_review":0,"processing":0,"ready_assignment":0
    })
    recruit_step = 2 if identity.get("id") else 1
    active_members = fetch_all("""SELECT id,rank_code,first_name,last_name,unit_code FROM personnel
                                WHERE separated_at IS NULL
                                ORDER BY last_name,first_name""")
    if request.method == "POST":
        if not identity.get("id") or not identity.get("username"):
            flash("VERIFY YOUR DISCORD IDENTITY BEFORE SUBMITTING THE APPLICATION.", "danger")
            return redirect(url_for("recruiting") + "#discord-identity")
        timezone_name = (request.form.get("timezone_name") or "").strip()
        hll_experience = (request.form.get("hll_experience") or "").strip()
        role_interest = (request.form.get("role_interest") or "").strip()
        looking_for = (request.form.get("looking_for") or "").strip()
        play_style = (request.form.get("play_style") or "").strip()
        follows_chain = request.form.get("follows_chain")
        participation = (request.form.get("participation") or "").strip()
        applicant_notes = (request.form.get("applicant_notes") or "").strip() or None
        recruited_by_personnel_id = (request.form.get("recruited_by_personnel_id") or "").strip() or None
        if recruited_by_personnel_id and not fetch_one("SELECT 1 FROM personnel WHERE id=%s AND separated_at IS NULL", (recruited_by_personnel_id,)):
            flash("THE SELECTED RECRUITER IS NOT AN ACTIVE MEMBER. PLEASE SELECT AGAIN.", "danger")
            return render_template("recruiting.html", discord_invite_url=CONFIG.discord_invite_url, form=request.form, discord_identity=identity, oauth_ready=_discord_oauth_ready(), active_members=active_members, recruiting_counts=recruiting_counts, recruit_step=recruit_step, current_date=date.today())
        age_raw = (request.form.get("age") or "").strip()
        if not all([timezone_name, hll_experience, role_interest, looking_for, play_style, follows_chain, participation]):
            flash("COMPLETE ALL REQUIRED APPLICATION FIELDS BEFORE SUBMITTING.", "danger")
            return render_template("recruiting.html", discord_invite_url=CONFIG.discord_invite_url, form=request.form, discord_identity=identity, oauth_ready=_discord_oauth_ready(), active_members=active_members, recruiting_counts=recruiting_counts, recruit_step=recruit_step, current_date=date.today())
        try:
            age = int(age_raw) if age_raw else None
        except ValueError:
            flash("AGE MUST BE A NUMBER OR LEFT BLANK.", "danger")
            return render_template("recruiting.html", discord_invite_url=CONFIG.discord_invite_url, form=request.form, discord_identity=identity, oauth_ready=_discord_oauth_ready(), active_members=active_members, recruiting_counts=recruiting_counts, recruit_step=recruit_step, current_date=date.today())
        discord_user_id = int(identity["id"])
        duplicate = fetch_one("""SELECT case_number,public_token,status FROM recruiting_cases
                                 WHERE discord_user_id=%s
                                   AND status NOT IN ('DENIED','CLOSED','ENLISTED')
                                 ORDER BY created_at DESC LIMIT 1""", (discord_user_id,))
        if duplicate:
            flash(f"AN ACTIVE APPLICATION IS ALREADY ON FILE: {duplicate['case_number']}", "warning")
            return redirect(url_for("recruiting_status", token=duplicate["public_token"]))
        case_number = _recruit_case_number()
        public_token = secrets.token_urlsafe(24)
        verified_name = identity.get("global_name") or identity.get("username")
        oauth_expires_at=datetime.now(timezone.utc)+timedelta(seconds=max(60,int(identity.get("expires_in") or 604800)))
        execute("""INSERT INTO recruiting_cases
                   (case_number,public_token,discord_username_input,discord_user_id,discord_verified_username,
                    discord_avatar_hash,discord_oauth_linked_at,discord_oauth_access_token_enc,discord_oauth_refresh_token_enc,
                    discord_oauth_expires_at,discord_oauth_scope,age,timezone_name,hll_experience,role_interest,
                    looking_for,play_style,follows_chain,participation,applicant_notes,recruited_by_personnel_id,status)
                   VALUES(%s,%s,%s,%s,%s,%s,NOW(),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING_COMMAND')""",
                (case_number,public_token,identity.get("username"),discord_user_id,verified_name,identity.get("avatar") or None,
                 identity.get("access_token_enc"),identity.get("refresh_token_enc"),oauth_expires_at,identity.get("scope"),
                 age,timezone_name,hll_experience,role_interest,looking_for,play_style,follows_chain=='YES',participation,applicant_notes,recruited_by_personnel_id))
        session['recruiting_case_token'] = public_token
        session.pop("recruit_discord_identity", None)
        return redirect(url_for("recruiting_status", token=public_token))
    return render_template("recruiting.html", discord_invite_url=CONFIG.discord_invite_url, form={}, discord_identity=identity, oauth_ready=_discord_oauth_ready(), active_members=active_members, recruiting_counts=recruiting_counts, recruit_step=recruit_step, current_date=date.today())


@app.route("/recruiting/status/<token>", methods=["GET", "POST"])
def recruiting_status(token):
    case = fetch_one("SELECT * FROM recruiting_cases WHERE public_token=%s", (token,))
    if not case:
        abort(404)
    if request.method == "POST":
        response = (request.form.get("applicant_response") or "").strip()
        if case.get("status") != "MORE_INFO_REQUIRED" or not response:
            flash("NO ADDITIONAL RESPONSE IS CURRENTLY REQUIRED.", "warning")
        else:
            execute("UPDATE recruiting_cases SET applicant_response=%s,status='PENDING_COMMAND',updated_at=NOW() WHERE id=%s", (response,case['id']))
            flash("YOUR RESPONSE HAS BEEN RETURNED TO BATTALION HEADQUARTERS.", "success")
        return redirect(url_for("recruiting_status", token=token))
    return render_template("recruiting_status.html", case=case, discord_invite_url=CONFIG.discord_invite_url)


@app.post("/award-recommendation")
@login_required
def award_recommendation():
    recommender = linked_personnel()
    staff_role = session.get("access_role")
    if not member_is_nco(recommender) and staff_role not in {"nco", "company_hq"} and staff_role not in COMMAND_ROLES:
        abort(403)
    personnel_id = request.form.get("personnel_id")
    award_name = (request.form.get("award_name") or "").strip()
    justification = (request.form.get("justification") or "").strip()
    if not personnel_id or not award_name or not justification:
        flash("SOLDIER, PROPOSED AWARD, AND JUSTIFICATION ARE REQUIRED.", "danger")
        return redirect(request.referrer or url_for("my_soldier_record"))
    target = fetch_one("SELECT id,rank_code,first_name,last_name FROM personnel WHERE id=%s", (personnel_id,))
    if not target:
        abort(404)
    execute(
        """INSERT INTO personnel_recommendations
           (personnel_id,recommendation_type,recommended_action,justification,recommending_personnel_id,status)
           VALUES(%s,'AWARD',%s,%s,%s,'PENDING_S1')""",
        (personnel_id, award_name, justification, recommender.get("id") if recommender else None),
    )
    flash("AWARD RECOMMENDATION FORWARDED TO S-1 PERSONNEL FOR ADMINISTRATIVE REVIEW.", "success")
    return redirect(request.referrer or url_for("my_soldier_record"))




@app.route('/staff/personnel/<personnel_id>/manage',methods=['GET','POST'])
@login_required
def staff_personnel_manage(personnel_id):
    role=session.get('access_role')
    if role not in {'s1','s2','s3','s4','training','battalion_hq','commander','admin'}: abort(403)
    person=fetch_one('SELECT * FROM personnel WHERE id=%s AND separated_at IS NULL',(personnel_id,))
    if not person: abort(404)
    catalogs=personnel_form_catalogs(); authority=session.get('display_name') or session.get('username') or role.upper(); today=date.today()
    awards=fetch_all("SELECT ribbon_code,ribbon_name FROM ribbon_catalog WHERE is_active=TRUE ORDER BY sort_order,ribbon_name")
    operations_list=fetch_all("SELECT id,operation_number,title,start_at,status FROM operations ORDER BY COALESCE(start_at,created_at) DESC LIMIT 100")
    qual_types=fetch_all("SELECT id,code,display_name,battlefield_unit AS category FROM duty_qualification_types WHERE is_active=TRUE ORDER BY battlefield_unit,sort_order,display_name")
    training_programs=fetch_all("SELECT program_code,program_name,'TRAINING' AS category FROM training_program_catalog WHERE is_active=TRUE ORDER BY sort_order,program_name")
    current_weapon=current_weapon_for(person)
    evidence=award_recommendation_evidence(personnel_id)
    promotion_packet=promotion_board_packet(personnel_id)
    situation=current_situation_snapshot(person)
    if request.method=='POST':
        action=(request.form.get('action') or '').upper()
        eff_raw=request.form.get('effective_date') or today.isoformat()
        eff=date.fromisoformat(str(eff_raw)[:10]) if not isinstance(eff_raw,date) else eff_raw
        remarks=(request.form.get('remarks') or '').strip() or None
        if action=='ASSIGNMENT':
            if role not in {'s1','battalion_hq','commander','admin'}: abort(403)
            node=validate_system_choice(request.form.get('unit_node_id'),catalogs['assignment_options'],'id'); duty=validate_system_choice(request.form.get('duty_position'),catalogs['duty_positions'],'value'); mos=validate_system_choice((request.form.get('mos_code') or '').upper(),catalogs['mos_catalog'],'mos_code')
            if not node or not duty or not mos: flash('SELECT AN ASSIGNMENT, MOS, AND DUTY POSITION FROM THE AUTHORIZED LISTS.','danger')
            else:
                file_primary_mos_change(personnel_id,mos['mos_code'],eff,authority,remarks); process_assignment_action(personnel_id,node['id'],duty['value'],eff,authority,None,remarks); flash('ASSIGNMENT / TRANSFER FILED AND DISCORD ROLE MIRROR QUEUED.','success')
        elif action=='RANK':
            if role not in {'s1','battalion_hq','commander','admin'}: abort(403)
            rank=validate_system_choice((request.form.get('rank_code') or '').upper(),catalogs['ranks'],'rank_code')
            if rank: process_rank_action(personnel_id,rank['rank_code'],eff,authority,None,remarks); flash('RANK ACTION FILED AND DISCORD ROLE MIRROR QUEUED.','success')
        elif action=='APPOINTMENT':
            if role not in {'s1','battalion_hq','commander','admin'}: abort(403)
            appt=validate_system_choice(request.form.get('appointment_code'),catalogs['appointment_catalog'],'appointment_code'); unit_id=request.form.get('appointment_unit_node_id') or None
            if unit_id: validate_system_choice(unit_id,catalogs['organization_nodes'],'id')
            status=(request.form.get('appointment_status') or 'PERMANENT').upper()
            if status not in {'PERMANENT','ACTING','TEMPORARY'}: abort(400)
            if appt: process_appointment_action(personnel_id,appt['appointment_code'],None,status,eff,authority,None,remarks,unit_id); flash('APPOINTMENT FILED AND DISCORD ROLE MIRROR QUEUED.','success')
        elif action=='AWARD':
            if role not in {'s1','battalion_hq','commander','admin'}: abort(403)
            award=next((a for a in awards if a['ribbon_code']==request.form.get('ribbon_code')),None)
            if not award: abort(400)
            op_id=request.form.get('operation_id') or None
            citation=(request.form.get('citation') or '').strip()
            op=operation_record(op_id) if op_id else None
            body=f"The Soldier named herein is awarded {award['ribbon_name']}." + (f" For service connected with {op.get('operation_number') or op.get('title')}." if op else '') + (f" {citation}" if citation else '')
            doc=create_personnel_order(personnel_id,'AWARD','AWARD ORDERS',body,effective_date=eff,authority=authority,details={'award_name':award['ribbon_name'],'operation_id':str(op_id) if op_id else None,'citation':citation},source_key=f"ACTION-AWARD:{personnel_id}:{award['ribbon_code']}:{eff}:{op_id or 'NONE'}")
            order_no=(doc or {}).get('document_number')
            pa=fetch_one("INSERT INTO personnel_awards(personnel_id,award_name,award_date,citation,order_number) VALUES(%s,%s,%s,%s,%s) RETURNING id",(personnel_id,award['ribbon_name'],eff,citation or None,order_no))
            file_named_ribbon_award(personnel_id,award['ribbon_name'],eff,order_no,citation)
            write_service_entry(personnel_id,'AWARD',award['ribbon_name'],citation or body,authority,order_no,eff if isinstance(eff,date) else date.today())
            if op_id:
                execute("UPDATE personnel_field_citations SET used_for_award_id=%s WHERE personnel_id=%s AND operation_id=%s AND used_for_award_id IS NULL",((pa or {}).get('id'),personnel_id,op_id))
            flash('AWARD FILED WITH EVIDENCE AND PERMANENT ORDERS.','success')
        elif action=='FIELD_CITATION':
            if role not in {'s1','s3','battalion_hq','commander','admin'}: abort(403)
            body=(request.form.get('citation') or '').strip(); op_id=request.form.get('operation_id') or None
            if not body: flash('FIELD CITATION TEXT IS REQUIRED.','danger')
            else:
                execute("INSERT INTO personnel_field_citations(personnel_id,operation_id,citation_date,citation_type,citation_text,cited_by) VALUES(%s,%s,%s,%s,%s,%s)",(personnel_id,op_id,eff,request.form.get('citation_type') or 'FIELD CITATION',body,authority))
                write_service_entry(personnel_id,'FIELD CITATION','FIELD CITATION',body,authority,(operation_record(op_id) or {}).get('operation_number') if op_id else None,eff if isinstance(eff,date) else date.today())
                flash('FIELD CITATION ADDED TO AWARD EVIDENCE.','success')
        elif action=='QUALIFICATION':
            if role not in {'s3','training','battalion_hq','commander','admin'}: abort(403)
            qid=request.form.get('qualification_type_id'); validate_system_choice(qid,qual_types,'id')
            exp_raw=request.form.get('expiration_date') or None
            exp_date=date.fromisoformat(str(exp_raw)[:10]) if exp_raw else None
            award_duty_qualification(personnel_id,qid,None,eff,exp_date,remarks,authority)
            flash('QUALIFICATION FILED FROM THE AUTHORIZED QUALIFICATION CATALOG.','success')
        elif action=='TRAINING':
            if role not in {'s3','training','battalion_hq','commander','admin'}: abort(403)
            code=request.form.get('program_code'); validate_system_choice(code,training_programs,'program_code')
            certify_training_program(personnel_id,code,authority,remarks,eff)
            flash('TRAINING COMPLETION FILED FROM THE AUTHORIZED PROGRAM CATALOG.','success')
        elif action=='LEAVE':
            if role not in {'s1','battalion_hq','commander','admin'}: abort(403)
            start_raw=request.form.get('leave_start') or eff.isoformat(); end_raw=request.form.get('leave_end') or None
            start=date.fromisoformat(str(start_raw)[:10]); end=date.fromisoformat(str(end_raw)[:10]) if end_raw else None
            execute("UPDATE personnel SET duty_status='LEAVE',loa_start_date=%s,loa_expected_return_date=%s,loa_actual_return_date=NULL,loa_remarks=%s,updated_at=NOW() WHERE id=%s",(start,end,remarks,personnel_id))
            narrative=f"Authorized leave beginning {start}" + (f" through {end}" if end else '') + '.' + (f" {remarks}" if remarks else '')
            doc=create_personnel_order(personnel_id,'LEAVE','LEAVE ORDERS',narrative,effective_date=start,authority=authority,details={'return_date':end},source_key=f"ENGINE-LEAVE:{personnel_id}:{start}")
            write_service_entry(personnel_id,'LEAVE','PLACED ON AUTHORIZED LEAVE',narrative,authority,(doc or {}).get('document_number'),date.fromisoformat(str(start)[:10]))
            flash('AUTHORIZED LEAVE FILED. INACTIVITY ACCOUNTABILITY WILL RESPECT THE LEAVE PERIOD.','success')
        elif action=='WEAPON':
            if role not in {'s4','battalion_hq','commander','admin'}: abort(403)
            w=current_weapon_for(person)
            wa=(request.form.get('weapon_action') or '').upper()
            if wa=='ISSUE' and not w:
                issue_m16(personnel_id); flash('M16 ISSUED FROM S-4 INVENTORY.','success')
            elif w and wa in {'CLEANED','INSPECTED','PLACED IN MAINTENANCE','MAINTENANCE COMPLETED'}:
                weapon_maintenance_action(w['id'],wa,personnel_id,authority,remarks); flash('M16 ACTION FILED IN THE WEAPON SERVICE HISTORY.','success')
            else: flash('SELECT A VALID WEAPON ACTION FOR THE CURRENT ISSUE STATUS.','warning')
        elif action=='WATCHLIST':
            if role not in {'battalion_hq','commander','admin'}: abort(403)
            wt=request.form.get('watch_type')
            if wt not in {'WATCH FOR LEADERSHIP','PROMOTION CANDIDATE','TRAINING PRIORITY','RELIABILITY CONCERN'}: abort(400)
            execute("""INSERT INTO command_watchlist(personnel_id,watch_type,note,created_by,resolved_at,resolved_by)
                       VALUES(%s,%s,%s,%s,NULL,NULL) ON CONFLICT(personnel_id,watch_type) DO UPDATE SET note=EXCLUDED.note,created_by=EXCLUDED.created_by,created_at=NOW(),resolved_at=NULL,resolved_by=NULL""",(personnel_id,wt,remarks,authority))
            flash('COMMAND WATCHLIST ENTRY UPDATED.','success')
        elif action=='COMMAND_REMARK':
            if role not in {'s1','s3','s4','battalion_hq','commander','admin'}: abort(403)
            if remarks: write_service_entry(personnel_id,'COMMAND REMARK','COMMAND / STAFF REMARK',remarks,authority,None,date.today()); flash('COMMAND / STAFF REMARK FILED.','success')
        else: abort(400)
        return redirect(url_for('staff_personnel_manage',personnel_id=personnel_id))
    return render_template('staff_personnel_manage.html',personnel=person,authority=authority,today=today.isoformat(),
                           award_catalog=awards,operations_list=operations_list,qualification_types=qual_types,
                           training_programs=training_programs,current_weapon=current_weapon,evidence=evidence,
                           promotion_packet=promotion_packet,current_situation=situation,**catalogs)

@app.post("/personnel/<personnel_id>/staff-action")
@login_required
def staff_soldier_action(personnel_id):
    role=session.get("access_role")
    if role not in STAFF_ROLES: abort(403)
    person=fetch_one("SELECT * FROM personnel WHERE id=%s",(personnel_id,))
    if not person: abort(404)
    action=(request.form.get("staff_action") or "").upper()
    authority=session.get("display_name") or session.get("username") or role.upper()
    if action=="AWARD":
        if role not in {"s1","battalion_hq","commander","admin"}: abort(403)
        award_name=(request.form.get("award_name") or "").strip()
        citation=(request.form.get("citation") or "").strip()
        if not award_name:
            flash("SELECT AN AWARD BEFORE FILING.","danger")
        else:
            eff=request.form.get("award_date") or date.today()
            order=create_personnel_order(personnel_id,"AWARD","AWARD ORDERS",f"The Soldier named herein is awarded {award_name}. {citation}",effective_date=eff,authority=authority,details={"award_name":award_name,"citation":citation},source_key=f"DIRECT-AWARD:{personnel_id}:{award_name}:{eff}")
            order_no=(order or {}).get("document_number") if order else None
            execute("INSERT INTO personnel_awards(personnel_id,award_name,award_date,citation,order_number) VALUES(%s,%s,%s,%s,%s)",(personnel_id,award_name,eff,citation or None,order_no))
            file_named_ribbon_award(personnel_id,award_name,eff,order_no,citation)
            write_service_entry(personnel_id,"AWARD",award_name,citation,authority,order_no,eff if isinstance(eff,date) else date.today())
            staff_log("S-1","AWARD FILED",f"{person.get('rank_code')} {person.get('last_name')} — {award_name}",authority)
            flash("AWARD FILED DIRECTLY IN THE SOLDIER'S 201 FILE.","success")
    elif action=="COMMAND_REMARK":
        if role not in {"s1","s3","s4","battalion_hq","commander","admin"}: abort(403)
        remark=(request.form.get("remark") or "").strip()
        if remark:
            write_service_entry(personnel_id,"COMMAND REMARK","COMMAND / STAFF REMARK",remark,authority,None,date.today())
            staff_log(_staff_section(role) or "HQ","COMMAND REMARK",f"{person.get('rank_code')} {person.get('last_name')}: {remark[:100]}",authority)
            flash("COMMAND / STAFF REMARK FILED IN SERVICE HISTORY.","success")
    else:
        kind=(request.form.get("action_type") or action or "PERSONNEL").upper()
        subject=(request.form.get("subject") or f"{kind} ACTION — {person.get('rank_code')} {person.get('last_name')}").strip()
        details=(request.form.get("details") or "").strip()
        section=action_section_for_type(kind) or _staff_section(role) or "HQ"
        open_personnel_action(personnel_id,kind,subject,section,"ROUTINE",authority,{"remarks":details,"from_201":True})
        flash(f"{kind} ACTION OPENED FOR THIS SOLDIER.","success")
    return redirect(url_for("personnel_service_record",personnel_id=personnel_id)+"#staff-actions")


@app.route("/s1", methods=["GET", "POST"])
@login_required
@role_required("s1")
def s1():
    issued_packet = None
    if request.method == "POST":
        action = request.form.get("action", "create")
        if action == "create":
            first_name = request.form.get("first_name", "").strip()
            last_name = request.form.get("last_name", "").strip()
            if not first_name or not last_name:
                flash("NAME IS REQUIRED BEFORE A PERSONNEL RECORD CAN BE OPENED.", "danger")
            else:
                catalogs=personnel_form_catalogs()
                node=validate_system_choice(request.form.get("unit_node_id"),catalogs["assignment_options"],"id")
                mos=validate_system_choice((request.form.get("mos_code") or "").upper(),catalogs["mos_catalog"],"mos_code")
                duty=validate_system_choice(request.form.get("duty_position"),catalogs["duty_positions"],"value")
                if not node or not mos or not duty:
                    flash("SELECT ASSIGNMENT, MOS, AND DUTY POSITION FROM THE AUTHORIZED LISTS.","danger")
                    return redirect(url_for("s1"))
                legacy=legacy_assignment_from_node(node["id"])
                service_number = allocate_service_number()
                personnel = fetch_one(
                    """INSERT INTO personnel(service_number,first_name,last_name,rank_code,mos_code,duty_position,unit_node_id,unit_code,platoon,squad,date_joined,rvn_arrival_date,deros_date,field_status,readiness_status,readiness_percent,duty_status,roster_entered_at) VALUES (%s,%s,%s,'PVT',%s,%s,%s,%s,%s,%s,%s,%s,%s,'Assigned','PROCESSING',25,'PRESENT FOR DUTY',%s) RETURNING *""",
                    (service_number,first_name,last_name,mos["mos_code"],duty["value"],node["id"],legacy["unit_code"],legacy["platoon"],legacy["squad"],request.form.get("date_joined") or date.today(),request.form.get("rvn_arrival_date") or date.today(),request.form.get("deros_date") or None,date.today()),
                )
                execute("""INSERT INTO assignment_history(personnel_id,unit_node_id,unit_code,platoon,squad,duty_position,effective_date,is_current) VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE)""",(personnel["id"],node["id"],personnel["unit_code"],personnel.get("platoon"),personnel.get("squad"),personnel.get("duty_position"),personnel.get("date_joined") or date.today()))
                card, field_code = issue_battle_roster_card(personnel["id"])
                weapon = issue_m16(personnel["id"])
                communications_id = request.form.get("communications_id", "").strip()
                if communications_id and ":" in communications_id:
                    guild_id, communications_user_id = communications_id.split(":", 1)
                    execute(
                        "INSERT INTO website_member_links (guild_id,discord_user_id,personnel_id) VALUES (%s,%s,%s) ON CONFLICT (guild_id,discord_user_id) DO UPDATE SET personnel_id=EXCLUDED.personnel_id, linked_at=NOW()",
                        (int(guild_id), int(communications_user_id), str(personnel["id"])),
                    )
                write_service_entry(personnel["id"], "ARRIVAL", "REPORTED FOR DUTY", f"Reported for duty with 1st Battalion, 5th Cavalry Regiment and entered on the battalion roster.")
                write_service_entry(personnel["id"], "ASSIGNMENT", "ASSIGNED TO BATTALION", f"Assigned to {personnel['unit_code']}" + (f", {personnel['platoon']}" if personnel.get('platoon') else '') + (f", {personnel['squad']}" if personnel.get('squad') else '') + ".")
                write_service_entry(personnel["id"], "EQUIPMENT", "INDIVIDUAL WEAPON ISSUED", f"Issued U.S. Rifle, 5.56-MM, M16, Serial No. {weapon['serial_number']}, Rack No. {weapon['rack_number']}.")
                issued_packet = {"personnel": personnel, "card": card, "field_code": field_code, "weapon": weapon}
                flash("PERSONNEL RECORD OPENED. BATTLE ROSTER CARD AND INDIVIDUAL WEAPON ISSUED.", "success")
        elif action == "issue_credentials":
            pid = request.form.get("personnel_id")
            card, field_code = issue_battle_roster_card(pid)
            weapon = issue_m16(pid)
            personnel = fetch_one("SELECT * FROM personnel WHERE id=%s", (pid,))
            if field_code:
                write_service_entry(pid, "ADMIN", "BATTLE ROSTER CARD ISSUED", f"Battle Roster Card {card['roster_number']} issued by S-1.")
            issued_packet = {"personnel": personnel, "card": card, "field_code": field_code, "weapon": weapon}
            flash("BATTLE ROSTER / ARMS RECORD CHECK COMPLETE.", "success")
        elif action == "reset_field_code":
            pid = request.form.get("personnel_id")
            card = fetch_one("SELECT * FROM battle_roster_cards WHERE personnel_id=%s AND is_active=TRUE", (pid,))
            personnel = fetch_one("SELECT * FROM personnel WHERE id=%s", (pid,))
            if card:
                field_code = _random_field_code()
                execute("UPDATE battle_roster_cards SET field_code_hash=%s WHERE id=%s", (generate_password_hash(field_code), card["id"]))
                weapon = issue_m16(pid)
                issued_packet = {"personnel": personnel, "card": card, "field_code": field_code, "weapon": weapon}
                write_service_entry(pid, "ADMIN", "FIELD CODE REISSUED", "Battle Roster field credential reissued by S-1.")
                flash("NEW FIELD CODE ISSUED.", "success")
        elif action == "assign_communications":
            pid = request.form.get("personnel_id")
            communications_id = request.form.get("communications_id", "").strip()
            if communications_id and ":" in communications_id:
                guild_id, communications_user_id = communications_id.split(":", 1)
                execute("INSERT INTO website_member_links (guild_id,discord_user_id,personnel_id) VALUES (%s,%s,%s) ON CONFLICT (guild_id,discord_user_id) DO UPDATE SET personnel_id=EXCLUDED.personnel_id, linked_at=NOW()", (int(guild_id), int(communications_user_id), str(pid)))
                flash("BATTALION COMMUNICATIONS ROSTER ENTRY FILED.", "success")
        elif action == "certify_s1_onboarding":
            pid = request.form.get("personnel_id")
            authority = request.form.get("authority") or session.get("display_name") or session.get("username") or "S-1 PERSONNEL"
            progress = personnel_progress(pid)
            if not progress.get("s1_onboarded_at"):
                execute("UPDATE personnel_progress_control SET s1_onboarded_at=NOW(),s1_onboarded_by=%s,updated_at=NOW() WHERE personnel_id=%s", (authority,pid))
                write_service_entry(pid,"ADMIN","S-1 ONBOARDING COMPLETE","Completed initial S-1 personnel interview and battalion in-processing.",authority)
            person = soldier_view(fetch_one("SELECT * FROM personnel WHERE id=%s", (pid,)))
            replacement_training_status(person)
            flash("S-1 ONBOARDING CERTIFIED AND FILED IN THE SOLDIER'S PERSONNEL PROCESSING RECORD.", "success")
        elif action == "quick_personnel_correction":
            pid=request.form.get("personnel_id"); authority=session.get("display_name") or session.get("username") or "S-1 PERSONNEL"
            old=fetch_one("SELECT * FROM personnel WHERE id=%s",(pid,))
            if not old: abort(404)
            catalogs=personnel_form_catalogs(); rank=validate_system_choice((request.form.get("rank_code") or old.get("rank_code") or "").upper(),catalogs["ranks"],"rank_code"); mos=validate_system_choice((request.form.get("mos_code") or old.get("mos_code") or "").upper(),catalogs["mos_catalog"],"mos_code"); duty=validate_system_choice(request.form.get("duty_position") or old.get("duty_position"),catalogs["duty_positions"],"value"); node=validate_system_choice(request.form.get("unit_node_id") or old.get("unit_node_id"),catalogs["assignment_options"],"id")
            if not rank or not mos or not duty or not node: flash("CORRECTION BLOCKED — USE ONLY AUTHORIZED RANK / MOS / ASSIGNMENT / DUTY VALUES.","danger")
            else:
                if str(old.get("unit_node_id") or "")!=str(node["id"]) or (old.get("duty_position") or "")!=duty["value"]: process_assignment_action(pid,node["id"],duty["value"],date.today(),authority,None,"S-1 structured roster correction.")
                file_primary_mos_change(pid,mos["mos_code"],date.today(),authority,"S-1 structured roster correction.")
                if (old.get("rank_code") or "").upper()!=rank["rank_code"]: execute("UPDATE personnel SET rank_code=%s,updated_at=NOW() WHERE id=%s",(rank["rank_code"],pid))
                write_service_entry(pid,"ADMIN","S-1 PERSONNEL RECORD CORRECTION","Roster correction filed using authorized battalion dropdown values.",authority)
                flash("STRUCTURED PERSONNEL CORRECTION SAVED. NO FREE-TEXT ORGANIZATION VALUES WERE ACCEPTED.","success")
        elif action == "delete_erroneous_record":
            if session.get("access_role") not in {"battalion_hq","commander","admin"}: abort(403)
            pid=request.form.get("personnel_id")
            if (request.form.get("confirmation") or '').strip().upper()!='DELETE': flash("TYPE DELETE EXACTLY TO CONFIRM.","danger")
            else:
                execute("DELETE FROM website_member_links WHERE personnel_id=%s",(str(pid),)); execute("DELETE FROM personnel WHERE id=%s",(pid,)); flash("ERRONEOUS PERSONNEL RECORD DELETED.","success")
        elif action == "set_promotion_hold":
            pid = request.form.get("personnel_id")
            hold = request.form.get("promotion_hold") == "1"
            reason = request.form.get("promotion_hold_reason") or None
            personnel_progress(pid)
            execute("UPDATE personnel_progress_control SET promotion_hold=%s,promotion_hold_reason=%s,updated_at=NOW() WHERE personnel_id=%s", (hold,reason,pid))
            write_service_entry(pid,"PERSONNEL ACTION","PROMOTION HOLD " + ("ENTERED" if hold else "REMOVED"), reason or ("Promotion hold placed by S-1." if hold else "Promotion hold removed by S-1."), session.get("display_name") or session.get("username"))
            flash("PROMOTION HOLD STATUS UPDATED.", "success")
        elif action == "promotion_recommendation":
            pid = request.form.get("personnel_id")
            target = (request.form.get("target_rank") or "").upper()
            justification = request.form.get("justification") or "Promotion recommended based on demonstrated performance and eligibility."
            recommender = linked_personnel()
            execute(
                """INSERT INTO personnel_recommendations
                   (personnel_id,recommendation_type,recommended_action,justification,promotion_narrative,recommending_personnel_id,status)
                   VALUES(%s,'PROMOTION',%s,%s,%s,%s,'PENDING')""",
                (pid, f"PROMOTION TO {target}", justification, justification, recommender.get("id") if recommender else None),
            )
            write_service_entry(pid,"PERSONNEL ACTION","PROMOTION RECOMMENDATION",f"Recommended for promotion to {target}. {justification}",session.get("display_name") or session.get("username"))
            flash("PROMOTION RECOMMENDATION FORWARDED FOR COMMAND CONSIDERATION.", "success")
        elif action == "grant_award":
            pid = request.form.get("personnel_id")
            ribbon_code = (request.form.get("ribbon_code") or "").strip().upper()
            award_date = request.form.get("award_date") or date.today()
            citation = (request.form.get("citation") or "").strip()
            remarks = (request.form.get("remarks") or "").strip() or None
            authority = session.get("display_name") or session.get("username") or "S-1 PERSONNEL"
            person = fetch_one("SELECT * FROM personnel WHERE id=%s AND separated_at IS NULL", (pid,))
            catalog = fetch_one("SELECT * FROM ribbon_catalog WHERE ribbon_code=%s AND is_active=TRUE", (ribbon_code,))
            if not person or not catalog:
                flash("SELECT A VALID ACTIVE SOLDIER AND AWARD.", "danger")
            elif not citation:
                flash("A CITATION IS REQUIRED BEFORE THE AWARD CAN BE FILED.", "danger")
            elif fetch_one("SELECT 1 FROM personnel_ribbons WHERE personnel_id=%s AND ribbon_code=%s", (pid, ribbon_code)):
                flash(f"{catalog['ribbon_name'].upper()} IS ALREADY RECORDED FOR THIS SOLDIER.", "warning")
            else:
                award_name = catalog["ribbon_name"]
                narrative = f"{award_name} is awarded to {person.get('rank_code','')} {person.get('first_name','')} {person.get('last_name','')} for the following cited service: {citation}"
                if remarks:
                    narrative += f" Administrative remarks: {remarks}"
                doc = create_personnel_order(
                    pid, "AWARD", "AWARD ORDERS", narrative,
                    effective_date=award_date, authority=authority,
                    details={"award": award_name, "ribbon_code": ribbon_code, "citation": citation, "remarks": remarks},
                    source_key=f"S1-AWARD:{pid}:{ribbon_code}:{award_date}",
                )
                order_number = doc.get("document_number") if doc else None
                execute(
                    "INSERT INTO personnel_awards(personnel_id,award_name,award_date,citation,order_number) VALUES(%s,%s,%s,%s,%s)",
                    (pid, award_name, award_date, citation, order_number),
                )
                execute(
                    """INSERT INTO personnel_ribbons(personnel_id,ribbon_code,earned_at,source_type,source_reference,notes,is_worn)
                       VALUES(%s,%s,%s,'S-1 DIRECT AWARD',%s,%s,FALSE)
                       ON CONFLICT(personnel_id,ribbon_code) DO NOTHING""",
                    (pid, ribbon_code, award_date, order_number, citation),
                )
                write_service_entry(
                    pid, "AWARD", award_name.upper(),
                    f"Award filed by S-1. {citation}", authority, order_number,
                    award_date if isinstance(award_date, date) else date.fromisoformat(str(award_date)),
                )
                notify_soldier(
                    pid, "S-1 PERSONNEL", f"Award filed — {award_name}",
                    f"{award_name} has been entered in your permanent service record. You may choose whether to wear the ribbon on your uniform.",
                    notification_type="AWARD", source_key=f"AWARD-NOTICE:{pid}:{ribbon_code}:{order_number}", target_anchor="ribbons",
                )
                flash(f"{award_name.upper()} FILED. ORDER {order_number or 'NUMBER GENERATED'} ADDED TO THE SOLDIER'S 201 FILE. RIBBON IS AVAILABLE FOR THE MEMBER TO WEAR.", "success")
        elif action == "award_s1_forward":
            rec_id = request.form.get("recommendation_id")
            rec = fetch_one("SELECT * FROM personnel_recommendations WHERE id=%s AND UPPER(recommendation_type)='AWARD'", (rec_id,))
            if not rec:
                abort(404)
            award_name = (request.form.get("award_name") or rec.get("s1_award_name") or rec.get("recommended_action") or "").strip()
            justification = (request.form.get("justification") or rec.get("s1_justification") or rec.get("justification") or "").strip()
            remarks = (request.form.get("s1_remarks") or "").strip() or None
            authority = session.get("display_name") or session.get("username") or "S-1 PERSONNEL"
            execute(
                """UPDATE personnel_recommendations
                   SET s1_award_name=%s,s1_justification=%s,s1_reviewed_by=%s,s1_reviewed_at=NOW(),
                       remarks=%s,status='PENDING_COMMAND' WHERE id=%s""",
                (award_name, justification, authority, remarks, rec_id),
            )
            flash("AWARD RECOMMENDATION REVIEWED BY S-1 AND FORWARDED TO BATTALION HEADQUARTERS.", "success")
        elif action == "award_s1_return":
            rec_id = request.form.get("recommendation_id")
            authority = session.get("display_name") or session.get("username") or "S-1 PERSONNEL"
            remarks = (request.form.get("s1_remarks") or "RETURNED FOR CORRECTION").strip()
            execute(
                """UPDATE personnel_recommendations SET status='RETURNED_TO_RECOMMENDER',
                   s1_reviewed_by=%s,s1_reviewed_at=NOW(),remarks=%s WHERE id=%s""",
                (authority, remarks, rec_id),
            )
            flash("AWARD RECOMMENDATION RETURNED TO THE RECOMMENDING NCO.", "warning")
        elif action == "rank_action":
            pid = request.form.get("personnel_id")
            process_rank_action(
                pid, request.form.get("rank_code"),
                request.form.get("effective_date") or date.today(),
                request.form.get("authority") or None,
                request.form.get("order_number") or None,
                request.form.get("remarks") or None,
            )
            flash("RANK ACTION FILED IN THE SOLDIER'S PERMANENT SERVICE RECORD.", "success")
        elif action == "appointment_action":
            pid = request.form.get("personnel_id")
            process_appointment_action(
                pid, request.form.get("appointment_code"),
                request.form.get("organization") or None,
                request.form.get("appointment_status") or "PERMANENT",
                request.form.get("effective_date") or date.today(),
                request.form.get("authority") or None,
                request.form.get("order_number") or None,
                request.form.get("remarks") or None,
                request.form.get("appointment_unit_node_id") or None,
            )
            flash("APPOINTMENT FILED AND DUTY ACCESS UPDATED.", "success")
        elif action == "assignment_action":
            pid=request.form.get("personnel_id"); catalogs=personnel_form_catalogs(); node=validate_system_choice(request.form.get("unit_node_id"),catalogs["assignment_options"],"id"); duty=validate_system_choice(request.form.get("duty_position"),catalogs["duty_positions"],"value"); mos=validate_system_choice((request.form.get("mos_code") or "").upper(),catalogs["mos_catalog"],"mos_code") if request.form.get("mos_code") else None; authority=session.get("display_name") or session.get("username") or "S-1 PERSONNEL"; eff=request.form.get("effective_date") or date.today()
            if not node or not duty: flash("SELECT A VALID ASSIGNMENT AND DUTY POSITION.","danger")
            else:
                if mos: file_primary_mos_change(pid,mos["mos_code"],eff,authority,request.form.get("remarks") or None)
                process_assignment_action(pid,node["id"],duty["value"],eff,authority,None,request.form.get("remarks") or None)
                flash("ASSIGNMENT ORDERS FILED FROM SYSTEM DROPDOWNS. ROSTER, CHAIN OF COMMAND AND 201 FILE UPDATED.","success")
        elif action == "relieve_appointment":
            relieve_appointment(
                request.form.get("appointment_id"),
                request.form.get("ended_date") or date.today(),
                request.form.get("authority") or None,
                request.form.get("order_number") or None,
                request.form.get("remarks") or None,
            )
            flash("RELIEF FROM APPOINTMENT FILED IN THE SERVICE RECORD.", "success")
        elif action == "inactivity_contact":
            pid = request.form.get("personnel_id")
            authority = session.get("display_name") or session.get("username") or "S-1 PERSONNEL"
            method = (request.form.get("contact_method") or "DISCORD").upper()
            notes = (request.form.get("notes") or "").strip()
            execute("INSERT INTO inactivity_contact_log(personnel_id,contact_type,contact_method,notes,contacted_by) VALUES(%s,'CONTACT',%s,%s,%s)", (pid,method,notes,authority))
            execute("UPDATE personnel SET inactivity_disposition='CONTACTED',inactivity_disposition_at=NOW(),updated_at=NOW() WHERE id=%s", (pid,))
            write_service_entry(pid,"PERSONNEL","INACTIVITY CONTACT RECORDED",f"S-1/leadership contact recorded by {authority} via {method}. {notes}",authority)
            flash("INACTIVITY CONTACT FILED.", "success")
        elif action == "inactivity_leave":
            pid = request.form.get("personnel_id")
            authority = session.get("display_name") or session.get("username") or "S-1 PERSONNEL"
            start_date = request.form.get("loa_start_date") or date.today()
            return_date = request.form.get("loa_expected_return_date") or None
            reason = (request.form.get("notes") or "Authorized absence").strip()
            execute("UPDATE personnel SET duty_status='LEAVE',loa_start_date=%s,loa_expected_return_date=%s,loa_actual_return_date=NULL,loa_remarks=%s,inactivity_disposition='EXCUSED ABSENCE',inactivity_disposition_at=NOW(),updated_at=NOW() WHERE id=%s", (start_date,return_date,reason,pid))
            execute("UPDATE duty_status_history SET is_current=FALSE,ended_at=NOW() WHERE personnel_id=%s AND is_current=TRUE",(pid,))
            execute("INSERT INTO duty_status_history(personnel_id,duty_status,authority,remarks) VALUES(%s,'LEAVE',%s,%s)",(pid,authority,reason))
            write_service_entry(pid,"LEAVE","EXCUSED ABSENCE AUTHORIZED",f"Authorized absence from {start_date}" + (f" through {return_date}" if return_date else "") + f". {reason}",authority)
            flash("EXCUSED ABSENCE FILED — INACTIVITY AND M16 NEGLECT CLOCKS PAUSED.", "success")
        elif action == "inactivity_return":
            pid = request.form.get("personnel_id")
            authority = session.get("display_name") or session.get("username") or "S-1 PERSONNEL"
            execute("UPDATE personnel SET duty_status='PRESENT FOR DUTY',loa_actual_return_date=CURRENT_DATE,inactivity_disposition='RETURNED TO ACTIVE',inactivity_disposition_at=NOW(),activity_last_seen_at=NOW(),updated_at=NOW() WHERE id=%s", (pid,))
            write_service_entry(pid,"LEAVE","RETURNED TO ACTIVE STATUS","Soldier returned from authorized absence; inactivity tracking resumed from return date.",authority)
            flash("SOLDIER RETURNED TO ACTIVE STATUS.", "success")
        elif action == "inactivity_refer":
            pid = request.form.get("personnel_id")
            authority = session.get("display_name") or session.get("username") or "S-1 PERSONNEL"
            notes=(request.form.get("notes") or "30-day inactivity command review").strip()
            execute("UPDATE personnel SET administrative_review=TRUE,inactivity_disposition='COMMAND REVIEW',inactivity_disposition_at=NOW(),updated_at=NOW() WHERE id=%s", (pid,))
            open_personnel_action(pid,"PERSONNEL","Command inactivity / property accountability review","HQ","URGENT",authority,{"remarks":notes},source_key=f"INACTIVITY-COMMAND:{pid}")
            flash("SOLDIER REFERRED FOR COMMAND / PROPERTY ACCOUNTABILITY REVIEW.", "success")
    counts = fetch_one("SELECT COUNT(*) total, COUNT(*) FILTER (WHERE readiness_percent>=80) ready FROM personnel")
    recent = fetch_all("SELECT * FROM personnel WHERE archived=FALSE AND separated_at IS NULL ORDER BY last_name,first_name LIMIT 300")
    cards = fetch_all("SELECT brc.personnel_id,brc.roster_number,brc.issued_at,brc.last_used_at FROM battle_roster_cards brc WHERE brc.is_active=TRUE")
    card_map = {str(c['personnel_id']): c for c in cards}
    weapons = fetch_all("SELECT wih.personnel_id,wi.serial_number,wi.rack_number,wi.status,wi.condition_state FROM weapon_issue_history wih JOIN weapon_inventory wi ON wi.id=wih.weapon_id WHERE wih.is_current=TRUE")
    weapon_map = {str(w['personnel_id']): w for w in weapons}
    communications_roster = fetch_all("SELECT guild_id,discord_user_id,username,display_name FROM discord_members WHERE is_bot=FALSE AND active=TRUE ORDER BY COALESCE(display_name,username)")
    catalogs=personnel_form_catalogs(); ranks=catalogs["ranks"]; mos_catalog=catalogs["mos_catalog"]; appointment_catalog=catalogs["appointment_catalog"]; organization_nodes=catalogs["organization_nodes"]; assignment_options=catalogs["assignment_options"]; duty_positions=catalogs["duty_positions"]; staff_authority=session.get("display_name") or session.get("username") or "S-1 PERSONNEL"
    current_appointments = fetch_all("""SELECT pa.id,pa.personnel_id,pa.organization,pa.appointment_status,pa.effective_date,ac.appointment_name FROM personnel_appointments pa JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code WHERE pa.is_current=TRUE ORDER BY ac.sort_order""")
    appointment_map = {}
    for appt in current_appointments:
        appointment_map.setdefault(str(appt["personnel_id"]), []).append(appt)
    replacement_map = {}
    promotion_map = {}
    progress_map = {}
    for row in recent:
        soldier = soldier_view(row)
        replacement_map[str(row["id"])] = replacement_training_status(soldier)
        promotion_map[str(row["id"])] = promotion_eligibility(soldier)
        progress_map[str(row["id"])] = personnel_progress(row["id"])
    training_programs = fetch_all("SELECT * FROM training_program_catalog WHERE is_active=TRUE ORDER BY sort_order")
    award_catalog = fetch_all("SELECT ribbon_code,ribbon_name,automation_mode,sort_order FROM ribbon_catalog WHERE is_active=TRUE ORDER BY sort_order,ribbon_name")
    award_queue = fetch_all(
        """SELECT pr.*,p.rank_code,p.last_name,p.first_name,p.unit_code,
                  rp.rank_code AS recommender_rank,rp.last_name AS recommender_last,rp.first_name AS recommender_first
           FROM personnel_recommendations pr
           JOIN personnel p ON p.id=pr.personnel_id
           LEFT JOIN personnel rp ON rp.id=pr.recommending_personnel_id
           WHERE UPPER(pr.recommendation_type)='AWARD' AND pr.status IN ('PENDING_S1','RETURNED_TO_S1','PENDING')
           ORDER BY pr.created_at ASC"""
    )
    forwarded_awards = fetch_all(
        """SELECT pr.*,p.rank_code,p.last_name,p.first_name FROM personnel_recommendations pr
           JOIN personnel p ON p.id=pr.personnel_id
           WHERE UPPER(pr.recommendation_type)='AWARD' AND pr.status IN ('PENDING_COMMAND','APPROVED','DENIED')
           ORDER BY pr.created_at DESC LIMIT 30"""
    )
    inactivity_board=[]
    inactivity_counts={"CURRENT":0,"WATCH":0,"DEFICIENT":0,"INACTIVE":0,"COMMAND REVIEW":0,"EXCUSED ABSENCE":0}
    for row in recent:
        snap=inactivity_snapshot(row)
        inactivity_counts[snap["state"]]=inactivity_counts.get(snap["state"],0)+1
        weapon=weapon_map.get(str(row["id"]))
        inactivity_board.append({"person":row,"status":snap,"weapon":weapon})
    inactivity_board.sort(key=lambda x: (-int(x["status"].get("days") or 0), str(x["person"].get("last_name") or "")))
    s1_suspense = fetch_all("""SELECT pa.*,p.rank_code,p.first_name,p.last_name,p.unit_code,
                              CASE WHEN pa.due_date IS NULL THEN NULL ELSE (pa.due_date-CURRENT_DATE) END AS days_remaining
                       FROM personnel_actions pa LEFT JOIN personnel p ON p.id=pa.personnel_id
                       WHERE pa.owning_section='S-1' AND pa.status NOT IN ('COMPLETE','CLOSED','DENIED')
                       ORDER BY pa.due_date NULLS LAST,pa.priority DESC,pa.created_at LIMIT 100""")
    return render_template("s1_personnel.html", counts=counts, recent=recent, card_map=card_map, weapon_map=weapon_map, issued_packet=issued_packet, communications_roster=communications_roster, ranks=ranks, mos_catalog=mos_catalog, duty_positions=duty_positions, assignment_options=assignment_options, staff_authority=staff_authority, appointment_catalog=appointment_catalog, appointment_map=appointment_map, organization_nodes=organization_nodes, replacement_map=replacement_map, promotion_map=promotion_map, progress_map=progress_map, training_programs=training_programs, award_catalog=award_catalog, award_queue=award_queue, forwarded_awards=forwarded_awards, inactivity_board=inactivity_board, inactivity_counts=inactivity_counts, s1_suspense=s1_suspense, s1_today=date.today().isoformat(), workload=staff_workload("S-1"))


@app.route("/personnel-actions", methods=["GET","POST"])
@login_required
def personnel_actions():
    role=session.get("access_role")
    if role not in {"s1","s3","training","s4","battalion_hq","commander","admin"}: abort(403)
    role_section={"s1":"S-1","s3":"S-3","training":"S-3","s4":"S-4"}.get(role)
    if request.method=="POST":
        action=request.form.get("action")
        authority=session.get("display_name") or session.get("username") or role.upper()
        if action=="create":
            section=(request.form.get("owning_section") or role_section or "S-1").upper()
            if role_section and section!=role_section: abort(403)
            row=open_personnel_action(request.form.get("personnel_id") or None, request.form.get("action_type") or "PERSONNEL",
                                      request.form.get("subject") or "PERSONNEL ACTION", section, request.form.get("priority") or "ROUTINE", authority,
                                      {"remarks":request.form.get("remarks") or ""}, due_date=request.form.get("due_date") or None)
            flash("PERSONNEL ACTION OPENED AND ROUTED TO THE RESPONSIBLE STAFF SECTION.","success")
        elif action=="transition":
            row=fetch_one("SELECT * FROM personnel_actions WHERE id=%s",(request.form.get("action_id"),))
            if not row: abort(404)
            if role_section and row.get("owning_section")!=role_section: abort(403)
            new_status=(request.form.get("new_status") or "IN REVIEW").upper()
            new_section=(request.form.get("route_section") or row.get("owning_section")).upper()
            if role_section and new_section!=role_section and new_section!="HQ":
                abort(403)
            transition_personnel_action(row["id"],new_status,authority,request.form.get("remarks") or None,request.form.get("assigned_to") or None,new_section)
            flash("ACTION STATUS UPDATED AND ENTERED IN THE AUDIT LEDGER.","success")
        return redirect(url_for("personnel_actions"))
    view=(request.args.get("view") or "active").lower()
    mine=(request.args.get("mine") or "0") in {"1","true","yes"}
    authority=session.get("display_name") or session.get("username") or role.upper()
    section_filter="" if not role_section else " AND pa.owning_section=%s"
    params=() if not role_section else (role_section,)
    mine_filter=" AND pa.assigned_to=%s" if mine else ""
    if mine: params=params+(authority,)
    rows=fetch_all(f"""SELECT pa.*,p.rank_code,p.first_name,p.last_name,p.unit_code FROM personnel_actions pa
                        LEFT JOIN personnel p ON p.id=pa.personnel_id
                        WHERE pa.status NOT IN ('COMPLETE','CLOSED','DENIED') {section_filter}{mine_filter}
                        ORDER BY CASE pa.status WHEN 'OPEN' THEN 0 WHEN 'IN REVIEW' THEN 1 WHEN 'PENDING COMMAND' THEN 2 WHEN 'RETURNED' THEN 3 ELSE 5 END,
                        CASE pa.priority WHEN 'URGENT' THEN 0 WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 ELSE 2 END,pa.created_at DESC LIMIT 200""",params)
    archived_actions=[]
    archive_params=() if not role_section else (role_section,)
    if view=="archive":
        archived_actions=fetch_all(f"""SELECT pa.*,p.rank_code,p.first_name,p.last_name,p.unit_code FROM personnel_actions pa
                           LEFT JOIN personnel p ON p.id=pa.personnel_id
                           WHERE pa.status IN ('COMPLETE','CLOSED','DENIED') {section_filter}
                           ORDER BY pa.updated_at DESC,pa.created_at DESC LIMIT 250""",archive_params)
    count_where="" if not role_section else " AND owning_section=%s"
    archive_count=(fetch_one(f"SELECT COUNT(*) total FROM personnel_actions WHERE status IN ('COMPLETE','CLOSED','DENIED'){count_where}",archive_params) or {"total":0})["total"]
    personnel_list=fetch_all("SELECT id,rank_code,last_name,first_name,unit_code FROM personnel ORDER BY last_name,first_name")
    return render_template("personnel_actions.html",actions=rows,archived_actions=archived_actions,archive_count=archive_count,view=view,mine=mine,personnel_list=personnel_list,role_section=role_section,counts=section_action_counts(role_section))


@app.post("/documents/<document_id>/amend")
@login_required
def document_amendment(document_id):
    if session.get("access_role") not in {"s1","battalion_hq","commander","admin"}: abort(403)
    doc=fetch_one("SELECT * FROM personnel_documents WHERE id=%s",(document_id,))
    if not doc: abort(404)
    authority=session.get("display_name") or session.get("username") or "HEADQUARTERS"
    number=f"AMD-{date.today().strftime('%y%m%d')}-{str(document_id)[:4].upper()}"
    execute("INSERT INTO personnel_document_amendments(document_id,amendment_number,authority,reason,corrected_text) VALUES(%s,%s,%s,%s,%s)",
            (document_id,number,authority,request.form.get("reason") or "ADMINISTRATIVE CORRECTION",request.form.get("corrected_text") or None))
    write_service_entry(doc["personnel_id"],"DOCUMENT","AMENDMENT TO OFFICIAL ORDERS",f"{number} filed against {doc['document_number']}. {request.form.get('reason') or 'Administrative correction.'}",authority,number,date.today())
    flash("AMENDMENT FILED. THE ORIGINAL DOCUMENT WAS PRESERVED.","success")
    return redirect(request.referrer or url_for("personnel_service_record",personnel_id=doc["personnel_id"]))


@app.get("/s2")
@login_required
@role_required("s2")
def s2():
    return render_template("section.html", section="S-2 INTELLIGENCE", section_code="s2", subtitle="Maps, intelligence summaries, threat reporting and operational intelligence.", counts=None, recent=[])


@app.get("/s3")
@login_required
@role_required("s3")
def s3():
    recent = fetch_all("SELECT * FROM operations ORDER BY created_at DESC LIMIT 10")
    counts=section_action_counts("S-3")
    training_due=fetch_one("SELECT COUNT(*) total FROM qualifications WHERE expires_at BETWEEN CURRENT_DATE AND CURRENT_DATE + 30") or {"total":0}
    operation_board=fetch_all("SELECT * FROM operations WHERE lifecycle_status NOT IN ('AAR FILED','CANCELLED') ORDER BY COALESCE(start_at,created_at),created_at LIMIT 20")
    deficiencies=training_deficiencies()
    return render_template("section.html", section="S-3 OPERATIONS & TRAINING", section_code="s3", subtitle="Operations ledger, training, attendance, qualifications, readiness and after-action workflow.", counts={"total":counts.get("open",0),"ready":training_due.get("total",0)}, recent=recent, action_counts=counts, operation_board=operation_board, deficiencies=deficiencies, workload=staff_workload("S-3"))


@app.get("/s4")
@login_required
@role_required("s4")
def s4():
    counts = fetch_one("SELECT COUNT(*) total, COUNT(*) FILTER (WHERE condition_percent>=80) ready FROM equipment_issues")
    action_counts=section_action_counts("S-4")
    due_weapons=fetch_all("""SELECT p.id AS personnel_id,p.rank_code,p.last_name,p.first_name,wi.id AS weapon_id,wi.serial_number,wi.rack_number,wi.condition_state,wi.last_inspected_at
                           FROM weapon_issue_history wih JOIN weapon_inventory wi ON wi.id=wih.weapon_id JOIN personnel p ON p.id=wih.personnel_id
                           WHERE wih.is_current=TRUE AND (wi.last_inspected_at IS NULL OR wi.last_inspected_at<NOW()-INTERVAL '14 days') ORDER BY wi.last_inspected_at NULLS FIRST""")
    return render_template("section.html", section="S-4 SUPPLY", section_code="s4", subtitle="Arms room, property book, equipment accountability, inspections, maintenance and logistics.", counts=counts, recent=[], action_counts=action_counts, due_weapons=due_weapons, workload=staff_workload("S-4"))


@app.route("/hq", methods=["GET", "POST"])
@login_required
@role_required("battalion_hq")
def hq():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "award_command_action":
            rec_id = request.form.get("recommendation_id")
            decision = (request.form.get("decision") or "").upper()
            rec = fetch_one(
                """SELECT pr.*,p.rank_code,p.first_name,p.last_name FROM personnel_recommendations pr
                   JOIN personnel p ON p.id=pr.personnel_id
                   WHERE pr.id=%s AND UPPER(pr.recommendation_type)='AWARD'""", (rec_id,)
            )
            if not rec:
                abort(404)
            authority = session.get("display_name") or session.get("username") or "BATTALION HEADQUARTERS"
            remarks = (request.form.get("command_remarks") or "").strip() or None
            if decision == "APPROVE":
                if rec.get("status") == "APPROVED":
                    flash("THIS AWARD RECOMMENDATION HAS ALREADY BEEN APPROVED.", "warning")
                else:
                    award_name = (request.form.get("award_name") or rec.get("s1_award_name") or rec.get("recommended_action") or "AWARD").strip()
                    award_date = request.form.get("award_date") or date.today()
                    order_number = _order_number("AWARD")
                    citation = rec.get("s1_justification") or rec.get("justification") or ""
                    execute(
                        "INSERT INTO personnel_awards(personnel_id,award_name,award_date,citation,order_number) VALUES(%s,%s,%s,%s,%s)",
                        (rec["personnel_id"], award_name, award_date, citation, order_number),
                    )
                    file_named_ribbon_award(rec["personnel_id"], award_name, award_date=award_date, source_reference=order_number, notes=citation)
                    execute(
                        """UPDATE personnel_recommendations SET status='APPROVED',command_decision='APPROVED',
                           command_reviewed_by=%s,command_reviewed_at=NOW(),award_order_number=%s,award_effective_date=%s,remarks=COALESCE(%s,remarks) WHERE id=%s""",
                        (authority, order_number, award_date, remarks, rec_id),
                    )
                    write_service_entry(rec["personnel_id"], "AWARD", award_name, f"Award approved by Battalion Headquarters. {citation}", authority, order_number, award_date if isinstance(award_date, date) else date.fromisoformat(str(award_date)))
                    create_personnel_order(rec["personnel_id"], "AWARD", "AWARD ORDERS", f"{award_name} is awarded by Battalion Headquarters. {citation}", effective_date=award_date, authority=authority, details={"award":award_name,"order_number":order_number}, source_key=f"AWARD:{rec_id}", document_number=order_number)
                    flash("AWARD APPROVED AND FILED IN THE SOLDIER'S PERMANENT SERVICE RECORD.", "success")
            elif decision == "RETURN":
                execute(
                    """UPDATE personnel_recommendations SET status='RETURNED_TO_S1',command_decision='RETURNED',
                       command_reviewed_by=%s,command_reviewed_at=NOW(),remarks=%s WHERE id=%s""",
                    (authority, remarks or "RETURNED TO S-1 FOR CORRECTION", rec_id),
                )
                flash("AWARD RECOMMENDATION RETURNED TO S-1 FOR CORRECTION.", "warning")
            elif decision == "DENY":
                execute(
                    """UPDATE personnel_recommendations SET status='DENIED',command_decision='DENIED',
                       command_reviewed_by=%s,command_reviewed_at=NOW(),remarks=%s WHERE id=%s""",
                    (authority, remarks, rec_id),
                )
                flash("AWARD RECOMMENDATION DENIED AND CLOSED.", "warning")
            else:
                abort(400)
        return redirect(url_for("hq"))
    personnel_count = fetch_one("SELECT COUNT(*) total FROM personnel")
    ready_count = fetch_one("SELECT COUNT(*) total FROM personnel WHERE readiness_status='READY' OR readiness_percent>=80")
    current_ops = fetch_all("SELECT * FROM operations ORDER BY operation_date NULLS LAST, created_at DESC LIMIT 4")
    pending = fetch_one("SELECT COUNT(*) total FROM personnel WHERE readiness_status<>'READY' AND readiness_percent<80")
    weapons_due = fetch_one("SELECT COUNT(*) total FROM weapon_inventory WHERE status='ISSUED' AND (last_inspected_at IS NULL OR last_inspected_at < NOW() - INTERVAL '14 days')")
    award_queue = fetch_all(
        """SELECT pr.*,p.rank_code,p.last_name,p.first_name,p.unit_code,
                  rp.rank_code AS recommender_rank,rp.last_name AS recommender_last
           FROM personnel_recommendations pr JOIN personnel p ON p.id=pr.personnel_id
           LEFT JOIN personnel rp ON rp.id=pr.recommending_personnel_id
           WHERE UPPER(pr.recommendation_type)='AWARD' AND pr.status='PENDING_COMMAND'
           ORDER BY pr.s1_reviewed_at ASC NULLS FIRST,pr.created_at ASC"""
    )
    award_pending_count = len(award_queue)
    section_notifications={s:section_action_counts(s) for s in ("S-1","S-3","S-4","HQ")}
    promotion_board=[]
    for p in fetch_all("SELECT * FROM personnel ORDER BY last_name,first_name"):
        sv=soldier_view(p)
        for e in promotion_eligibility(sv):
            if e.get("status") in {"ELIGIBLE FOR CONSIDERATION","RECOMMENDED FOR PROMOTION"}:
                promotion_board.append({"person":sv,"eligibility":e})
    recruiting_counts = fetch_one("""SELECT COUNT(*) FILTER (WHERE status IN ('SUBMITTED','DISCORD_VERIFIED','PENDING_COMMAND')) AS new_count,
                                               COUNT(*) FILTER (WHERE status='MORE_INFO_REQUIRED') AS info_count,
                                               COUNT(*) FILTER (WHERE status IN ('REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING')) AS approved_count
                                        FROM recruiting_cases""") or {}
    return render_template("hq.html", personnel_count=personnel_count, ready_count=ready_count, pending=pending, weapons_due=weapons_due, current_ops=current_ops, award_queue=award_queue, award_pending_count=award_pending_count, section_notifications=section_notifications, promotion_board=promotion_board, command_workload=staff_workload("HQ"), command_personnel=fetch_all("SELECT id,rank_code,last_name,first_name FROM personnel WHERE separated_at IS NULL ORDER BY last_name,first_name"), recruiting_counts=recruiting_counts, unit_nodes=fetch_all("SELECT id,display_name FROM unit_nodes WHERE is_active=TRUE ORDER BY sort_order,display_name"))




@app.post("/my-soldier-record/notifications/<notification_id>/acknowledge")
@login_required
def notification_ack(notification_id):
    p=linked_personnel()
    if not p: abort(403)
    execute("UPDATE soldier_notifications SET acknowledged_at=NOW() WHERE id=%s AND personnel_id=%s",(notification_id,p["id"]))
    return redirect(url_for("my_soldier_record"))


@app.post("/my-soldier-record/weapon/clean")
@login_required
def member_clean_weapon():
    p = linked_personnel()
    if not p:
        abort(403)
    weapon = fetch_one(
        """SELECT wi.* FROM weapon_issue_history wih
           JOIN weapon_inventory wi ON wi.id=wih.weapon_id
           WHERE wih.personnel_id=%s AND wih.is_current=TRUE LIMIT 1""",
        (p["id"],),
    )
    if not weapon:
        flash("NO INDIVIDUAL M16 IS CURRENTLY ISSUED TO YOUR RECORD.", "warning")
        return redirect(url_for("my_soldier_record") + "#equipment")
    if str(weapon.get("status") or "").upper() == "MAINTENANCE":
        flash("THIS M16 IS IN S-4 MAINTENANCE AND CANNOT BE MEMBER-CLEANED UNTIL RELEASED.", "warning")
        return redirect(url_for("my_soldier_record") + "#equipment")
    performer = f"{p.get('rank_code') or ''} {p.get('last_name') or ''}".strip() or "SOLDIER"
    try:
        weapon_maintenance_action(
            weapon["id"], "CLEANED", p["id"], performer,
            "Operator cleaning performed by assigned Soldier."
        )
    except Exception:
        # A Soldier must never be stranded on an error page because an older
        # maintenance-log schema or optional history write is behind. Preserve
        # the actual cleaning, log the recovery, and let bootstrap migrations
        # repair the detailed ledger on subsequent deployments.
        log.exception(
            "Member weapon-clean audit recovery personnel_id=%s weapon_id=%s",
            p.get("id"), weapon.get("id"),
        )
        execute(
            """UPDATE weapon_inventory
               SET rounds_since_cleaning=0,last_cleaned_at=NOW(),
                   condition_percent=100,condition_state='SERVICEABLE',updated_at=NOW()
               WHERE id=%s""",
            (weapon["id"],),
        )
        try:
            refresh_weapon_condition(weapon["id"])
        except Exception:
            log.exception("Member weapon-clean condition refresh recovery weapon_id=%s", weapon.get("id"))
        try:
            write_service_entry(
                p["id"], "ARMS", "CLEANED",
                f"M16 serial {weapon.get('serial_number')} — operator cleaning completed.",
                performer, None, date.today(),
            )
        except Exception:
            log.exception("Member weapon-clean service-entry recovery personnel_id=%s", p.get("id"))
    execute("UPDATE personnel SET activity_last_seen_at=NOW(),updated_at=NOW() WHERE id=%s", (p["id"],))
    flash("M16 OPERATOR CLEANING COMPLETED — FOULING COUNTER RESET AND ENTRY FILED.", "success")
    return redirect(url_for("my_soldier_record") + "#equipment")


@app.post("/s4/weapon-inspection")
@login_required
def weapon_inspection_action():
    if session.get("access_role") not in {"s4","battalion_hq","commander","admin"}: abort(403)
    weapon_id=request.form.get("weapon_id")
    personnel_id=request.form.get("personnel_id") or None
    authority=session.get("display_name") or session.get("username") or "S-4 SUPPLY"
    condition=(request.form.get("condition_state") or "SERVICEABLE").upper()
    due=request.form.get("next_due_date") or (date.today()+timedelta(days=14))
    remarks=request.form.get("remarks") or None
    execute("""INSERT INTO weapon_inspections(weapon_id,personnel_id,inspection_date,next_due_date,condition_state,inspected_by,remarks)
               VALUES(%s,%s,%s,%s,%s,%s,%s)""",(weapon_id,personnel_id,request.form.get("inspection_date") or date.today(),due,condition,authority,remarks))
    execute("UPDATE weapon_inventory SET last_inspected_at=NOW(),condition_state=%s,updated_at=NOW() WHERE id=%s",(condition,weapon_id))
    if personnel_id:
        write_service_entry(personnel_id,"EQUIPMENT","M16 INSPECTION COMPLETED",f"Individual weapon inspected; condition {condition}. Next inspection due {due}.",authority,None,date.today())
        notify_soldier(personnel_id,"S-4","M16 inspection completed",f"Next inspection due {due}.",source_key=f"INSP-COMPLETE:{weapon_id}:{date.today()}",target_anchor="weapon")
    staff_log("S-4","WEAPON INSPECTION",f"Weapon inspection completed; next due {due}.",authority,personnel_id,details={"weapon_id":str(weapon_id),"condition":condition})
    flash("WEAPON INSPECTION FILED AND NEXT DUE DATE ESTABLISHED.","success")
    return redirect(request.referrer or url_for("s4"))


@app.post("/personnel/<personnel_id>/lifecycle")
@login_required
def personnel_lifecycle_action(personnel_id):
    if session.get("access_role") not in {"s1","battalion_hq","commander","admin"}: abort(403)
    p=fetch_one("SELECT * FROM personnel WHERE id=%s",(personnel_id,))
    if not p: abort(404)
    action=(request.form.get("action") or "").upper()
    authority=session.get("display_name") or session.get("username") or "HEADQUARTERS"
    if action=="SEPARATE":
        reason=request.form.get("reason") or "RELIEVED FROM ASSIGNMENT"
        # Close current property and return M16 to stock without deleting the historical issue record.
        issues=fetch_all("SELECT * FROM weapon_issue_history WHERE personnel_id=%s AND is_current=TRUE",(personnel_id,))
        for wi in issues:
            execute("UPDATE weapon_issue_history SET is_current=FALSE,turned_in_at=CURRENT_DATE,condition_at_turn_in='SERVICEABLE' WHERE id=%s",(wi["id"],))
            execute("UPDATE weapon_inventory SET status='AVAILABLE FOR ISSUE',updated_at=NOW() WHERE id=%s",(wi["weapon_id"],))
        equipment_rows=fetch_all("SELECT id,equipment_id FROM equipment_issue_history WHERE personnel_id=%s AND is_current=TRUE",(personnel_id,))
        for eq in equipment_rows:
            execute("UPDATE equipment_issue_history SET is_current=FALSE,returned_at=NOW(),condition_at_return='SERVICEABLE',turn_in_authority=%s WHERE id=%s",(authority,eq["id"]))
            execute("UPDATE equipment_inventory SET status='AVAILABLE',updated_at=NOW() WHERE id=%s",(eq["equipment_id"],))
        execute("UPDATE personnel SET separated_at=CURRENT_DATE,separation_reason=%s,lifecycle_state='SEPARATED',duty_status='INACTIVE',lifecycle_updated_at=NOW(),updated_at=NOW() WHERE id=%s",(reason,personnel_id))
        doc=create_personnel_order(personnel_id,"SEPARATION","RELIEF FROM ASSIGNMENT / SEPARATION",f"{p.get('rank_code','')} {p.get('first_name','')} {p.get('last_name','')} is relieved from active assignment with 1st Battalion, 5th Cavalry Regiment. {reason}",authority=authority,source_key=f"SEPARATION:{personnel_id}:{date.today()}")
        write_service_entry(personnel_id,"SEPARATION","RELIEVED FROM ASSIGNMENT",reason,authority,doc.get("document_number") if doc else None,date.today())
        battalion_history_entry("SEPARATION",f"{p.get('rank_code','')} {p.get('last_name','')} separated",reason,personnel_id,reference_number=doc.get("document_number") if doc else None)
        staff_log("S-1","SEPARATION",f"Separated {p.get('rank_code','')} {p.get('last_name','')}",authority,personnel_id,doc.get("document_number") if doc else None)
        ensure_tour_completion_summary(personnel_id,authority)
        enqueue_discord_role_sync(personnel_id,'SEPARATED')
    elif action=="ARCHIVE":
        execute("UPDATE personnel SET archived=TRUE,lifecycle_state='ARCHIVED',lifecycle_updated_at=NOW(),updated_at=NOW() WHERE id=%s",(personnel_id,))
        write_service_entry(personnel_id,"ADMIN","201 FILE ARCHIVED","Active personnel jacket transferred to former-member archive.",authority)
        staff_log("S-1","ARCHIVE",f"Archived 201 File for {p.get('rank_code','')} {p.get('last_name','')}",authority,personnel_id)
        enqueue_discord_role_sync(personnel_id,'ARCHIVED')
    elif action=="REOPEN":
        execute("UPDATE personnel SET archived=FALSE,separated_at=NULL,separation_reason=NULL,lifecycle_state='IN PROCESSING',duty_status='REPLACEMENT — UNASSIGNED',lifecycle_updated_at=NOW(),updated_at=NOW() WHERE id=%s",(personnel_id,))
        write_service_entry(personnel_id,"ADMIN","201 FILE REOPENED","Former Soldier returned to battalion control; prior service history retained.",authority)
        battalion_history_entry("RETURN TO UNIT",f"{p.get('rank_code','')} {p.get('last_name','')} returned to battalion","Historical 201 File reopened.",personnel_id)
        staff_log("S-1","REOPEN FILE",f"Reopened 201 File for {p.get('rank_code','')} {p.get('last_name','')}",authority,personnel_id)
        enqueue_discord_role_sync(personnel_id,'REOPENED')
    elif action=="EXTEND TOUR":
        new_deros=request.form.get("new_deros")
        if not new_deros: abort(400)
        old=p.get("deros_date")
        nd=date.fromisoformat(new_deros)
        days=(nd-old).days if old else None
        execute("UPDATE personnel SET deros_date=%s,deros_extension_date=%s,lifecycle_state='PRESENT FOR DUTY',updated_at=NOW() WHERE id=%s",(nd,nd,personnel_id))
        doc=create_personnel_order(personnel_id,"TOUR EXTENSION","TOUR EXTENSION ORDERS",f"Tour of duty extended to {nd}.",effective_date=date.today(),authority=authority,details={"previous_deros":str(old) if old else None,"new_deros":str(nd)},source_key=f"TOUR-EXT:{personnel_id}:{nd}")
        execute("INSERT INTO personnel_tour_extensions(personnel_id,previous_deros,new_deros,extension_days,authority,remarks,document_id) VALUES(%s,%s,%s,%s,%s,%s,%s)",(personnel_id,old,nd,days,authority,request.form.get("reason") or None,doc.get("id") if doc else None))
        write_service_entry(personnel_id,"TOUR","TOUR EXTENDED",f"DEROS extended from {old or 'not filed'} to {nd}.",authority,doc.get("document_number") if doc else None,date.today())
        staff_log("S-1","TOUR EXTENSION",f"Tour extended to {nd}",authority,personnel_id,doc.get("document_number") if doc else None)
    else: abort(400)
    flash("PERSONNEL LIFECYCLE ACTION FILED IN THE 201 RECORD.","success")
    return redirect(request.referrer or url_for("personnel_service_record",personnel_id=personnel_id))


@app.post("/operations/<operation_id>/lifecycle")
@login_required
def operation_lifecycle_action(operation_id):
    if session.get("access_role") not in {"s3","battalion_hq","commander","admin"}: abort(403)
    op=operation_record(operation_id)
    if not op: abort(404)
    action=(request.form.get("action") or "").upper()
    authority=session.get("display_name") or session.get("username") or "S-3 OPERATIONS"
    mapping={"PUBLISH":"PUBLISHED","START":"ACTIVE","CLOSE":"CLOSED","FILE_AAR":"AAR FILED"}
    target=mapping.get(action)
    if not target: abort(400)
    if target=="PUBLISHED": execute("UPDATE operations SET lifecycle_status='PUBLISHED',status='PUBLISHED',published_at=NOW(),updated_at=NOW() WHERE id=%s",(operation_id,))
    elif target=="ACTIVE": execute("UPDATE operations SET lifecycle_status='ACTIVE',status='ACTIVE',start_at=COALESCE(start_at,NOW()),updated_at=NOW() WHERE id=%s",(operation_id,))
    elif target=="CLOSED":
        execute("UPDATE operations SET lifecycle_status='CLOSED',status='CLOSED',completed_at=COALESCE(completed_at,NOW()),updated_at=NOW() WHERE id=%s",(operation_id,))
        try: update_unit_readiness_streaks(operation_id)
        except Exception: log.exception("Readiness streak update failed for operation %s",operation_id)
    else: execute("UPDATE operations SET lifecycle_status='AAR FILED',aar_filed_at=NOW(),updated_at=NOW() WHERE id=%s",(operation_id,))
    staff_log("S-3","OPERATION STATUS",f"{op.get('operation_number') or op.get('operation_code')} — {target}",authority,reference_number=op.get("operation_number"),details={"operation_id":str(operation_id)})
    battalion_history_entry("OPERATION",f"{op.get('operation_number') or op.get('operation_code')} — {target}",op.get("title") or "",operation_id=operation_id,reference_number=op.get("operation_number"))
    flash(f"OPERATION STATUS UPDATED — {target}.","success")
    return redirect(request.referrer or url_for("operations"))


@app.route("/battalion-control", methods=["GET","POST"])
@login_required
def battalion_control():
    if session.get("access_role") not in {"battalion_hq","commander","admin"}: abort(403)
    if request.method=="POST":
        action=(request.form.get("action") or "").upper()
        pid=request.form.get("personnel_id")
        authority=session.get("display_name") or session.get("username") or "BATTALION HEADQUARTERS"
        p=fetch_one("SELECT * FROM personnel WHERE id=%s",(pid,)) if pid else None
        if action=="RECALCULATE" and p:
            reconcile_lifecycle(soldier_view(p),authority)
            replacement_training_status(soldier_view(p))
            promotion_eligibility(soldier_view(p))
            flash("SOLDIER LIFECYCLE, TRAINING, AND PROMOTION ELIGIBILITY RECALCULATED.","success")
        elif action=="REGENERATE_ORDERS" and p:
            replacement_orders_for(pid)
            flash("INITIAL PERSONNEL ORDERS VERIFIED / REGENERATED IF MISSING.","success")
        elif action=="REISSUE_PROPERTY" and p:
            issue_m16(pid); ensure_standard_uniform(pid)
            flash("STANDARD PROPERTY ISSUE VERIFIED; MISSING ISSUE RECORDS RESTORED.","success")
        elif action=="REISSUE_ROSTER" and p:
            card,field=issue_battle_roster_card(pid)
            flash("BATTLE ROSTER CREDENTIAL RECORD VERIFIED. A NEW FIELD CODE IS ONLY CREATED IF NO ACTIVE CARD EXISTED.","success")
        elif action=="DELETE_PERSONNEL" and p:
            confirmation=(request.form.get("delete_confirmation") or "").strip().upper()
            if confirmation != "DELETE":
                flash("DELETE CANCELLED — TYPE DELETE EXACTLY TO CONFIRM PERMANENT REMOVAL.","danger")
            else:
                display=f"{p.get('rank_code') or ''} {p.get('first_name') or ''} {p.get('last_name') or ''}".strip()
                # Permanent duplicate-record cleanup is intentionally Command-only and transactional.
                # History rows with ON DELETE SET NULL remain in battalion history/audit tables;
                # Soldier-owned records cascade with the personnel row.
                with connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT user_id FROM user_personnel_links WHERE personnel_id=%s",(pid,))
                        linked_user_ids=[r[0] if not isinstance(r,dict) else r.get("user_id") for r in cur.fetchall()]
                        cur.execute("UPDATE supply_requisitions SET requested_by_personnel_id=NULL WHERE requested_by_personnel_id=%s",(pid,))
                        cur.execute("DELETE FROM website_member_links WHERE personnel_id=%s",(str(pid),))
                        cur.execute("DELETE FROM personnel WHERE id=%s",(pid,))
                        for uid in linked_user_ids:
                            if uid:
                                cur.execute("DELETE FROM site_users WHERE id=%s AND access_role='member'",(uid,))
                flash(f"DUPLICATE PERSONNEL RECORD PERMANENTLY DELETED — {display}.","success")
        else:
            flash("NO RECOVERY ACTION WAS PERFORMED.","warning")
        return redirect(url_for("battalion_control"))
    active=fetch_one("SELECT COUNT(*) total FROM personnel WHERE archived=FALSE AND separated_at IS NULL") or {"total":0}
    archived=fetch_one("SELECT COUNT(*) total FROM personnel WHERE archived=TRUE OR separated_at IS NOT NULL") or {"total":0}
    missing_mos=fetch_one("SELECT COUNT(*) total FROM personnel WHERE archived=FALSE AND (mos_code IS NULL OR BTRIM(mos_code)='')") or {"total":0}
    unlinked=fetch_one("""SELECT COUNT(*) total FROM personnel p LEFT JOIN website_member_links w ON w.personnel_id=p.id::text WHERE p.archived=FALSE AND w.personnel_id IS NULL""") or {"total":0}
    open_actions=section_action_counts(None)
    due_weapons=fetch_all("""SELECT p.id AS personnel_id,p.rank_code,p.last_name,p.first_name,wi.id AS weapon_id,wi.serial_number,wi.rack_number,wi.last_inspected_at
                           FROM weapon_issue_history wih JOIN weapon_inventory wi ON wi.id=wih.weapon_id JOIN personnel p ON p.id=wih.personnel_id
                           WHERE wih.is_current=TRUE AND (wi.last_inspected_at IS NULL OR wi.last_inspected_at<NOW()-INTERVAL '14 days') ORDER BY wi.last_inspected_at NULLS FIRST LIMIT 50""")
    lifecycle=fetch_all("SELECT lifecycle_state,COUNT(*) total FROM personnel GROUP BY lifecycle_state ORDER BY lifecycle_state")
    logs=fetch_all("SELECT * FROM staff_duty_log ORDER BY created_at DESC LIMIT 60")
    history=fetch_all("SELECT * FROM battalion_history ORDER BY history_date DESC,created_at DESC LIMIT 60")
    personnel_list=fetch_all("SELECT id,rank_code,last_name,first_name,lifecycle_state,unit_code FROM personnel ORDER BY archived,last_name,first_name")
    health={"active":active["total"],"archived":archived["total"],"missing_mos":missing_mos["total"],"unlinked":unlinked["total"],"open_actions":open_actions.get("open",0),"urgent":open_actions.get("urgent",0),"weapon_due":len(due_weapons)}
    daily=fetch_all("SELECT section,COUNT(*) total FROM staff_duty_log WHERE created_at::date=CURRENT_DATE GROUP BY section ORDER BY section")
    ops_count=int((fetch_one("SELECT COUNT(*) total FROM operations WHERE status IN ('COMPLETED','CLOSED') OR lifecycle_status IN ('CLOSED','AAR FILED')") or {"total":0})["total"] or 0)
    awards_count=int((fetch_one("SELECT COUNT(*) total FROM personnel_awards") or {"total":0})["total"] or 0)
    quals_count=int((fetch_one("SELECT COUNT(*) total FROM personnel_duty_qualifications WHERE status='QUALIFIED'") or {"total":0})["total"] or 0)
    milestones=[
        {"title":"10 OFFICIAL OPERATIONS","current":ops_count,"goal":10,"earned":ops_count>=10},
        {"title":"25 OFFICIAL OPERATIONS","current":ops_count,"goal":25,"earned":ops_count>=25},
        {"title":"50 AWARDS FILED","current":awards_count,"goal":50,"earned":awards_count>=50},
        {"title":"100 DUTY QUALIFICATIONS","current":quals_count,"goal":100,"earned":quals_count>=100},
    ]
    company_history=fetch_all("""SELECT COALESCE(p.unit_code,'BATTALION') unit_code,COUNT(*) total,MAX(bh.history_date) last_entry
                               FROM battalion_history bh LEFT JOIN personnel p ON p.id=bh.personnel_id
                               GROUP BY COALESCE(p.unit_code,'BATTALION') ORDER BY unit_code""")
    return render_template("battalion_control.html",health=health,lifecycle=lifecycle,due_weapons=due_weapons,logs=logs,history=history,personnel_list=personnel_list,daily=daily,milestones=milestones,company_history=company_history)


def _clerk_authorized() -> bool:
    """Private machine-to-machine authorization; never shown in member UI."""
    if not CONFIG.clerk_sync_key:
        return False
    supplied = request.headers.get("X-Battalion-Clerk-Key", "")
    return secrets.compare_digest(supplied, CONFIG.clerk_sync_key)


def _parse_iso(value):
    if not value:
        return None
    value = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _credit_scheduled_duty(event, personnel_id, seconds, source_reference=None):
    """Idempotently accumulate duty time and file credit once 45 minutes is met."""
    seconds = max(0, int(seconds or 0))
    row = fetch_one(
        "SELECT * FROM battalion_event_attendance WHERE event_id=%s AND personnel_id=%s",
        (event["id"], personnel_id),
    )
    previous = int(row.get("qualifying_seconds") or 0) if row else 0
    total = previous + seconds
    if row:
        execute("""UPDATE battalion_event_attendance
                   SET qualifying_seconds=%s,last_seen_at=NOW(),source_reference=COALESCE(%s,source_reference),updated_at=NOW()
                   WHERE id=%s""", (total, source_reference, row["id"]))
    else:
        execute("""INSERT INTO battalion_event_attendance
                   (event_id,personnel_id,qualifying_seconds,first_seen_at,last_seen_at,source_reference)
                   VALUES (%s,%s,%s,NOW(),NOW(),%s)""",
                (event["id"], personnel_id, total, source_reference))

    already = bool(row and row.get("credited_at"))
    threshold_seconds=max(300,int(event.get("credit_threshold_minutes") or 45)*60)
    partial_seconds=min(1200,max(300,threshold_seconds//2))
    grade = "FULL CREDIT" if total >= threshold_seconds else ("PARTIAL / LATE" if total >= partial_seconds else "NO CREDIT")
    attendance_percent = min(100, round((total / threshold_seconds) * 100))
    execute("""UPDATE battalion_event_attendance SET attendance_grade=%s,attendance_percent=%s,updated_at=NOW()
               WHERE event_id=%s AND personnel_id=%s""", (grade,attendance_percent,event["id"],personnel_id))

    # OPERATION ammunition is no longer delayed until closeout. Every verified
    # Battalion Clerk voice chunk advances the issued rifle toward the S-3 configured
    # full-operation expenditure.
    live_rounds=accrue_live_operation_weapon_rounds(event,personnel_id,total)
    if total < threshold_seconds or already:
        return False, total

    execute("""UPDATE battalion_event_attendance SET credited_at=NOW(),attendance_grade=%s,attendance_percent=%s,updated_at=NOW()
               WHERE event_id=%s AND personnel_id=%s""", (grade,attendance_percent,event["id"], personnel_id))
    execute("""INSERT INTO personnel_activity_credit
               (personnel_id,source,source_reference,activity_type,activity_date,duration_seconds,credited)
               VALUES (%s,'BATTALION DUTY',%s,%s,%s,%s,TRUE)""",
            (personnel_id, str(event["id"]), event["event_type"], event["starts_at"].date(), total))
    execute("UPDATE personnel SET activity_last_duty_at=NOW(),activity_last_seen_at=NOW(),updated_at=NOW() WHERE id=%s", (personnel_id,))
    write_service_entry(
        personnel_id,
        "FIELD SERVICE" if event["event_type"] == "OPERATION" else "DUTY",
        f'{event["event_type"]} — {grade}',
        f'{event["title"]} — {grade}; {total // 60} MINUTES RECORDED.',
        None,
        source_reference or event.get("external_event_id"),
    )
    if event["event_type"] == "OPERATION" and event.get("operation_id"):
        file_operation_participation(
            event["operation_id"], personnel_id,
            attendance_status="FULL CREDIT",
            rounds_expended=int((live_rounds or {}).get("target") or operation_round_target_for_time(event,total)),
            remarks=f'Automatic Battalion Clerk credit: {total // 60} qualifying minutes.',
            credited_by="BATTALION CLERK"
        )
    fresh=fetch_one("SELECT * FROM personnel WHERE id=%s",(personnel_id,))
    if fresh: sync_readiness(fresh)
    return True, total



# ---------------------------------------------------------------------------
# Battalion Clerk automatic personnel identity / Battle Roster provisioning
# ---------------------------------------------------------------------------

CLERK_RANK_ROLE_MAP = {
    "PVT": "PVT", "PRIVATE": "PVT",
    "PFC": "PFC", "PRIVATE FIRST CLASS": "PFC",
    "CPL": "CPL", "CORPORAL": "CPL",
    "SP4": "SP4", "SPECIALIST 4": "SP4", "SPECIALIST FOUR": "SP4",
    "SP5": "SP5", "SPECIALIST 5": "SP5", "SPECIALIST FIVE": "SP5",
    "SGT": "SGT", "SERGEANT": "SGT",
    "SP6": "SP6", "SPECIALIST 6": "SP6", "SPECIALIST SIX": "SP6",
    "SSG": "SSG", "STAFF SERGEANT": "SSG",
    "SFC": "SFC", "SERGEANT FIRST CLASS": "SFC",
    "SP7": "SP7", "SPECIALIST 7": "SP7", "SPECIALIST SEVEN": "SP7",
    "MSG": "MSG", "MASTER SERGEANT": "MSG",
    "1SG": "1SG", "FIRST SERGEANT": "1SG",
    "SGM": "SGM", "SERGEANT MAJOR": "SGM",
    "2LT": "2LT", "SECOND LIEUTENANT": "2LT",
    "1LT": "1LT", "FIRST LIEUTENANT": "1LT",
    "CPT": "CPT", "CAPTAIN": "CPT",
    "MAJ": "MAJ", "MAJOR": "MAJ",
    "LTC": "LTC", "LIEUTENANT COLONEL": "LTC",
}
CLERK_RANK_PRECEDENCE = {
    "PVT": 1, "PFC": 2, "CPL": 3, "SP4": 3, "SP5": 4, "SGT": 4,
    "SP6": 5, "SSG": 5, "SFC": 6, "SP7": 6, "MSG": 7, "1SG": 7,
    "SGM": 8, "2LT": 20, "1LT": 21, "CPT": 22, "MAJ": 23, "LTC": 24,
}

# 1/5 Cavalry community MOS codes. These intentionally describe the Soldier's
# preferred/playable HLL: Vietnam battlefield role rather than a historical Army PMOS.
CLERK_MOS_ROLE_MAP = {
    "00C": "Battalion Commander",
    "11L": "Infantry Squad Leader",
    "11R": "Rifleman",
    "11G": "Grenadier / Assault",
    "11M": "Machine Gunner",
    "91M": "Combat Medic",
    "12E": "Combat Engineer",
    "76S": "Supply & Support Specialist",
    "11S": "Reconnaissance Team Leader",
    "11N": "Sniper",
    "19C": "Armor Commander",
    "19K": "Armor Crewman",
    "67L": "Aviation Logistics",
    "67P": "Rotary-Wing Pilot",
    "67C": "Helicopter Crew Chief",
    "67G": "Aerial Door Gunner",
    "11O": "Mortar Observer",
    "11A": "Mortar Ammunition Bearer",
    "11T": "Mortar Gunner",
}
CLERK_MOS_ROLE_ALIASES = {
    "COMMANDER":"00C", "BATTALION COMMANDER":"00C",
    "SQUAD LEADER":"11L", "OFFICER":"11L", "INFANTRY SQUAD LEADER":"11L",
    "RIFLEMAN":"11R",
    "GRENADIER":"11G", "ASSAULT":"11G",
    "MACHINE GUNNER":"11M", "MACHINEGUNNER":"11M",
    "MEDIC":"91M", "COMBAT MEDIC":"91M",
    "ENGINEER":"12E", "COMBAT ENGINEER":"12E",
    "SUPPORT":"76S", "SUPPLY & SUPPORT SPECIALIST":"76S",
    "SPOTTER":"11S", "RECONNAISSANCE TEAM LEADER":"11S",
    "SNIPER":"11N",
    "TANK COMMANDER":"19C", "ARMOR COMMANDER":"19C",
    "CREWMAN":"19K", "ARMOR CREWMAN":"19K", "TANK CREWMAN":"19K",
    "HELICOPTER LOGISTICS OFFICER":"67L", "AVIATION LOGISTICS":"67L",
    "HELICOPTER PILOT":"67P", "ROTARY-WING PILOT":"67P",
    "HELICOPTER CREWMAN":"67C", "HELICOPTER CREW CHIEF":"67C",
    "HELICOPTER GUNNER":"67G", "DOOR GUNNER":"67G", "AERIAL DOOR GUNNER":"67G",
    "MORTAR OBSERVER":"11O",
    "MORTAR SUPPORT":"11A", "MORTAR AMMUNITION BEARER":"11A",
    "MORTAR GUNNER":"11T",
}

def _mos_from_discord_roles(roles) -> tuple[str | None, str | None]:
    # Prefer an explicit MOS code in the Discord role name, e.g. "11R — Rifleman".
    normalized = [re.sub(r"\s+", " ", str(r or "").upper().strip()) for r in (roles or [])]
    for cleaned in normalized:
        for code, title in CLERK_MOS_ROLE_MAP.items():
            if re.search(rf"(^|[^A-Z0-9]){re.escape(code)}($|[^A-Z0-9])", cleaned):
                return code, title
    for cleaned in normalized:
        if cleaned in CLERK_MOS_ROLE_ALIASES:
            code = CLERK_MOS_ROLE_ALIASES[cleaned]
            return code, CLERK_MOS_ROLE_MAP[code]
        for label, code in CLERK_MOS_ROLE_ALIASES.items():
            if re.search(rf"(^|[^A-Z0-9]){re.escape(label)}($|[^A-Z0-9])", cleaned):
                return code, CLERK_MOS_ROLE_MAP[code]
    return None, None

def _rank_from_discord_roles(roles) -> str | None:
    matches = []
    for role in roles or []:
        cleaned = re.sub(r"\s+", " ", str(role or "").upper().strip())
        if cleaned in CLERK_RANK_ROLE_MAP:
            matches.append(CLERK_RANK_ROLE_MAP[cleaned])
            continue
        for label, code in CLERK_RANK_ROLE_MAP.items():
            if re.search(rf"(^|[^A-Z0-9]){re.escape(label)}($|[^A-Z0-9])", cleaned):
                matches.append(code)
    return max(matches, key=lambda c: CLERK_RANK_PRECEDENCE.get(c, 0)) if matches else None


def _assignment_from_discord_roles(roles):
    unit=None; platoon=None; squad=None
    normalized=[re.sub(r"\s+"," ",str(r or "").upper().strip()) for r in (roles or [])]
    company_map={
        "A COMPANY":"A/1-5 CAV","ALPHA COMPANY":"A/1-5 CAV","A/1-5 CAV":"A/1-5 CAV",
        "B COMPANY":"B/1-5 CAV","BRAVO COMPANY":"B/1-5 CAV","B/1-5 CAV":"B/1-5 CAV",
        "C COMPANY":"C/1-5 CAV","CHARLIE COMPANY":"C/1-5 CAV","C/1-5 CAV":"C/1-5 CAV",
        "HHC":"HHC/1-5 CAV","HHC/1-5 CAV":"HHC/1-5 CAV","HEADQUARTERS & HEADQUARTERS COMPANY":"HHC/1-5 CAV"
    }
    for cleaned in normalized:
        for label,value in company_map.items():
            if cleaned==label or re.search(rf"(^|[^A-Z0-9]){re.escape(label)}($|[^A-Z0-9])",cleaned):
                unit=value
        m=re.search(r"\b(1ST|2ND|3RD|4TH)\s+(?:PLATOON|PLT)\b",cleaned)
        if m: platoon=f"{m.group(1)} PLATOON"
        m=re.search(r"\b(1ST|2ND|3RD|4TH)\s+(?:SQUAD|SQD)\b",cleaned)
        if m: squad=f"{m.group(1)} SQUAD"
    return unit,platoon,squad

def _sync_discord_assignment(person, roles, authority="HEADQUARTERS — BATTALION CLERK", *, fill_missing_only=False):
    """Apply Discord organizational roles to the personnel record.

    Discord is allowed to establish a missing initial company/platoon/squad assignment.
    Once an assignment exists in the personnel database, the database remains authoritative
    unless staff deliberately changes the assignment through the website.
    """
    unit,platoon,squad=_assignment_from_discord_roles(roles)
    changes=[]
    current_unit=person.get("unit_code")
    current_platoon=person.get("platoon")
    current_squad=person.get("squad")
    generic_unit=current_unit in {None,"","1-5 CAV","REPLACEMENT DETACHMENT"}

    if fill_missing_only:
        new_unit = unit if unit and generic_unit else current_unit
        new_platoon = platoon if platoon and not current_platoon else current_platoon
        new_squad = squad if squad and not current_squad else current_squad
    else:
        new_unit=unit or current_unit
        new_platoon=platoon or current_platoon
        new_squad=squad or current_squad

    if new_unit!=current_unit: changes.append(f"unit {new_unit}")
    if new_platoon!=current_platoon: changes.append(f"platoon {new_platoon}")
    if new_squad!=current_squad: changes.append(f"squad {new_squad}")

    if changes:
        execute("UPDATE assignment_history SET is_current=FALSE,ended_date=CURRENT_DATE WHERE personnel_id=%s AND is_current=TRUE",(person["id"],))
        assigned = bool(new_unit not in {None,"","1-5 CAV","REPLACEMENT DETACHMENT"} and new_platoon and new_squad)
        field_status = "Assigned" if assigned else (person.get("field_status") or "Replacement")
        duty_status = "PRESENT FOR DUTY" if assigned and str(person.get("duty_status") or "").upper() in {"REPLACEMENT — UNASSIGNED","REPLACEMENT - UNASSIGNED","IN PROCESSING"} else person.get("duty_status")
        execute("UPDATE personnel SET unit_code=%s,platoon=%s,squad=%s,field_status=%s,duty_status=COALESCE(%s,duty_status),updated_at=NOW() WHERE id=%s",(new_unit,new_platoon,new_squad,field_status,duty_status,person["id"]))
        execute("INSERT INTO assignment_history(personnel_id,unit_code,platoon,squad,duty_position,effective_date,is_current) VALUES(%s,%s,%s,%s,%s,CURRENT_DATE,TRUE)",(person["id"],new_unit,new_platoon,new_squad,person.get("duty_position")))
        write_service_entry(person["id"],"ASSIGNMENT","UNIT ASSIGNMENT UPDATED",f"Discord intake roles established the Soldier's organizational assignment: {', '.join(changes)}.",authority)
        create_personnel_order(person["id"],"ASSIGNMENT","UNIT ASSIGNMENT ORDERS",f"Effective this date, the Soldier is assigned to {new_unit}{' / '+new_platoon if new_platoon else ''}{' / '+new_squad if new_squad else ''}.",authority=authority,details={"unit":new_unit,"platoon":new_platoon,"squad":new_squad},source_key=f"ASSIGNMENT:{person['id']}:{new_unit}:{new_platoon}:{new_squad}")
    return fetch_one("SELECT * FROM personnel WHERE id=%s",(person["id"],))

def _discord_personnel_name(username: str, display_name: str) -> tuple[str, str]:
    raw = (display_name or username or "Replacement").strip()
    raw = re.sub(r"^\[[^\]]+\]\s*", "", raw).strip()
    parts = [p for p in raw.split() if p]
    if len(parts) >= 2:
        return parts[0][:80], " ".join(parts[1:])[:80]
    return "", (raw or username or "Replacement")[:80]

def _existing_personnel_candidate(username: str, display_name: str):
    candidates = []
    for name in {str(username or "").strip(), str(display_name or "").strip()}:
        if not name:
            continue
        candidates.extend(fetch_all(
            """SELECT * FROM personnel
               WHERE LOWER(TRIM(last_name))=LOWER(%s)
                  OR LOWER(TRIM(first_name || ' ' || last_name))=LOWER(%s)
               LIMIT 3""", (name, name)
        ))
    unique = {str(row["id"]): row for row in candidates}
    return next(iter(unique.values())) if len(unique) == 1 else None

def _ensure_clerk_personnel(guild_id:int, discord_user_id:int, username:str, display_name:str,
                            roles=None, *, create_if_missing=False, reason="identity_sync"):
    link = fetch_one("SELECT personnel_id FROM website_member_links WHERE guild_id=%s AND discord_user_id=%s",
                     (guild_id, discord_user_id))
    if link:
        person = fetch_one("SELECT * FROM personnel WHERE id=%s", (str(link["personnel_id"]),))
        if person:
            # The personnel table is authoritative after the 201 File exists. Rank and MOS
            # are never silently overwritten from Discord. Organizational roles are allowed
            # to fill an INITIAL missing company/platoon/squad assignment so a newly processed
            # Soldier appears correctly on the Battle Roster after the 30-second role settle.
            person = _sync_discord_assignment(person, roles, fill_missing_only=True)
            discord_rank = _rank_from_discord_roles(roles)
            discord_mos, _discord_mos_title = _mos_from_discord_roles(roles)
            drift=[]
            if discord_rank and discord_rank != person.get("rank_code"):
                drift.append({"field":"rank","discord":discord_rank,"canonical":person.get("rank_code")})
            if discord_mos and discord_mos != person.get("mos_code"):
                drift.append({"field":"mos","discord":discord_mos,"canonical":person.get("mos_code")})
            card, field_code = issue_battle_roster_card(person["id"])
            weapon = issue_m16(person["id"])
            ensure_standard_uniform(person["id"])
            reconcile_lifecycle(soldier_view(person),"HEADQUARTERS — BATTALION CLERK")
            person = canonical_personnel_snapshot(person["id"]) or person
            return {"created":False,"linked":True,"personnel":person,
                    "canonical_rank":person.get("rank_code"),"canonical_mos":person.get("mos_code"),
                    "canonical_unit":person.get("unit_code"),"canonical_platoon":person.get("platoon"),
                    "canonical_squad":person.get("squad"),"role_drift":drift,
                    "roster":card,"field_code":field_code,"weapon":weapon}



    rank = _rank_from_discord_roles(roles)
    mos_code, mos_title = _mos_from_discord_roles(roles)

    # A new 201 File may be opened ONLY when the Discord member currently holds
    # a recognized Army rank role. Voice-channel presence, joining the server,
    # or any generic create_if_missing request is not sufficient.
    if not rank:
        return {"created":False,"linked":False,"reason":"no recognized rank role"}
    if not mos_code:
        return {"created":False,"linked":False,"reason":"no recognized battlefield MOS role"}

    candidate = _existing_personnel_candidate(username, display_name)
    if candidate:
        execute("""INSERT INTO website_member_links(guild_id,discord_user_id,personnel_id)
                   VALUES(%s,%s,%s)
                   ON CONFLICT(guild_id,discord_user_id)
                   DO UPDATE SET personnel_id=EXCLUDED.personnel_id,linked_at=NOW()""",
                (guild_id, discord_user_id, str(candidate["id"])))
        card, field_code = issue_battle_roster_card(candidate["id"])
        weapon = issue_m16(candidate["id"])
        if mos_code and (mos_code != candidate.get("mos_code") or mos_title != candidate.get("duty_position")):
            execute("UPDATE personnel SET mos_code=%s,duty_position=%s,updated_at=NOW() WHERE id=%s",
                    (mos_code,mos_title,candidate["id"]))
            write_service_entry(candidate["id"],"MOS","BATTLEFIELD MOS RECORDED",
                f"Primary battlefield MOS recorded as {mos_code} — {mos_title}.",
                "HEADQUARTERS — BATTALION CLERK")
        # Existing 201 Files remain authoritative. Discord rank is an intake signal only;
        # attaching an existing record must never silently rewrite its historical/current rank.
        candidate=fetch_one("SELECT * FROM personnel WHERE id=%s",(candidate["id"],))
        candidate=_sync_discord_assignment(candidate,roles)
        reconcile_lifecycle(soldier_view(candidate),"HEADQUARTERS — BATTALION CLERK")
        return {"created":False,"linked":True,"attached_existing":True,
                "personnel":fetch_one("SELECT * FROM personnel WHERE id=%s",(candidate["id"],)),
                "roster":card,"field_code":field_code,"weapon":weapon}

    if not create_if_missing:
        return {"created":False,"linked":False}

    first_name,last_name = _discord_personnel_name(username, display_name)
    person = fetch_one(
        """INSERT INTO personnel
           (service_number,first_name,last_name,rank_code,mos_code,duty_position,unit_code,
            date_joined,rvn_arrival_date,field_status,readiness_status,readiness_percent,duty_status,roster_entered_at)
           VALUES(%s,%s,%s,%s,%s,%s,'1-5 CAV',
                  CURRENT_DATE,CURRENT_DATE,'Replacement','PROCESSING',10,'REPLACEMENT — UNASSIGNED',CURRENT_DATE)
           RETURNING *""",
        (allocate_service_number(), first_name, last_name, rank or "", mos_code, mos_title))
    execute("""INSERT INTO promotion_history
               (personnel_id,old_rank_code,new_rank_code,effective_date,authority,remarks)
               VALUES(%s,NULL,%s,CURRENT_DATE,'HEADQUARTERS — BATTALION CLERK','Initial rank recorded when the Soldier was entered on the battalion rolls.')""",
            (person["id"],rank))
    execute("""INSERT INTO assignment_history(personnel_id,unit_code,duty_position,effective_date)
               VALUES(%s,'1-5 CAV',%s,CURRENT_DATE)""",(person["id"],mos_title))
    execute("""INSERT INTO personnel_mos_records(personnel_id,mos_code,mos_title,mos_kind,effective_date,qualified_by,remarks)
               VALUES(%s,%s,%s,'PRIMARY',CURRENT_DATE,'HEADQUARTERS — BATTALION CLERK','Initial battlefield MOS read from Discord role set.')
               ON CONFLICT(personnel_id,mos_code,mos_kind) DO NOTHING""",(person["id"],mos_code,mos_title))
    person=_sync_discord_assignment(person,roles)
    # Organizational assignment is independent of entry grade. A SGT/2LT/etc. with
    # company+platoon+squad roles is an assigned Soldier immediately, not an
    # unassigned Replacement Depot member. Administrative in-processing may still be open.
    if person.get("platoon") and person.get("squad") and person.get("unit_code") not in {None,"","1-5 CAV"}:
        execute("UPDATE personnel SET field_status='Assigned',duty_status='PRESENT FOR DUTY',updated_at=NOW() WHERE id=%s",(person["id"],))
        person=fetch_one("SELECT * FROM personnel WHERE id=%s",(person["id"],))
    execute("""INSERT INTO website_member_links(guild_id,discord_user_id,personnel_id)
               VALUES(%s,%s,%s)
               ON CONFLICT(guild_id,discord_user_id)
               DO UPDATE SET personnel_id=EXCLUDED.personnel_id,linked_at=NOW()""",
            (guild_id,discord_user_id,str(person["id"])))
    card,field_code=issue_battle_roster_card(person["id"])
    weapon=issue_m16(person["id"])
    _assigned_on_entry = bool(person.get("platoon") and person.get("squad") and person.get("unit_code") not in {None,"","1-5 CAV"})
    _arrival_status = f"Assigned to {person.get('unit_code')} / {person.get('platoon')} / {person.get('squad')}." if _assigned_on_entry else "Awaiting organizational assignment."
    write_service_entry(person["id"],"ARRIVAL","PERSONNEL RECORD OPENED",
        f"Entered on the battalion personnel roster at initial grade {rank}. {_arrival_status}",
        "HEADQUARTERS — BATTALION CLERK")
    write_service_entry(person["id"],"ADMIN","BATTLE ROSTER CARD ISSUED",
        f"Battle Roster Card {card['roster_number']} issued for battalion identification and record access.",
        "HEADQUARTERS — BATTALION CLERK")
    write_service_entry(person["id"],"MOS","BATTLEFIELD MOS RECORDED",
        f"Primary battlefield MOS recorded as {mos_code} — {mos_title}.",
        "HEADQUARTERS — BATTALION CLERK")
    if weapon:
        write_service_entry(person["id"],"EQUIPMENT","INDIVIDUAL WEAPON ISSUED",
            f"U.S. Rifle, 5.56-MM, M16, Serial No. {weapon['serial_number']}, Rack No. {weapon['rack_number']}.",
            "S-4 SUPPLY")
    open_personnel_action(person["id"],"PERSONNEL","Initial S-1 onboarding required","S-1","HIGH","BATTALION CLERK",source_key=f"S1-ONBOARD:{person['id']}")
    if rank == "PVT":
        open_personnel_action(person["id"],"TRAINING","Replacement Training opened","S-3","ROUTINE","BATTALION CLERK",source_key=f"REPLACEMENT-TRAINING:{person['id']}")
    else:
        open_personnel_action(person["id"],"PERSONNEL",f"Initial battalion in-processing — entry grade {rank}","S-1","ROUTINE","BATTALION CLERK",source_key=f"INITIAL-INPROCESS:{person['id']}")
    person=reconcile_lifecycle(soldier_view(fetch_one("SELECT * FROM personnel WHERE id=%s",(person["id"],))),"HEADQUARTERS — BATTALION CLERK") or person
    initial_order = replacement_orders_for(person["id"])
    staff_log("S-1","NEW SOLDIER",f"{rank} {person.get('last_name','')} entered on battalion rolls", "BATTALION CLERK",person["id"],initial_order.get("document_number") if initial_order else None,{"mos":mos_code})
    battalion_history_entry("ARRIVAL",f"{rank} {person.get('last_name','')} entered battalion rolls",f"Primary MOS {mos_code} — {mos_title}.",person["id"],reference_number=initial_order.get("document_number") if initial_order else None)
    return {"created":True,"linked":True,"personnel":person,"roster":card,
            "field_code":field_code,"weapon":weapon,"initial_order":initial_order,"reason":reason}

@app.post("/internal/clerk/personnel/sync")
def clerk_personnel_sync():
    if not _clerk_authorized():
        return {"ok":False,"error":"authorization required"},401
    data=request.get_json(silent=True) or {}
    try:
        guild_id=int(data.get("guild_id"))
        discord_user_id=int(data.get("discord_user_id") or data.get("member_id"))
    except (TypeError,ValueError):
        return {"ok":False,"error":"guild_id and discord_user_id required"},400
    result=_ensure_clerk_personnel(
        guild_id,discord_user_id,str(data.get("username") or ""),
        str(data.get("display_name") or data.get("username") or ""),
        data.get("roles") or [],
        create_if_missing=bool(data.get("create_if_missing")),
        reason=str(data.get("reason") or "identity_sync"))
    person=result.get("personnel") or {}
    card=result.get("roster") or {}
    weapon=result.get("weapon") or {}
    discord_appointment_roles = []
    if person:
        appointment_role_map = {
            "PSG": "Platoon Sergeant",
            "SL": "Squad Leader",
            "ASST_SL": "Assistant Squad Leader",
            "FTL": "Team Leader",
        }
        current_appointment_rows = fetch_all(
            """SELECT appointment_code FROM personnel_appointments
               WHERE personnel_id=%s AND is_current=TRUE
               ORDER BY effective_date,created_at""",
            (person.get("id"),),
        )
        discord_appointment_roles = [
            appointment_role_map[row["appointment_code"]]
            for row in current_appointment_rows
            if row.get("appointment_code") in appointment_role_map
        ]
    return {"ok":True,"created":bool(result.get("created")),"linked":bool(result.get("linked")),
            "attached_existing":bool(result.get("attached_existing")),
            "personnel_id":str(person.get("id")) if person else None,
            "rank_code":person.get("rank_code") if person else None,
            "mos_code":person.get("mos_code") if person else None,
            "mos_title":person.get("duty_position") if person else None,
            "unit_code":person.get("unit_code") if person else None,
            "platoon":person.get("platoon") if person else None,
            "squad":person.get("squad") if person else None,
            "appointment_roles":discord_appointment_roles,
            "role_drift":result.get("role_drift") or [],
            "roster_number":card.get("roster_number") if card else None,
            "field_code":result.get("field_code"),
            "weapon_serial":weapon.get("serial_number") if weapon else None,
            "initial_order_id":str((result.get("initial_order") or {}).get("id")) if result.get("initial_order") else None}


@app.post("/internal/clerk/personnel/reissue-login")
def clerk_reissue_login():
    if not _clerk_authorized(): return {"ok":False,"error":"authorization required"},401
    data=request.get_json(silent=True) or {}
    try:
        guild_id=int(data.get("guild_id")); discord_user_id=int(data.get("discord_user_id") or data.get("member_id"))
    except (TypeError,ValueError): return {"ok":False,"error":"guild_id and discord_user_id required"},400
    link=fetch_one("SELECT personnel_id FROM website_member_links WHERE guild_id=%s AND discord_user_id=%s",(guild_id,discord_user_id))
    if not link: return {"ok":False,"error":"member is not linked to a personnel record"},404
    p=fetch_one("SELECT * FROM personnel WHERE id=%s",(str(link["personnel_id"]),))
    if not p: return {"ok":False,"error":"personnel record not found"},404
    card=fetch_one("SELECT * FROM battle_roster_cards WHERE personnel_id=%s",(p["id"],))
    field_code=_random_field_code()
    if card:
        execute("UPDATE battle_roster_cards SET field_code_hash=%s,is_active=TRUE,replaced_at=NOW() WHERE id=%s",(generate_password_hash(field_code),card["id"]))
        card=fetch_one("SELECT * FROM battle_roster_cards WHERE id=%s",(card["id"],))
    else:
        card,_unused=issue_battle_roster_card(p["id"])
        execute("UPDATE battle_roster_cards SET field_code_hash=%s WHERE id=%s",(generate_password_hash(field_code),card["id"]))
    write_service_entry(p["id"],"ADMIN","SOLDIER RECORD ACCESS REISSUED","Private Field Code reissued by Headquarters. Battle Roster Number retained.","HEADQUARTERS — BATTALION CLERK")
    staff_log("S-1","LOGIN REISSUE",f"Soldier Record access reissued for {p.get('rank_code','')} {p.get('last_name','')}","BATTALION CLERK",p["id"])
    return {"ok":True,"personnel_id":str(p["id"]),"rank_code":p.get("rank_code"),"first_name":p.get("first_name"),"last_name":p.get("last_name"),"roster_number":card.get("roster_number"),"field_code":field_code}

@app.post("/internal/clerk/personnel/departure")
def clerk_personnel_departure():
    if not _clerk_authorized(): return {"ok":False,"error":"authorization required"},401
    data=request.get_json(silent=True) or {}
    try:
        guild_id=int(data.get("guild_id")); discord_user_id=int(data.get("discord_user_id") or data.get("member_id"))
    except (TypeError,ValueError): return {"ok":False,"error":"guild_id and discord_user_id required"},400
    link=fetch_one("SELECT personnel_id FROM website_member_links WHERE guild_id=%s AND discord_user_id=%s",(guild_id,discord_user_id))
    if not link: return {"ok":True,"linked":False}
    p=fetch_one("SELECT * FROM personnel WHERE id=%s",(str(link["personnel_id"]),))
    if not p: return {"ok":True,"linked":False}
    execute("UPDATE personnel SET duty_status='INACTIVE',lifecycle_state='IN PROCESSING',updated_at=NOW() WHERE id=%s",(p["id"],))
    open_personnel_action(p["id"],"PERSONNEL","Discord departure — S-1 disposition required","S-1","HIGH","BATTALION CLERK",{"reason":data.get("reason") or "member_left"},source_key=f"DISCORD-DEPART:{p['id']}:{date.today()}")
    write_service_entry(p["id"],"ADMIN","COMMUNICATIONS ROSTER DEPARTURE","Battalion Clerk detected departure from the Discord server. S-1 review required before archive or separation.","HEADQUARTERS — BATTALION CLERK")
    return {"ok":True,"linked":True,"personnel_id":str(p["id"])}


@app.post("/internal/clerk/personnel/reset")
def clerk_personnel_reset():
    """Clear personnel-derived records and return issued property to stock.

    This deliberately preserves:
    - site administrator/staff accounts
    - unit organization/catalogs
    - operation definitions
    - rank/appointment/qualification catalogs
    - Battalion Clerk duty-channel assignments
    - weapon inventory serial numbers

    It removes the current personnel roster so rank-role holders can be rebuilt cleanly.
    """
    if not _clerk_authorized():
        return {"ok": False, "error": "authorization required"}, 401

    data = request.get_json(silent=True) or {}
    if str(data.get("confirmation") or "").strip().upper() != "RESET ROSTER":
        return {"ok": False, "error": "confirmation must be RESET ROSTER"}, 400

    before = fetch_one("SELECT COUNT(*)::int AS count FROM personnel") or {"count": 0}

    # Clear non-cascading personnel references first.
    execute("UPDATE supply_requisitions SET requested_by_personnel_id=NULL WHERE requested_by_personnel_id IS NOT NULL")
    execute("""UPDATE equipment_issue_history
              SET personnel_id=NULL, is_current=FALSE,
                  returned_at=COALESCE(returned_at,NOW()),
                  condition_at_return=COALESCE(condition_at_return,condition_at_issue)
              WHERE personnel_id IS NOT NULL""")
    execute("UPDATE weapon_maintenance_log SET personnel_id=NULL WHERE personnel_id IS NOT NULL")
    execute("UPDATE weapon_round_events SET personnel_id=NULL WHERE personnel_id IS NOT NULL")
    execute("DELETE FROM website_member_links")

    # personnel has ON DELETE CASCADE relationships for 201 Files, roster cards,
    # assignments, promotions, appointments, awards, qualifications, attendance,
    # weapon issue history, service history, etc.
    execute("DELETE FROM personnel")

    # Return persistent inventories to a clean issue state while retaining serial numbers.
    execute("""UPDATE weapon_inventory
              SET status='AVAILABLE FOR ISSUE',
                  condition_state='SERVICEABLE',
                  condition_percent=100,
                  rounds_since_cleaning=0,
                  last_cleaned_at=NOW(),
                  last_inspected_at=NOW(),
                  maintenance_notes=NULL,
                  updated_at=NOW()""")
    execute("""UPDATE equipment_inventory
              SET status='AVAILABLE',
                  condition_state='SERVICEABLE',
                  condition_percent=100,
                  updated_at=NOW()
              WHERE status <> 'AVAILABLE' OR condition_state <> 'SERVICEABLE' OR condition_percent <> 100""")

    return {
        "ok": True,
        "cleared_personnel": int(before.get("count") or 0),
        "message": "Battalion personnel roster cleared. Rank-role holders may now be rebuilt.",
    }


@app.post("/internal/clerk/events")
def clerk_file_event():
    """Receive a scheduled Training, Operation, or Meeting duty window."""
    if not _clerk_authorized():
        return {"ok": False, "error": "authorization required"}, 401
    data = request.get_json(silent=True) or {}
    event_type = str(data.get("event_type", "")).upper().strip()
    if event_type not in {"TRAINING", "OPERATION", "MEETING"}:
        return {"ok": False, "error": "invalid event_type"}, 400
    starts_at, ends_at = _parse_iso(data.get("starts_at")), _parse_iso(data.get("ends_at"))
    if not starts_at or not ends_at or ends_at <= starts_at:
        return {"ok": False, "error": "valid starts_at and ends_at required"}, 400
    channel_name = str(data.get("channel_name") or event_type.title()).strip()
    if channel_name.upper() != event_type:
        return {"ok": False, "error": "channel must be Training, Operation, or Meeting"}, 400
    external = str(data.get("external_event_id") or "").strip() or None
    operation_id = data.get("operation_id") or None
    rounds_per_soldier = max(0, min(1000, int(data.get("rounds_per_soldier") or 0)))
    credit_threshold_minutes=max(5,min(720,int(data.get("credit_threshold_minutes") or 45)))
    reminder_minutes=str(data.get("reminder_minutes") or "1440,120,30")
    # Deployment-safe migration for existing databases.
    execute("ALTER TABLE battalion_events ADD COLUMN IF NOT EXISTS rounds_per_soldier INTEGER NOT NULL DEFAULT 0")
    if external:
        event = fetch_one("""INSERT INTO battalion_events
            (external_event_id,event_type,title,starts_at,ends_at,channel_name,channel_id,operation_id,rounds_per_soldier,credit_threshold_minutes,reminder_minutes,status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'SCHEDULED')
            ON CONFLICT(external_event_id) DO UPDATE SET event_type=EXCLUDED.event_type,title=EXCLUDED.title,
              starts_at=EXCLUDED.starts_at,ends_at=EXCLUDED.ends_at,channel_name=EXCLUDED.channel_name,
              channel_id=EXCLUDED.channel_id,operation_id=EXCLUDED.operation_id,rounds_per_soldier=EXCLUDED.rounds_per_soldier,credit_threshold_minutes=EXCLUDED.credit_threshold_minutes,reminder_minutes=EXCLUDED.reminder_minutes,status='SCHEDULED'
            RETURNING *""", (external,event_type,data.get("title") or event_type,starts_at,ends_at,
                               channel_name,data.get("channel_id"),operation_id,rounds_per_soldier,credit_threshold_minutes,reminder_minutes))
    else:
        event = fetch_one("""INSERT INTO battalion_events
            (event_type,title,starts_at,ends_at,channel_name,channel_id,operation_id,rounds_per_soldier,credit_threshold_minutes,reminder_minutes,status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'SCHEDULED') RETURNING *""",
            (event_type,data.get("title") or event_type,starts_at,ends_at,channel_name,data.get("channel_id"),operation_id,rounds_per_soldier,credit_threshold_minutes,reminder_minutes))
    instructors=[]
    if event_type == 'TRAINING' and ('instructor_discord_user_ids' in data or 'instructor_personnel_ids' in data):
        execute("DELETE FROM battalion_event_instructors WHERE event_id=%s", (event['id'],))
        for pid in data.get('instructor_personnel_ids') or []:
            person=fetch_one("SELECT id FROM personnel WHERE id=%s AND separated_at IS NULL", (pid,))
            if person: instructors.append(str(person['id']))
        guild_id=data.get('guild_id')
        for uid in data.get('instructor_discord_user_ids') or []:
            link=fetch_one("""SELECT p.id FROM website_member_links w JOIN personnel p ON p.id::text=w.personnel_id
                              WHERE w.discord_user_id=%s AND (%s IS NULL OR w.guild_id=%s) AND p.separated_at IS NULL LIMIT 1""",
                           (uid,guild_id,guild_id))
            if link: instructors.append(str(link['id']))
        roles=data.get('instructor_roles') or {}
        for pid in dict.fromkeys(instructors):
            execute("""INSERT INTO battalion_event_instructors(event_id,personnel_id,instructor_role) VALUES(%s,%s,%s)
                       ON CONFLICT(event_id,personnel_id) DO UPDATE SET instructor_role=EXCLUDED.instructor_role""",
                    (event['id'],pid,roles.get(pid) or 'INSTRUCTOR'))
    return {"ok": True, "event_id": str(event["id"]), "credit_threshold_minutes": credit_threshold_minutes, "instructors_filed": len(set(instructors))}




@app.route("/internal/clerk/channels", methods=["GET", "POST", "DELETE"])
def clerk_channels():
    """Persist permanent Training / Operation / Meeting voice-channel assignments."""
    if not _clerk_authorized():
        return {"ok": False, "error": "authorization required"}, 401
    data = request.get_json(silent=True) or {}
    try:
        guild_id = int(data.get("guild_id") or request.args.get("guild_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "guild_id required"}, 400
    if request.method == "GET":
        rows = fetch_all("SELECT event_type,channel_id,channel_name,updated_at FROM clerk_duty_channels WHERE guild_id=%s ORDER BY event_type", (guild_id,))
        return {"ok": True, "channels": rows}
    event_type = str(data.get("event_type") or "").upper().strip()
    if event_type not in {"TRAINING", "OPERATION", "MEETING"}:
        return {"ok": False, "error": "invalid event_type"}, 400
    if request.method == "DELETE":
        execute("DELETE FROM clerk_duty_channels WHERE guild_id=%s AND event_type=%s", (guild_id,event_type))
        return {"ok": True}
    try:
        channel_id = int(data.get("channel_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "channel_id required"}, 400
    channel_name = str(data.get("channel_name") or event_type.title()).strip()
    execute("""INSERT INTO clerk_duty_channels(guild_id,event_type,channel_id,channel_name) VALUES(%s,%s,%s,%s)
               ON CONFLICT(guild_id,event_type) DO UPDATE SET channel_id=EXCLUDED.channel_id,channel_name=EXCLUDED.channel_name,updated_at=NOW()""",
            (guild_id,event_type,channel_id,channel_name))
    return {"ok": True, "event_type": event_type, "channel_id": channel_id, "channel_name": channel_name}


@app.get("/internal/clerk/events/status")
def clerk_event_status():
    if not _clerk_authorized():
        return {"ok": False, "error": "authorization required"}, 401
    try:
        guild_id = int(request.args.get("guild_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "guild_id required"}, 400
    # guild is represented by configured channel binding; events themselves are global to this battalion site.
    rows = fetch_all("""SELECT e.*, COUNT(a.id) AS tracked_count,
                     COALESCE(SUM(CASE WHEN a.credited_at IS NOT NULL THEN 1 ELSE 0 END),0) AS qualified_count
                     FROM battalion_events e LEFT JOIN battalion_event_attendance a ON a.event_id=e.id
                     WHERE e.status IN ('SCHEDULED','ACTIVE') AND e.ends_at > NOW() - INTERVAL '12 hours'
                     GROUP BY e.id ORDER BY e.starts_at""")
    for event in rows:
        event["attendance"] = fetch_all("""SELECT p.rank_code,p.first_name,p.last_name,a.qualifying_seconds,a.credited_at,a.last_seen_at
            FROM battalion_event_attendance a JOIN personnel p ON p.id=a.personnel_id
            WHERE a.event_id=%s ORDER BY a.qualifying_seconds DESC,p.last_name""", (event["id"],))
    return {"ok": True, "events": rows, "credit_threshold_minutes": 45}


@app.post("/internal/clerk/events/<event_id>/close")
def clerk_close_event(event_id):
    if not _clerk_authorized():
        return {"ok":False,"error":"authorization required"},401
    event=fetch_one("SELECT * FROM battalion_events WHERE id=%s",(event_id,))
    if not event:
        return {"ok":False,"error":"event not found"},404
    summary=finalize_operation_event(event_id,"BATTALION CLERK")
    # retain AAR suspense behavior
    if str(event.get("event_type") or "").upper()=="OPERATION":
        source=f"AAR:{event_id}"
        existing=fetch_one("SELECT id FROM personnel_actions WHERE source_key=%s",(source,))
        if not existing:
            execute("""INSERT INTO personnel_actions(personnel_id,action_type,subject,owning_section,status,priority,initiated_by,details_json,source_key)
                       VALUES(NULL,'OPERATIONS',%s,'S-3','OPEN','HIGH','BATTALION CLERK',%s::jsonb,%s)""",
                    (f"After Action Report — {event.get('title')}",json.dumps({'event_id':str(event_id),'operation_id':str(event.get('operation_id')) if event.get('operation_id') else None,'tracked':summary['tracked']}),source))
        summary['aar_task_opened']=True
    return {"ok":True,"summary":summary}


@app.get("/internal/clerk/automation/promotion-eligibility")
def clerk_promotion_eligibility():
    """Return newly recommendable Soldiers plus the closest linked NCO in their formation."""
    if not _clerk_authorized():
        return {"ok": False, "error": "authorization required"}, 401
    try:
        guild_id=int(request.args.get('guild_id'))
    except (TypeError,ValueError):
        return {"ok":False,"error":"guild_id required"},400
    people=fetch_all("""SELECT * FROM personnel
                        WHERE COALESCE(lifecycle_state,'') NOT IN ('SEPARATED','ARCHIVED')
                        ORDER BY unit_code,platoon,squad,last_name,first_name""")
    rank_priority={'CPL':1,'SGT':2,'SSG':3,'SFC':4,'MSG':5,'1SG':6,'SGM':7}
    linked={str(r['personnel_id']):str(r['discord_user_id']) for r in fetch_all("SELECT personnel_id,discord_user_id FROM website_member_links WHERE guild_id=%s",(guild_id,))}
    eligible=[]
    for p in people:
        paths=promotion_eligibility(soldier_view(p))
        for path in paths:
            # The NCO notification is meant to trigger BEFORE a recommendation exists.
            nonrec=[x for x in path.get('requirements',[]) if x.get('kind')!='recommendation']
            recommendable=bool(nonrec) and all(x.get('complete') for x in nonrec) and not path.get('recommended')
            if not recommendable:
                continue
            candidates=[]
            for n in people:
                rp=rank_priority.get(str(n.get('rank_code') or '').upper())
                if not rp or str(n.get('id'))==str(p.get('id')) or str(n.get('id')) not in linked:
                    continue
                score=0
                if n.get('unit_code')==p.get('unit_code'): score+=100
                if n.get('platoon') and n.get('platoon')==p.get('platoon'): score+=50
                if n.get('squad') and n.get('squad')==p.get('squad'): score+=75
                score += rp
                candidates.append((score,rp,n))
            candidates.sort(key=lambda x:(x[0],x[1]), reverse=True)
            leader=candidates[0][2] if candidates else None
            eligible.append({'personnel_id':str(p['id']),'rank_code':p.get('rank_code'),'first_name':p.get('first_name'),'last_name':p.get('last_name'),'target_rank':path.get('target'),'leader_discord_user_id':linked.get(str(leader['id'])) if leader else None})
    return {'ok':True,'eligible':eligible}



@app.post("/internal/clerk/weapons/refresh-inactivity")
def clerk_refresh_inactivity_weapons():
    if not _clerk_authorized():
        return {"ok":False,"error":"authorization required"},401
    rows=fetch_all("SELECT DISTINCT weapon_id FROM weapon_issue_history WHERE is_current=TRUE")
    changed=[]
    for row in rows:
        before=fetch_one("SELECT condition_state,condition_percent FROM weapon_inventory WHERE id=%s",(row["weapon_id"],)) or {}
        after=refresh_weapon_condition(row["weapon_id"]) or {}
        if before.get("condition_state")!=after.get("condition_state") or int(before.get("condition_percent") or 0)!=int(after.get("condition_percent") or 0):
            changed.append({"weapon_id":str(row["weapon_id"]),"state":after.get("condition_state"),"percent":after.get("condition_percent")})
    return {"ok":True,"issued_weapons":len(rows),"changed":changed}


@app.post("/internal/clerk/attendance")
def clerk_file_attendance():
    """Receive a voice interval; only overlap with a scheduled matching duty counts."""
    if not _clerk_authorized():
        return {"ok": False, "error": "authorization required"}, 401
    data = request.get_json(silent=True) or {}
    try:
        guild_id = int(data.get("guild_id"))
        member_id = int(data.get("member_id") or data.get("discord_user_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "guild_id and member_id required"}, 400
    channel_name = str(data.get("channel_name") or "").upper().strip()
    if channel_name not in {"TRAINING", "OPERATION", "MEETING"}:
        return {"ok": True, "credited": False, "reason": "non-duty channel ignored"}
    joined_at, left_at = _parse_iso(data.get("joined_at")), _parse_iso(data.get("left_at"))
    if not joined_at or not left_at or left_at <= joined_at:
        return {"ok": False, "error": "valid joined_at and left_at required"}, 400
    link = fetch_one("SELECT personnel_id FROM website_member_links WHERE guild_id=%s AND discord_user_id=%s",
                     (guild_id, member_id))
    if not link:
        return {"ok":True,"credited":False,"reason":"no 201 File on battalion roster"}
    try:
        personnel_id = str(link["personnel_id"])
    except Exception:
        return {"ok": False, "error": "invalid personnel link"}, 409
    events = fetch_all("""SELECT * FROM battalion_events
        WHERE event_type=%s AND status IN ('SCHEDULED','ACTIVE')
          AND starts_at < %s AND ends_at > %s
          AND (channel_id IS NULL OR channel_id=%s)
        ORDER BY starts_at""",
        (channel_name,left_at,joined_at,data.get("channel_id")))
    results=[]
    base_segment=str(data.get("session_id") or data.get("segment_id") or "").strip()
    for event in events:
        overlap_start=max(joined_at,event["starts_at"])
        overlap_end=min(left_at,event["ends_at"])
        seconds=max(0,int((overlap_end-overlap_start).total_seconds()))
        if seconds:
            segment_id = f"{base_segment}:{event['id']}" if base_segment else None
            if segment_id:
                prior=fetch_one("SELECT qualifying_seconds FROM battalion_attendance_segments WHERE segment_id=%s", (segment_id,))
                if prior:
                    current=fetch_one("SELECT qualifying_seconds,credited_at FROM battalion_event_attendance WHERE event_id=%s AND personnel_id=%s", (event["id"],personnel_id)) or {}
                    results.append({"event_id":str(event["id"]),"qualifying_seconds":int(current.get("qualifying_seconds") or 0),"credited_now":False,"duplicate_segment":True})
                    continue
            credited,total=_credit_scheduled_duty(event,personnel_id,seconds,segment_id or base_segment)
            if segment_id:
                execute("""INSERT INTO battalion_attendance_segments(segment_id,guild_id,discord_user_id,event_id,personnel_id,qualifying_seconds)
                           VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(segment_id) DO NOTHING""",
                        (segment_id,guild_id,member_id,event["id"],personnel_id,seconds))
            results.append({"event_id":str(event["id"]),"qualifying_seconds":total,"credited_now":credited})
    return {"ok": True, "credited": any(r["credited_now"] for r in results), "events": results}


# ---------------------------------------------------------------------------
# DEEP BATTALION IMMERSION / FLOW PACK
# ---------------------------------------------------------------------------

MOS_PROFICIENCY_ORDER = {"QUALIFIED":1,"EXPERIENCED":2,"SENIOR":3,"INSTRUCTOR":4}


def canonical_personnel_snapshot(personnel_id):
    """Return active Soldier state from the authoritative personnel row.
    History, documents and Discord are evidence/synchronization sources only.
    """
    p = fetch_one("SELECT * FROM personnel WHERE id=%s", (personnel_id,))
    if not p:
        return None
    # Mirror the active primary MOS into the derived MOS ledger, never the reverse.
    if p.get("mos_code"):
        title_row = fetch_one("SELECT mos_title FROM battalion_mos_catalog WHERE mos_code=%s", (p["mos_code"],))
        execute("""INSERT INTO personnel_mos_records(personnel_id,mos_code,mos_title,mos_kind,status,effective_date,remarks)
                   VALUES(%s,%s,%s,'PRIMARY','CURRENT',%s,'Mirrored from authoritative personnel record.')
                   ON CONFLICT(personnel_id,mos_code,mos_kind) DO UPDATE SET status='CURRENT',mos_title=EXCLUDED.mos_title""",
                (personnel_id,p["mos_code"],(title_row or {}).get("mos_title") or p.get("duty_position") or p["mos_code"],p.get("date_joined") or date.today()))
    return p


def soldier_current_story(person):
    p = canonical_personnel_snapshot(person["id"]) or person
    elig = promotion_eligibility(soldier_view(p))
    next_promo = next((x for x in elig if x.get("target")), None)
    rt = replacement_training_status(soldier_view(p))
    inspection = weapon_inspection_status(p["id"])
    current_op = fetch_one("""SELECT o.*,oda.duty_role FROM operation_duty_assignments oda
                              JOIN operations o ON o.id=oda.operation_id
                              WHERE oda.personnel_id=%s AND o.lifecycle_status IN ('PLANNING','PUBLISHED','ACTIVE')
                              ORDER BY COALESCE(o.start_at,o.created_at) LIMIT 1""", (p["id"],))
    if not rt.get("complete"):
        objective = f"Complete {rt.get('program_title') or 'initial processing'}"
    elif current_op:
        objective = f"Prepare for {current_op.get('operation_number') or current_op.get('title')} — {current_op.get('duty_role')}"
    elif next_promo:
        objective = f"Progress toward {next_promo.get('target')}"
    else:
        objective = "Maintain battalion readiness"
    next_action = soldier_next_step(soldier_view(p))
    if inspection and inspection.get("overdue"):
        next_action = f"M16 inspection overdue — report to S-4"
    return {"status":p.get("duty_status") or "PRESENT FOR DUTY","assignment":f"{p.get('unit_code') or ''}{' • '+p.get('platoon') if p.get('platoon') else ''}{' • '+p.get('squad') if p.get('squad') else ''}","objective":objective,"next_action":next_action,"current_operation":current_op,"next_promotion":next_promo}


def billet_strength_rows():
    billets = fetch_all("SELECT * FROM unit_billets WHERE is_active=TRUE ORDER BY unit_code,sort_order,billet_title")
    rows=[]
    for b in billets:
        filled=fetch_one("""SELECT COUNT(*) total FROM personnel WHERE separated_at IS NULL AND unit_code=%s
                            AND (UPPER(COALESCE(duty_position,''))=UPPER(%s) OR UPPER(COALESCE(mos_code,''))=UPPER(COALESCE(%s,'')))""",
                         (b["unit_code"],b["billet_title"],b.get("preferred_mos_code"))) or {"total":0}
        f=int(filled.get("total") or 0); auth=int(b.get("authorized_strength") or 0)
        rows.append({**b,"filled":f,"vacant":max(auth-f,0),"fill_percent":round((f/auth)*100) if auth else 100})
    return rows


def mos_recruiting_needs():
    rows=fetch_all("""SELECT mc.mos_code,mc.mos_title,mc.category,
                      COALESCE(SUM(ub.authorized_strength),0) authorized,
                      COALESCE((SELECT COUNT(*) FROM personnel p WHERE p.separated_at IS NULL AND p.mos_code=mc.mos_code),0) filled
                      FROM battalion_mos_catalog mc LEFT JOIN unit_billets ub ON ub.preferred_mos_code=mc.mos_code AND ub.is_active=TRUE
                      WHERE mc.is_active=TRUE GROUP BY mc.mos_code,mc.mos_title,mc.category,mc.sort_order
                      ORDER BY (COALESCE(SUM(ub.authorized_strength),0)-COALESCE((SELECT COUNT(*) FROM personnel p WHERE p.separated_at IS NULL AND p.mos_code=mc.mos_code),0)) DESC,mc.sort_order""")
    for r in rows:
        r["needed"]=max(int(r.get("authorized") or 0)-int(r.get("filled") or 0),0)
    return [r for r in rows if r["needed"]>0][:8]


def staff_workload(section):
    counts=section_action_counts(section)
    overdue=fetch_all("""SELECT pa.*,p.rank_code,p.last_name,p.first_name,(CURRENT_DATE-pa.due_date) AS days_overdue FROM personnel_actions pa
                          LEFT JOIN personnel p ON p.id=pa.personnel_id
                          WHERE pa.owning_section=%s AND pa.status NOT IN ('CLOSED','COMPLETE','DENIED')
                          AND pa.due_date IS NOT NULL AND pa.due_date<CURRENT_DATE ORDER BY pa.due_date""",(section,))
    for a in overdue:
        days=int(a.get("days_overdue") or 0)
        escalated="CRITICAL" if days>=5 else ("HIGH" if days>=2 else a.get("priority") or "ROUTINE")
        if escalated != a.get("priority"):
            execute("UPDATE personnel_actions SET priority=%s,updated_at=NOW() WHERE id=%s",(escalated,a["id"]))
            a["priority"]=escalated
    return {"counts":counts,"overdue":overdue,"overdue_count":len(overdue)}


def operation_readiness(operation_id):
    op=fetch_one("SELECT * FROM operations WHERE id=%s",(operation_id,))
    assignments=fetch_all("""SELECT oda.*,p.rank_code,p.last_name,p.first_name,p.mos_code,p.duty_status,wi.condition_state
                             FROM operation_duty_assignments oda JOIN personnel p ON p.id=oda.personnel_id
                             LEFT JOIN weapon_issue_history wih ON wih.personnel_id=p.id AND wih.is_current=TRUE
                             LEFT JOIN weapon_inventory wi ON wi.id=wih.weapon_id WHERE oda.operation_id=%s""",(operation_id,))
    total=len(assignments)
    present=sum(1 for x in assignments if str(x.get("duty_status") or "").upper() in {"PRESENT FOR DUTY","FIELD DUTY","TRAINING","ATTACHED","TEMPORARY DUTY"})
    svc=sum(1 for x in assignments if str(x.get("condition_state") or "").upper() in {"SERVICEABLE","CLEAN","FIELD WORN"})
    qualified=sum(1 for x in assignments if not x.get("mos_code") or x.get("mos_code")==x.get("mos_code"))
    pct=round(((present/total)*50 + (svc/total)*30 + (qualified/total)*20)) if total else 0
    return {"operation":op,"assignments":assignments,"assigned":total,"present":present,"serviceable_weapons":svc,"qualified_roles":qualified,"total_roles":total,"percent":pct}


def add_tour_book_entry(personnel_id,entry_type,title,narrative=None,operation_id=None,document_id=None,source_key=None):
    if source_key:
        prior=fetch_one("SELECT * FROM soldier_tour_book WHERE source_key=%s",(source_key,))
        if prior: return prior
    return fetch_one("""INSERT INTO soldier_tour_book(personnel_id,entry_type,title,narrative,operation_id,document_id,source_key)
                        VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *""",(personnel_id,entry_type,title,narrative,operation_id,document_id,source_key))




def update_unit_readiness_streaks(operation_id):
    r=operation_readiness(operation_id)
    if not r.get("assignments"):
        return
    by_unit={}
    for a in r["assignments"]:
        by_unit.setdefault(a.get("unit_code") or "BATTALION",[]).append(a)
    for unit,rows in by_unit.items():
        total=len(rows); present=sum(1 for x in rows if str(x.get("duty_status") or "").upper() in {"PRESENT FOR DUTY","FIELD DUTY","TRAINING","ATTACHED","TEMPORARY DUTY"})
        ready=round((present/total)*100) if total else 0
        current=fetch_one("SELECT * FROM unit_readiness_streaks WHERE unit_code=%s",(unit,)) or {}
        streak=int(current.get("current_streak") or 0)+1 if ready>=85 else 0
        best=max(int(current.get("best_streak") or 0),streak)
        execute("""INSERT INTO unit_readiness_streaks(unit_code,current_streak,best_streak,last_qualified_operation_id,last_updated_at)
                   VALUES(%s,%s,%s,%s,NOW()) ON CONFLICT(unit_code) DO UPDATE SET current_streak=EXCLUDED.current_streak,best_streak=EXCLUDED.best_streak,last_qualified_operation_id=EXCLUDED.last_qualified_operation_id,last_updated_at=NOW()""",(unit,streak,best,operation_id if ready>=85 else current.get("last_qualified_operation_id")))

@app.get('/tour-book')
@login_required
def my_tour_book():
    p=linked_personnel()
    if not p: abort(403)
    entries=fetch_all("SELECT * FROM soldier_tour_book WHERE personnel_id=%s ORDER BY entry_date DESC,created_at DESC",(p["id"],))
    if not entries:
        # Seed from permanent service history without modifying the authoritative state.
        history=fetch_all("SELECT * FROM personnel_service_history WHERE personnel_id=%s ORDER BY entry_date,created_at",(p["id"],))
        for h in history:
            add_tour_book_entry(p["id"],h.get("entry_type") or "SERVICE",h.get("title") or "Service Entry",h.get("narrative"),source_key=f"HIST:{h['id']}")
        entries=fetch_all("SELECT * FROM soldier_tour_book WHERE personnel_id=%s ORDER BY entry_date DESC,created_at DESC",(p["id"],))
    return render_template('tour_book.html',personnel=soldier_view(p),entries=entries,current_story=soldier_current_story(p))


@app.get('/staff-workload/<section>')
@login_required
def staff_workload_page(section):
    section=section.upper()
    role=session.get('access_role')
    allowed={'S-1':{'s1'},'S-3':{'s3','training'},'S-4':{'s4'},'HQ':{'battalion_hq'}}
    if role not in {'battalion_hq','commander','admin'} and role not in allowed.get(section,set()): abort(403)
    return render_template('staff_workload.html',section=section,workload=staff_workload(section))


@app.route('/s3/mos-proficiency',methods=['POST'])
@login_required
@role_required('s3')
def mos_proficiency_action():
    pid=request.form.get('personnel_id'); code=(request.form.get('mos_code') or '').upper(); level=(request.form.get('proficiency_level') or 'QUALIFIED').upper()
    if level not in MOS_PROFICIENCY_ORDER: abort(400)
    authority=session.get('display_name') or session.get('username') or 'S-3'
    execute("""INSERT INTO personnel_mos_proficiency(personnel_id,mos_code,proficiency_level,proficiency_order,effective_date,certified_by,remarks)
               VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(personnel_id,mos_code) DO UPDATE SET proficiency_level=EXCLUDED.proficiency_level,
               proficiency_order=EXCLUDED.proficiency_order,effective_date=EXCLUDED.effective_date,certified_by=EXCLUDED.certified_by,remarks=EXCLUDED.remarks,is_current=TRUE""",
            (pid,code,level,MOS_PROFICIENCY_ORDER[level],request.form.get('effective_date') or date.today(),authority,request.form.get('remarks')))
    write_service_entry(pid,'TRAINING',f'{code} PROFICIENCY — {level}',f'Battlefield MOS proficiency certified at {level}.',authority)
    flash('MOS PROFICIENCY FILED.','success'); return redirect(request.referrer or url_for('s3'))


@app.route('/s3/instructor',methods=['POST'])
@login_required
@role_required('s3')
def instructor_qualification_action():
    pid=request.form.get('personnel_id'); area=(request.form.get('qualification_area') or '').strip().upper(); authority=session.get('display_name') or session.get('username') or 'S-3'
    if not area: abort(400)
    execute("""INSERT INTO instructor_qualifications(personnel_id,qualification_area,effective_date,expires_at,certified_by,status,remarks)
               VALUES(%s,%s,%s,%s,%s,'CURRENT',%s) ON CONFLICT(personnel_id,qualification_area) DO UPDATE SET effective_date=EXCLUDED.effective_date,
               expires_at=EXCLUDED.expires_at,certified_by=EXCLUDED.certified_by,status='CURRENT',remarks=EXCLUDED.remarks""",
            (pid,area,request.form.get('effective_date') or date.today(),request.form.get('expires_at') or None,authority,request.form.get('remarks')))
    write_service_entry(pid,'TRAINING',f'BATTALION INSTRUCTOR — {area}',f'Certified to instruct {area}.',authority)
    flash('INSTRUCTOR QUALIFICATION FILED.','success'); return redirect(request.referrer or url_for('s3'))


@app.route('/leadership-record',methods=['POST'])
@login_required
def leadership_record_action():
    if session.get('access_role') not in {'nco','s1','s3','company_hq','battalion_hq','commander','admin'}: abort(403)
    pid=request.form.get('personnel_id'); authority=session.get('display_name') or session.get('username') or 'BATTALION LEADERSHIP'
    title=(request.form.get('title') or '').strip(); narrative=(request.form.get('narrative') or '').strip()
    if not title or not narrative: abort(400)
    execute("""INSERT INTO leadership_performance_records(personnel_id,record_date,leadership_type,title,narrative,operation_id,recorded_by)
               VALUES(%s,%s,%s,%s,%s,%s,%s)""",(pid,request.form.get('record_date') or date.today(),request.form.get('leadership_type') or 'LEADERSHIP SERVICE',title,narrative,request.form.get('operation_id') or None,authority))
    write_service_entry(pid,'LEADERSHIP',title,narrative,authority)
    add_tour_book_entry(pid,'LEADERSHIP',title,narrative,request.form.get('operation_id') or None,source_key=f"LEAD:{pid}:{title}:{date.today()}")
    flash('LEADERSHIP PERFORMANCE RECORD FILED.','success'); return redirect(request.referrer or url_for('my_soldiers'))


@app.route('/acting-appointment',methods=['POST'])
@login_required
def acting_appointment_action():
    if session.get('access_role') not in {'s1','company_hq','battalion_hq','commander','admin'}: abort(403)
    pid=request.form.get('personnel_id'); authority=session.get('display_name') or session.get('username') or 'S-1'
    execute("""INSERT INTO acting_appointments(personnel_id,billet_title,unit_code,effective_date,authority,remarks)
               VALUES(%s,%s,%s,%s,%s,%s)""",(pid,request.form.get('billet_title'),request.form.get('unit_code'),request.form.get('effective_date') or date.today(),authority,request.form.get('remarks')))
    write_service_entry(pid,'APPOINTMENT',f"ACTING {request.form.get('billet_title')}",f"Temporarily appointed in an acting capacity for {request.form.get('unit_code') or 'the battalion'}.",authority)
    flash('ACTING APPOINTMENT FILED.','success'); return redirect(request.referrer or url_for('s1'))


@app.route('/s3/operation/<operation_id>/duty-assignments',methods=['GET','POST'])
@login_required
@role_required('s3')
def operation_duty_assignments_page(operation_id):
    op=fetch_one('SELECT * FROM operations WHERE id=%s',(operation_id,))
    if not op: abort(404)
    authority=session.get('display_name') or session.get('username') or 'S-3'
    if request.method=='POST':
        pid=request.form.get('personnel_id'); role=(request.form.get('duty_role') or '').strip()
        if not pid or not role: abort(400)
        person=canonical_personnel_snapshot(pid)
        execute("""INSERT INTO operation_duty_assignments(operation_id,personnel_id,duty_role,mos_code,element,assigned_by,remarks)
                   VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(operation_id,personnel_id) DO UPDATE SET duty_role=EXCLUDED.duty_role,
                   mos_code=EXCLUDED.mos_code,element=EXCLUDED.element,assigned_by=EXCLUDED.assigned_by,assigned_at=NOW(),discord_published_at=NULL,remarks=EXCLUDED.remarks""",
                (operation_id,pid,role,request.form.get('mos_code') or (person or {}).get('mos_code'),request.form.get('element'),authority,request.form.get('remarks')))
        flash('OPERATION DUTY ASSIGNMENT FILED.','success')
    assignments=fetch_all("""SELECT oda.*,p.rank_code,p.last_name,p.first_name,p.unit_code,p.platoon,p.squad FROM operation_duty_assignments oda
                             JOIN personnel p ON p.id=oda.personnel_id WHERE oda.operation_id=%s ORDER BY oda.element NULLS LAST,p.unit_code,p.last_name""",(operation_id,))
    candidates=fetch_all("SELECT id,rank_code,last_name,first_name,unit_code,platoon,squad,mos_code FROM personnel WHERE separated_at IS NULL ORDER BY unit_code,platoon,squad,last_name")
    readiness=operation_readiness(operation_id)
    return render_template('operation_duty_assignments.html',operation=op,assignments=assignments,candidates=candidates,readiness=readiness)


@app.post('/s3/operation/<operation_id>/readiness-snapshot')
@login_required
@role_required('s3')
def operation_readiness_snapshot_action(operation_id):
    r=operation_readiness(operation_id)
    execute("""INSERT INTO operation_readiness_snapshots(operation_id,assigned_personnel,present_personnel,serviceable_weapons,qualified_roles,total_roles,readiness_percent,remarks)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",(operation_id,r['assigned'],r['present'],r['serviceable_weapons'],r['qualified_roles'],r['total_roles'],r['percent'],request.form.get('remarks')))
    flash(f"OPERATION READINESS SNAPSHOT FILED — {r['percent']}%.",'success'); return redirect(url_for('operation_duty_assignments_page',operation_id=operation_id))


@app.get('/internal/clerk/operation-duty/pending')
def clerk_operation_duty_pending():
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    guild_id=request.args.get('guild_id',type=int)
    rows=fetch_all("""SELECT oda.id,oda.operation_id,oda.duty_role,oda.element,o.operation_number,o.title,o.start_at,
                      p.rank_code,p.first_name,p.last_name,wml.guild_id
                      FROM operation_duty_assignments oda JOIN operations o ON o.id=oda.operation_id JOIN personnel p ON p.id=oda.personnel_id
                      LEFT JOIN website_member_links wml ON wml.personnel_id=CAST(p.id AS TEXT)
                      WHERE oda.discord_published_at IS NULL AND o.lifecycle_status='PUBLISHED'
                      AND (%s IS NULL OR wml.guild_id=%s) ORDER BY o.start_at NULLS LAST,o.created_at,p.unit_code,p.last_name""",(guild_id,guild_id))
    grouped={}
    for row in rows:
        key=str(row['operation_id']); grouped.setdefault(key,{'operation_id':key,'operation_number':row.get('operation_number'),'title':row.get('title'),'start_at':row.get('start_at').isoformat() if row.get('start_at') else None,'assignments':[]})
        grouped[key]['assignments'].append({'id':str(row['id']),'rank':row.get('rank_code'),'first_name':row.get('first_name'),'last_name':row.get('last_name'),'duty_role':row.get('duty_role'),'element':row.get('element')})
    return {'ok':True,'operations':list(grouped.values())}


@app.post('/internal/clerk/operation-duty/<operation_id>/posted')
def clerk_operation_duty_posted(operation_id):
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    execute('UPDATE operation_duty_assignments SET discord_published_at=NOW() WHERE operation_id=%s',(operation_id,))
    return {'ok':True}


@app.get('/weapon/<weapon_id>/history')
@login_required
def weapon_history(weapon_id):
    weapon=fetch_one('SELECT * FROM weapon_inventory WHERE id=%s',(weapon_id,))
    if not weapon: abort(404)
    current=linked_personnel(); role=session.get('access_role')
    authorized=role in {'s4','battalion_hq','commander','admin'} or (current and fetch_one('SELECT 1 FROM weapon_issue_history WHERE weapon_id=%s AND personnel_id=%s',(weapon_id,current['id'])))
    if not authorized: abort(403)
    history=fetch_all("""SELECT wih.*,p.rank_code,p.first_name,p.last_name FROM weapon_issue_history wih JOIN personnel p ON p.id=wih.personnel_id
                         WHERE wih.weapon_id=%s ORDER BY wih.issued_at DESC,wih.created_at DESC""",(weapon_id,))
    rounds=fetch_all('SELECT * FROM weapon_round_events WHERE weapon_id=%s ORDER BY recorded_at DESC LIMIT 100',(weapon_id,))
    maint=fetch_all('SELECT * FROM weapon_maintenance_log WHERE weapon_id=%s ORDER BY performed_at DESC LIMIT 100',(weapon_id,))
    return render_template('weapon_history.html',weapon=weapon,history=history,rounds=rounds,maintenance=maint)


@app.route('/hq/honor-soldier',methods=['POST'])
@login_required
@role_required('battalion_hq')
def honor_soldier_action():
    pid=request.form.get('personnel_id'); period=(request.form.get('period_label') or date.today().strftime('%B %Y')).upper(); authority=session.get('display_name') or session.get('username') or 'BATTALION HEADQUARTERS'
    recognition_type=(request.form.get('recognition_type') or 'SOLDIER OF THE MONTH').upper()
    if recognition_type not in {'SOLDIER OF THE MONTH','SOLDIER OF THE QUARTER','BATTALION HONOR SOLDIER'}: recognition_type='SOLDIER OF THE MONTH'
    execute("""INSERT INTO soldier_recognitions(personnel_id,recognition_type,period_label,effective_date,narrative,authority)
               VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(recognition_type,period_label) DO UPDATE SET personnel_id=EXCLUDED.personnel_id,effective_date=EXCLUDED.effective_date,narrative=EXCLUDED.narrative,authority=EXCLUDED.authority""",
            (pid,recognition_type,period,request.form.get('effective_date') or date.today(),request.form.get('narrative'),authority))
    write_service_entry(pid,'RECOGNITION',f'{recognition_type} — {period}',request.form.get('narrative') or 'Selected by Battalion Headquarters for sustained participation, readiness, training, leadership, and positive contribution.',authority)
    add_tour_book_entry(pid,'RECOGNITION',f'{recognition_type} — {period}',request.form.get('narrative'),source_key=f'RECOG:{recognition_type}:{period}')
    flash(f'{recognition_type} FILED.','success'); return redirect(request.referrer or url_for('hq'))


@app.route('/hq/command-change',methods=['POST'])
@login_required
@role_required('battalion_hq')
def command_change_action():
    incoming=request.form.get('incoming_personnel_id') or None; outgoing=request.form.get('outgoing_personnel_id') or None; authority=session.get('display_name') or session.get('username') or 'BATTALION HEADQUARTERS'
    incoming_p=fetch_one('SELECT * FROM personnel WHERE id=%s',(incoming,)) if incoming else None
    doc=None
    if incoming_p:
        doc=create_personnel_order(incoming,'APPOINTMENT','CHANGE OF COMMAND / RESPONSIBILITY',f"Effective {request.form.get('effective_date') or date.today()}, {incoming_p.get('rank_code')} {incoming_p.get('first_name')} {incoming_p.get('last_name')} assumes duties as {request.form.get('billet_title')} for {request.form.get('unit_code')}.",effective_date=request.form.get('effective_date') or date.today(),authority=authority,source_key=f"CMDCHANGE:{request.form.get('unit_code')}:{request.form.get('billet_title')}:{request.form.get('effective_date') or date.today()}")
    execute("""INSERT INTO command_change_history(unit_code,billet_title,outgoing_personnel_id,incoming_personnel_id,effective_date,authority,document_id,remarks)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",(request.form.get('unit_code'),request.form.get('billet_title'),outgoing,incoming,request.form.get('effective_date') or date.today(),authority,doc.get('id') if doc else None,request.form.get('remarks')))
    if incoming: write_service_entry(incoming,'COMMAND',f"ASSUMED {request.form.get('billet_title')}",f"Assumed command/responsibility for {request.form.get('unit_code')}.",authority,doc.get('document_number') if doc else None)
    if outgoing: write_service_entry(outgoing,'COMMAND',f"RELINQUISHED {request.form.get('billet_title')}",f"Relinquished command/responsibility for {request.form.get('unit_code')}.",authority)
    flash('CHANGE OF COMMAND / RESPONSIBILITY FILED.','success'); return redirect(request.referrer or url_for('hq'))



@app.get("/my-career")
@login_required
def my_career():
    p=linked_personnel()
    if not p: abort(403)
    return render_template("member_career.html",**member_record_context(p))

@app.get("/my-service-statistics")
@login_required
def my_service_statistics():
    p=linked_personnel()
    if not p: abort(403)
    return render_template("member_service_statistics.html",personnel=soldier_view(p),**member_career_context(p))

@app.get("/my-weekly-report")
@login_required
def my_weekly_report():
    p=linked_personnel()
    if not p: abort(403)
    return render_template("member_weekly_report.html",personnel=soldier_view(p),report=member_weekly_report(p))

@app.get("/my-qualification/<source>/<record_id>")
@login_required
def my_qualification_card(source,record_id):
    p=linked_personnel()
    if not p: abort(403)
    if source not in {"qualification","duty"}: abort(404)
    card=qualification_card_record(p["id"],source,record_id)
    if not card: abort(404)
    return render_template("qualification_card.html",personnel=soldier_view(p),card=card)

@app.get("/my-weapon-history")
@login_required
def my_weapon_service_history():
    p=linked_personnel()
    if not p: abort(403)
    weapon=current_weapon_for(p)
    if not weapon: return render_template("member_weapon_history.html",personnel=soldier_view(p),weapon=None,issue_history=[],maintenance=[],inspections=[],operations=[])
    issue_history=fetch_all("""SELECT wih.*,wi.serial_number,wi.rack_number FROM weapon_issue_history wih JOIN weapon_inventory wi ON wi.id=wih.weapon_id
      WHERE wih.personnel_id=%s ORDER BY wih.issued_at DESC,wih.created_at DESC""",(p["id"],))
    maintenance=fetch_all("SELECT * FROM weapon_maintenance_log WHERE weapon_id=%s ORDER BY performed_at DESC",(weapon["id"],))
    inspections=fetch_all("SELECT * FROM weapon_inspections WHERE weapon_id=%s ORDER BY inspection_date DESC,created_at DESC",(weapon["id"],))
    operations=fetch_all("""SELECT op.*,o.operation_code,o.title,o.operation_date FROM operation_participation op JOIN operations o ON o.id=op.operation_id
      WHERE op.personnel_id=%s ORDER BY COALESCE(o.operation_date,CURRENT_DATE) DESC""",(p["id"],))
    return render_template("member_weapon_history.html",personnel=soldier_view(p),weapon=weapon,issue_history=issue_history,maintenance=maintenance,inspections=inspections,operations=operations)

@app.get("/my-squad")
@login_required
def my_squad():
    p=linked_personnel()
    if not p: abort(403)
    return render_template("member_formation.html",personnel=soldier_view(p),snapshot=member_formation_snapshot(p,"squad"),formation_type="SQUAD")

@app.get("/my-platoon")
@login_required
def my_platoon_identity():
    p=linked_personnel()
    if not p: abort(403)
    return render_template("member_formation.html",personnel=soldier_view(p),snapshot=member_formation_snapshot(p,"platoon"),formation_type="PLATOON")


@app.post("/my-career/goals")
@login_required
def my_career_goals():
    p=linked_personnel()
    if not p: abort(403)
    selected=request.form.getlist("goal_code")[:2]
    selected=[x for x in selected if x in SERVICE_GOALS]
    execute("UPDATE member_service_goals SET is_active=FALSE WHERE personnel_id=%s",(p["id"],))
    for code in selected:
        meta=SERVICE_GOALS[code]
        execute("""INSERT INTO member_service_goals(personnel_id,goal_code,goal_label,is_active,created_at,completed_at)
          VALUES(%s,%s,%s,TRUE,NOW(),NULL)
          ON CONFLICT(personnel_id,goal_code) DO UPDATE SET goal_label=EXCLUDED.goal_label,is_active=TRUE,created_at=COALESCE(member_service_goals.created_at,NOW())""",
          (p["id"],code,meta["label"]))
    flash("SERVICE GOALS UPDATED.","success")
    return redirect(url_for("my_career"))

@app.post("/hq/unit-identity")
@login_required
@role_required("battalion_hq")
def hq_unit_identity():
    unit_node_id=request.form.get("unit_node_id")
    nickname=(request.form.get("nickname") or "").strip()[:80]
    call_sign=(request.form.get("call_sign") or "").strip()[:40]
    authority=session.get("display_name") or session.get("username") or "BATTALION HEADQUARTERS"
    execute("""INSERT INTO unit_identity_settings(unit_node_id,nickname,call_sign,approved_by,approved_at,updated_at)
      VALUES(%s,%s,%s,%s,NOW(),NOW())
      ON CONFLICT(unit_node_id) DO UPDATE SET nickname=EXCLUDED.nickname,call_sign=EXCLUDED.call_sign,approved_by=EXCLUDED.approved_by,approved_at=NOW(),updated_at=NOW()""",
      (unit_node_id,nickname or None,call_sign or None,authority))
    flash("UNIT NICKNAME / CALL SIGN UPDATED.","success")
    return redirect(request.referrer or url_for("hq"))

@app.get('/my-unit')
@login_required
def my_unit():
    p=linked_personnel()
    if not p: abort(403)
    # Assignment-scoped member view: only their own unit_code is visible.
    members=fetch_all("SELECT id,rank_code,last_name,first_name,unit_code,platoon,squad,duty_position,mos_code FROM personnel WHERE separated_at IS NULL AND unit_code=%s ORDER BY platoon NULLS FIRST,squad NULLS FIRST,last_name",(p['unit_code'],))
    return render_template('my_unit.html',personnel=soldier_view(p),members=members)


@app.get('/command/billet-strength')
@login_required
@role_required('battalion_hq')
def billet_strength_page():
    return render_template('billet_strength.html',rows=billet_strength_rows(),needs=mos_recruiting_needs())


@app.get('/public/recruiting-needs')
def public_recruiting_needs():
    return {'needs':[{'mos_code':x['mos_code'],'mos_title':x['mos_title'],'needed':x['needed']} for x in mos_recruiting_needs()]}



def prospective_replacements_rows():
    """Active Discord members who do not yet have a linked battalion personnel record."""
    return fetch_all("""
        SELECT dm.guild_id,dm.discord_user_id,dm.username,dm.display_name,dm.joined_at,dm.updated_at,
               rc.id AS case_id,rc.case_number,rc.public_token,rc.status AS recruiting_status,
               rc.discord_verified_username,rc.approved_at,rc.created_at AS application_created_at,
               CASE
                 WHEN rc.id IS NULL THEN 'NO APPLICATION'
                 WHEN rc.status='APPROVED_AWAITING_DISCORD' THEN 'APPROVED — DISCORD LINK REQUIRED'
                 WHEN rc.status IN ('REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING') THEN 'APPROVED REPLACEMENT'
                 WHEN rc.status='MORE_INFO_REQUIRED' THEN 'MORE INFO REQUIRED'
                 WHEN rc.status IN ('SUBMITTED','DISCORD_VERIFIED','PENDING_COMMAND') THEN 'COMMAND REVIEW'
                 WHEN rc.status IN ('DENIED','CLOSED') THEN 'CASE CLOSED'
                 WHEN rc.status='ENLISTED' THEN 'ENLISTED — LINK PENDING'
                 ELSE REPLACE(COALESCE(rc.status,'NO APPLICATION'),'_',' ')
               END AS intake_status
        FROM discord_members dm
        LEFT JOIN website_member_links wml
          ON wml.guild_id::text=dm.guild_id::text
         AND wml.discord_user_id::text=dm.discord_user_id::text
        LEFT JOIN LATERAL (
          SELECT rc0.*
          FROM recruiting_cases rc0
          WHERE rc0.discord_user_id::text=dm.discord_user_id::text
            AND (rc0.guild_id IS NULL OR rc0.guild_id::text=dm.guild_id::text)
          ORDER BY rc0.created_at DESC
          LIMIT 1
        ) rc ON TRUE
        WHERE dm.active=TRUE
          AND COALESCE(dm.is_bot,FALSE)=FALSE
          AND wml.personnel_id IS NULL
        ORDER BY COALESCE(dm.joined_at,dm.updated_at) DESC,dm.display_name ASC
    """)

@app.get('/hq/prospective-replacements')
@login_required
@role_required('battalion_hq')
def prospective_replacements():
    rows=prospective_replacements_rows()
    counts={
      'total':len(rows),
      'no_application':sum(1 for r in rows if not r.get('case_id')),
      'review':sum(1 for r in rows if r.get('recruiting_status') in {'SUBMITTED','DISCORD_VERIFIED','PENDING_COMMAND','MORE_INFO_REQUIRED'}),
      'approved':sum(1 for r in rows if r.get('recruiting_status') in {'APPROVED_AWAITING_DISCORD','REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING'}),
    }
    return render_template('prospective_replacements.html',rows=rows,counts=counts)


def create_command_adopted_recruiting_case(guild_id, discord_user_id, username, display_name, adopted_by):
    """Create a valid Recruiting Case for a Discord-first joiner without duplicating records."""
    existing_personnel=fetch_one("""SELECT wml.personnel_id FROM website_member_links wml
      WHERE wml.guild_id::text=%s AND wml.discord_user_id::text=%s""",(str(guild_id),str(discord_user_id)))
    if existing_personnel and existing_personnel.get("personnel_id"):
        return {"error":"PERSONNEL_EXISTS"}

    active_case=fetch_one("""SELECT id,case_number,status FROM recruiting_cases
      WHERE discord_user_id::text=%s AND (guild_id IS NULL OR guild_id::text=%s)
        AND status NOT IN ('DENIED','CLOSED','ENLISTED')
      ORDER BY created_at DESC LIMIT 1""",(str(discord_user_id),str(guild_id)))
    if active_case:
        return {"existing":active_case}

    case_number=_recruit_case_number()
    public_token=secrets.token_urlsafe(24)
    verification_code=_random_field_code(10)
    verification_expires_at=datetime.now(timezone.utc)+timedelta(days=7)
    notes=("COMMAND-ADOPTED DISCORD-FIRST INTAKE.\n"
           f"Created by {adopted_by} from the Prospective Replacements board.\n"
           "No public website application was submitted before Discord entry.")

    inserted=fetch_one("""
      INSERT INTO recruiting_cases(
        case_number,public_token,verification_code,verification_expires_at,guild_id,status,
        discord_user_id,discord_verified_username,discord_username_input,
        timezone_name,hll_experience,role_interest,looking_for,play_style,
        follows_chain,participation,applicant_notes,
        command_notes,reviewed_by,reviewed_at,discord_oauth_linked_at,
        created_at,updated_at
      ) VALUES(
        %s,%s,%s,%s,%s,'PENDING_COMMAND',
        %s,%s,%s,
        'NOT COLLECTED — COMMAND ADOPTED',
        'NOT COLLECTED — COMMAND ADOPTED',
        'NOT COLLECTED — COMMAND ADOPTED',
        'COMMAND-ADOPTED DISCORD-FIRST INTAKE',
        'NOT COLLECTED — COMMAND ADOPTED',
        TRUE,'NOT COLLECTED — COMMAND ADOPTED',
        'Applicant entered through Discord first; public application responses were not collected.',
        %s,%s,NOW(),NOW(),NOW(),NOW()
      )
      RETURNING id,case_number,status
    """,(case_number,public_token,verification_code,verification_expires_at,str(guild_id),
          str(discord_user_id),display_name or username,username,notes,adopted_by))
    return {"created":inserted}


@app.post('/hq/prospective-replacements/adopt')
@login_required
@role_required('battalion_hq')
def adopt_prospective_replacement():
    discord_user_id=(request.form.get('discord_user_id') or '').strip()
    guild_id=(request.form.get('guild_id') or '').strip()
    username=(request.form.get('username') or '').strip()
    display_name=(request.form.get('display_name') or '').strip()
    if not discord_user_id or not guild_id:
        flash('DISCORD MEMBER IDENTIFICATION DATA WAS MISSING.','danger')
        return redirect(url_for('prospective_replacements'))

    actor=session.get('display_name') or session.get('username') or 'BATTALION HEADQUARTERS'
    result=create_command_adopted_recruiting_case(guild_id, discord_user_id, username, display_name, actor)

    if result.get("error") == "PERSONNEL_EXISTS":
        flash('THIS DISCORD MEMBER ALREADY HAS AN OFFICIAL PERSONNEL RECORD. NO RECRUITING CASE WAS CREATED.','warning')
        return redirect(url_for('prospective_replacements'))

    if result.get("existing"):
        flash(f"AN ACTIVE RECRUITING CASE ALREADY EXISTS: {result['existing']['case_number']} ({result['existing']['status'].replace('_',' ')}).",'warning')
        return redirect(url_for('recruiting_case_archive', case_id=result["existing"]["id"]))

    created=result.get("created")
    flash(f"DISCORD MEMBER ADOPTED AS {created['case_number']}. REVIEW AND APPROVE THE CASE BELOW.",'success')
    return redirect(url_for('recruiting_control')+'#active')

@app.route('/hq/recruiting', methods=['GET','POST'])
@login_required
@role_required('battalion_hq')
def recruiting_control():
    if request.method == 'POST':
        case_id=request.form.get('case_id'); action=(request.form.get('action') or '').upper(); remarks=(request.form.get('command_notes') or '').strip() or None
        case=fetch_one('SELECT * FROM recruiting_cases WHERE id=%s',(case_id,))
        if not case: abort(404)
        authority=session.get('display_name') or session.get('username') or 'BATTALION HEADQUARTERS'
        if action=='APPROVE':
            if case.get('status') in {'DENIED','CLOSED','ENLISTED'}:
                flash('THIS RECRUITING CASE IS ALREADY CLOSED AND CANNOT BE APPROVED.','warning')
            elif case.get('discord_user_id'):
                execute("""UPDATE recruiting_cases SET status='REPLACEMENT_DEPOT',replacement_depot_entered_at=COALESCE(replacement_depot_entered_at,NOW()),
                           command_notes=%s,reviewed_by=%s,reviewed_at=NOW(),approved_at=COALESCE(approved_at,NOW()),
                           discord_join_error=NULL,credentials_delivery_error=NULL,updated_at=NOW() WHERE id=%s""",(remarks,authority,case_id))
                flash('APPLICATION APPROVED. BATTALION CLERK WILL ADD THE RECRUIT TO DISCORD, ASSIGN REPLACEMENT DEPOT, OPEN THE 201 FILE, AND DELIVER LOGIN CREDENTIALS AUTOMATICALLY.','success')
            else:
                execute("UPDATE recruiting_cases SET status='APPROVED_AWAITING_DISCORD',command_notes=%s,reviewed_by=%s,reviewed_at=NOW(),approved_at=COALESCE(approved_at,NOW()),updated_at=NOW() WHERE id=%s",(remarks,authority,case_id))
                flash('APPLICATION APPROVED. APPLICANT MUST VERIFY DISCORD BEFORE BATTALION CLERK CAN BEGIN REPLACEMENT PROCESSING.','success')
        elif action=='MORE_INFO':
            request_text=(request.form.get('command_request') or '').strip()
            if not request_text:
                flash('ENTER THE INFORMATION YOU NEED FROM THE APPLICANT.','danger')
            else:
                execute("UPDATE recruiting_cases SET status='MORE_INFO_REQUIRED',command_request=%s,command_notes=%s,reviewed_by=%s,reviewed_at=NOW(),updated_at=NOW() WHERE id=%s",(request_text,remarks,authority,case_id))
                flash('APPLICATION RETURNED FOR MORE INFORMATION.','success')
        elif action=='DENY':
            execute("UPDATE recruiting_cases SET status='DENIED',command_notes=%s,reviewed_by=%s,reviewed_at=NOW(),denied_at=NOW(),updated_at=NOW() WHERE id=%s",(remarks,authority,case_id))
            flash('APPLICATION DENIED AND FILED IN THE CLOSED RECRUITING RECORDS.','warning')
        return redirect(url_for('recruiting_control'))
    rows=fetch_all("""SELECT rc.*,rp.rank_code AS recruiter_rank,rp.first_name AS recruiter_first_name,rp.last_name AS recruiter_last_name
                      FROM recruiting_cases rc LEFT JOIN personnel rp ON rp.id=rc.recruited_by_personnel_id
                      ORDER BY CASE WHEN rc.status IN ('SUBMITTED','DISCORD_VERIFIED','PENDING_COMMAND') THEN 0 WHEN rc.status='MORE_INFO_REQUIRED' THEN 1 WHEN rc.status='APPROVED_AWAITING_DISCORD' THEN 2 WHEN rc.status IN ('REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING') THEN 3 ELSE 3 END, rc.created_at ASC""")
    counts={}
    for row in rows: counts[row['status']]=counts.get(row['status'],0)+1
    prospective_rows=prospective_replacements_rows()
    prospective_counts={
      'total':len(prospective_rows),
      'no_application':sum(1 for r in prospective_rows if not r.get('case_id')),
      'review':sum(1 for r in prospective_rows if r.get('recruiting_status') in {'SUBMITTED','DISCORD_VERIFIED','PENDING_COMMAND','MORE_INFO_REQUIRED'}),
      'approved':sum(1 for r in prospective_rows if r.get('recruiting_status') in {'APPROVED_AWAITING_DISCORD','REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING'}),
    }
    return render_template('recruiting_control.html',cases=rows,counts=counts,
                           prospective_rows=prospective_rows,prospective_counts=prospective_counts)





@app.get('/hq/recruiting/<case_id>')
@login_required
@role_required('battalion_hq')
def recruiting_case_archive(case_id):
    case=fetch_one("""SELECT rc.*,rp.rank_code AS recruiter_rank,rp.first_name AS recruiter_first_name,
                      rp.last_name AS recruiter_last_name
                      FROM recruiting_cases rc
                      LEFT JOIN personnel rp ON rp.id=rc.recruited_by_personnel_id
                      WHERE rc.id=%s""",(case_id,))
    if not case:
        abort(404)
    return render_template('recruiting_case_archive.html',case=case)

@app.post('/hq/recruiting/<case_id>/return-to-review')
@login_required
@role_required('battalion_hq')
def recruiting_case_return_to_review(case_id):
    case=fetch_one("SELECT * FROM recruiting_cases WHERE id=%s",(case_id,))
    if not case:
        abort(404)
    if case.get('status') == 'ENLISTED':
        flash('AN ENLISTED CASE CANNOT BE RETURNED TO RECRUITING REVIEW. USE THE PERSONNEL RECORD FOR FURTHER ACTION.','warning')
        return redirect(url_for('recruiting_case_archive',case_id=case_id))
    allowed={'REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING','APPROVED_AWAITING_DISCORD','DENIED','CLOSED'}
    if case.get('status') not in allowed:
        flash('THIS CASE IS ALREADY IN AN ACTIVE REVIEW STATUS.','warning')
        return redirect(url_for('recruiting_case_archive',case_id=case_id))
    authority=session.get('display_name') or session.get('username') or 'BATTALION HEADQUARTERS'
    note=(request.form.get('return_reason') or '').strip()
    prior=case.get('command_notes') or ''
    audit=f"[RETURNED TO REVIEW BY {authority}] {note}".strip()
    merged=(prior+"\n"+audit).strip() if prior else audit
    execute("""UPDATE recruiting_cases
               SET status='PENDING_COMMAND',
                   command_notes=%s,
                   reviewed_by=%s,
                   reviewed_at=NOW(),
                   updated_at=NOW()
               WHERE id=%s""",(merged,authority,case_id))
    flash('RECRUITING CASE RETURNED TO COMMAND REVIEW. NO NEW APPLICATION OR PERSONNEL RECORD WAS CREATED.','success')
    return redirect(url_for('recruiting_control')+'#active')



@app.post('/internal/clerk/operations/<operation_id>/reconcile-rounds')
def clerk_reconcile_operation_rounds(operation_id):
    if not _clerk_authorized():
        return {"ok":False,"error":"authorization required"},401
    op=fetch_one("SELECT * FROM operations WHERE id=%s",(operation_id,))
    if not op:
        return {"ok":False,"error":"operation not found"},404
    rows=fetch_all("""SELECT opart.personnel_id,opart.rounds_expended,p.rank_code,p.last_name
                      FROM operation_participation opart JOIN personnel p ON p.id=opart.personnel_id
                      WHERE opart.operation_id=%s
                        AND UPPER(COALESCE(opart.attendance_status,'')) NOT IN ('ABSENT','NO CREDIT','PARTIAL / LATE')""",(operation_id,))
    repaired=0; members=[]
    for row in rows:
        delta=reconcile_operation_weapon_rounds(operation_id,row["personnel_id"],row.get("rounds_expended") or 0,
                                                "BATTALION CLERK",
                                                f"Administrative reconciliation for {op.get('title') or op.get('operation_number') or 'operation'}.")
        if delta:
            repaired += delta
            members.append({"personnel_id":str(row["personnel_id"]),
                            "name":f"{row.get('rank_code') or ''} {row.get('last_name') or ''}".strip(),
                            "rounds":delta})
    return {"ok":True,"operation_id":str(operation_id),"repaired_rounds":repaired,"members":members}


@app.post('/internal/clerk/readiness/recheck')
def clerk_readiness_recheck():
    if not _clerk_authorized():
        return {"ok":False,"error":"authorization required"},401
    data=request.get_json(silent=True) or {}
    personnel_id=data.get("personnel_id")
    if not personnel_id and data.get("guild_id") and data.get("discord_user_id"):
        link=fetch_one("SELECT personnel_id FROM website_member_links WHERE guild_id=%s AND discord_user_id=%s",(data.get("guild_id"),data.get("discord_user_id")))
        personnel_id=link.get("personnel_id") if link else None
    if not personnel_id:
        return {"ok":False,"error":"personnel not linked"},404
    person=fetch_one("SELECT * FROM personnel WHERE id=%s",(personnel_id,))
    if not person:
        return {"ok":False,"error":"personnel not found"},404
    score,status,breakdown=sync_readiness(person)
    return {"ok":True,"personnel_id":str(personnel_id),"readiness_percent":score,"readiness_status":status,"breakdown":breakdown}


@app.post('/internal/clerk/ribbons/recheck')
def clerk_ribbons_recheck():
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    people=fetch_all("SELECT id FROM personnel WHERE separated_at IS NULL")
    automatic_ribbon_recheck([p['id'] for p in people])
    return {'ok':True,'checked':len(people)}


@app.get('/internal/clerk/recruiting/status')
def clerk_recruiting_status():
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    discord_user_id=request.args.get('discord_user_id',type=int); guild_id=request.args.get('guild_id',type=int)
    if not discord_user_id: return {'ok':False,'error':'discord_user_id required'},400
    case=fetch_one("""SELECT id,case_number,public_token,status,discord_user_id,discord_verified_username,discord_notified_at,personnel_id,guild_id
                      FROM recruiting_cases WHERE discord_user_id=%s AND (guild_id=%s OR guild_id IS NULL)
                      ORDER BY created_at DESC LIMIT 1""",(discord_user_id,guild_id))
    if not case: return {'ok':True,'exists':False}
    if guild_id and not case.get('guild_id'):
        execute("UPDATE recruiting_cases SET guild_id=%s,updated_at=NOW() WHERE id=%s",(guild_id,case['id']))
        case['guild_id']=guild_id
    return {'ok':True,'exists':True,'case':case}


@app.post('/internal/clerk/recruiting/verify')
def clerk_recruiting_verify():
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    data=request.get_json(silent=True) or {}; code=(data.get('code') or '').strip().upper(); discord_user_id=data.get('discord_user_id'); guild_id=data.get('guild_id'); username=(data.get('username') or '').strip()
    if not code or not discord_user_id: return {'ok':False,'error':'code and discord_user_id required'},400
    case=fetch_one("SELECT * FROM recruiting_cases WHERE UPPER(verification_code)=UPPER(%s)",(code,))
    if not case: return {'ok':False,'error':'verification code not found'},404
    if case.get('verification_used_at'): return {'ok':False,'error':'verification code has already been used'},409
    if case.get('verification_expires_at') and case['verification_expires_at'] < datetime.now(timezone.utc): return {'ok':False,'error':'verification code expired'},410
    conflict=fetch_one("SELECT case_number FROM recruiting_cases WHERE discord_user_id=%s AND id<>%s AND status NOT IN ('DENIED','CLOSED','ENLISTED') LIMIT 1",(discord_user_id,case['id']))
    if conflict: return {'ok':False,'error':f"discord account already linked to {conflict['case_number']}"},409
    new_status='PENDING_COMMAND' if case.get('status') in {'SUBMITTED','DISCORD_VERIFICATION_PENDING'} else case.get('status')
    execute("""UPDATE recruiting_cases SET discord_user_id=%s,guild_id=%s,discord_verified_username=%s,verification_used_at=NOW(),status=%s,updated_at=NOW() WHERE id=%s""",(discord_user_id,guild_id,username,new_status,case['id']))
    return {'ok':True,'case_number':case['case_number'],'status':new_status}


@app.get('/internal/clerk/recruiting/notifications')
def clerk_recruiting_notifications():
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    guild_id=request.args.get('guild_id',type=int)
    rows=fetch_all("""SELECT id,case_number,public_token,status,discord_user_id,discord_verified_username,guild_id,
                             discord_last_notified_status,discord_last_notified_at
                      FROM recruiting_cases
                      WHERE discord_user_id IS NOT NULL
                        AND status NOT IN ('CLOSED')
                        AND (guild_id=%s OR guild_id IS NULL)
                        AND (discord_last_notified_status IS DISTINCT FROM status)
                      ORDER BY updated_at ASC""",(guild_id,))
    return {'ok':True,'cases':rows}


@app.post('/internal/clerk/recruiting/<case_id>/status-notified')
def clerk_recruiting_status_notified(case_id):
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    data=request.get_json(silent=True) or {}
    status=(data.get('status') or '').strip()
    if not status: return {'ok':False,'error':'status required'},400
    execute("""UPDATE recruiting_cases SET discord_last_notified_status=%s,discord_last_notified_at=NOW(),
               discord_notified_at=CASE WHEN %s IN ('REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING') THEN NOW() ELSE discord_notified_at END,
               guild_id=COALESCE(%s,guild_id),updated_at=NOW() WHERE id=%s""",(status,status,data.get('guild_id'),case_id))
    return {'ok':True}



def _provision_replacement_personnel(case, *, guild_id=None, discord_user_id=None, username=None, display_name=None):
    """Open the 201 File for an approved recruit before permanent assignment.

    The website is authoritative. Discord rank/MOS/company/platoon/squad roles are
    not prerequisites for creating the Replacement Detachment record.
    """
    if not case:
        return {'ok':False,'error':'recruiting case not found'}
    status=str(case.get('status') or '').upper()
    if status not in {'REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING'}:
        return {'ok':False,'error':f'case status {status or "UNKNOWN"} is not provisionable'}
    discord_user_id=discord_user_id or case.get('discord_user_id')
    guild_id=guild_id or case.get('guild_id')
    if not discord_user_id:
        return {'ok':False,'error':'verified Discord identity required'}

    # Reuse an already linked personnel record instead of ever creating duplicates.
    personnel_id=case.get('personnel_id')
    if not personnel_id and guild_id:
        link=fetch_one("SELECT personnel_id FROM website_member_links WHERE guild_id=%s AND discord_user_id=%s",(guild_id,discord_user_id))
        personnel_id=(link or {}).get('personnel_id')
    if personnel_id:
        person=fetch_one("SELECT * FROM personnel WHERE id=%s",(personnel_id,))
        if person:
            if guild_id and discord_user_id:
                execute("""INSERT INTO website_member_links(guild_id,discord_user_id,personnel_id) VALUES(%s,%s,%s)
                           ON CONFLICT(guild_id,discord_user_id) DO UPDATE SET personnel_id=EXCLUDED.personnel_id,linked_at=NOW()""",
                        (guild_id,discord_user_id,personnel_id))
            execute("UPDATE recruiting_cases SET personnel_id=%s,status='APPROVED_AWAITING_PROCESSING',guild_id=COALESCE(%s,guild_id),updated_at=NOW() WHERE id=%s",(personnel_id,guild_id,case['id']))
            return {'ok':True,'created':False,'personnel_id':str(personnel_id),'personnel':person}

    preferred=(case.get('discord_verified_username') or display_name or username or case.get('discord_username_input') or 'Replacement').strip()
    first_name,last_name=_discord_personnel_name(username or preferred, display_name or preferred)
    person=fetch_one("""INSERT INTO personnel
        (service_number,first_name,last_name,rank_code,mos_code,duty_position,unit_code,date_joined,rvn_arrival_date,
         field_status,readiness_status,readiness_percent,duty_status,roster_entered_at,lifecycle_state)
        VALUES(%s,%s,%s,'PVT','00R','MOS PENDING','1-5 CAV',CURRENT_DATE,CURRENT_DATE,
               'Replacement','PROCESSING',10,'REPLACEMENT — UNASSIGNED',CURRENT_DATE,'IN PROCESSING') RETURNING *""",
        (allocate_service_number(),first_name,last_name))
    execute("""INSERT INTO promotion_history(personnel_id,old_rank_code,new_rank_code,effective_date,authority,remarks)
               VALUES(%s,NULL,'PVT',CURRENT_DATE,'BATTALION HEADQUARTERS','Initial entry grade filed on approval into Replacement Detachment.')""",(person['id'],))
    execute("""INSERT INTO assignment_history(personnel_id,unit_code,duty_position,effective_date)
               VALUES(%s,'1-5 CAV','Replacement Detachment — MOS Pending',CURRENT_DATE)""",(person['id'],))
    execute("INSERT INTO personnel_progress_control(personnel_id) VALUES(%s) ON CONFLICT(personnel_id) DO NOTHING",(person['id'],))
    if guild_id:
        execute("""INSERT INTO website_member_links(guild_id,discord_user_id,personnel_id)
                   VALUES(%s,%s,%s) ON CONFLICT(guild_id,discord_user_id)
                   DO UPDATE SET personnel_id=EXCLUDED.personnel_id,linked_at=NOW()""",(guild_id,discord_user_id,person['id']))
    execute("""UPDATE recruiting_cases SET personnel_id=%s,status='APPROVED_AWAITING_PROCESSING',
               replacement_depot_entered_at=COALESCE(replacement_depot_entered_at,NOW()),updated_at=NOW() WHERE id=%s""",(person['id'],case['id']))
    card,field_code=issue_battle_roster_card(person['id'])
    ensure_standard_uniform(person['id'])
    open_personnel_action(person['id'],'PERSONNEL','Initial S-1 In-Processing','S-1','HIGH','BATTALION CLERK',
                          {'workflow':'REPLACEMENT DETACHMENT','case_number':case.get('case_number')},source_key=f"REPLACEMENT-INPROCESS:{person['id']}",due_date=date.today()+timedelta(days=3))
    open_personnel_action(person['id'],'TRAINING','Replacement Training Required','S-1','ROUTINE','BATTALION CLERK',
                          {'workflow':'REPLACEMENT DETACHMENT'},source_key=f"REPLACEMENT-TRAINING:{person['id']}")
    execute("""INSERT INTO personnel_training_records(personnel_id,program_code,status,started_at)
               VALUES(%s,'REPLACEMENT','IN PROGRESS',CURRENT_DATE) ON CONFLICT(personnel_id,program_code) DO NOTHING""",(person['id'],))
    write_service_entry(person['id'],'ARRIVAL','REPLACEMENT DETACHMENT — 201 FILE OPENED',
        f"Approved recruit entered on battalion rolls as PVT and placed in Replacement Detachment under Recruiting Case {case.get('case_number') or 'N/A'}. Permanent MOS and formation assignment pending.",
        'BATTALION HEADQUARTERS')
    initial_order=replacement_orders_for(person['id'])
    enqueue_discord_role_sync(person['id'],'APPROVED REPLACEMENT PROVISIONED')
    staff_log('S-1','NEW REPLACEMENT',f"PVT {person.get('last_name','')} entered Replacement Detachment",'BATTALION CLERK',person['id'],
              (initial_order or {}).get('document_number') if initial_order else None,{'case_number':case.get('case_number')})
    return {'ok':True,'created':True,'personnel_id':str(person['id']),'personnel':person,'roster':card,'field_code':field_code,
            'initial_order':initial_order}


def _ensure_recruit_login_delivery(case: dict, provision: dict) -> dict:
    """Return stable one-time plaintext credentials until successful DM delivery is recorded."""
    if not provision.get("ok"):
        return provision
    current=fetch_one("SELECT credentials_sent_at,credentials_pending_field_code_enc FROM recruiting_cases WHERE id=%s",(case["id"],)) or {}
    if current.get("credentials_sent_at"):
        provision["credentials_already_sent"]=True
        return provision
    personnel_id=provision.get("personnel_id") or case.get("personnel_id")
    if not personnel_id:
        return provision
    card=provision.get("roster")
    if not card:
        card=fetch_one("SELECT * FROM battle_roster_cards WHERE personnel_id=%s AND is_active=TRUE",(personnel_id,))
    field_code=provision.get("field_code") or _oauth_decrypt(current.get("credentials_pending_field_code_enc"))
    if not field_code:
        # A previous partial provisioning pass may have created a hash without
        # ever exposing plaintext to Battalion Clerk. Rotate exactly once and
        # preserve that pending plaintext encrypted until DM delivery succeeds.
        if not card:
            card,field_code=issue_battle_roster_card(personnel_id)
        else:
            field_code=_random_field_code()
            execute("UPDATE battle_roster_cards SET field_code_hash=%s WHERE id=%s",
                    (generate_password_hash(field_code),card["id"]))
    execute("UPDATE recruiting_cases SET credentials_pending_field_code_enc=%s,updated_at=NOW() WHERE id=%s",
            (_oauth_encrypt(field_code),case["id"]))
    weapon=fetch_one("""SELECT wi.serial_number FROM weapon_issue_history wih JOIN weapon_inventory wi ON wi.id=wih.weapon_id
                        WHERE wih.personnel_id=%s AND wih.is_current=TRUE LIMIT 1""",(personnel_id,))
    provision["roster_number"]=(card or {}).get("roster_number") if isinstance(card,dict) else None
    provision["field_code"]=field_code
    provision["weapon_serial"]=(weapon or {}).get("serial_number")
    return provision


@app.get('/internal/clerk/recruiting/<case_id>/join-authorization')
def clerk_recruiting_join_authorization(case_id):
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    case=fetch_one("SELECT * FROM recruiting_cases WHERE id=%s",(case_id,))
    if not case: return {'ok':False,'error':'recruiting case not found'},404
    if str(case.get('status') or '').upper() not in {'REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING'}:
        return {'ok':False,'error':'recruiting case is not approved for Discord entry'},409
    if not case.get('discord_user_id'):
        return {'ok':False,'error':'verified Discord identity required'},409
    scope=str(case.get('discord_oauth_scope') or '')
    if 'guilds.join' not in scope.split():
        return {'ok':False,'error':'Discord authorization does not include guilds.join; applicant must reconnect Discord'},409
    access=_oauth_decrypt(case.get('discord_oauth_access_token_enc'))
    expires=case.get('discord_oauth_expires_at')
    if not access or not expires or expires <= datetime.now(timezone.utc)+timedelta(minutes=5):
        try:
            refreshed=_discord_refresh_oauth(case); access=refreshed['access_token']; scope=refreshed.get('scope') or scope
        except Exception as exc:
            execute("UPDATE recruiting_cases SET discord_join_error=%s,discord_join_last_attempt_at=NOW(),updated_at=NOW() WHERE id=%s",(str(exc)[:500],case_id))
            return {'ok':False,'error':str(exc)},409
    return {'ok':True,'case_id':str(case_id),'discord_user_id':str(case['discord_user_id']),'access_token':access,'scope':scope}


@app.post('/internal/clerk/recruiting/<case_id>/join-status')
def clerk_recruiting_join_status(case_id):
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    data=request.get_json(silent=True) or {}
    joined=bool(data.get('joined'))
    error=(data.get('error') or '').strip() or None
    execute("""UPDATE recruiting_cases SET guild_id=COALESCE(%s,guild_id),discord_joined_at=CASE WHEN %s THEN COALESCE(discord_joined_at,NOW()) ELSE discord_joined_at END,
               discord_join_error=%s,discord_join_last_attempt_at=NOW(),updated_at=NOW() WHERE id=%s""",
            (data.get('guild_id'),joined,None if joined else error,case_id))
    return {'ok':True}


@app.post('/internal/clerk/recruiting/<case_id>/credentials-status')
def clerk_recruiting_credentials_status(case_id):
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    data=request.get_json(silent=True) or {}
    sent=bool(data.get('sent')); error=(data.get('error') or '').strip() or None
    execute("""UPDATE recruiting_cases SET credentials_sent_at=CASE WHEN %s THEN COALESCE(credentials_sent_at,NOW()) ELSE credentials_sent_at END,
               credentials_delivery_error=%s,credentials_last_attempt_at=NOW(),
               credentials_pending_field_code_enc=CASE WHEN %s THEN NULL ELSE credentials_pending_field_code_enc END,
               discord_notified_at=CASE WHEN %s THEN COALESCE(discord_notified_at,NOW()) ELSE discord_notified_at END,updated_at=NOW() WHERE id=%s""",
            (sent,None if sent else error,sent,sent,case_id))
    return {'ok':True}


@app.post('/internal/clerk/recruiting/<case_id>/provision')
def clerk_recruiting_provision(case_id):
    if not _clerk_authorized():
        return {'ok':False,'error':'authorization required'},401
    case=fetch_one("SELECT * FROM recruiting_cases WHERE id=%s",(case_id,))
    if not case:
        return {'ok':False,'error':'recruiting case not found'},404
    data=request.get_json(silent=True) or {}
    result=_provision_replacement_personnel(case,guild_id=data.get('guild_id'),discord_user_id=data.get('discord_user_id'),
                                            username=data.get('username'),display_name=data.get('display_name'))
    if result.get('ok') and data.get('ensure_credentials',True):
        case=fetch_one("SELECT * FROM recruiting_cases WHERE id=%s",(case_id,)) or case
        result=_ensure_recruit_login_delivery(case,result)
    return result,(200 if result.get('ok') else 409)

@app.get('/internal/clerk/recruiting/approved-pending')
def clerk_recruiting_approved_pending():
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    guild_id=request.args.get('guild_id',type=int)
    rows=fetch_all("""SELECT id,case_number,public_token,discord_user_id,discord_verified_username,status,discord_notified_at,discord_joined_at,discord_join_error,discord_join_last_attempt_at,credentials_sent_at,credentials_delivery_error,credentials_last_attempt_at,personnel_id
                      FROM recruiting_cases
                      WHERE status IN ('REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING')
                        AND discord_user_id IS NOT NULL AND (guild_id=%s OR guild_id IS NULL)
                        AND (
                          (discord_joined_at IS NULL AND (discord_join_last_attempt_at IS NULL OR discord_join_last_attempt_at < NOW()-INTERVAL '5 minutes'))
                          OR (discord_joined_at IS NOT NULL AND personnel_id IS NULL)
                          OR (discord_joined_at IS NOT NULL AND personnel_id IS NOT NULL AND credentials_sent_at IS NULL
                              AND (credentials_last_attempt_at IS NULL OR credentials_last_attempt_at < NOW()-INTERVAL '15 minutes'))
                        )
                      ORDER BY approved_at ASC NULLS FIRST""",(guild_id,))
    return {'ok':True,'cases':rows}


@app.post('/internal/clerk/recruiting/<case_id>/notified')
def clerk_recruiting_notified(case_id):
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    execute('UPDATE recruiting_cases SET discord_notified_at=NOW(),updated_at=NOW() WHERE id=%s',(case_id,))
    return {'ok':True}


@app.post('/internal/clerk/recruiting/converted')
def clerk_recruiting_converted():
    """Compatibility endpoint: link the 201 File but keep the recruit in processing.

    ENLISTED is now filed only when S-1 releases the Soldier from Replacement
    Detachment after training, property, MOS and permanent assignment are complete.
    """
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    data=request.get_json(silent=True) or {}; discord_user_id=data.get('discord_user_id'); personnel_id=data.get('personnel_id')
    if not discord_user_id: return {'ok':False,'error':'discord_user_id required'},400
    case=fetch_one("""SELECT * FROM recruiting_cases WHERE discord_user_id=%s
                      AND status IN ('REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING')
                      ORDER BY approved_at DESC NULLS LAST LIMIT 1""",(discord_user_id,))
    if case and personnel_id:
        execute("""UPDATE recruiting_cases SET status='APPROVED_AWAITING_PROCESSING',personnel_id=%s,
                   replacement_depot_entered_at=COALESCE(replacement_depot_entered_at,NOW()),updated_at=NOW() WHERE id=%s""",
                (personnel_id,case['id']))
    return {'ok':True,'status':'APPROVED_AWAITING_PROCESSING','movement_order_number':None}


@app.get('/internal/clerk/personnel-actions/suspense')
def clerk_personnel_action_suspense():
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    rows=fetch_all("""SELECT pa.id,pa.personnel_id,pa.subject,pa.action_type,pa.owning_section,pa.priority,pa.status,pa.due_date,
                             p.rank_code,p.first_name,p.last_name,p.unit_code,
                             (pa.due_date-CURRENT_DATE)::int AS days_remaining
                      FROM personnel_actions pa LEFT JOIN personnel p ON p.id=pa.personnel_id
                      WHERE pa.status NOT IN ('COMPLETE','CLOSED','DENIED') AND pa.due_date IS NOT NULL
                        AND pa.due_date<=CURRENT_DATE+INTERVAL '2 days'
                      ORDER BY pa.due_date,pa.priority DESC""")
    return {'ok':True,'actions':rows}

@app.get("/health")
def health():
    missing_oauth = []
    if not CONFIG.discord_client_id:
        missing_oauth.append("DISCORD_CLIENT_ID")
    if not CONFIG.discord_client_secret:
        missing_oauth.append("DISCORD_CLIENT_SECRET")
    return {
        "ok": True,
        "site": "5th Cavalry Regiment",
        "database": database_ready(),
        "discord_oauth_configured": _discord_oauth_ready(),
        "discord_oauth_missing": missing_oauth,
        "discord_oauth_redirect_uri": _discord_oauth_redirect_uri(),
    }


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=CONFIG.port, debug=True)
