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
import os
from datetime import date, datetime, timezone, timedelta, time
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo
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

RECRUIT_DISCORD_FALLBACK_URL = "https://discord.gg/BjrSqS7zg"

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
app.secret_key = CONFIG.secret_key
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

app.teardown_appcontext(close_request_connection)


@app.after_request
def prevent_authenticated_record_caching(response):
    """Member/staff records must always reflect the latest authoritative database state."""
    try:
        if session.get("user_id") and str(response.mimetype or "").lower()=="text/html":
            response.headers["Cache-Control"]="no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"]="no-cache"
            response.headers["Expires"]="0"
    except Exception:
        pass
    return response


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
    try:
        reconcile_formation_billets()
    except NameError:
        # Function is defined later during module import; a request/next process boot will reconcile.
        pass
    except Exception:
        log.exception("Formation billet reconciliation failed during bootstrap")
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
    # Recruiting is a public front door. If anything unexpected breaks in this
    # workflow, never strand the applicant on the generic website error page.
    if request.path.startswith("/recruiting"):
        return render_template(
            "recruiting_error.html",
            error_reference=ref,
            discord_invite_url=(CONFIG.discord_invite_url or RECRUIT_DISCORD_FALLBACK_URL),
        ),500
    # Member workspace pages are mission-critical. A data exception must never
    # strand a logged-in Soldier on the generic global error page.
    member_endpoints={"my_soldier_record","my_201_file","my_action_center","my_unit","my_squad","squad_soldier_combat_record","my_platoon_identity","my_company_identity","training","operations"}
    member_paths={"/my-soldier-record","/my-201-file","/my-action-center","/my-unit","/my-squad","/my-platoon","/my-company","/training","/operations"}
    if session.get("user_id") and session.get("access_role") in {"member","nco","company_hq"} and ((request.endpoint or "") in member_endpoints or request.path.rstrip("/") in member_paths):
        try:
            pid=session.get("personnel_id")
            person=fetch_one("SELECT * FROM personnel WHERE id=%s",(pid,)) if pid else None
            if person:
                context=member_record_fallback_context(person,ref)
                context["record_warning"]="A member-workspace module failed to load. Core personnel access has been preserved and Headquarters logged the diagnostic reference."
                return render_template("member_record_core.html",**context),200
        except Exception:
            log.exception("MEMBER GLOBAL RECOVERY FAILURE [%s]",ref)
        return Response(
            "<!doctype html><html><head><meta charset='utf-8'><title>Member Records Recovery</title></head>"
            "<body><main><h1>MEMBER RECORDS — RECOVERY MODE</h1>"
            f"<p>Headquarters logged diagnostic reference <b>{ref}</b>.</p>"
            "<p><a href='/my-soldier-record'>Retry Wall Locker</a> &nbsp; <a href='/logout'>Sign Out</a></p>"
            "</main></body></html>",status=200,mimetype="text/html")
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
    # Official paperwork uses an all-uppercase authority/signature block.
    authority = str(authority or "HEADQUARTERS — 1ST BATTALION, 5TH CAVALRY").upper()
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
         effective_date or date.today(), authority,
         body_text, Json(details or {}), source_key, guild.get("guild_id") if guild else None,
         "BY ORDER OF THE BATTALION COMMANDER" if document_type.upper() in {"REPLACEMENT","ASSIGNMENT","PROMOTION","APPOINTMENT","AWARD","SEPARATION","TOUR EXTENSION"} else "FOR THE COMMANDER",
         authority),
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
    notify_soldier(
        personnel_id, "S-4 SUPPLY", f"M16 issued — Serial {weapon.get('serial_number')}",
        "Your individual M16 has been entered on your property and weapon service record.",
        notification_type="WEAPON", priority="ROUTINE",
        source_key=f"M16-ISSUE-NOTICE:{personnel_id}:{weapon['id']}", target_anchor="weapon",
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
    field_seconds=int(record.get("server_seconds_since_cleaning") or 0)
    field_hours=field_seconds/3600.0
    if field_hours>=15: fouling="UNSERVICEABLE"
    elif field_hours>=10: fouling="HEAVILY FOULED"
    elif field_hours>=5: fouling="LIGHT FOULING"
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
    stages={"SERVICEABLE":0,"TRACE FOULING":1,"FOULED":2,"LIGHT FOULING":2,"HEAVY FOULING":3,"HEAVILY FOULED":3,"CLEANING REQUIRED":3,"MAINTENANCE REQUIRED":4,"UNSERVICEABLE":5}
    image_assets={
        "CLEAN":"art/m16-rifle-master.webp",
        "LIGHT FOULING":"art/m16-rifle-light-fouling.webp",
        "HEAVILY FOULED":"art/m16-rifle-heavily-fouled.webp",
        "UNSERVICEABLE":"art/m16-rifle-unserviceable.webp",
    }
    record.update({"display_state":state,"display_condition_percent":pct,"dirt_stage":stages.get(state,0),"inactive_days":inactive_days,"last_duty_date":last_duty,"fouling_status":fouling,"cleanliness_status":"CLEAN" if field_hours<5 else "DIRTY","neglect_status":neglect,"inspection_status":inspection_label,"serviceability_status":state,"image_asset":image_assets.get(fouling,"art/m16-rifle-master.webp")})
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
    member_welcome_active=False
    try:
        if session.get("user_id") and session.get("access_role") in {"member","nco","company_hq"}:
            lp=linked_personnel()
            member_nco=bool(lp and str(lp.get("rank_code") or "").upper() in NCO_RANKS)
            if lp:
                wp=fetch_one("SELECT status FROM welcome_packets WHERE personnel_id=%s",(lp.get('id'),))
                member_welcome_active=bool(wp and str(wp.get('status') or '').upper() not in {'COMPLETE','CLOSED','ARCHIVED','PENDING_ASSIGNMENT'})
    except Exception:
        member_nco=False
        member_welcome_active=False
    return {
        "site_name": "5th Cavalry Regiment",
        "unit_name": "1st Battalion, 5th Cavalry Regiment",
        "today": date.today(),
        "member_nco": member_nco,
        "member_welcome_active": member_welcome_active,
        "public_system_configured": database_ready(),
    }


COMMAND_ROLES = {"battalion_hq", "commander", "admin"}
NCO_RANKS = {"CPL", "SGT", "SSG", "SFC", "MSG", "1SG", "SGM"}
SQUAD_LEADERSHIP_RANKS = {"CPL", "SGT", "SSG", "SFC"}


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
        "public_recruiting_needs", "public_server_status_api",
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
        "my_weapon_service_history", "my_squad", "squad_soldier_combat_record", "my_platoon_identity", "my_company_identity",
        "my_unit", "my_tour_book", "weapon_history",
        "orders", "training", "operations", "operation_detail",
        "welcome_packet",
    }
    staff_common = {
        "staff_action_center", "staff_personnel_snapshot", "staff_personnel_drawer", "personnel_service_record", "staff_reliability",
        "smart_personnel_search_page", "personnel_compare_page",
        "logout", "home",
    }
    scoped = {
        "s1": staff_common | {
            "staff_batch_action", "staff_soldier_action", "staff_personnel_manage", "staff_assign_soldier", "staff_formation_control",
            "replacement_detachment", "replacement_quick_action", "replacement_batch_action",
            "personnel_action_quick", "s1", "personnel_office", "personnel_service_record",
            "personnel_document", "personnel_document_preview", "morning_report",
            "duty_status_action", "personnel_actions", "document_amendment",
            "personnel_lifecycle_action", "staff_workload_page", "award_recommendation",
            "staff_onboarding", "staff_onboarding_archive", "staff_welcome_packet", "staff_welcome_packet_member_preview",
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
        items.append({"title":f"{replacement.get('program_title','Replacement Training')} — {remaining} requirement{' remains' if remaining == 1 else 's remain'}","section":"S-1 / S-3" if replacement.get("replacement_required") else "S-1","status":"IN PROCESSING"})
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

def _normalized_member_notification(row):
    """Return a safe, canonical member destination for both new and legacy notices.

    Soldier notifications are long-lived records, so old rows may point at routes or
    anchors that existed before the canonical 201 File tabs.  Normalize at read time
    instead of requiring a destructive migration.
    """
    n=dict(row or {})
    kind=str(n.get("notification_type") or "NOTICE").upper()
    title=str(n.get("title") or "").upper()
    endpoint=str(n.get("target_endpoint") or "").strip()
    anchor=str(n.get("target_anchor") or "").strip()

    if kind == "AWARD" or "AWARD" in title or "RIBBON" in title:
        endpoint, anchor = "my_201_file", "awards"
    elif kind in {"PROMOTION","CAREER"} or "PROMOTION" in title:
        endpoint, anchor = "my_201_file", "career"
    elif kind in {"ASSIGNMENT","TRANSFER"} or any(x in title for x in ("ASSIGNMENT","TRANSFER")):
        endpoint, anchor = "my_201_file", "service"
    elif kind in {"ORDER","ORDERS","DOCUMENT"} or "ORDER" in title:
        endpoint, anchor = "my_201_file", "orders"
    elif kind in {"WEAPON","M16"} or "M16" in title:
        endpoint, anchor = "my_201_file", "m16"
    elif kind in {"QUALIFICATION","TRAINING"} or any(x in title for x in ("QUALIFICATION","TRAINING")):
        endpoint, anchor = "my_201_file", "qualifications"
    elif kind in {"READINESS","INACTIVITY"} or any(x in title for x in ("READINESS","INACTIV")):
        endpoint, anchor = "my_201_file", "readiness"
    elif "WELCOME PACKET" in title or kind == "ONBOARDING":
        endpoint, anchor = "welcome_packet", ""
    else:
        allowed={
            "my_soldier_record","my_201_file","my_action_center","my_unit","my_squad",
            "my_weapon_service_history","my_qualification_card","my_tour_book",
            "welcome_packet","orders","training","operations"
        }
        if endpoint not in allowed:
            endpoint="my_201_file"
        legacy={"ribbons":"awards","uniform":"awards","promotion-eligibility":"career",
                "tour":"career","weapon":"m16","arms":"m16","replacement-training":"qualifications",
                "assignment":"service","record":"service","orders-file":"orders"}
        anchor=legacy.get(anchor,anchor)
    n["target_endpoint"]=endpoint
    n["target_anchor"]=anchor or None
    return n

def current_notifications(personnel_id):
    rows=fetch_all("""SELECT * FROM soldier_notifications WHERE personnel_id=%s AND acknowledged_at IS NULL
                        AND (expires_at IS NULL OR expires_at>NOW()) ORDER BY CASE priority WHEN 'URGENT' THEN 0 WHEN 'HIGH' THEN 1 ELSE 2 END,created_at DESC""",(personnel_id,))
    return [_normalized_member_notification(r) for r in rows]

def member_notification_history(personnel_id, limit=30):
    """Recent acknowledged member notices retained as a read-only action history."""
    rows=fetch_all("""SELECT * FROM soldier_notifications
                        WHERE personnel_id=%s AND acknowledged_at IS NOT NULL
                        ORDER BY acknowledged_at DESC, created_at DESC LIMIT %s""",
                   (personnel_id, max(1,min(int(limit or 30),100))))
    return [_normalized_member_notification(r) for r in rows]

def member_landing_endpoint(personnel_id):
    """Choose the simplest post-login destination without allowing onboarding drift to block access."""
    try:
        packet=fetch_one("SELECT status FROM welcome_packets WHERE personnel_id=%s LIMIT 1",(personnel_id,)) or {}
        packet_status=str(packet.get('status') or '').upper()
        # Existing/legacy Soldiers may not have a Welcome Packet at all. They must still be able to log in.
        if not packet:
            return 'dashboard'
        if packet_status not in {'COMPLETE','CLOSED','ARCHIVED'}:
            return 'member_report_for_duty'
    except Exception:
        # Member access is more important than optional onboarding routing. If this lookup ever
        # drifts from production schema again, log it and fall back to the Wall Locker.
        log.exception("Member landing/onboarding lookup failed for %s; falling back to Wall Locker", personnel_id)
        return 'dashboard'
    return 'dashboard'

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
        elif not p.get("platoon"): target="IN PROCESSING"
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
        notify_soldier(pid,"S-1 / S-3",f"{repl.get('program_title','Replacement Training')} — {remaining} requirement{' remains' if remaining == 1 else 's remain'}","Complete the remaining battalion in-processing requirements.",source_key=f"ENTRY-PROCESSING:{pid}",target_anchor="replacement-training")
    inspection=weapon_inspection_status(pid)
    if inspection and inspection["overdue"]:
        notify_soldier(pid,"S-4",f"M16 inspection overdue — Serial {inspection['weapon']['serial_number']}",f"Weapon inspection was due {inspection['due']}.",priority="HIGH",source_key=f"WEAPON-INSP:{pid}:{inspection['due']}",target_anchor="weapon")
    weapon=current_weapon_for(p)
    if weapon:
        condition=str(weapon.get('condition_state') or 'SERVICEABLE').upper()
        if condition in {'HEAVILY FOULED','UNSERVICEABLE'}:
            notify_soldier(
                pid,"S-4",f"M16 requires attention — {condition}",
                "Open your weapon record and complete the required cleaning or coordinate with S-4.",
                notification_type="WEAPON",priority="HIGH",
                source_key=f"WEAPON-CONDITION:{pid}:{weapon.get('id')}:{condition}",target_anchor="weapon")
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
    notify_soldier(
        personnel_id, "S-1 PERSONNEL", f"Promotion orders — {new_rank_code}",
        f"Your permanent service record now reflects promotion from {old_rank or 'no prior rank'} to {new_rank_code} ({rank['rank_name']}).",
        notification_type="PROMOTION", priority="HIGH",
        source_key=f"PROMOTION-NOTICE:{personnel_id}:{new_rank_code}:{eff}", target_anchor="promotion",
    )
    enqueue_discord_role_sync(personnel_id,f'RANK {old_rank}->{new_rank_code}')


def process_appointment_action(personnel_id, appointment_code: str, organization=None, status="PERMANENT", effective_date=None, authority=None, order_number=None, remarks=None, unit_node_id=None, fire_team=None):
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
        (personnel_id,appointment_code,unit_node_id,fire_team,organization,appointment_status,effective_date,authority,order_number,remarks)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (personnel_id, appointment_code, unit_node_id, (fire_team or None), organization, status, eff, authority, order_number, remarks),
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
    notify_soldier(
        personnel_id, "S-1 PERSONNEL", f"Appointment orders — {appt['appointment_name']}",
        f"You have been appointed {appt['appointment_name']}{(' — ' + organization) if organization else ''}.",
        notification_type="APPOINTMENT", priority="HIGH",
        source_key=f"APPOINTMENT-NOTICE:{personnel_id}:{appointment_code}:{eff}", target_anchor="assignment",
    )
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


FORMATION_ORDINALS = {
    "1": "1st", "1ST": "1st", "FIRST": "1st",
    "2": "2nd", "2ND": "2nd", "2D": "2nd", "SECOND": "2nd",
    "3": "3rd", "3RD": "3rd", "3D": "3rd", "THIRD": "3rd",
    "4": "4th", "4TH": "4th", "FOURTH": "4th",
}

def canonical_formation_label(value, kind=None):
    """Return one display spelling for legacy/Discord organization aliases."""
    raw=re.sub(r"\s+"," ",str(value or "").strip())
    if not raw:
        return None
    upper=raw.upper().replace(".","")
    inferred=(kind or "").upper().strip()
    if not inferred:
        if "PLATOON" in upper or re.search(r"\bPLT\b", upper): inferred="PLATOON"
        elif "SQUAD" in upper or re.search(r"\bSQD\b", upper): inferred="SQUAD"
        elif "TEAM" in upper: inferred="TEAM"
    if inferred in {"PLATOON","SQUAD"}:
        token=re.sub(r"\b(?:PLATOON|PLT|SQUAD|SQD)\b","",upper).strip().split(" ")[0] if upper else ""
        ordinal=FORMATION_ORDINALS.get(token)
        if ordinal:
            return f"{ordinal} {inferred.title()}"
    if inferred=="TEAM":
        if "ALPHA" in upper: return "Alpha Team"
        if "BRAVO" in upper: return "Bravo Team"
    return raw.title() if raw.isupper() or raw.islower() else raw

def canonical_company_name(unit_code):
    unit=str(unit_code or "").upper().strip()
    if unit.startswith("A/") or unit.startswith("A-"): return "A Company"
    if unit.startswith("B/") or unit.startswith("B-"): return "B Company"
    if unit.startswith("C/") or unit.startswith("C-"): return "C Company"
    if unit.startswith("HHC"): return "HHC"
    return str(unit_code or "1/5 Cavalry")

app.jinja_env.filters["formation_label"] = canonical_formation_label

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
        "platoon": canonical_formation_label(platoon["display_name"], "PLATOON") if platoon else None,
        "squad": canonical_formation_label(squad["display_name"], "SQUAD") if squad else None,
    }


def format_assignment_node(node_id):
    ancestry = unit_ancestry(node_id)
    if not ancestry:
        return None
    visible = [n["display_name"] for n in reversed(ancestry)
               if n["unit_type"] not in {"Battalion"}]
    return " / ".join(visible)


def onboarding_assignment_gate(personnel_id):
    """Return whether a recruit may receive a permanent formation assignment.

    Approved recruits remain in Replacement Detachment until their Welcome Packet
    is completed by the Soldier AND accepted by Command. Established members with
    no Recruiting Case are not affected. If a Recruiting Case exists but an older
    deployment lost the packet, recreate/resume it instead of silently allowing
    assignment.
    """
    case=fetch_one("""SELECT *, (approved_at IS NOT NULL AND approved_at >= NOW()-INTERVAL '30 days') AS recent_approval
                      FROM recruiting_cases WHERE personnel_id=%s
                      ORDER BY approved_at DESC NULLS LAST,created_at DESC LIMIT 1""",(personnel_id,))
    pkt=fetch_one("SELECT * FROM welcome_packets WHERE personnel_id=%s",(personnel_id,))
    case_status=str((case or {}).get('status') or '').upper()
    recruit_case=bool(case and (case_status in {'APPROVED_AWAITING_DISCORD','REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING'}
                                or (case_status=='ENLISTED' and bool(case.get('recent_approval')))))
    if recruit_case and not pkt:
        try:
            pkt=ensure_welcome_packet(personnel_id,case.get('id'))
        except Exception:
            log.exception('Welcome Packet safeguard recreation failed for %s',personnel_id)
    if not pkt:
        return {'allowed':True,'packet':None,'case':case,'reason':None,'premature_assignment':False}
    status=str(pkt.get('status') or '').upper()
    accepted=bool(status in {'COMPLETE','CLOSED','ARCHIVED'} and pkt.get('approved_at'))
    person=fetch_one("SELECT * FROM personnel WHERE id=%s",(personnel_id,)) or {}
    formation=permanent_formation_state(person)
    premature=bool(formation.get('complete') and not accepted)
    reason=None if accepted else 'WELCOME PACKET MUST BE COMPLETED BY THE SOLDIER AND ACCEPTED BY COMMAND BEFORE PERMANENT COMPANY / PLATOON ASSIGNMENT.'
    return {'allowed':accepted,'packet':pkt,'case':case,'reason':reason,'premature_assignment':premature}


def process_assignment_action(personnel_id, unit_node_id, duty_position=None,
                              effective_date=None, authority=None,
                              order_number=None, remarks=None, fire_team=None):
    person = fetch_one("SELECT * FROM personnel WHERE id=%s", (personnel_id,))
    target = unit_node(unit_node_id)
    if not person or not target:
        raise ValueError("Personnel record or organization not found")

    target_type=str(target.get('unit_type') or '').upper()
    if target_type not in {'COMPANY','PLATOON','SQUAD','HEADQUARTERS','SECTION'}:
        raise ValueError(
            "Personnel assignments must terminate at a Company, Platoon, Squad, Headquarters, or Section."
        )

    gate=onboarding_assignment_gate(personnel_id)
    if not gate.get('allowed'):
        raise ValueError(gate.get('reason') or 'WELCOME PACKET MUST BE ACCEPTED BEFORE PERMANENT ASSIGNMENT.')
    if gate.get('case') and gate.get('packet') and target_type=='COMPANY':
        raise ValueError('NEW REPLACEMENTS MUST BE ASSIGNED THROUGH A PLATOON. SELECT THE AUTHORIZED COMPANY / PLATOON (OR SQUAD) DESTINATION SO MEMBERSHIP, ORDERS, AND DISCORD ROLES COMPLETE TOGETHER.')

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
    # Membership activation is tied to Platoon assignment (or a permanent HQ/Section
    # destination). Company-only assignment remains an in-processing state.
    becomes_member = bool(legacy.get("platoon")) or target_type in {'HEADQUARTERS','SECTION'}
    next_field_status = 'Assigned' if becomes_member else 'Processing'
    next_duty_status = 'PRESENT FOR DUTY' if becomes_member else (person.get('duty_status') or 'IN PROCESSING')
    execute(
        """UPDATE personnel
           SET unit_node_id=%s,unit_code=%s,platoon=%s,squad=%s,
               fire_team=%s,duty_position=%s,field_status=%s,duty_status=%s,updated_at=NOW()
           WHERE id=%s""",
        (unit_node_id, legacy["unit_code"], legacy["platoon"], legacy["squad"],
         fire_team if fire_team is not None else person.get("fire_team"), new_duty,
         next_field_status,next_duty_status,personnel_id),
    )
    execute(
        """INSERT INTO assignment_history
           (personnel_id,unit_node_id,unit_code,platoon,squad,fire_team,duty_position,effective_date,is_current)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,TRUE)""",
        (personnel_id, unit_node_id, legacy["unit_code"], legacy["platoon"],
         legacy["squad"], fire_team if fire_team is not None else person.get("fire_team"), new_duty, eff),
    )

    new_label = format_assignment_node(unit_node_id) or target["display_name"]
    narrative = f"Reassigned from {old_label or 'Replacement / Unassigned'} to {new_label}"
    effective_team = fire_team if fire_team is not None else person.get("fire_team")
    if effective_team:
        narrative += f"; fire team: {effective_team}"
    if new_duty:
        narrative += f"; duty: {new_duty}"
    narrative += "."
    if remarks:
        narrative += f" {remarks}"
    write_service_entry(personnel_id, "ASSIGNMENT", "REASSIGNED", narrative,
                        authority, order_number, eff)
    create_personnel_order(personnel_id, "ASSIGNMENT", "UNIT ASSIGNMENT ORDERS", narrative, effective_date=eff, authority=authority, details={"assignment":new_label,"duty_position":new_duty}, source_key=f"ASSIGNMENT:{personnel_id}:{unit_node_id}:{eff}", document_number=order_number)
    notify_soldier(
        personnel_id, "S-1 PERSONNEL", "New assignment orders filed",
        f"You have been assigned to {new_label}{(' — ' + new_duty) if new_duty else ''}. Open your Soldier Record for the filed orders and current assignment.",
        notification_type="ASSIGNMENT", priority="HIGH",
        source_key=f"ASSIGNMENT-NOTICE:{personnel_id}:{unit_node_id}:{eff}", target_anchor="assignment",
    )
    enqueue_discord_role_sync(personnel_id,f'ASSIGNMENT {old_label}->{new_label}')
    record_automation_event('PERSONNEL','ASSIGNMENT','COMPLETE',f'Authoritative assignment filed: {new_label}.',personnel_id=personnel_id,
                            source_key=f'ASSIGNMENT-AUTO:{personnel_id}',details={'old':old_label,'new':new_label,'member_active':becomes_member})
    if becomes_member and str(person.get('field_status') or '').upper()!='ASSIGNED':
        write_service_entry(personnel_id,'ADMIN','BATTALION MEMBERSHIP ACTIVATED',
            f'Platoon assignment established active 1/5 Cavalry membership: {new_label}.',authority,order_number,eff)
    # Membership/Replacement release occurs at the first real Platoon assignment
    # (or permanent HQ/Section destination), never at Company-only assignment.
    try:
        if 'release_replacement_on_company_assignment' in globals():
            release_replacement_on_company_assignment(personnel_id,authority or 'BATTALION S-1')
    except Exception:
        log.exception('Replacement company-release check failed after assignment for %s',personnel_id)
    try:
        if 'reconcile_welcome_packet' in globals() and fetch_one("SELECT 1 FROM welcome_packets WHERE personnel_id=%s",(personnel_id,)):
            reconcile_welcome_packet(personnel_id)
    except Exception:
        log.exception('Welcome Packet assignment reconciliation failed for %s',personnel_id)
    artifact_state=ensure_assignment_artifacts(personnel_id,authority,eff,remarks)
    return artifact_state



def ensure_assignment_artifacts(personnel_id, authority=None, effective_date=None, remarks=None):
    """Guarantee that the current permanent assignment has all required artifacts."""
    person = fetch_one("SELECT * FROM personnel WHERE id=%s", (personnel_id,))
    if not person or not person.get("unit_node_id"):
        return {"ok": False, "reason": "NO CURRENT STRUCTURED ASSIGNMENT", "document": None, "repaired": []}
    node = unit_node(person.get("unit_node_id"))
    if not node:
        return {"ok": False, "reason": "CURRENT ASSIGNMENT NODE NOT FOUND", "document": None, "repaired": []}
    repaired=[]
    hist = fetch_one("""SELECT * FROM assignment_history WHERE personnel_id=%s AND is_current=TRUE ORDER BY effective_date DESC,created_at DESC LIMIT 1""", (personnel_id,))
    eff = effective_date or (hist or {}).get("effective_date") or date.today()
    legacy = legacy_assignment_from_node(person.get("unit_node_id"))
    fire_team = person.get("fire_team")
    duty = person.get("duty_position")
    if not hist or str(hist.get("unit_node_id") or "") != str(person.get("unit_node_id") or ""):
        execute("UPDATE assignment_history SET is_current=FALSE,ended_date=COALESCE(ended_date,%s) WHERE personnel_id=%s AND is_current=TRUE", (eff, personnel_id))
        execute("""INSERT INTO assignment_history (personnel_id,unit_node_id,unit_code,platoon,squad,fire_team,duty_position,effective_date,is_current) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,TRUE)""", (personnel_id, person.get("unit_node_id"), legacy.get("unit_code"), legacy.get("platoon"), legacy.get("squad"), fire_team, duty, eff))
        repaired.append("ASSIGNMENT HISTORY")
    label = format_assignment_node(person.get("unit_node_id")) or node.get("display_name") or person.get("unit_code") or "1/5 CAV"
    source_key=f"ASSIGNMENT:{personnel_id}:{person.get('unit_node_id')}:{eff}"
    doc = fetch_one("SELECT * FROM personnel_documents WHERE source_key=%s", (source_key,))
    if not doc:
        doc = fetch_one("""SELECT * FROM personnel_documents WHERE personnel_id=%s AND UPPER(COALESCE(document_type,''))='ASSIGNMENT' AND effective_date=%s ORDER BY created_at DESC LIMIT 1""", (personnel_id, eff))
    if not doc:
        narrative=f"Effective {eff}, the Soldier named herein is assigned to {label}"
        if fire_team: narrative += f"; fire team: {fire_team}"
        if duty: narrative += f"; duty: {duty}"
        narrative += "."
        if remarks: narrative += f" {remarks}"
        doc=create_personnel_order(personnel_id,"ASSIGNMENT","UNIT ASSIGNMENT ORDERS",narrative,effective_date=eff,authority=authority,details={"assignment":label,"unit_node_id":str(person.get("unit_node_id")),"duty_position":duty,"fire_team":fire_team},source_key=source_key)
        if doc:
            repaired.append("OFFICIAL ASSIGNMENT ORDERS")
            write_service_entry(personnel_id,"ASSIGNMENT","ASSIGNMENT ORDERS FILED",narrative,authority,doc.get("document_number"),eff)
    if not doc:
        raise RuntimeError("Assignment could not be completed because official orders were not created.")
    notify_soldier(personnel_id,"S-1 PERSONNEL","Permanent assignment filed",f"Your permanent assignment to {label} is on file under {doc.get('document_number')}. Open your 201 File to review the order.",notification_type="ASSIGNMENT",priority="HIGH",source_key=f"ASSIGNMENT-COMPLETE-NOTICE:{personnel_id}:{person.get('unit_node_id')}:{eff}",target_anchor="orders-file")
    enqueue_discord_role_sync(personnel_id, f"ASSIGNMENT ARTIFACT RECONCILE — {label}")
    try:
        release_replacement_on_company_assignment(personnel_id, authority or "BATTALION S-1")
    except Exception:
        log.exception("Replacement release reconciliation failed while ensuring assignment artifacts for %s", personnel_id)
    record_automation_event("PERSONNEL","ASSIGNMENT_ARTIFACTS","COMPLETE",f"Assignment post-condition verified: {label}; orders {doc.get('document_number')}",personnel_id=personnel_id,source_key=f"ASSIGNMENT-ARTIFACTS:{personnel_id}:{person.get('unit_node_id')}:{eff}",details={"assignment":label,"document_number":doc.get("document_number"),"repaired":repaired})
    return {"ok": True, "document": doc, "repaired": repaired, "assignment": label}

def personnel_form_catalogs():
    """Authoritative dropdown values for staff personnel actions."""
    ranks=fetch_all("""SELECT rank_code,rank_name,pay_grade,precedence FROM rank_catalog WHERE is_active=TRUE ORDER BY precedence""")
    mos=fetch_all("""SELECT mos_code,mos_title,category,sort_order FROM battalion_mos_catalog WHERE is_active=TRUE ORDER BY category,sort_order,mos_code""")
    nodes=fetch_all("""SELECT id,parent_id,unit_code,display_name,unit_type,sort_order FROM unit_nodes WHERE is_active=TRUE ORDER BY CASE unit_type WHEN 'Battalion' THEN 0 WHEN 'Company' THEN 1 WHEN 'Headquarters' THEN 2 WHEN 'Section' THEN 3 WHEN 'Platoon' THEN 4 WHEN 'Squad' THEN 5 ELSE 9 END,sort_order,display_name""")
    assignments=[]
    for n in nodes:
        unit_type=str(n.get('unit_type') or '').upper()
        # Permanent formation assignment is available only after Welcome Packet acceptance; Platoon assignment activates membership.
        # Squad and Team may be filed immediately or later.
        if unit_type not in {'COMPANY','PLATOON','SQUAD','HEADQUARTERS','SECTION'}:
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
    fire_teams=[{'value':'','label':'UNASSIGNED / SQUAD HQ'},{'value':'ALPHA TEAM','label':'ALPHA TEAM'},{'value':'BRAVO TEAM','label':'BRAVO TEAM'}]
    duty_statuses=[
        {'value':'PRESENT FOR DUTY','label':'PRESENT FOR DUTY'},
        {'value':'LEAVE','label':'AUTHORIZED LEAVE'},
        {'value':'TEMPORARY DUTY','label':'TEMPORARY DUTY'},
        {'value':'DETACHED','label':'DETACHED'},
        {'value':'NOT PRESENT','label':'NOT PRESENT'},
    ]
    return {'ranks':ranks,'mos_catalog':mos,'organization_nodes':nodes,'assignment_options':assignments,'duty_positions':sorted(duty.values(),key=lambda x:x['label'].upper()),'appointment_catalog':appointments,'billet_catalog':billets,'fire_teams':fire_teams,'duty_statuses':duty_statuses}


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


def record_automation_event(system, event_type, status, summary, *, personnel_id=None, source_key=None, details=None):
    """Write an observable automation event without changing the authoritative personnel record."""
    key=source_key or f"AUTO:{system}:{event_type}:{personnel_id}:{secrets.token_hex(6)}"
    execute("""INSERT INTO automation_ledger(personnel_id,system,event_type,status,summary,details_json,source_key,updated_at,completed_at)
               VALUES(%s,%s,%s,%s,%s,%s,%s,NOW(),CASE WHEN %s IN ('COMPLETE','FAILED','BLOCKED','SUPERSEDED') THEN NOW() ELSE NULL END)
               ON CONFLICT(source_key) DO UPDATE SET status=EXCLUDED.status,summary=EXCLUDED.summary,
                   details_json=EXCLUDED.details_json,updated_at=NOW(),completed_at=EXCLUDED.completed_at""",
            (personnel_id,system,event_type,status,summary,Json(details or {}),key,status))
    return key


def enqueue_discord_role_sync(personnel_id, reason):
    """Website is authoritative; Battalion Clerk consumes this queue and mirrors canonical Discord roles."""
    link=fetch_one("SELECT guild_id,discord_user_id FROM website_member_links WHERE personnel_id=%s",(str(personnel_id),))
    if not link:
        record_automation_event('DISCORD','ROLE_SYNC','BLOCKED','Discord role synchronization blocked: no linked Discord account.',personnel_id=personnel_id,source_key=f'DISCORD-SYNC:{personnel_id}',details={'reason':reason})
        return None
    # A new authoritative reconciliation supersedes historical terminal failures.
    execute("""UPDATE discord_role_sync_queue SET status='SUPERSEDED'
               WHERE personnel_id=%s AND UPPER(COALESCE(status,'')) IN ('FAILED','BLOCKED')""",
            (personnel_id,))
    execute("""INSERT INTO discord_role_sync_queue(personnel_id,guild_id,discord_user_id,reason,status,requested_at)
               VALUES(%s,%s,%s,%s,'PENDING',NOW())
               ON CONFLICT(personnel_id) WHERE status='PENDING'
               DO UPDATE SET guild_id=EXCLUDED.guild_id,discord_user_id=EXCLUDED.discord_user_id,
                             reason=EXCLUDED.reason,requested_at=NOW(),error_text=NULL,processed_at=NULL,
                             attempt_count=0,last_attempt_at=NULL,next_retry_at=NULL""",
            (personnel_id,link.get('guild_id'),link.get('discord_user_id'),reason))
    record_automation_event('DISCORD','ROLE_SYNC','PENDING',f'Discord reconciliation queued: {reason}',personnel_id=personnel_id,source_key=f'DISCORD-SYNC:{personnel_id}',details={'guild_id':str(link.get('guild_id') or ''),'discord_user_id':str(link.get('discord_user_id') or ''),'reason':reason})
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
            tags.append({'code':'M16_MAINTAINED','label':'M16 MAINTAINED','detail':'Your issued M16 is serviceable, recently maintained, and within battalion fouling/inspection standards.'})
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


def hll_field_service_timeline(personnel_id, limit=80):
    """Create immutable-looking timeline entries from the authoritative RCON ledger.

    These rows are derived from hll_player_match_stats rather than manually filed;
    they never grant awards, rank, qualifications, or official operation credit.
    """
    try:
        rows=fetch_all("""SELECT ps.connected_seconds,ps.distance_meters,ps.infantry_kills,ps.deaths,ps.vehicle_kills,
                                 ps.last_role_id,ms.id AS match_id,ms.map_id,ms.map_name,ms.game_mode,ms.started_at
                          FROM hll_player_match_stats ps JOIN hll_match_sessions ms ON ms.id=ps.match_id
                          WHERE ps.personnel_id=%s AND COALESCE(ps.connected_seconds,0)>0
                          ORDER BY ms.started_at DESC LIMIT %s""",(str(personnel_id),limit)) or []
        out=[]
        for r in rows:
            mode=r.get('game_mode') or _hll_mode_from_layer(r.get('map_id')) or 'Field Service'
            seconds=int(r.get('connected_seconds') or 0); km=float(r.get('distance_meters') or 0)/1000
            narrative=(f"RCON-verified HLL: Vietnam service: {seconds//3600}h {(seconds%3600)//60}m, "
                       f"{km:.2f} km traveled, {int(r.get('infantry_kills') or 0)} infantry kills / {int(r.get('deaths') or 0)} deaths")
            if r.get('vehicle_kills'): narrative+=f", {int(r.get('vehicle_kills') or 0)} vehicle kills"
            if r.get('last_role_id'): narrative+=f". Last observed HLL role {r.get('last_role_id')}."
            else: narrative+='.'
            out.append({'id':f"HLL-{r.get('match_id')}",'event_type':'HLL_FIELD_SERVICE','personnel_id':personnel_id,
                        'effective_date':r.get('started_at').date() if r.get('started_at') else date.today(),
                        'title':f"FIELD SERVICE — {r.get('map_name') or r.get('map_id') or 'HLL: VIETNAM'}",
                        'narrative':narrative,'reference_number':str(mode).upper(),'created_at':r.get('started_at')})
        return out
    except Exception:
        return []


def _member_facing_record_text(value):
    text=str(value or '')
    replacements={
        'Discord intake roles established': 'Initial processing established',
        'Discord server': 'battalion communications roster',
        'Discord roles': 'organizational assignments',
        'Discord role': 'organizational assignment',
        'Discord sync': 'record synchronization',
        'DISCORD SYNC': 'RECORD SYNC',
        'Discord': 'battalion communications system',
        'guild': 'unit',
        'Guild': 'Unit',
    }
    for old,new in replacements.items(): text=text.replace(old,new)
    return text


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
    # RCON field-service events are derived evidence, merged chronologically with
    # administrative history without mutating Command-owned personnel records.
    rows=list(rows or []) + hll_field_service_timeline(personnel_id, min(80,limit))
    def _event_sort(r):
        d=r.get('effective_date') or date.min
        c=r.get('created_at') or datetime.min.replace(tzinfo=timezone.utc)
        try:
            if isinstance(c,datetime) and c.tzinfo is None: c=c.replace(tzinfo=timezone.utc)
        except Exception: pass
        return (d,c)
    rows.sort(key=_event_sort)
    for row in rows:
        if isinstance(row,dict):
            for key in ('title','narrative','reference_number'):
                if key in row and row.get(key) is not None:
                    row[key]=_member_facing_record_text(row.get(key))
    return rows[-limit:]


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
    try:
        current_holder=fetch_one("SELECT personnel_id FROM weapon_issue_history WHERE weapon_id=%s AND turned_in_at IS NULL ORDER BY issued_at DESC LIMIT 1",(weapon_id,)) or {}
        hll=hll_service_statistics(current_holder.get('personnel_id')) if current_holder.get('personnel_id') else {}
        rcon_observation={'verified_field_seconds':int(((hll or {}).get('totals') or {}).get('connected_seconds') or 0),
                          'verified_matches':int(((hll or {}).get('totals') or {}).get('matches') or 0),
                          'note':'RCON FIELD-CARRY OBSERVATION ONLY — server play does not automatically add rounds fired or change weapon condition.'}
    except Exception:
        rcon_observation={'verified_field_seconds':0,'verified_matches':0,'note':'RCON field-carry observation unavailable.'}
    return {'weapon':w,'operations_carried':ops,'cleanings':cleanings,'inspections':inspections,'holders':holders,'round_events':events,'maintenance':maintenance,'rcon_observation':rcon_observation}


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
    """Global staff search across the canonical personnel record and its stable identifiers.

    Supports Soldier name, roster/service number, Discord identity, HLL/Steam identity,
    assignment text, MOS/duty, and personnel/order reference numbers.  The personnel
    table remains the authority; linked-system identifiers are only lookup keys.
    """
    q=(query or '').strip()
    if not q: return []
    qlower=q.lower()
    special_words={'not','ready','inactive','inactivity','promotion','eligible','weapon','cleaning','days','day','company'}
    tokens=[x for x in re.findall(r'[a-z0-9_@./:-]+',qlower) if x not in special_words and not x.isdigit()]
    clauses=[]; params=[]
    for token in tokens:
        like=f'%{token}%'
        clauses.append("""(
            LOWER(COALESCE(p.first_name,'')) LIKE %s OR LOWER(COALESCE(p.last_name,'')) LIKE %s OR
            LOWER(COALESCE(p.rank_code,'')) LIKE %s OR LOWER(COALESCE(p.mos_code,'')) LIKE %s OR
            LOWER(COALESCE(p.unit_code,'')) LIKE %s OR LOWER(COALESCE(p.platoon,'')) LIKE %s OR
            LOWER(COALESCE(p.squad,'')) LIKE %s OR LOWER(COALESCE(p.fire_team,'')) LIKE %s OR
            LOWER(COALESCE(p.duty_position,'')) LIKE %s OR LOWER(COALESCE(p.service_number,'')) LIKE %s OR
            LOWER(COALESCE(p.roster_number,'')) LIKE %s OR
            EXISTS(SELECT 1 FROM website_member_links wml LEFT JOIN discord_members dm ON dm.guild_id=wml.guild_id AND dm.discord_user_id=wml.discord_user_id WHERE wml.personnel_id::text=p.id::text AND
                   (LOWER(COALESCE(dm.username,'')) LIKE %s OR LOWER(COALESCE(dm.display_name,'')) LIKE %s OR COALESCE(wml.discord_user_id::text,'') LIKE %s)) OR
            EXISTS(SELECT 1 FROM hll_personnel_links hpl WHERE hpl.personnel_id::text=p.id::text AND
                   (LOWER(COALESCE(hpl.steam_id,'')) LIKE %s OR LOWER(COALESCE(hpl.hll_player_name,'')) LIKE %s OR LOWER(COALESCE(hpl.platform_user_id,'')) LIKE %s)) OR
            EXISTS(SELECT 1 FROM hll_identity_claims hic WHERE hic.personnel_id::text=p.id::text AND
                   (LOWER(COALESCE(hic.claimed_identity,'')) LIKE %s OR LOWER(COALESCE(hic.normalized_identity,'')) LIKE %s)) OR
            EXISTS(SELECT 1 FROM personnel_documents pd WHERE pd.personnel_id=p.id AND
                   LOWER(COALESCE(pd.document_number,'')) LIKE %s)
        )""")
        params += [like]*20
    sql="""SELECT DISTINCT p.*,
      (SELECT dm.username FROM website_member_links wml LEFT JOIN discord_members dm ON dm.guild_id=wml.guild_id AND dm.discord_user_id=wml.discord_user_id WHERE wml.personnel_id::text=p.id::text LIMIT 1) AS discord_username,
      (SELECT hpl.steam_id FROM hll_personnel_links hpl WHERE hpl.personnel_id::text=p.id::text LIMIT 1) AS steam_id,
      (SELECT COALESCE(NULLIF(hpl.platform_user_id,''),hpl.hll_player_name) FROM hll_personnel_links hpl WHERE hpl.personnel_id::text=p.id::text LIMIT 1) AS game_identity
      FROM personnel p WHERE p.separated_at IS NULL AND p.archived=FALSE"""
    if clauses: sql += ' AND ' + ' AND '.join(clauses)
    if 'not ready' in qlower: sql += " AND COALESCE(p.readiness_percent,0)<80"
    if 'inactive' in qlower or 'inactivity' in qlower:
        m=re.search(r'(?:inactive|inactivity)\s+(\d+)',qlower); days=int(m.group(1)) if m else 7
        sql += " AND COALESCE(p.activity_last_seen_at,p.activity_last_duty_at,p.created_at)<NOW()-(%s || ' days')::interval"; params.append(days)
    company_match=re.search(r'\b([abch])\s+company\b',qlower)
    if company_match:
        letter=company_match.group(1).upper(); code='HHC' if letter=='H' else letter+'/'
        sql += " AND UPPER(COALESCE(p.unit_code,'')) LIKE %s"; params.append(code+'%')
    sql += " ORDER BY p.last_name,p.first_name LIMIT %s"; params.append(limit)
    try:
        rows=fetch_all(sql,tuple(params))
    except Exception:
        # Compatibility fallback for older databases that have not yet received every optional identity column/table.
        log.exception('Expanded staff search fell back to core personnel search')
        core_like=f'%{qlower}%'
        rows=fetch_all("""SELECT * FROM personnel WHERE separated_at IS NULL AND archived=FALSE AND (
            LOWER(COALESCE(first_name,'')) LIKE %s OR LOWER(COALESCE(last_name,'')) LIKE %s OR
            LOWER(COALESCE(rank_code,'')) LIKE %s OR LOWER(COALESCE(mos_code,'')) LIKE %s OR
            LOWER(COALESCE(unit_code,'')) LIKE %s OR LOWER(COALESCE(platoon,'')) LIKE %s OR
            LOWER(COALESCE(squad,'')) LIKE %s OR LOWER(COALESCE(duty_position,'')) LIKE %s OR
            LOWER(COALESCE(service_number,'')) LIKE %s OR LOWER(COALESCE(roster_number,'')) LIKE %s)
            ORDER BY last_name,first_name LIMIT %s""", tuple([core_like]*10+[limit]))
    if 'weapon cleaning' in qlower:
        rows=[r for r in rows if (lambda w: bool(w and float(w.get('field_hours_since_cleaning') or 0)>=5))(current_weapon_for(r))]
    if 'promotion eligible' in qlower:
        filtered=[]
        for r in rows:
            try:
                if any(x.get('eligible') for x in promotion_eligibility(soldier_view(r))): filtered.append(r)
            except Exception: pass
        rows=filtered
    return rows


def automation_reliability_report():
    """Exception-oriented end-to-end audit of the battalion's automation contract.

    This never mutates personnel.  Each row answers one operational question and links
    staff to the authoritative work center that can resolve the exception.
    """
    rows=[]
    def add(name, endpoint, description, sql=None, params=(), healthy_when_zero=True, state_override=None, count_override=None):
        count=count_override
        state=state_override
        detail=description
        if count is None and sql:
            try: count=int((fetch_one(sql,params) or {'total':0}).get('total') or 0)
            except Exception:
                log.exception('Reliability audit failed: %s',name); count=0; state='CHECK FAILED'
        if state is None:
            state='CURRENT' if ((count or 0)==0 if healthy_when_zero else bool(count)) else 'ACTION REQUIRED'
        rows.append({'name':name,'state':state,'count':int(count or 0),'detail':detail,'endpoint':endpoint})
    add('APPLICATION APPROVAL','recruiting_control','Approved cases must resolve to one personnel record and a completed accession state.',"""SELECT COUNT(*) total FROM recruiting_cases rc WHERE rc.status IN ('ENLISTED','APPROVED','COMPLETE') AND (rc.personnel_id IS NULL OR NOT EXISTS(SELECT 1 FROM personnel p WHERE p.id=rc.personnel_id))""")
    add('STEAM / GAMERTAG LINKING','hll_telemetry_lab_page','Assigned members should have one verified or pending game identity path. Replacement Detachment personnel are not treated as failures while their identity is still being processed.',"""SELECT COUNT(*) total FROM personnel p WHERE p.archived=FALSE AND p.separated_at IS NULL AND UPPER(COALESCE(p.field_status,''))='ASSIGNED' AND NOT EXISTS(SELECT 1 FROM hll_personnel_links h WHERE h.personnel_id::text=p.id::text) AND NOT EXISTS(SELECT 1 FROM hll_identity_claims c WHERE c.personnel_id::text=p.id::text AND UPPER(COALESCE(c.status,'')) IN ('PENDING','VERIFIED','LINKED'))""")
    add('DISCORD SYNC','personnel_sync_control','Failed/blocked synchronization jobs require staff review; pending work is allowed to settle.',"""SELECT COUNT(*) total FROM (SELECT DISTINCT ON (personnel_id) personnel_id,status FROM discord_role_sync_queue ORDER BY personnel_id,requested_at DESC) latest WHERE UPPER(COALESCE(status,'')) IN ('FAILED','BLOCKED')""")
    add('ASSIGNMENT CHANGES','personnel_actions','Open assignment actions past their due date indicate an incomplete personnel transaction.',"""SELECT COUNT(*) total FROM personnel_actions WHERE UPPER(COALESCE(action_type,'')) IN ('ASSIGNMENT','TRANSFER','REASSIGNMENT') AND status NOT IN ('COMPLETE','CLOSED','DENIED') AND due_date<CURRENT_DATE""")
    add('PROMOTIONS','personnel_actions','Promotion actions should close after the personnel record, order, and member notice are filed.',"""SELECT COUNT(*) total FROM personnel_actions WHERE UPPER(COALESCE(action_type,'')) LIKE '%%PROMOT%%' AND status NOT IN ('COMPLETE','CLOSED','DENIED') AND due_date<CURRENT_DATE""")
    add('AWARDS','awards_decorations','Every filed award must carry its order number and citation.',"""SELECT COUNT(*) total FROM personnel_awards WHERE COALESCE(BTRIM(order_number),'')='' OR COALESCE(BTRIM(citation),'')=''""")
    add('MEMBER ACTIONS','personnel_actions','Expired member notices and unresolved generated requirements are surfaced for cleanup.',"""SELECT COUNT(*) total FROM soldier_notifications WHERE acknowledged_at IS NULL AND expires_at IS NOT NULL AND expires_at<NOW()""")
    add('WELCOME PACKET','staff_onboarding','Packets stalled more than 24 hours after activity require review.',"""SELECT COUNT(*) total FROM welcome_packets WHERE status NOT IN ('COMPLETE','CLOSED','ARCHIVED') AND updated_at<NOW()-INTERVAL '24 hours'""")
    add('M16 FOULING / CLEANING','arms_room','Current M16 issues must have a single issue record and a coherent maintenance/field-service ledger.',"""SELECT COUNT(*) total FROM (SELECT personnel_id FROM weapon_issue_history WHERE is_current=TRUE GROUP BY personnel_id HAVING COUNT(*)>1) x""")
    add('MOS PROGRESSION','training','The current MOS proficiency grade must derive from verified HLL role service. Historical legacy grades remain in the service record and are not treated as live automation failures.',"""SELECT COUNT(*) total FROM personnel_mos_proficiency pmp JOIN personnel p ON p.id=pmp.personnel_id WHERE p.archived=FALSE AND p.separated_at IS NULL AND COALESCE(pmp.is_current,TRUE)=TRUE AND UPPER(COALESCE(p.mos_code,'')) NOT IN ('','00','00R','PENDING','UNASSIGNED') AND pmp.proficiency_order>0 AND UPPER(COALESCE(pmp.certified_by,''))<>'HLL SERVER SERVICE SYSTEM'""")
    add('READINESS / INACTIVITY','personnel_office','Assigned members who have been on the roster long enough for activity tracking need verified HLL server activity. Discord voice timestamps do not satisfy this check.',"""SELECT COUNT(*) total FROM personnel p WHERE p.archived=FALSE AND p.separated_at IS NULL AND UPPER(COALESCE(p.field_status,''))='ASSIGNED' AND COALESCE(p.roster_entered_at,p.date_joined,p.created_at::date) <= CURRENT_DATE-14 AND NOT EXISTS(SELECT 1 FROM hll_player_match_stats ps WHERE ps.personnel_id::text=p.id::text)""")
    add('OPERATIONS CREDIT','operations','Published active operations must be connected to Battalion Clerk before automated credit is trusted.',"""SELECT COUNT(*) total FROM operations WHERE UPPER(COALESCE(status,'')) NOT IN ('CANCELLED','CANCELED','CLOSED','COMPLETE','COMPLETED','ARCHIVED','DELETED') AND UPPER(COALESCE(publish_status,'DRAFT'))='PUBLISHED' AND clerk_event_id IS NULL""")
    raw=hll_live_server_snapshot() or {}
    heartbeat=raw.get('last_success_at') or raw.get('updated_at')
    stale=True
    try:
        if heartbeat:
            hb=heartbeat if getattr(heartbeat,'tzinfo',None) else heartbeat.replace(tzinfo=timezone.utc)
            stale=(datetime.now(timezone.utc)-hb).total_seconds()>120
    except Exception: stale=True
    add('SERVER TELEMETRY','hll_telemetry_lab_page','Collector heartbeat must remain fresh for server service, MOS, readiness, and M16 evidence.',state_override='ACTION REQUIRED' if stale else 'CURRENT',count_override=1 if stale else 0)
    career=fetch_one("SELECT * FROM career_reconciliation_status WHERE id=1") if database_ready() else None
    career_stale=True
    try:
        if career and career.get('last_run_at'):
            stamp=career['last_run_at'] if getattr(career['last_run_at'],'tzinfo',None) else career['last_run_at'].replace(tzinfo=timezone.utc)
            career_stale=(datetime.now(timezone.utc)-stamp).total_seconds()>900 or int(career.get('error_count') or 0)>0
    except Exception:
        career_stale=True
    add('CAREER PROGRESSION','training','The five-minute progression worker keeps verified HLL service synchronized into readiness, MOS proficiency, ribbon eligibility and promotion evidence.',state_override='ACTION REQUIRED' if career_stale else 'CURRENT',count_override=(int((career or {}).get('error_count') or 0) if career else 1))
    return rows


def automation_reliability_details():
    """Human-readable exception detail for the reliability board.

    Only live, actionable records are included. Replacement/onboarding records and
    historical MOS grades are deliberately excluded so the board does not manufacture
    work from normal accession/history data.
    """
    details={'identity':[],'mos':[],'readiness':[]}
    try:
        details['identity']=fetch_all("""
            SELECT p.id,p.rank_code,p.first_name,p.last_name,p.unit_code,p.platoon,p.squad
            FROM personnel p
            WHERE p.archived=FALSE AND p.separated_at IS NULL
              AND UPPER(COALESCE(p.field_status,''))='ASSIGNED'
              AND NOT EXISTS(SELECT 1 FROM hll_personnel_links h WHERE h.personnel_id::text=p.id::text)
              AND NOT EXISTS(SELECT 1 FROM hll_identity_claims c WHERE c.personnel_id::text=p.id::text
                             AND UPPER(COALESCE(c.status,'')) IN ('PENDING','VERIFIED','LINKED'))
            ORDER BY p.last_name,p.first_name
        """) or []
    except Exception:
        log.exception('Reliability identity detail unavailable')
    try:
        details['mos']=fetch_all("""
            SELECT p.id,p.rank_code,p.first_name,p.last_name,p.mos_code,pmp.proficiency_level,pmp.certified_by
            FROM personnel_mos_proficiency pmp JOIN personnel p ON p.id=pmp.personnel_id
            WHERE p.archived=FALSE AND p.separated_at IS NULL AND COALESCE(pmp.is_current,TRUE)=TRUE
              AND UPPER(COALESCE(p.mos_code,'')) NOT IN ('','00','00R','PENDING','UNASSIGNED')
              AND pmp.proficiency_order>0
              AND UPPER(COALESCE(pmp.certified_by,''))<>'HLL SERVER SERVICE SYSTEM'
            ORDER BY p.last_name,p.first_name
        """) or []
    except Exception:
        log.exception('Reliability MOS detail unavailable')
    try:
        details['readiness']=fetch_all("""
            SELECT p.id,p.rank_code,p.first_name,p.last_name,p.unit_code,p.platoon,p.squad,
                   p.roster_entered_at,p.date_joined
            FROM personnel p
            WHERE p.archived=FALSE AND p.separated_at IS NULL
              AND UPPER(COALESCE(p.field_status,''))='ASSIGNED'
              AND COALESCE(p.roster_entered_at,p.date_joined,p.created_at::date) <= CURRENT_DATE-14
              AND NOT EXISTS(SELECT 1 FROM hll_player_match_stats ps WHERE ps.personnel_id::text=p.id::text)
            ORDER BY p.last_name,p.first_name
        """) or []
    except Exception:
        log.exception('Reliability readiness detail unavailable')
    return details


def staff_server_seed_snapshot():
    snap=public_hll_server_snapshot()
    players=int(snap.get('player_count') or 0); threshold=max(1,int(os.getenv('HLL_SEED_READY_PLAYERS','40') or 40))
    if not snap.get('online'): state='OFFLINE'
    elif players>=threshold: state='READY TO LAUNCH'
    else: state='SEEDING'
    return {**snap,'seed_state':state,'seed_threshold':threshold,'players_needed':max(threshold-players,0)}

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
                if load: reasons.append(f"{load} recent leadership assignment{'s' if load != 1 else ''}")
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
    server_activity=inactivity_snapshot(p)
    server_state=str(server_activity.get('state') or '').upper()
    # WATCH is an informational readiness state, not an incomplete task.  A Soldier
    # can be below the 2H/7-day READY target without having an S-1 deficiency.  Only
    # the actual inactivity escalation states belong in ACTION REQUIRED.  This also
    # means the card disappears automatically on the first request after verified
    # HLL time brings the Soldier back below the escalation threshold.
    if server_state in {'AT RISK','INACTIVE','ADMIN REVIEW'}:
        detail=(f"{server_activity.get('time_7d','0H 00M')} verified server time in the last 7 days")
        detail += f" • {int(server_activity.get('days') or 0)} days since last verified server activity"
        items.append({'section':'S-1','title':f'SERVER ACTIVITY — {server_state}','detail':detail,'priority':'HIGH' if server_state in {'INACTIVE','ADMIN REVIEW'} else 'WATCH','target':'my_201_file','anchor':'readiness'})
    weapon=current_weapon_for(p)
    if weapon and int(weapon.get('rounds_since_cleaning') or 0)>=300:
        items.append({'section':'S-4','title':'M16 CLEANING REQUIRED','detail':f"{int(weapon.get('rounds_since_cleaning') or 0)} rounds since last cleaning. Clean the issued rifle from the Wall Locker.",'priority':'HIGH' if int(weapon.get('rounds_since_cleaning') or 0)>=600 else 'WATCH','target':'my_weapon_service_history','anchor':None})
    insp=weapon_inspection_status(pid)
    # Coming-due inspections are forecasts, not member-clearable actions.  Keep the
    # ACTION REQUIRED card for an inspection only after it is actually overdue.
    # Once S-4 records the inspection, weapon_inspection_status() immediately points
    # at the new next-due date and the card drops off on refresh.
    if insp and insp.get('overdue'):
        overdue_days=max(1,abs(int(insp.get('days') or -1)))
        items.append({'section':'S-4','title':'M16 INSPECTION REQUIRED','detail':f"Inspection overdue by {overdue_days} day{'s' if overdue_days != 1 else ''}. Coordinate with S-4 for inspection.",'priority':'HIGH','target':'my_weapon_service_history','anchor':None})
    exp=fetch_one("""SELECT MIN(expires_at) due FROM qualifications WHERE personnel_id=%s AND expires_at BETWEEN CURRENT_DATE AND CURRENT_DATE+30""",(pid,))
    dexp=fetch_one("""SELECT MIN(expiration_date) due FROM personnel_duty_qualifications WHERE personnel_id=%s AND expiration_date BETWEEN CURRENT_DATE AND CURRENT_DATE+30""",(pid,))
    dates=[x.get('due') for x in [exp or {},dexp or {}] if x.get('due')]
    if dates:
        due=min(dates); items.append({'section':'S-3','title':'QUALIFICATION EXPIRATION','detail':f'{(due-date.today()).days} days remaining.','priority':'WATCH','target':'training','anchor':None})
    next_op=fetch_one("SELECT operation_number,title,start_at FROM operations WHERE start_at>NOW() AND UPPER(COALESCE(status,'')) NOT IN ('CANCELLED','CLOSED') ORDER BY start_at LIMIT 1")
    if next_op:
        items.append({'section':'S-3','title':'UPCOMING OPERATION','detail':f"{next_op.get('operation_number') or ''} {next_op.get('title')} — {next_op.get('start_at')}",'priority':'ROUTINE','target':'operations','anchor':None})
    tour=member_tour_phase(p)
    if tour.get('days_to_deros') is not None:
        items.append({'section':'S-1','title':'DEROS','detail':f"{tour.get('days_to_deros')} days remaining in current tour.",'priority':'WATCH' if tour.get('days_to_deros')<=60 else 'ROUTINE','target':'my_201_file','anchor':'tour'})
    # Promotion gates belong in the 201 File career/progression display.  Many are
    # passive conditions (especially time in grade), so they must never appear as
    # dismissible/completable ACTION REQUIRED tasks.  The promotion panel remains
    # live and will update from promotion_eligibility() as evidence accrues.
    return items


def next_recommended_action(person):
    items=member_personal_action_center(person)
    return items[0] if items else {'section':'HEADQUARTERS','title':'MAINTAIN READINESS','detail':'No immediate deficiency is on file. Continue unit participation and maintain current qualifications.','priority':'ROUTINE'}


MEMBER_DUTY_ENDPOINTS = {
    'my_soldier_record','welcome_packet','my_201_file','orders','training','operations',
    'my_squad','my_unit','my_weapon_service_history','my_qualification_card','my_tour_book'
}

def _member_duty_requirement_open(person, title):
    """Return False when a known Duty Desk requirement has already been satisfied.

    This keeps stale notifications from surviving after the authoritative record changes.
    Unknown notices remain visible until acknowledged/expired.
    """
    if not person: return False
    p=soldier_view(person); pid=p.get('id'); upper=str(title or '').upper()
    try:
        if 'M16 INSPECTION' in upper:
            st=weapon_inspection_status(pid)
            return bool(st and st.get('overdue'))
        if 'SERVER ACTIVITY' in upper:
            state=str((inactivity_snapshot(p) or {}).get('state') or '').upper()
            return state in {'AT RISK','INACTIVE','ADMIN REVIEW'}
        if 'M16' in upper and ('CLEAN' in upper or 'CLEANING' in upper):
            w=current_weapon_for(p)
            return bool(w and int(w.get('rounds_since_cleaning') or 0) >= 300)
        if 'QUALIFICATION' in upper and ('EXPIR' in upper or 'DUE' in upper):
            exp=fetch_one("SELECT 1 ok FROM qualifications WHERE personnel_id=%s AND expires_at BETWEEN CURRENT_DATE AND CURRENT_DATE+30 LIMIT 1",(pid,))
            dexp=fetch_one("SELECT 1 ok FROM personnel_duty_qualifications WHERE personnel_id=%s AND expiration_date BETWEEN CURRENT_DATE AND CURRENT_DATE+30 LIMIT 1",(pid,))
            return bool(exp or dexp)
        if 'NEXT PROMOTION REQUIREMENT' in upper or 'PROMOTION REQUIREMENT' in upper:
            # Legacy promotion reminders were incorrectly filed as Duty Desk actions.
            # Career progression remains visible in the 201 File instead.
            return False
        if 'WELCOME PACKET' in upper:
            wp=welcome_packet_context(pid) or {}; packet=wp.get('packet') or {}
            return bool(packet and str(packet.get('status') or '').upper()!='COMPLETE')
    except Exception:
        return True
    return True


def member_duty_desk(person):
    """Read-only attention layer for the Wall Locker.

    This deliberately does not create or modify personnel, training, operations, weapon,
    onboarding, or Discord records. It only points the Soldier at the existing
    authoritative system that owns the action.
    """
    if not person:
        return {'items': [], 'count': 0, 'all_clear': True}
    p=soldier_view(person); pid=p['id']; items=[]; seen=set()
    def add(title, detail, section, endpoint, anchor=None, priority='ROUTINE', kind='ACTION'):
        key=(str(title or '').strip().upper(), str(endpoint or ''))
        if not title or key in seen: return
        seen.add(key)
        endpoint=endpoint if endpoint in MEMBER_DUTY_ENDPOINTS else 'my_soldier_record'
        items.append({'title':title,'detail':detail or '', 'section':section or 'HEADQUARTERS',
                      'endpoint':endpoint,'anchor':anchor,'priority':priority or 'ROUTINE','kind':kind})
    try:
        wp=welcome_packet_context(pid)
        packet=wp.get('packet') if isinstance(wp,dict) else None
        if packet and str(packet.get('status') or '').upper()!='COMPLETE':
            add('WELCOME PACKET — ACTION REQUIRED',f"{wp.get('percent',0)}% complete • {str(packet.get('status') or 'IN PROGRESS').replace('_',' ')}",
                'S-1','welcome_packet',priority='HIGH',kind='ONBOARDING')
    except Exception:
        log.exception('Member Duty Desk welcome-packet read failed for %s',pid)
    try:
        for n in current_notifications(pid)[:6]:
            title=n.get('title') or 'HEADQUARTERS NOTICE'
            if not _member_duty_requirement_open(p,title):
                continue
            endpoint=str(n.get('target_endpoint') or 'my_soldier_record')
            anchor=n.get('target_anchor')
            upper_title=str(title).upper()
            # Legacy notices were filed against Soldier Record anchors that no
            # longer exist. Route them into the authoritative 201 File section.
            if 'ASSIGNMENT ORDERS' in upper_title or str(n.get('notification_type') or '').upper()=='ASSIGNMENT':
                endpoint='my_201_file'; anchor='assignment'
            elif 'OFFICIAL DOCUMENT' in upper_title or str(n.get('notification_type') or '').upper()=='ORDERS':
                endpoint='my_201_file'; anchor='orders-file'
            before=len(items)
            add(title,n.get('message') or '',n.get('section') or 'HEADQUARTERS',
                endpoint,anchor,n.get('priority') or 'ROUTINE','NOTICE')
            if len(items)>before:
                items[-1]['notification_id']=n.get('id')
    except Exception:
        log.exception('Member Duty Desk notification read failed for %s',pid)
    try:
        for a in member_personal_action_center(p):
            title=str(a.get('title') or '')
            endpoint=a.get('target') or 'my_soldier_record'; anchor=a.get('anchor')
            upper=title.upper()
            if 'M16' in upper and 'CLEAN' in upper:
                endpoint='my_weapon_service_history'; anchor=None
            elif 'M16' in upper:
                endpoint='my_weapon_service_history'; anchor=None
            elif 'QUALIFICATION' in upper or 'TRAINING' in upper:
                endpoint='training'; anchor=None
            elif 'OPERATION' in upper:
                endpoint='operations'; anchor=None
            elif 'PROMOTION' in upper:
                endpoint='my_201_file'; anchor='promotion-eligibility'
            add(title,a.get('detail'),a.get('section'),endpoint,anchor,a.get('priority'),'ACTION')
    except Exception:
        log.exception('Member Duty Desk action read failed for %s',pid)
    return {'items':items[:7],'count':len(items),'all_clear':not bool(items)}


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
    # Fire-team Soldiers report first to their Team Leader when one is formally appointed
    # to the same squad and carries the same Alpha/Bravo fire-team assignment.
    if squad and personnel.get("fire_team"):
        team_leader = fetch_one(
            """SELECT p.*,pa.organization,pa.effective_date AS appointment_effective_date
               FROM personnel_appointments pa
               JOIN personnel p ON p.id=pa.personnel_id
               WHERE pa.appointment_code='FTL' AND pa.is_current=TRUE
                 AND pa.unit_node_id=%s AND UPPER(COALESCE(p.fire_team,''))=UPPER(%s)
                 AND p.separated_at IS NULL AND COALESCE(p.archived,FALSE)=FALSE
               ORDER BY pa.effective_date DESC LIMIT 1""",
            (squad["id"], personnel.get("fire_team")),
        )
        if team_leader and str(team_leader["id"]) != str(personnel["id"]):
            team_leader["chain_title"] = "Team Leader"
            chain.append(team_leader)
    checks = [
        ("Squad Leader","SL", squad),
        ("Platoon Sergeant","PSG", platoon),
        ("Platoon Leader","PL", platoon),
        ("First Sergeant","CO_1SG", company),
        ("Company Commander","CO_CO", company),
        ("Battalion Sergeant Major","BN_SGM", None),
        ("Battalion Commander","BN_CO", None),
    ]
    seen = {str(x["id"]) for x in chain}
    for title, code, node in checks:
        leader = appointment_for_node(code, node["id"] if node else None)
        if leader and str(leader["id"]) != str(personnel["id"]) and str(leader["id"]) not in seen:
            leader["chain_title"] = title
            chain.append(leader)
            seen.add(str(leader["id"]))
    return chain


def _rank_order_sql(alias="p"):
    """Canonical rank-precedence ordering for every personnel roster."""
    return f"COALESCE(rc.precedence,0) DESC, {alias}.last_name, {alias}.first_name"


def current_field_leadership_appointment(personnel):
    """Return the Soldier's most relevant current field-leadership billet."""
    if not personnel:
        return None
    rows=fetch_all(
        """SELECT pa.*,ac.appointment_name,ac.echelon,ac.sort_order
           FROM personnel_appointments pa
           JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code
           WHERE pa.personnel_id=%s AND pa.is_current=TRUE
             AND pa.appointment_code IN ('PSG','SL','ASST_SL','FTL')
           ORDER BY CASE pa.appointment_code WHEN 'PSG' THEN 1 WHEN 'SL' THEN 2 WHEN 'ASST_SL' THEN 3 WHEN 'FTL' THEN 4 ELSE 9 END,
                    pa.effective_date DESC,pa.created_at DESC""",
        (personnel['id'],),
    ) or []
    return rows[0] if rows else None


def member_squad_scope(personnel):
    """Resolve what MY SQUAD should display from billet first, assignment second."""
    if not personnel:
        return None, None, None
    appt=current_field_leadership_appointment(personnel)
    if appt and appt.get('unit_node_id'):
        node=unit_node(appt['unit_node_id'])
        if node:
            code=appt.get('appointment_code')
            if code=='PSG':
                platoon=node if str(node.get('unit_type') or '').lower()=='platoon' else next((n for n in unit_ancestry(node['id']) if str(n.get('unit_type') or '').lower()=='platoon'),None)
                return platoon, None, appt
            squad=node if str(node.get('unit_type') or '').lower()=='squad' else next((n for n in unit_ancestry(node['id']) if str(n.get('unit_type') or '').lower()=='squad'),None)
            if squad:
                team=(appt.get('fire_team') or personnel.get('fire_team') or '').upper().strip() if code=='FTL' else None
                return squad, team or None, appt
    if personnel.get('unit_node_id'):
        ancestry=unit_ancestry(personnel['unit_node_id'])
        squad=next((n for n in ancestry if str(n.get('unit_type') or '').lower()=='squad'),None)
        if squad:
            return squad, None, appt
    return None, None, appt


def squad_roster_for(personnel):
    target, action_team, appt = member_squad_scope(personnel)
    if not target:
        return [], None, None, appt, set()
    ids=unit_descendant_ids(target['id']) or [target['id']]
    rows=fetch_all("""SELECT p.*,COALESCE(rc.precedence,0) AS rank_precedence
                       FROM personnel p
                       LEFT JOIN rank_catalog rc ON rc.rank_code=p.rank_code
                       WHERE p.unit_node_id=ANY(%s) AND p.separated_at IS NULL
                         AND COALESCE(p.archived,FALSE)=FALSE
                       ORDER BY COALESCE(rc.precedence,0) DESC,p.last_name,p.first_name""",(ids,)) or []
    actionable=set()
    code=(appt or {}).get('appointment_code')
    if code in {'SL','ASST_SL','PSG'}:
        actionable={str(x.get('id')) for x in rows}
    elif code=='FTL' and action_team:
        actionable={str(x.get('id')) for x in rows if str(x.get('fire_team') or '').upper().strip()==action_team}
    return rows,target,action_team,appt,actionable


def scoped_personnel_for(personnel):
    """Personnel a leader should see in the legacy leader scope helper."""
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
    # A Team Leader owns only his Alpha/Bravo team. Other NCO leadership appointments
    # retain their normal squad/platoon/company scope.
    active_ftl = next((x for x in current_appts if x["appointment_code"]=="FTL" and x.get("unit_node_id") and str(x.get("unit_node_id"))==str(scope_node.get("id"))), None)
    team_filter = (personnel.get("fire_team") or '').upper().strip() if active_ftl else ''
    if team_filter:
        soldiers = fetch_all(
            """SELECT p.*,
                      wi.serial_number AS weapon_serial,
                      wi.condition_state AS weapon_condition,
                      wi.condition_percent AS weapon_percent
               FROM personnel p
               LEFT JOIN weapon_issue_history wih ON wih.personnel_id=p.id AND wih.is_current=TRUE
               LEFT JOIN weapon_inventory wi ON wi.id=wih.weapon_id
               WHERE p.unit_node_id = ANY(%s) AND UPPER(COALESCE(p.fire_team,''))=%s
               ORDER BY COALESCE((SELECT precedence FROM rank_catalog rc WHERE rc.rank_code=p.rank_code),0) DESC,p.last_name,p.first_name""",
            (ids,team_filter),
        )
        scope_node={**scope_node,'display_name':f"{scope_node.get('display_name')} — {team_filter}",'fire_team':team_filter}
    else:
        soldiers = fetch_all(
            """SELECT p.*,
                      wi.serial_number AS weapon_serial,
                      wi.condition_state AS weapon_condition,
                      wi.condition_percent AS weapon_percent
               FROM personnel p
               LEFT JOIN weapon_issue_history wih ON wih.personnel_id=p.id AND wih.is_current=TRUE
               LEFT JOIN weapon_inventory wi ON wi.id=wih.weapon_id
               WHERE p.unit_node_id = ANY(%s)
               ORDER BY COALESCE((SELECT precedence FROM rank_catalog rc WHERE rc.rank_code=p.rank_code),0) DESC,p.last_name,p.first_name""",
            (ids,),
        )
    return soldiers, scope_node


def authorized_absence_active(person):
    """Pause inactivity penalties for filed/authorized absences."""
    if not person:
        return False
    duty = str(person.get("duty_status") or "").upper().strip()
    lifecycle = str(person.get("lifecycle_state") or "").upper().strip()
    excused_states = {"LEAVE", "AUTHORIZED LEAVE", "LOA", "TAD", "TEMPORARY DUTY", "EXCUSED ABSENCE"}
    if duty not in excused_states and lifecycle not in excused_states:
        return False
    expected = person.get("loa_expected_return_date")
    if isinstance(expected, str):
        try:
            expected = date.fromisoformat(expected)
        except Exception:
            expected = None
    return expected is None or expected >= date.today()


def inactivity_thresholds_for_person(person):
    """Server-authoritative inactivity thresholds.

    The unit standard is intentionally fixed around verified HLL: Vietnam server
    participation: 14 days = AT RISK, 21 = INACTIVE, 30 = ADMIN REVIEW.
    The 7-day READY requirement is handled separately as two verified server hours.
    """
    return {"warning":7, "s1":14, "property":21, "command":30}


def _format_server_duration(seconds):
    seconds=max(0,int(seconds or 0))
    hours, rem=divmod(seconds,3600)
    minutes=rem//60
    return f"{hours}H {minutes:02d}M"


def server_activity_snapshot(person):
    """Return the authoritative inactivity/readiness activity state from HLL telemetry."""
    empty={
        "linked":False,"seconds_7d":0,"hours_7d":0.0,"time_7d":"0H 00M",
        "last_activity":None,"days":0,"days_since_activity":0,"source":"HLL: VIETNAM SERVER",
        "state":"WATCH","next_label":"AT RISK","days_to_next":14,
        "excused":authorized_absence_active(person),"expected_return":person.get("loa_expected_return_date") if person else None,
    }
    if not person or not person.get("id"):
        return empty
    if empty["excused"]:
        empty.update({"state":"EXCUSED ABSENCE","next_label":None,"days_to_next":None})
    try:
        link=fetch_one("SELECT steam_id FROM hll_personnel_links WHERE personnel_id=%s LIMIT 1",(str(person["id"]),))
        empty["linked"]=bool(link and link.get("steam_id"))
        if empty["linked"]:
            row=fetch_one("""SELECT
                    COALESCE(SUM(CASE WHEN COALESCE(ps.last_seen_at,ms.last_seen_at,ms.ended_at,ms.started_at) >= NOW()-INTERVAL '7 days'
                                      THEN COALESCE(ps.connected_seconds,0) ELSE 0 END),0) AS seconds_7d,
                    MAX(COALESCE(ps.last_seen_at,ms.last_seen_at,ms.ended_at,ms.started_at)) AS last_activity
                FROM hll_player_match_stats ps
                LEFT JOIN hll_match_sessions ms ON ms.id=ps.match_id
                WHERE ps.personnel_id=%s""",(str(person["id"]),)) or {}
            empty["seconds_7d"]=int(row.get("seconds_7d") or 0)
            empty["hours_7d"]=round(empty["seconds_7d"]/3600.0,2)
            empty["time_7d"]=_format_server_duration(empty["seconds_7d"])
            empty["last_activity"]=row.get("last_activity")
    except Exception:
        log.exception("Server inactivity telemetry unavailable for %s",person.get("id"))

    stamp=empty.get("last_activity")
    if stamp:
        if getattr(stamp,"tzinfo",None) is None:
            stamp=stamp.replace(tzinfo=timezone.utc)
        days=max(0,(datetime.now(timezone.utc)-stamp).days)
    else:
        joined=person.get("date_joined") or person.get("created_at")
        if hasattr(joined,"date") and not isinstance(joined,date):
            joined=joined.date()
        if isinstance(joined,datetime):
            joined=joined.date()
        days=max(0,(date.today()-joined).days) if isinstance(joined,date) else 0
    empty["days"]=days
    empty["days_since_activity"]=days

    if empty["excused"]:
        return empty
    if empty["seconds_7d"] >= 2*3600:
        empty.update({"state":"READY","next_label":"MAINTAIN 2H / 7 DAYS","days_to_next":None})
    elif days < 14:
        empty.update({"state":"WATCH","next_label":"AT RISK","days_to_next":max(0,14-days)})
    elif days < 21:
        empty.update({"state":"AT RISK","next_label":"INACTIVE","days_to_next":max(0,21-days)})
    elif days < 30:
        empty.update({"state":"INACTIVE","next_label":"ADMIN REVIEW","days_to_next":max(0,30-days)})
    else:
        empty.update({"state":"ADMIN REVIEW","next_label":None,"days_to_next":None})
    return empty


def activity_classification(person):
    snap=server_activity_snapshot(person)
    return snap["state"], int(snap.get("days") or 0)


def inactivity_snapshot(person):
    snap=server_activity_snapshot(person)
    try:
        snap["latest_contact"]=fetch_one("SELECT * FROM inactivity_contact_log WHERE personnel_id=%s ORDER BY contacted_at DESC LIMIT 1",(person["id"],))
    except Exception:
        snap["latest_contact"]=None
    return snap


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

    # 1. Activity / participation — verified HLL: Vietnam server time is authoritative.
    activity=server_activity_snapshot(person)
    inactive_days=int(activity.get("days") or 0)
    seconds_7d=int(activity.get("seconds_7d") or 0)
    if activity.get("excused"):
        activity_points=30
        breakdown["activity"]={"points":30,"max":30,"detail":"Authorized absence — server inactivity penalties paused"}
    elif seconds_7d >= 7200:
        activity_points=30
        breakdown["activity"]={"points":30,"max":30,"detail":f"{activity.get('time_7d')} verified server time in the last 7 days"}
    elif seconds_7d >= 3600:
        activity_points=25
        breakdown["activity"]={"points":25,"max":30,"detail":f"{activity.get('time_7d')} verified server time in the last 7 days"}
    elif seconds_7d >= 1800:
        activity_points=20
        breakdown["activity"]={"points":20,"max":30,"detail":f"{activity.get('time_7d')} verified server time in the last 7 days"}
    elif seconds_7d > 0:
        activity_points=15
        breakdown["activity"]={"points":15,"max":30,"detail":f"{activity.get('time_7d')} verified server time in the last 7 days"}
    elif inactive_days < 14:
        activity_points=10
        breakdown["activity"]={"points":10,"max":30,"detail":f"No server time in the last 7 days • {inactive_days} day{'s' if inactive_days != 1 else ''} since last server activity"}
    elif inactive_days < 21:
        activity_points=5
        breakdown["activity"]={"points":5,"max":30,"detail":f"{inactive_days} days since last verified server activity"}
    else:
        activity_points=0
        breakdown["activity"]={"points":0,"max":30,"detail":f"{inactive_days} days since last verified server activity"}

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
        breakdown["training"] = {"points":training_points,"max":25,"detail":f"{qual_count} current qualification{'s' if qual_count != 1 else ''} • {expired_count} expired"}
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
        breakdown["duty"] = {"points":duty_points,"max":10,"detail":f"{duty_count} credited official duty period{'s' if duty_count != 1 else ''} in the last 30 days"}
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
    if authorized_absence_active(person):
        activity, inactive_days = "EXCUSED ABSENCE", 0
    else:
        activity, inactive_days = activity_classification(person)
    weapon = fetch_one(
        """SELECT wi.* FROM weapon_issue_history wih
           JOIN weapon_inventory wi ON wi.id=wih.weapon_id
           WHERE wih.personnel_id=%s AND wih.is_current=TRUE LIMIT 1""",
        (person["id"],),
    )
    deficiencies = []
    if activity in {"AT RISK","INACTIVE","ADMIN REVIEW"}:
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
            deficiencies.append(("TRAINING",f"{expired['total']} EXPIRED QUALIFICATION{'S' if int(expired['total']) != 1 else ''}"))
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
        if r["activity"] in {"INACTIVE","ADMIN REVIEW"}:
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


def weapon_server_seconds_since_cleaning(weapon, person=None):
    """Verified HLL server time accumulated by the current holder since cleaning/issue."""
    if not person or not person.get("id"):
        return 0
    boundary=weapon.get("last_cleaned_at") or weapon.get("_issued_at") or weapon.get("issued_at")
    try:
        if boundary:
            row=fetch_one("""SELECT COALESCE(SUM(connected_delta_seconds),0) AS seconds
                 FROM hll_research_samples WHERE personnel_id=%s AND observed_at>%s
                   AND COALESCE(connected_delta_seconds,0)>0""",(str(person["id"]),boundary)) or {}
        else:
            row=fetch_one("""SELECT COALESCE(SUM(connected_seconds),0) AS seconds
                 FROM hll_player_match_stats WHERE personnel_id=%s""",(str(person["id"]),)) or {}
        return max(0,int(row.get("seconds") or 0))
    except Exception:
        log.exception("Unable to derive server-hours fouling for M16 %s",weapon.get("id"))
        return 0


def weapon_condition_from_rounds_and_time(weapon, person=None):
    """Derive M16 condition from verified HLL server hours since the last cleaning.

    Cleaning (or a new issue when no cleaning is on file) resets the field-service clock.
    The fouling process begins at five verified server hours and advances in five-hour
    stages. Round counts remain a service statistic and no longer determine fouling.
    """
    field_seconds=weapon_server_seconds_since_cleaning(weapon,person)
    field_hours=field_seconds/3600.0
    if field_hours >= 15:
        state="UNSERVICEABLE"; score=15
    elif field_hours >= 10:
        state="HEAVILY FOULED"; score=40
    elif field_hours >= 5:
        state="LIGHT FOULING"; score=70
    else:
        state="SERVICEABLE"; score=100
    if str(weapon.get("status") or "").upper()=="MAINTENANCE":
        score=min(score,30)
    weapon["server_seconds_since_cleaning"]=field_seconds
    weapon["server_hours_since_cleaning"]=round(field_hours,2)
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


def reconcile_weapon_rounds_since_cleaning(weapon_id):
    """Rebuild the fouling counter from the authoritative round ledger.

    Historical voice/operation reconciliation can arrive after a Soldier cleans the
    rifle.  Those old rounds must increase lifetime rounds without making a freshly
    cleaned rifle dirty again.  The maintenance timestamp is therefore the boundary
    for the current fouling cycle.
    """
    weapon=fetch_one("SELECT id,last_cleaned_at FROM weapon_inventory WHERE id=%s",(weapon_id,))
    if not weapon:
        return 0
    if not weapon.get("last_cleaned_at"):
        return int((fetch_one("SELECT rounds_since_cleaning FROM weapon_inventory WHERE id=%s",(weapon_id,)) or {}).get("rounds_since_cleaning") or 0)
    row=fetch_one("""SELECT COALESCE(SUM(rounds_fired),0)::int AS total
                     FROM weapon_round_events
                     WHERE weapon_id=%s AND recorded_at>%s""",(weapon_id,weapon["last_cleaned_at"])) or {"total":0}
    total=max(0,int(row.get("total") or 0))
    execute("UPDATE weapon_inventory SET rounds_since_cleaning=%s,updated_at=NOW() WHERE id=%s",(total,weapon_id))
    return total


def _round_event_time_from_source_key(source_key):
    """Recover the real firing time from idempotent Discord voice source keys."""
    key=str(source_key or "")
    try:
        if key.startswith("VOICE:"):
            parts=key.split(":")
            if len(parts)>=6:
                start=datetime.fromtimestamp(int(parts[-2]),tz=timezone.utc)
                return start+timedelta(seconds=max(0,int(parts[-1]))*300)
        if key.startswith("VOICE-HIST:"):
            # VOICE-HIST:guild:user:channel:<ISO8601>:block
            m=re.match(r"^VOICE-HIST:[^:]+:[^:]+:[^:]+:(.+):(\d+)$",key)
            if m:
                start=datetime.fromisoformat(m.group(1).replace("Z","+00:00"))
                if start.tzinfo is None: start=start.replace(tzinfo=timezone.utc)
                return start+timedelta(seconds=max(0,int(m.group(2)))*300)
    except Exception:
        return None
    return None


def repair_weapon_round_ledger_integrity():
    """Repair historical event timestamps, then rebuild post-clean fouling counters.

    Safe and idempotent.  This specifically prevents delayed Clerk backfills from
    making a rifle appear unclean immediately after the Soldier cleaned it.
    """
    timestamp_repairs=0
    for row in fetch_all("""SELECT id,source_key,recorded_at FROM weapon_round_events
                            WHERE source_key LIKE 'VOICE:%%' OR source_key LIKE 'VOICE-HIST:%%'"""):
        actual=_round_event_time_from_source_key(row.get("source_key"))
        if actual and row.get("recorded_at") and abs((row["recorded_at"]-actual).total_seconds())>60:
            execute("UPDATE weapon_round_events SET recorded_at=%s WHERE id=%s",(actual,row["id"]))
            timestamp_repairs+=1
    # Completed operation reconciliations belong to the operation, not the later
    # maintenance job that happened to discover them.
    execute("""UPDATE weapon_round_events wre SET recorded_at=COALESCE(o.completed_at,o.start_at,wre.recorded_at)
               FROM operations o
               WHERE wre.operation_id=o.id AND UPPER(COALESCE(wre.source_type,''))='OPERATION RECORD'
                 AND UPPER(COALESCE(o.status,'')) IN ('CLOSED','COMPLETE','COMPLETED','ARCHIVED')
                 AND COALESCE(o.completed_at,o.start_at) IS NOT NULL
                 AND wre.recorded_at>COALESCE(o.completed_at,o.start_at)+INTERVAL '5 minutes'""")
    counters=0
    for row in fetch_all("SELECT id FROM weapon_inventory WHERE last_cleaned_at IS NOT NULL"):
        reconcile_weapon_rounds_since_cleaning(row["id"]); refresh_weapon_condition(row["id"]); counters+=1
    return {"timestamp_repairs":timestamp_repairs,"counters_rebuilt":counters}


def file_weapon_cleaning(weapon_id,personnel_id=None,performed_by=None,remarks=None):
    """Atomically reset the rifle and file its maintenance record."""
    row=fetch_one("""WITH prior AS (
                       SELECT id,condition_state,total_rounds FROM weapon_inventory WHERE id=%s FOR UPDATE
                     ), updated AS (
                       UPDATE weapon_inventory wi SET rounds_since_cleaning=0,last_cleaned_at=NOW(),
                         condition_percent=100,condition_state='SERVICEABLE',updated_at=NOW()
                       FROM prior WHERE wi.id=prior.id
                       RETURNING wi.id,wi.serial_number,wi.total_rounds,wi.last_cleaned_at,wi.condition_state,wi.condition_percent,prior.condition_state AS condition_before
                     ), logged AS (
                       INSERT INTO weapon_maintenance_log
                         (weapon_id,personnel_id,action_type,condition_before,condition_after,rounds_at_action,performed_by,remarks)
                       SELECT id,%s,'CLEANED',condition_before,'SERVICEABLE',total_rounds,%s,%s FROM updated
                       RETURNING id AS maintenance_log_id
                     )
                     SELECT updated.*,logged.maintenance_log_id FROM updated CROSS JOIN logged""",
                  (weapon_id,personnel_id,performed_by,remarks))
    if not row:
        raise ValueError("Weapon cleaning could not be filed")
    reconcile_weapon_rounds_since_cleaning(weapon_id)
    refreshed=refresh_weapon_condition(weapon_id) or {}
    row.update(refreshed)
    return row


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
            "Standard service uniform issue upon entry on the Battle Roster.",
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
    already_logged=False
    if action == "CLEANED":
        refreshed=file_weapon_cleaning(weapon_id,personnel_id,performed_by,remarks)
        new_state=refreshed.get("condition_state") or "SERVICEABLE"
        new_pct=int(refreshed.get("condition_percent") or 100)
        already_logged=True
    elif action == "INSPECTED":
        new_state,new_pct = weapon_condition_from_rounds_and_time({**weapon,"condition_percent":max(int(weapon.get("condition_percent") or 0),85)}, None)
        if new_state in {"FIELD WORN","FOULED"}:
            new_state="SERVICEABLE"
            new_pct=max(new_pct,85)
        execute("""UPDATE weapon_inventory SET last_inspected_at=NOW(),condition_percent=%s,
                   condition_state=%s,updated_at=NOW() WHERE id=%s""",(new_pct,new_state,weapon_id))
    elif action == "MAINTENANCE COMPLETED":
        new_state,new_pct="SERVICEABLE",100
        current_issue=fetch_one("SELECT 1 FROM weapon_issue_history WHERE weapon_id=%s AND is_current=TRUE",(weapon_id,))
        inventory_status="ISSUED" if current_issue else "AVAILABLE FOR ISSUE"
        execute("""UPDATE weapon_inventory SET condition_state=%s,condition_percent=%s,status=%s,
                   last_inspected_at=NOW(),last_cleaned_at=NOW(),rounds_since_cleaning=0,
                   updated_at=NOW() WHERE id=%s""",(new_state,new_pct,inventory_status,weapon_id))
        reconcile_weapon_rounds_since_cleaning(weapon_id)
    elif action == "PLACED IN MAINTENANCE":
        new_state,new_pct="MAINTENANCE REQUIRED",min(int(weapon.get("condition_percent") or 100),30)
        execute("""UPDATE weapon_inventory SET condition_state=%s,condition_percent=%s,
                   status='MAINTENANCE',updated_at=NOW() WHERE id=%s""",(new_state,new_pct,weapon_id))
    else:
        raise ValueError("Unsupported maintenance action")

    if not already_logged:
        execute("""INSERT INTO weapon_maintenance_log
                   (weapon_id,personnel_id,action_type,condition_before,condition_after,
                    rounds_at_action,performed_by,remarks)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                (weapon_id,personnel_id,action,before,new_state,int(weapon.get("total_rounds") or 0),performed_by,remarks))
    if personnel_id:
        write_service_entry(personnel_id,"ARMS",action,
                            f"M16 serial {weapon.get('serial_number')} — {action.lower()}.",
                            performed_by,None,date.today())
    return refresh_weapon_condition(weapon_id) or fetch_one("SELECT * FROM weapon_inventory WHERE id=%s",(weapon_id,))


def record_weapon_rounds(weapon_id,rounds,personnel_id=None,operation_id=None,source_type="MANUAL ENTRY",recorded_by=None,remarks=None,occurred_at=None):
    rounds=max(0,int(rounds))
    occurred_at=occurred_at or datetime.now(timezone.utc)
    execute("""INSERT INTO weapon_round_events
               (weapon_id,personnel_id,operation_id,rounds_fired,source_type,recorded_at,recorded_by,remarks)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
            (weapon_id,personnel_id,operation_id,rounds,source_type,occurred_at,recorded_by,remarks))
    execute("""UPDATE weapon_inventory SET total_rounds=COALESCE(total_rounds,0)+%s,
               rounds_since_cleaning=COALESCE(rounds_since_cleaning,0)+CASE WHEN last_cleaned_at IS NULL OR %s>last_cleaned_at THEN %s ELSE 0 END,
               last_fired_at=GREATEST(COALESCE(last_fired_at,%s),%s),updated_at=NOW() WHERE id=%s""",
            (rounds,occurred_at,rounds,occurred_at,occurred_at,weapon_id))
    reconcile_weapon_rounds_since_cleaning(weapon_id)
    refresh_weapon_condition(weapon_id)
    if personnel_id:
        op=operation_record(operation_id) if operation_id else None
        emit_state_event('WEAPON_ROUNDS_FIRED',personnel_id=personnel_id,operation_id=operation_id,weapon_id=weapon_id,
                         effective_date=occurred_at.date(),title='M16 AMMUNITION EXPENDITURE',
                         narrative=f"{rounds} rounds recorded" + (f" for {op.get('operation_number') or op.get('title')}" if op else '') + '.',
                         source_key=f"ROUND:{weapon_id}:{personnel_id}:{operation_id}:{source_type}:{rounds}:{occurred_at.isoformat()}",
                         details={'rounds':rounds,'source_type':source_type,'recorded_by':recorded_by})


def record_voice_weapon_rounds(personnel_id, rounds, source_key, source_type="DISCORD ACTIVITY VOICE", recorded_by="BATTALION CLERK", remarks=None, occurred_at=None):
    """Idempotently file a verified Discord voice ammunition segment at its real time."""
    rounds=max(0,int(rounds or 0))
    if rounds<=0 or not source_key:
        return {"applied":0,"reason":"no completed ammunition block"}
    weapon=fetch_one("""SELECT wi.* FROM weapon_issue_history wih JOIN weapon_inventory wi ON wi.id=wih.weapon_id
                        WHERE wih.personnel_id=%s AND wih.is_current=TRUE ORDER BY wih.issued_at DESC LIMIT 1""",(personnel_id,))
    if not weapon:
        return {"applied":0,"reason":"no current M16 issued"}
    occurred_at=occurred_at or _round_event_time_from_source_key(source_key) or datetime.now(timezone.utc)
    inserted=fetch_one("""INSERT INTO weapon_round_events
        (weapon_id,personnel_id,rounds_fired,source_type,recorded_at,recorded_by,remarks,source_key)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(source_key) DO NOTHING RETURNING id""",
        (weapon["id"],personnel_id,rounds,source_type,occurred_at,recorded_by,remarks,source_key))
    if not inserted:
        return {"applied":0,"duplicate":True,"weapon_id":str(weapon["id"])}
    execute("""UPDATE weapon_inventory SET total_rounds=COALESCE(total_rounds,0)+%s,
               rounds_since_cleaning=COALESCE(rounds_since_cleaning,0)+CASE WHEN last_cleaned_at IS NULL OR %s>last_cleaned_at THEN %s ELSE 0 END,
               last_fired_at=GREATEST(COALESCE(last_fired_at,%s),%s),updated_at=NOW() WHERE id=%s""",
            (rounds,occurred_at,rounds,occurred_at,occurred_at,weapon["id"]))
    current_since=reconcile_weapon_rounds_since_cleaning(weapon["id"])
    refreshed=refresh_weapon_condition(weapon["id"]) or {}
    return {"applied":rounds,"weapon_id":str(weapon["id"]),"serial_number":weapon.get("serial_number"),
            "condition":refreshed.get("condition_state"),"rounds_since_cleaning":current_since,"occurred_at":occurred_at.isoformat()}



def _hll_weapon_for_time(personnel_id, occurred_at):
    """Return the rifle issued to this Soldier at the observed field-use time."""
    when = occurred_at or datetime.now(timezone.utc)
    return fetch_one("""SELECT wi.* FROM weapon_issue_history wih
                        JOIN weapon_inventory wi ON wi.id=wih.weapon_id
                        WHERE wih.personnel_id=%s
                          AND wih.issued_at<=%s::date
                          AND (wih.turned_in_at IS NULL OR wih.turned_in_at>=%s::date)
                        ORDER BY wih.is_current DESC,wih.issued_at DESC LIMIT 1""",
                     (personnel_id,when,when))


def _file_hll_m16_block(personnel_id, match_id, block_index, occurred_at, seconds_source=300):
    """File one idempotent 5-minute HLL M16 field-use block as an estimate.

    HLL: Vietnam does not expose shots fired. The battalion's established model is
    300 estimated rounds/hour, therefore one completed five-minute M16-equipped
    interval equals 25 estimated rounds. The source key makes reprocessing safe.
    """
    if not personnel_id or not match_id or int(block_index or 0) <= 0:
        return {"applied":0,"reason":"invalid HLL M16 block"}
    source_key=f"HLL-M16:{match_id}:{personnel_id}:{int(block_index)}"
    if fetch_one("SELECT id FROM weapon_round_events WHERE source_key=%s",(source_key,)):
        return {"applied":0,"duplicate":True}
    weapon=_hll_weapon_for_time(personnel_id,occurred_at)
    if not weapon:
        return {"applied":0,"reason":"no M16 issued at observed time"}
    rounds=25
    inserted=fetch_one("""INSERT INTO weapon_round_events
        (weapon_id,personnel_id,rounds_fired,source_type,recorded_at,recorded_by,remarks,source_key)
        VALUES(%s,%s,%s,'HLL SERVER M16 ESTIMATE',%s,'BATTALION CLERK',%s,%s)
        ON CONFLICT(source_key) DO NOTHING RETURNING id""",
        (weapon["id"],personnel_id,rounds,occurred_at,
         f"Estimated M16 expenditure from {int(seconds_source or 300)} seconds of verified HLL: Vietnam field use. 300 rounds/hour model; not an exact shots-fired count.",source_key))
    if not inserted:
        return {"applied":0,"duplicate":True,"weapon_id":str(weapon["id"])}
    execute("""UPDATE weapon_inventory SET total_rounds=COALESCE(total_rounds,0)+%s,
               last_fired_at=GREATEST(COALESCE(last_fired_at,%s),%s),updated_at=NOW()
               WHERE id=%s""",(rounds,occurred_at,occurred_at,weapon["id"]))
    current_since=reconcile_weapon_rounds_since_cleaning(weapon["id"])
    refreshed=refresh_weapon_condition(weapon["id"]) or {}
    return {"applied":rounds,"weapon_id":str(weapon["id"]),"serial_number":weapon.get("serial_number"),
            "rounds_since_cleaning":current_since,"condition":refreshed.get("condition_state")}


def reconcile_hll_m16_rounds(personnel_id=None, days=365):
    """Backfill/advance issued-M16 expenditure from verified HLL server telemetry.

    Verified active-server samples are authoritative for an issued M16. A linked
    Soldier who is actively observed on the HLL: Vietnam server is treated as
    carrying the rifle issued to that Soldier at that observed time; this avoids
    losing rifle service when HLLV omits or changes its loadout label. Processing
    remains idempotent by (match, Soldier, completed 5-minute block).
    """
    summary={"players":0,"matches":0,"blocks_checked":0,"rounds_applied":0,"weapons":set()}
    try:
        params=[]
        pid_clause=""
        if personnel_id:
            pid_clause=" AND COALESCE(rs.personnel_id,l.personnel_id)=%s"
            params.append(str(personnel_id))
        params.append(max(1,int(days or 365)))
        rows=fetch_all("""SELECT rs.match_id,rs.steam_id,COALESCE(rs.personnel_id,l.personnel_id) AS personnel_id,
                                  rs.observed_at,COALESCE(rs.connected_delta_seconds,0)::int AS seconds,COALESCE(rs.loadout,'') AS loadout
                           FROM hll_research_samples rs
                           LEFT JOIN hll_personnel_links l ON l.steam_id=rs.steam_id AND l.verified=TRUE
                           WHERE COALESCE(rs.personnel_id,l.personnel_id) IS NOT NULL
                             AND COALESCE(rs.connected_delta_seconds,0)>0
                             """+pid_clause+"""
                             AND rs.observed_at>=NOW()-make_interval(days => %s)
                           ORDER BY COALESCE(rs.personnel_id,l.personnel_id),rs.match_id,rs.observed_at""",tuple(params))
        grouped={}
        for r in rows:
            key=(str(r.get("personnel_id")),int(r.get("match_id") or 0))
            if not key[0] or not key[1]: continue
            grouped.setdefault(key,[]).append(r)
        processed_groups=set(grouped)
        for (pid,match_id),samples in grouped.items():
            cumulative=0; next_block=1
            for sample in samples:
                cumulative+=max(0,int(sample.get("seconds") or 0))
                while cumulative>=next_block*300:
                    result=_file_hll_m16_block(pid,match_id,next_block,sample.get("observed_at"),300)
                    summary["blocks_checked"]+=1
                    summary["rounds_applied"]+=int(result.get("applied") or 0)
                    if result.get("weapon_id"): summary["weapons"].add(result["weapon_id"])
                    next_block+=1
        # Older telemetry may have match-level M16 seconds but no research rows.
        params=[]; ps_clause=""
        if personnel_id:
            ps_clause=" AND COALESCE(ps.personnel_id,l.personnel_id)=%s"
            params.append(str(personnel_id))
        params.append(max(1,int(days or 365)))
        fallback=fetch_all("""SELECT ps.match_id,ps.steam_id,COALESCE(ps.personnel_id,l.personnel_id) AS personnel_id,
                                     ps.first_seen_at,ps.last_seen_at,COALESCE(ps.connected_seconds,0)::int AS seconds
                              FROM hll_player_match_stats ps
                              LEFT JOIN hll_personnel_links l ON l.steam_id=ps.steam_id AND l.verified=TRUE
                              WHERE COALESCE(ps.personnel_id,l.personnel_id) IS NOT NULL
                                AND COALESCE(ps.connected_seconds,0)>=300
                                """+ps_clause+"""
                                AND ps.last_seen_at>=NOW()-make_interval(days => %s)
                              ORDER BY ps.first_seen_at""",tuple(params))
        for r in fallback:
            pid=str(r.get("personnel_id") or ""); match_id=int(r.get("match_id") or 0)
            if not pid or not match_id or (pid,match_id) in processed_groups: continue
            blocks=max(0,int(r.get("seconds") or 0)//300)
            start=r.get("first_seen_at") or r.get("last_seen_at") or datetime.now(timezone.utc)
            last=r.get("last_seen_at") or start
            for idx in range(1,blocks+1):
                occurred=min(start+timedelta(seconds=idx*300),last)
                result=_file_hll_m16_block(pid,match_id,idx,occurred,300)
                summary["blocks_checked"]+=1
                summary["rounds_applied"]+=int(result.get("applied") or 0)
                if result.get("weapon_id"): summary["weapons"].add(result["weapon_id"])
        summary["players"]=len({k[0] for k in processed_groups} | {str(r.get('personnel_id')) for r in fallback if r.get('personnel_id')})
        summary["matches"]=len(processed_groups | {(str(r.get('personnel_id')),int(r.get('match_id') or 0)) for r in fallback if r.get('personnel_id') and r.get('match_id')})
    except Exception:
        log.exception("HLL M16 round reconciliation failed personnel=%s",personnel_id)
        summary["error"]="telemetry unavailable"
    summary["weapons_updated"]=len(summary.pop("weapons",set()))
    return summary


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
                                      recorded_by="BATTALION CLERK", remarks=None, occurred_at=None):
    expected=max(0,int(expected_rounds or 0))
    if expected<=0:
        return 0
    op=operation_record(operation_id) or {}
    op_date=(op.get("start_at").date() if op.get("start_at") else op.get("operation_date")) or date.today()
    # Historical reconciliation must credit the rifle that was actually issued on the
    # operation date when issue history is available, not blindly today's current rifle.
    weapon=fetch_one("""SELECT wi.id,wi.serial_number FROM weapon_issue_history wih
                        JOIN weapon_inventory wi ON wi.id=wih.weapon_id
                        WHERE wih.personnel_id=%s AND wih.issued_at<=%s
                          AND (wih.turned_in_at IS NULL OR wih.turned_in_at>=%s)
                        ORDER BY CASE WHEN wih.is_current THEN 0 ELSE 1 END,wih.issued_at DESC LIMIT 1""",
                     (personnel_id,op_date,op_date))
    if not weapon:
        weapon=fetch_one("""SELECT wi.id,wi.serial_number FROM weapon_issue_history wih
                            JOIN weapon_inventory wi ON wi.id=wih.weapon_id
                            WHERE wih.personnel_id=%s AND wih.is_current=TRUE
                            ORDER BY wih.issued_at DESC LIMIT 1""",(personnel_id,))
    if not weapon:
        return 0
    applied=operation_weapon_rounds_applied(operation_id,personnel_id,weapon["id"])
    delta=max(0,expected-applied)
    if delta:
        inferred_time=occurred_at or op.get("completed_at") or op.get("start_at") or datetime.now(timezone.utc)
        if isinstance(inferred_time,date) and not isinstance(inferred_time,datetime):
            inferred_time=datetime.combine(inferred_time,time.min,tzinfo=timezone.utc)
        elif getattr(inferred_time,"tzinfo",None) is None:
            inferred_time=inferred_time.replace(tzinfo=timezone.utc)
        record_weapon_rounds(weapon["id"],delta,personnel_id,operation_id,
                             "OPERATION RECORD",recorded_by,
                             remarks or f"Automatic operation ammunition reconciliation; {delta} previously unapplied rounds filed.",
                             occurred_at=inferred_time)
    return delta



def operation_round_target_for_time(event, qualifying_seconds):
    """Legacy compatibility helper.

    Discord/Operation voice presence is attendance evidence only. It must never
    manufacture M16 ammunition expenditure. HLL server telemetry owns rifle field
    service; manually filed actual expenditure may still remain on an Operation.
    """
    return 0


def accrue_live_operation_weapon_rounds(event, personnel_id, qualifying_seconds, authority="BATTALION CLERK"):
    """Legacy compatibility helper; voice attendance no longer changes the M16 ledger."""
    return {"target":0,"applied":0,"deprecated":True}



def operation_presence_status(event, qualifying_seconds):
    """Map verified Clerk voice presence to a non-inflating operation status."""
    seconds=max(0,int(qualifying_seconds or 0))
    threshold=max(300,int((event or {}).get("credit_threshold_minutes") or 45)*60)
    partial_threshold=min(1200,max(300,threshold//2))
    if seconds>=threshold:
        return "FULL CREDIT",100
    percent=min(99,round((seconds/threshold)*100)) if threshold else 0
    if seconds>=partial_threshold:
        return "PARTIAL / LATE",percent
    if seconds>0:
        return "TRACKED PRESENCE",percent
    return "NO CREDIT",0


def sync_operation_presence_from_attendance(event, personnel_id, qualifying_seconds,
                                            authority="BATTALION CLERK", historical=False):
    """Mirror verified duty-channel attendance into the Soldier's operation history.

    Voice presence is attendance evidence only. It never creates M16 rounds or field
    service; issued-rifle service is HLL-server-authoritative.
    """
    if str((event or {}).get("event_type") or "").upper()!="OPERATION" or not (event or {}).get("operation_id"):
        return {"status":"IGNORED","rounds_target":0,"rounds_applied":0,"full_credit":False}
    seconds=max(0,int(qualifying_seconds or 0))
    if seconds<=0:
        return {"status":"NO CREDIT","rounds_target":0,"rounds_applied":0,"full_credit":False}
    personnel=fetch_one("SELECT * FROM personnel WHERE id=%s",(personnel_id,))
    if not personnel:
        return {"status":"NO PERSONNEL","rounds_target":0,"rounds_applied":0,"full_credit":False}
    status,percent=operation_presence_status(event,seconds)
    operation_id=event["operation_id"]
    existing=fetch_one("SELECT * FROM operation_participation WHERE operation_id=%s AND personnel_id=%s",(operation_id,personnel_id))
    prior_status=str((existing or {}).get("attendance_status") or "").upper()
    rank={"":0,"NO CREDIT":0,"TRACKED PRESENCE":1,"PARTIAL / LATE":2,"PARTICIPATED":2,"PRESENT":2,"FULL CREDIT":3,"CREDITED":3,"COMPLETE":3,"COMPLETED":3}
    final_status=status if rank.get(status,0)>=rank.get(prior_status,0) else (existing.get("attendance_status") if existing else status)
    final_rounds=int((existing or {}).get("rounds_expended") or 0)
    note=("Historical Battalion Clerk reconciliation" if historical else "Automatic Battalion Clerk attendance") + f": {seconds//60} verified minutes ({percent}%)."
    execute("""INSERT INTO operation_participation
               (operation_id,personnel_id,unit_node_id,duty_role,attendance_status,rounds_expended,remarks,credited_by)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(operation_id,personnel_id) DO UPDATE SET
                 unit_node_id=COALESCE(EXCLUDED.unit_node_id,operation_participation.unit_node_id),
                 duty_role=COALESCE(operation_participation.duty_role,EXCLUDED.duty_role),
                 attendance_status=%s,
                 rounds_expended=GREATEST(COALESCE(operation_participation.rounds_expended,0),EXCLUDED.rounds_expended),
                 remarks=EXCLUDED.remarks,
                 credited_by=EXCLUDED.credited_by""",
            (operation_id,personnel_id,personnel.get("unit_node_id"),personnel.get("duty_position"),status,
             final_rounds,note,authority,final_status))
    rounds_applied=0
    full=(str(final_status).upper()=="FULL CREDIT")
    newly_full=full and prior_status not in {"FULL CREDIT","CREDITED","COMPLETE","COMPLETED"}
    if full:
        execute("""INSERT INTO personnel_activity_credit
                   (personnel_id,source,source_reference,activity_type,activity_date,duration_seconds,credited)
                   SELECT %s,'BATTALION DUTY',%s,'OPERATION',COALESCE(%s::date,CURRENT_DATE),%s,TRUE
                   WHERE NOT EXISTS (
                     SELECT 1 FROM personnel_activity_credit
                     WHERE personnel_id=%s AND source='BATTALION DUTY' AND source_reference=%s AND activity_type='OPERATION'
                   )""",
                (personnel_id,str(event["id"]),event.get("starts_at").date() if event.get("starts_at") else None,
                 seconds,personnel_id,str(event["id"])))
        if newly_full:
            try:
                operation_credit_cascade(operation_id,personnel_id,final_rounds,authority)
            except NameError:
                # During module import the cascade is defined later; runtime reconciliation will call it again.
                pass
    return {"status":final_status,"rounds_target":final_rounds,"rounds_applied":rounds_applied,"full_credit":full,"newly_full":newly_full}


def repair_historical_operation_attendance(authority="SYSTEM RECONCILIATION"):
    """Backfill operation history and M16 rounds from every existing verified attendance row."""
    if not database_ready():
        return {"attendance_rows":0,"participation_rows":0,"full_credit":0,"rounds_applied":0}
    rows=fetch_all("""SELECT a.event_id,a.personnel_id,a.qualifying_seconds,a.credited_at,
                             e.operation_id,e.event_type,e.title,e.starts_at,e.ends_at,e.rounds_per_soldier,e.credit_threshold_minutes
                      FROM battalion_event_attendance a
                      JOIN battalion_events e ON e.id=a.event_id
                      WHERE UPPER(COALESCE(e.event_type,''))='OPERATION'
                        AND e.operation_id IS NOT NULL AND COALESCE(a.qualifying_seconds,0)>0
                      ORDER BY e.starts_at,a.updated_at""")
    repaired={"attendance_rows":len(rows),"participation_rows":0,"full_credit":0,"rounds_applied":0}
    for row in rows:
        status,percent=operation_presence_status(row,row.get("qualifying_seconds") or 0)
        if status=="FULL CREDIT" and not row.get("credited_at"):
            execute("""UPDATE battalion_event_attendance SET credited_at=NOW(),attendance_grade='FULL CREDIT',attendance_percent=100,updated_at=NOW()
                       WHERE event_id=%s AND personnel_id=%s""",(row["event_id"],row["personnel_id"]))
        else:
            execute("""UPDATE battalion_event_attendance SET attendance_grade=%s,attendance_percent=%s,updated_at=NOW()
                       WHERE event_id=%s AND personnel_id=%s""",(status,percent,row["event_id"],row["personnel_id"]))
        result=sync_operation_presence_from_attendance(row,row["personnel_id"],row.get("qualifying_seconds") or 0,authority,True)
        repaired["participation_rows"]+=1
        repaired["rounds_applied"]+=int(result.get("rounds_applied") or 0)
        if result.get("full_credit"):
            repaired["full_credit"]+=1
    return repaired


def close_ended_operation_events(authority="SYSTEM RECONCILIATION"):
    """Stop stale Clerk tracking at scheduled end time and mark the operation completed.

    This does not fabricate an AAR. It only freezes verified attendance/rounds and
    removes the operation from the live/scheduled state. S-3 may file the AAR later.
    """
    if not database_ready():
        return 0
    events=fetch_all("""SELECT * FROM battalion_events
                        WHERE UPPER(COALESCE(event_type,''))='OPERATION'
                          AND operation_id IS NOT NULL
                          AND UPPER(COALESCE(status,'')) IN ('SCHEDULED','ACTIVE')
                          AND ends_at<=NOW()""")
    for event in events:
        attendance=fetch_all("SELECT * FROM battalion_event_attendance WHERE event_id=%s",(event["id"],))
        for a in attendance:
            secs=int(a.get("qualifying_seconds") or 0)
            if secs<=0: continue
            status,percent=operation_presence_status(event,secs)
            sync_operation_presence_from_attendance(event,a["personnel_id"],secs,authority)
            execute("""UPDATE battalion_event_attendance SET attendance_grade=%s,attendance_percent=%s,
                       credited_at=CASE WHEN %s='FULL CREDIT' THEN COALESCE(credited_at,NOW()) ELSE credited_at END,
                       updated_at=NOW() WHERE event_id=%s AND personnel_id=%s""",
                    (status,percent,status,event["id"],a["personnel_id"]))
        execute("UPDATE battalion_events SET status='CLOSED' WHERE id=%s",(event["id"],))
        execute("""UPDATE operations SET status='COMPLETED',lifecycle_status='CLOSED',publish_status='CLOSED',
                   completed_at=COALESCE(completed_at,%s),updated_at=NOW()
                   WHERE id=%s AND UPPER(COALESCE(status,'')) NOT IN ('ARCHIVED','CANCELLED','CANCELED')""",
                (event.get("ends_at"),event["operation_id"]))
    return len(events)


def archive_expired_operations():
    """Archive operations on the calendar day after their scheduled operation date."""
    if not database_ready():
        return 0
    stale=fetch_all("""SELECT id,start_at,operation_date,duration_minutes,status FROM operations
                       WHERE COALESCE(operation_date,start_at::date) < CURRENT_DATE
                         AND UPPER(COALESCE(status,'')) NOT IN ('ARCHIVED','CANCELLED','CANCELED')""")
    for op in stale:
        end_at=(op.get("start_at")+timedelta(minutes=max(1,int(op.get("duration_minutes") or 90)))) if op.get("start_at") else datetime.now(timezone.utc)
        execute("""UPDATE operations SET status='ARCHIVED',lifecycle_status='CLOSED',publish_status='CLOSED',
                   completed_at=COALESCE(completed_at,%s),updated_at=NOW() WHERE id=%s""",(end_at,op["id"]))
        execute("""UPDATE battalion_events SET status='CLOSED',ends_at=LEAST(ends_at,%s)
                   WHERE operation_id=%s AND UPPER(COALESCE(status,'')) IN ('SCHEDULED','ACTIVE')""",(end_at,op["id"]))
    return len(stale)


def run_operation_maintenance(authority="SYSTEM RECONCILIATION"):
    """Single maintenance transaction used by startup, S-3 pages, and Battalion Clerk."""
    repaired=repair_historical_operation_attendance(authority)
    completed=close_ended_operation_events(authority)
    archived=archive_expired_operations()
    weapon_integrity=repair_weapon_round_ledger_integrity()
    repaired["completed_operations"]=completed
    repaired["archived_operations"]=archived
    repaired["weapon_timestamp_repairs"]=weapon_integrity.get("timestamp_repairs",0)
    repaired["weapon_counters_rebuilt"]=weapon_integrity.get("counters_rebuilt",0)
    return repaired


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
      WHERE op.personnel_id=%s AND UPPER(COALESCE(op.attendance_status,'')) NOT IN ('ABSENT','NO CREDIT')
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


def verified_server_service_snapshot(personnel_id):
    """Authoritative HLL server-service totals used by progression dashboards.

    Raw server time never substitutes for an official Operation, billet, training
    event, or command verification.  It is the authoritative participation clock
    for readiness/MOS and is exposed to the ribbon engine as supporting evidence.
    """
    result={"linked":False,"seconds_lifetime":0,"hours_lifetime":0.0,"seconds_7d":0,"hours_7d":0.0,"last_activity":None,"seeding_seconds":0,"seeding_hours":0.0}
    if not personnel_id:
        return result
    try:
        link=fetch_one("SELECT steam_id FROM hll_personnel_links WHERE personnel_id=%s LIMIT 1",(str(personnel_id),))
        result["linked"]=bool(link and link.get("steam_id"))
        row=fetch_one("""SELECT
                COALESCE(SUM(COALESCE(ps.connected_seconds,0)),0) AS seconds_lifetime,
                COALESCE(SUM(CASE WHEN COALESCE(ps.last_seen_at,ms.last_seen_at,ms.ended_at,ms.started_at) >= NOW()-INTERVAL '7 days'
                                  THEN COALESCE(ps.connected_seconds,0) ELSE 0 END),0) AS seconds_7d,
                MAX(COALESCE(ps.last_seen_at,ms.last_seen_at,ms.ended_at,ms.started_at)) AS last_activity
            FROM hll_player_match_stats ps
            LEFT JOIN hll_match_sessions ms ON ms.id=ps.match_id
            WHERE ps.personnel_id=%s""",(str(personnel_id),)) or {}
        result["seconds_lifetime"]=max(0,int(row.get("seconds_lifetime") or 0))
        result["seconds_7d"]=max(0,int(row.get("seconds_7d") or 0))
        result["hours_lifetime"]=round(result["seconds_lifetime"]/3600.0,2)
        result["hours_7d"]=round(result["seconds_7d"]/3600.0,2)
        result["last_activity"]=row.get("last_activity")
        try:
            seed=fetch_one("SELECT COALESCE(SUM(credited_seconds),0) seconds FROM hll_seeding_service WHERE personnel_id=%s",(str(personnel_id),)) or {}
            result["seeding_seconds"]=max(0,int(seed.get("seconds") or 0))
            result["seeding_hours"]=round(result["seeding_seconds"]/3600.0,2)
        except Exception:
            pass
    except Exception:
        log.exception("Verified server-service snapshot unavailable for %s",personnel_id)
    return result


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
    server_service = verified_server_service_snapshot(personnel_id)
    server_hours = float(server_service.get('hours_lifetime') or 0.0)
    server_hours_7d = float(server_service.get('hours_7d') or 0.0)
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
    seeding_hours = float(server_service.get('seeding_hours') or 0.0)

    progress = [
        {'code':'INSTRUCTOR','name':'Instructor Ribbon','current':instructor_sessions,'target':5,'detail':f'{instructor_sessions} / 5 completed instructional periods','complete':instructor_sessions >= 5},
        {'code':'NCO_LEADERSHIP','name':'NCO Leadership Ribbon','current':min(nco_days,30),'target':30,'secondary_current':nco_events,'secondary_target':3,'detail':f'{nco_days} / 30 qualifying days • {nco_events} / 3 official events','complete':nco_days >= 30 and nco_events >= 3},
        {'code':'RECRUITING','name':'Recruiting Ribbon','current':successful_recruits,'target':3,'detail':f'{successful_recruits} / 3 successful recruits','complete':successful_recruits >= 3},
        {'code':'COMBAT_INFANTRY','name':'Combat Infantry Ribbon','current':operations,'target':10,'detail':f'{operations} / 10 credited official operations • {server_hours:.1f} verified HLL hours on file','complete':operations >= 10},
        {'code':'CAMPAIGN','name':'Campaign Ribbon','current':0,'target':1,'detail':'Progress begins when Headquarters designates a battalion campaign.','complete':False,'pending_system':True},
        {'code':'GOOD_CONDUCT','name':'Good Conduct Ribbon','current':min(service_days,90) if good_standing else service_days,'target':90,'detail':(f'{service_days} / 90 qualifying service days • {server_hours:.1f} verified HLL hours' if good_standing else 'Eligibility suspended — personnel file is not currently in good standing.'),'complete':service_days >= 90 and good_standing},
        {'code':'TOUR_OF_DUTY','name':'Tour of Duty Ribbon','current':min(service_days,180),'target':180,'secondary_current':operations,'secondary_target':20,'detail':f'{service_days} / 180 service days • {operations} / 20 official operations • {server_hours:.1f} verified HLL hours','complete':service_days >= 180 and operations >= 20},
        {'code':'MILITARY_SERVICE','name':'Military Service Ribbon','current':1 if military else 0,'target':1,'detail':'Military service verified by Battalion Headquarters.' if military else 'Command verification required.','complete':military},
    ]
    for row in progress:
        row['verified_server_hours']=server_hours
        row['verified_server_hours_7d']=server_hours_7d
        row['verified_seeding_hours']=seeding_hours
        row['server_last_activity']=server_service.get('last_activity')
        row['server_linked']=bool(server_service.get('linked'))
    earned = {r['ribbon_code'] for r in fetch_all("SELECT ribbon_code FROM personnel_ribbons WHERE personnel_id=%s", (personnel_id,))}
    if award_completed:
        for row in progress:
            if row['complete'] and row['code'] not in earned:
                execute("""INSERT INTO personnel_ribbons(personnel_id,ribbon_code,earned_at,source_type,source_reference,notes,is_worn)
                           VALUES(%s,%s,CURRENT_DATE,'AUTOMATIC','RIBBON ENGINE',%s,TRUE)
                           ON CONFLICT(personnel_id,ribbon_code) DO NOTHING""",
                        (personnel_id,row['code'],row['detail']))
                write_service_entry(personnel_id,'AWARD',row['name'].upper(),f"Automatically awarded after satisfying published ribbon requirements. {row['detail']}",'BATTALION CLERK / S-1')
                notify_soldier(personnel_id,'S-1 PERSONNEL',f"Award filed — {row['name']}",
                    f"Your 201 File has been updated with the {row['name']}. Open the Awards tab to review it.",
                    notification_type='AWARD',priority='HIGH',source_key=f"AUTO-AWARD-NOTICE:{personnel_id}:{row['code']}",target_endpoint='my_201_file',target_anchor='awards')
                earned.add(row['code'])
    for row in progress:
        row['earned'] = row['code'] in earned
        base = max(1, int(row.get('target') or 1))
        row['percent'] = 100 if row['earned'] else min(100, int((int(row.get('current') or 0) / base) * 100))
    return progress



def _award_device_label(award_count):
    """1965 Army-style subsequent-award device wording.

    One award carries no device.  Each additional award is represented by an oak
    leaf cluster; one silver oak leaf cluster represents five bronze clusters.
    """
    count=max(0,int(award_count or 0))
    additional=max(0,count-1)
    if additional == 0:
        return "NO DEVICE — FIRST AWARD"
    silver, bronze = divmod(additional,5)
    parts=[]
    if silver:
        parts.append(f"{silver} SILVER OAK LEAF CLUSTER" + ("S" if silver != 1 else ""))
    if bronze:
        parts.append(f"{bronze} BRONZE OAK LEAF CLUSTER" + ("S" if bronze != 1 else ""))
    return " • ".join(parts)


def ribbon_details_for_member(personnel_id):
    """One read-only source for clickable member ribbon cards/rack details."""
    catalog=fetch_all("""SELECT ribbon_code,ribbon_name,automation_mode,requirement_text,
                              description_text,earning_text,award_type_label,sort_order,image_filename
                       FROM ribbon_catalog WHERE is_active=TRUE ORDER BY sort_order,ribbon_name""")
    progress_map={r['code']:r for r in ribbon_progress_for(personnel_id,award_completed=False)}
    earned_map={r['ribbon_code']:r for r in fetch_all("SELECT * FROM personnel_ribbons WHERE personnel_id=%s",(personnel_id,))}
    award_rows=fetch_all("""SELECT pa.award_name,pa.award_date,pa.order_number,pa.citation,
                                   rc.ribbon_code
                            FROM personnel_awards pa
                            LEFT JOIN ribbon_catalog rc ON LOWER(TRIM(rc.ribbon_name))=LOWER(TRIM(pa.award_name))
                            WHERE pa.personnel_id=%s
                            ORDER BY pa.award_date DESC,pa.id DESC""",(personnel_id,))
    history_by={}
    for a in award_rows:
        code=a.get('ribbon_code')
        if not code: continue
        history_by.setdefault(code,[]).append({
            'award_date': a.get('award_date').isoformat() if hasattr(a.get('award_date'),'isoformat') else str(a.get('award_date') or ''),
            'order_number': a.get('order_number') or 'ORDER NUMBER NOT ENTERED',
            'citation': a.get('citation') or '',
        })
    details=[]
    for c in catalog:
        code=c['ribbon_code']; earned=earned_map.get(code); prog=progress_map.get(code) or {}
        history=history_by.get(code,[])
        # Automatic ribbons may have been filed directly into personnel_ribbons before a
        # personnel_awards row existed.  Count the authorization itself as award #1.
        award_count=max(len(history),1 if earned else 0)
        details.append({
            **c,
            'earned': bool(earned),
            'earned_at': (earned.get('earned_at').isoformat() if earned and hasattr(earned.get('earned_at'),'isoformat') else str((earned or {}).get('earned_at') or '')),
            'is_worn': bool((earned or {}).get('is_worn')),
            'award_count': award_count,
            'device_label': _award_device_label(award_count),
            'history': history,
            'progress': prog,
            'progress_percent': int(prog.get('percent') or (100 if earned else 0)),
            'progress_detail': prog.get('detail') or ('COMMAND RECOMMENDATION / HEADQUARTERS APPROVAL REQUIRED.' if c.get('automation_mode')=='RECOMMENDATION' else c.get('requirement_text')),
        })
    return details


def award_eligibility_board(personnel_rows):
    """S-1 read-only eligibility board.  It never awards or changes personnel."""
    eligible=[]; nearing=[]
    for person in personnel_rows:
        pid=person.get('id')
        if not pid: continue
        try:
            progress=ribbon_progress_for(pid,award_completed=False)
        except Exception as exc:
            log.warning('Award eligibility read failed for %s: %s',pid,exc)
            continue
        for row in progress:
            if row.get('earned') or row.get('pending_system'): continue
            entry={'person':person,'ribbon':row}
            if row.get('complete'):
                eligible.append(entry)
            elif int(row.get('percent') or 0) >= 70:
                nearing.append(entry)
    eligible.sort(key=lambda x:(str(x['ribbon'].get('name') or ''),str(x['person'].get('last_name') or '')))
    nearing.sort(key=lambda x:(-int(x['ribbon'].get('percent') or 0),str(x['person'].get('last_name') or '')))
    return {'eligible':eligible[:40],'nearing':nearing[:40]}

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
    """Build the issued-uniform ribbon rack left-to-right, top-to-bottom.

    The first earned/worn ribbon always occupies the upper-left slot. Each row
    fills to a maximum of three ribbons before a new row begins. This geometry
    is shared by the service-uniform views so the rack remains predictable and
    visually consistent everywhere.
    """
    ribbons = list(ribbons or [])
    if not ribbons:
        return []
    max_per_row = max(1, int(max_per_row or 3))
    return [ribbons[i:i + max_per_row] for i in range(0, len(ribbons), max_per_row)]


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
    """Return the rank the Soldier held when first entered on the Battle Roster."""
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
    if not welcome_packet_promotion_open(pid):
        return []
    progress = personnel_progress(pid)
    tig = time_in_grade_days(personnel)
    ops = credited_operation_count(pid)
    dqs = duty_qualification_count(pid)
    readiness = int(personnel.get('readiness_percent') or 0)
    server_service = verified_server_service_snapshot(pid)
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
        results.append({**path, "requirements": items, "eligible": eligible, "recommended": recommended, "status": status, "tig": tig, "operations": ops,
                        "verified_server_hours":float(server_service.get('hours_lifetime') or 0.0),
                        "verified_server_hours_7d":float(server_service.get('hours_7d') or 0.0),
                        "server_last_activity":server_service.get('last_activity')})
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
    notify_soldier(
        personnel_id, "S-3 TRAINING", f"Qualification earned — {q['display_name']}",
        f"{q['display_name']} has been entered as a current qualification in your service record through {expiration_date}.",
        notification_type="QUALIFICATION", priority="ROUTINE",
        source_key=f"QUALIFICATION-NOTICE:{personnel_id}:{qualification_type_id}:{qdate}", target_anchor="training",
    )
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


def _mos_role_time(personnel_id, mos_code):
    """Return verified HLL server time actually spent in roles mapped to this MOS.

    The player/match ledger is authoritative. Generic connected time, Discord
    presence, operation attendance, training, and unrelated roles never count.
    """
    mos=str(mos_code or '').strip().upper()
    if not personnel_id or not mos:
        return {'seconds':0,'role_ids':[],'roles':[],'mapping_available':False}
    mappings={}
    try:
        for row in fetch_all("""SELECT role_id,verified_role_name,mos_code,verified
                                FROM hll_role_mappings
                                WHERE verified=TRUE AND UPPER(COALESCE(mos_code,''))=%s""",(mos,)) or []:
            mappings[str(row.get('role_id'))]=dict(row)
    except Exception:
        mappings={}
    # Built-in confirmed mappings keep progression working during staggered
    # website/bot deployments and also repair old blank MOS mapping rows.
    for rid,known in HLL_KNOWN_ROLE_MAPPINGS.items():
        if known.get('verified') and str(known.get('mos_code') or '').upper()==mos:
            current=mappings.get(str(rid),{})
            current.update({'role_id':str(rid),'verified_role_name':known.get('verified_role_name'),
                            'mos_code':mos,'verified':True})
            mappings[str(rid)]=current
    role_ids=sorted(mappings.keys(), key=lambda x: int(x) if str(x).isdigit() else 9999)
    if not role_ids:
        return {'seconds':0,'role_ids':[],'roles':[],'mapping_available':False}
    totals={rid:0 for rid in role_ids}
    try:
        rows=fetch_all("SELECT role_seconds FROM hll_player_match_stats WHERE personnel_id=%s",(str(personnel_id),)) or []
        for row in rows:
            payload=_hll_json_dict(row.get('role_seconds'))
            for rid in role_ids:
                totals[rid]+=max(0,int(payload.get(rid,0) or 0))
    except Exception:
        pass
    roles=[{'role_id':rid,'role_name':mappings[rid].get('verified_role_name') or f'ROLE {rid}',
            'seconds':totals[rid]} for rid in role_ids]
    return {'seconds':sum(totals.values()),'role_ids':role_ids,'roles':roles,'mapping_available':True}


def _mos_proficiency_stage(mos_code, mos_title, seconds, mapping_available=True):
    """Five-stage server-time progression. An assigned MOS is not automatically rated."""
    mos=str(mos_code or '').upper(); title=str(mos_title or mos or 'MOS').strip()
    hours=max(0,float(seconds or 0)/3600.0)
    if not mapping_available:
        return {'order':0,'level':'NOT YET TRACKABLE','next_hours':None,'threshold_hours':None,
                'next_requirement':'WAITING FOR A VERIFIED HLL ROLE → MOS MAPPING'}
    # A new MOS starts UNRATED. The first proficiency grade must be earned on the server.
    if hours < 1:
        order=0; suffix='UNRATED'; next_hours=1
    elif hours < 5:
        order=1; suffix='III'; next_hours=5
    elif hours < 15:
        order=2; suffix='II'; next_hours=15
    elif hours < 30:
        order=3; suffix='I'; next_hours=30
    else:
        order=4; suffix='SENIOR'; next_hours=None
    if suffix=='UNRATED':
        label=f'{title} — Unrated'
    elif mos=='11R':
        label={'III':'Rifleman III','II':'Rifleman II','I':'Rifleman I','SENIOR':'Senior Rifleman'}[suffix]
    else:
        label=f'Senior {title}' if suffix=='SENIOR' else f'{title} {suffix}'
    if next_hours is None:
        next_text='MAXIMUM MOS PROFICIENCY — 30+ VERIFIED HOURS IN MATCHING SERVER ROLE'
    else:
        remaining=max(0.0,next_hours-hours)
        next_text=f'{remaining:.1f} MORE VERIFIED SERVER HOURS IN {title.upper()} ROLE ({next_hours}H TOTAL)'
    lower={0:0,1:1,2:5,3:15,4:30}[order]
    upper=next_hours
    if upper is None:
        progress=100
    else:
        span=max(0.001,upper-lower)
        progress=max(0,min(100,round(((hours-lower)/span)*100)))
    return {'order':order,'level':label,'next_hours':next_hours,'threshold_hours':lower,
            'next_requirement':next_text,'progress_percent':progress}


def sync_mos_proficiency(person):
    """Server-authoritative MOS proficiency; never changes rank or command authority."""
    if not person or not person.get('id') or not person.get('mos_code'):
        return None
    pid=person['id']; mos=str(person['mos_code']).strip().upper()
    title_row=fetch_one("SELECT mos_title FROM battalion_mos_catalog WHERE mos_code=%s",(mos,)) or {}
    base=(title_row.get('mos_title') or person.get('duty_position') or mos).strip()
    telemetry=_mos_role_time(pid,mos)
    stage=_mos_proficiency_stage(mos,base,telemetry.get('seconds',0),telemetry.get('mapping_available',False))
    seconds=int(telemetry.get('seconds') or 0); hours=seconds/3600.0
    # Only the current primary MOS is current. Prior MOS proficiency remains in
    # history but cannot compete with the active MOS display.
    execute("UPDATE personnel_mos_proficiency SET is_current=FALSE WHERE personnel_id=%s AND mos_code<>%s AND is_current=TRUE",(pid,mos))
    current=fetch_one("SELECT * FROM personnel_mos_proficiency WHERE personnel_id=%s AND mos_code=%s",(pid,mos))
    label=stage['level']; order=int(stage['order'])
    remarks=(f"SERVER-AUTHORITATIVE • {hours:.2f} verified hours • matching role IDs: " +
             (', '.join(telemetry.get('role_ids') or []) if telemetry.get('role_ids') else 'NONE VERIFIED'))
    if not current or int(current.get('proficiency_order') or -1)!=order or current.get('proficiency_level')!=label or current.get('remarks')!=remarks or not current.get('is_current'):
        execute("""INSERT INTO personnel_mos_proficiency(personnel_id,mos_code,proficiency_level,proficiency_order,effective_date,certified_by,remarks,is_current)
                   VALUES(%s,%s,%s,%s,CURRENT_DATE,'HLL SERVER SERVICE SYSTEM',%s,TRUE)
                   ON CONFLICT(personnel_id,mos_code) DO UPDATE SET proficiency_level=EXCLUDED.proficiency_level,
                     proficiency_order=EXCLUDED.proficiency_order,
                     effective_date=CASE WHEN personnel_mos_proficiency.proficiency_order<>EXCLUDED.proficiency_order OR personnel_mos_proficiency.proficiency_level<>EXCLUDED.proficiency_level THEN CURRENT_DATE ELSE personnel_mos_proficiency.effective_date END,
                     certified_by=EXCLUDED.certified_by,remarks=EXCLUDED.remarks,is_current=TRUE""",
                (pid,mos,label,order,remarks))
    return {'mos_code':mos,'mos_title':base,'level':label,'proficiency_level':label,'order':order,
            'server_seconds':seconds,'server_hours':hours,'role_ids':telemetry.get('role_ids') or [],
            'role_breakdown':telemetry.get('roles') or [],'mapping_available':telemetry.get('mapping_available',False),
            'progress_percent':stage.get('progress_percent',0),'next_requirement':stage['next_requirement']}


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
    """Return a member-safe snapshot of a company/platoon formation.

    Formation navigation is a core member workflow, so stale optional metrics or
    a partially-migrated legacy assignment must degrade to an empty/pending
    panel rather than throwing a global Record Processing Error.
    """
    if not person or not person.get("unit_node_id"):
        return None
    try:
        target=unit_node(person["unit_node_id"])
        desired=str(unit_type or "").lower()
        visited=set()
        while target and str(target.get("unit_type") or "").lower()!=desired:
            tid=str(target.get("id") or "")
            if tid in visited:
                log.warning("Formation ancestry loop personnel=%s unit=%s", person.get("id"), tid)
                return None
            visited.add(tid)
            target=unit_node(target["parent_id"]) if target.get("parent_id") else None
        if not target:
            return None
        ids=unit_descendant_ids(target["id"]) or [target["id"]]
        roster=fetch_all("""SELECT p.id,p.rank_code,p.first_name,p.last_name,p.mos_code,p.duty_position,p.unit_code,p.platoon,p.squad,p.fire_team,p.readiness_percent,
              COALESCE(rc.precedence,0) AS rank_precedence
          FROM personnel p LEFT JOIN rank_catalog rc ON rc.rank_code=p.rank_code
          WHERE p.unit_node_id=ANY(%s) AND p.separated_at IS NULL AND COALESCE(p.archived,FALSE)=FALSE
          ORDER BY COALESCE(rc.precedence,0) DESC,p.last_name,p.first_name""",(ids,)) or []
        for row in roster:
            row["platoon"]=canonical_formation_label(row.get("platoon"),"PLATOON") if row.get("platoon") else None
            row["squad"]=canonical_formation_label(row.get("squad"),"SQUAD") if row.get("squad") else None
            row["fire_team"]=canonical_formation_label(row.get("fire_team"),"TEAM") if row.get("fire_team") else None
        leaders=fetch_all("""SELECT pa.appointment_code,ac.appointment_name,p.rank_code,p.first_name,p.last_name FROM personnel_appointments pa
          JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code JOIN personnel p ON p.id=pa.personnel_id
          WHERE pa.unit_node_id=%s AND pa.is_current=TRUE AND p.separated_at IS NULL ORDER BY ac.sort_order""",(target["id"],)) or []
        cohesion=safe_member_panel(f"{desired.title()} formation cohesion", None, unit_cohesion, target["id"])
        experience=safe_member_panel(f"{desired.title()} formation experience", None, unit_experience, target["id"])
        identity=safe_member_panel(f"{desired.title()} formation identity", None, unit_identity, target["id"])
        return {"unit":target,"roster":roster,"leaders":leaders,"cohesion":cohesion,"experience":experience,"identity":identity}
    except Exception:
        log.exception("Member formation snapshot unavailable personnel=%s type=%s", person.get("id"), unit_type)
        return None

def member_organization_context(person):
    """Personalized formation tree for member-facing organization views.

    The squad node remains the personnel assignment authority; Alpha/Bravo are the
    final fire-team layer. Chain-of-command relationships are derived from current
    appointments, never typed separately onto the member record.
    """
    if not person:
        return {"company":None,"platoon":None,"squad":None,"team":None,"breadcrumbs":[],"squad_members":[],"teams":{},"leaders":{}}
    p=dict(person)
    p["platoon"]=canonical_formation_label(p.get("platoon"),"PLATOON") if p.get("platoon") else None
    p["squad"]=canonical_formation_label(p.get("squad"),"SQUAD") if p.get("squad") else None
    p["fire_team"]=canonical_formation_label(p.get("fire_team"),"TEAM") if p.get("fire_team") else None
    ancestry=unit_ancestry(p.get("unit_node_id")) if p.get("unit_node_id") else []
    company=next((n for n in ancestry if str(n.get("unit_type") or "").lower()=="company"),None)
    platoon=next((n for n in ancestry if str(n.get("unit_type") or "").lower()=="platoon"),None)
    squad=next((n for n in ancestry if str(n.get("unit_type") or "").lower()=="squad"),None)
    if platoon: platoon={**platoon,"display_name":canonical_formation_label(platoon.get("display_name"),"PLATOON")}
    if squad: squad={**squad,"display_name":canonical_formation_label(squad.get("display_name"),"SQUAD")}
    company_name=(company or {}).get("display_name") or canonical_company_name(p.get("unit_code"))
    team=p.get("fire_team")
    squad_members=[]
    if squad:
        try:
            squad_members=fetch_all("""SELECT p.id,p.rank_code,p.first_name,p.last_name,p.mos_code,p.duty_position,p.unit_code,p.platoon,p.squad,p.fire_team,p.readiness_percent,
                                      COALESCE(rc.precedence,0) AS rank_precedence
                               FROM personnel p LEFT JOIN rank_catalog rc ON rc.rank_code=p.rank_code
                               WHERE p.unit_node_id=%s AND p.separated_at IS NULL AND COALESCE(p.archived,FALSE)=FALSE
                               ORDER BY COALESCE(rc.precedence,0) DESC,p.last_name,p.first_name""",(squad["id"],)) or []
        except Exception:
            log.exception("Member organization squad roster unavailable personnel=%s",p.get("id"))
    for m in squad_members:
        m["platoon"]=canonical_formation_label(m.get("platoon"),"PLATOON") if m.get("platoon") else None
        m["squad"]=canonical_formation_label(m.get("squad"),"SQUAD") if m.get("squad") else None
        m["fire_team"]=canonical_formation_label(m.get("fire_team"),"TEAM") if m.get("fire_team") else None
    def leader(code,node):
        if not node: return None
        try:
            return fetch_one("""SELECT pa.appointment_code,ac.appointment_name,p.id AS personnel_id,p.rank_code,p.first_name,p.last_name,p.fire_team
                FROM personnel_appointments pa JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code
                JOIN personnel p ON p.id=pa.personnel_id
                WHERE pa.unit_node_id=%s AND pa.is_current=TRUE AND pa.appointment_code=%s AND p.separated_at IS NULL
                ORDER BY pa.effective_date DESC LIMIT 1""",(node["id"],code))
        except Exception:
            return None
    leaders={
        "company_commander":leader("CO_CO",company),"company_xo":leader("CO_XO",company),"first_sergeant":leader("CO_1SG",company),
        "platoon_leader":leader("PL",platoon),"platoon_sergeant":leader("PSG",platoon),"squad_leader":leader("SL",squad),
    }
    team_leaders={"Alpha Team":None,"Bravo Team":None}
    if squad:
        for team_name in ("Alpha Team","Bravo Team"):
            try:
                team_leaders[team_name]=fetch_one("""SELECT pa.appointment_code,ac.appointment_name,p.id AS personnel_id,p.rank_code,p.first_name,p.last_name,p.fire_team
                    FROM personnel_appointments pa JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code
                    JOIN personnel p ON p.id=pa.personnel_id
                    WHERE pa.unit_node_id=%s AND pa.is_current=TRUE AND pa.appointment_code IN ('FTL','TL','TEAM_LEADER')
                      AND UPPER(COALESCE(pa.fire_team,p.fire_team,''))=UPPER(%s) AND p.separated_at IS NULL
                    ORDER BY pa.effective_date DESC LIMIT 1""",(squad["id"],team_name))
            except Exception:
                team_leaders[team_name]=None
    leaders["team_leader"]=team_leaders.get(team) if team else None
    teams={"Alpha Team":[],"Bravo Team":[],"Squad HQ":[]}
    for m in squad_members:
        bucket=m.get("fire_team") if m.get("fire_team") in {"Alpha Team","Bravo Team"} else "Squad HQ"
        teams[bucket].append(m)
    readiness_values=[int(m.get("readiness_percent") or 0) for m in squad_members]
    avg_readiness=round(sum(readiness_values)/len(readiness_values)) if readiness_values else 0
    breadcrumbs=[]
    if company_name: breadcrumbs.append({"label":company_name,"level":"company"})
    if platoon or p.get("platoon"): breadcrumbs.append({"label":(platoon or {}).get("display_name") or p.get("platoon"),"level":"platoon"})
    if squad or p.get("squad"): breadcrumbs.append({"label":(squad or {}).get("display_name") or p.get("squad"),"level":"squad"})
    if team: breadcrumbs.append({"label":team,"level":"team"})
    return {"company":company,"company_name":company_name,"platoon":platoon,"squad":squad,"team":team,"breadcrumbs":breadcrumbs,
            "squad_members":squad_members,"teams":teams,"leaders":leaders,"team_leaders":team_leaders,"squad_readiness":avg_readiness,"squad_strength":len(squad_members),
            "member":p,"immediate_leader":leaders.get("team_leader") or leaders.get("squad_leader") or leaders.get("platoon_sergeant")}

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
    if insp: out.append({"title":"M16 inspection overdue" if insp.get("overdue") else "Next M16 inspection","detail":"Report to S-4 for inspection." if insp.get("overdue") else f"{insp.get('days')} day{'s' if int(insp.get('days') or 0) != 1 else ''} • due {insp.get('due')}","section":"S-4"})
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
    """Fault-isolated member career context.

    A stale/mid-migration table must never take the entire Career or Statistics
    page down. Each panel falls back independently while the rest of the record
    remains available.
    """
    if not person: return {}
    p=soldier_view(person); pid=p["id"]
    leadership_service=safe_member_panel("career leadership service", {"history":[],"totals":[],"total_days":0}, leadership_service_summary, pid)
    leadership_score=safe_member_panel("career leadership score", {"score":0,"rating":"NOT RATED","operations_led":0,"training_conducted":0}, combat_leadership_score, pid)
    career_stats=safe_member_panel("career service statistics", {"days_in_battalion":0,"operations":0,"training_events":0,"leadership_days":0,"qualifications":0,"awards":0,"rounds_fired":0,"m16_cleanings":0,"formations_served":0,"current_tour_day":0}, member_service_statistics, p)
    combat=safe_member_panel("career combat experience", {"level":"NEW ARRIVAL","operations":0,"order":0}, member_combat_experience, pid)
    tour=safe_member_panel("career tour phase", {"phase":"NEW IN COUNTRY","tour_day":0,"days_to_deros":None,"progress":0}, member_tour_phase, p)
    milestones=safe_member_panel("career milestones", [], member_career_milestones, p)
    assignments=safe_member_panel("career assignment history", [], member_assignment_history, pid)
    buddies=safe_member_panel("career buddy history", [], member_buddy_history, pid)
    weekly=safe_member_panel("career weekly report", {}, member_weekly_report, p)
    squad=safe_member_panel("career squad snapshot", None, member_formation_snapshot, p, "squad")
    mos=safe_member_panel("career MOS proficiency", {}, current_mos_proficiency, p) or {}
    return {"career_stats":career_stats,"combat_experience":combat,"career_tour":tour,
      "career_milestones":milestones,"leadership_service":leadership_service,"leadership_score":leadership_score,
      "assignment_history_full":assignments,"buddy_history":buddies,"weekly_report":weekly,
      "squad_snapshot":squad,
      "where_you_stand":{"tour":tour.get("progress",0),
                         "readiness":int(p.get("readiness_percent") or 0),
                         "promotion_readiness":0,
                         "mos_proficiency":mos.get("proficiency_level") or p.get("mos_code"),
                         "experience":combat.get("level"),
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


OPERATION_SITE_TIMEZONE = ZoneInfo(os.getenv("SITE_TIMEZONE", "America/New_York"))


def parse_operation_local_datetime(value):
    """Interpret datetime-local staff input in the battalion site timezone and store UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=OPERATION_SITE_TIMEZONE)
    return dt.astimezone(timezone.utc)


def operation_local_datetime(value):
    if not value:
        return None
    dt = value
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(OPERATION_SITE_TIMEZONE)


def safe_public_operation_number(value):
    """Only expose human S-3 operation numbers; never internal/Discord/UUID identifiers."""
    text = str(value or "").strip().upper()
    if re.fullmatch(r"OP-\\d{2,4}-\\d{4}", text):
        return text
    return "OPERATION"


def reconcile_operation_schedule_states():
    """Make website Operation status derive deterministically from its published schedule."""
    if not database_ready():
        return {"scheduled": 0, "active": 0, "completed": 0}
    rows = fetch_all("""SELECT id,start_at,duration_minutes,status,lifecycle_status,publish_status,completed_at,clerk_event_id
                        FROM operations
                        WHERE start_at IS NOT NULL
                          AND UPPER(COALESCE(publish_status,'DRAFT'))='PUBLISHED'
                          AND UPPER(COALESCE(status,'')) NOT IN ('CANCELLED','CANCELED','ARCHIVED')
                          AND UPPER(COALESCE(lifecycle_status,'')) NOT IN ('CANCELLED','CANCELED','ARCHIVED')""")
    now = datetime.now(timezone.utc)
    counts = {"scheduled": 0, "active": 0, "completed": 0}
    for op in rows:
        start = op.get("start_at")
        if not start:
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        end = start + timedelta(minutes=max(1, int(op.get("duration_minutes") or 90)))
        if now < start:
            status, lifecycle = "SCHEDULED", "PUBLISHED"
            counts["scheduled"] += 1
        elif now < end:
            status, lifecycle = "ACTIVE", "ACTIVE"
            counts["active"] += 1
        else:
            status, lifecycle = "COMPLETED", "COMPLETED"
            counts["completed"] += 1
        execute("""UPDATE operations
                   SET status=%s,lifecycle_status=%s,
                       completed_at=CASE WHEN %s='COMPLETED' THEN COALESCE(completed_at,%s) ELSE completed_at END,
                       updated_at=CASE WHEN UPPER(COALESCE(status,''))<>%s OR UPPER(COALESCE(lifecycle_status,''))<>%s THEN NOW() ELSE updated_at END
                   WHERE id=%s""", (status,lifecycle,status,end,status,lifecycle,op["id"]))
    return counts


def decorate_operation_times(rows):
    for row in rows or []:
        row["local_start_at"] = operation_local_datetime(row.get("start_at") or row.get("starts_at"))
        end = row.get("ends_at")
        if not end and row.get("start_at"):
            end = row["start_at"] + timedelta(minutes=max(1,int(row.get("duration_minutes") or 90)))
        row["local_end_at"] = operation_local_datetime(end)
        row["public_operation_number"] = safe_public_operation_number(row.get("operation_number"))
    return rows


def ensure_published_operation_events(authority="SYSTEM RECONCILIATION"):
    """Repair a missing/stale Battalion Clerk mirror for every live published Operation."""
    if not database_ready():
        return 0
    execute("""UPDATE operations SET publish_status='PUBLISHED',updated_at=NOW()
               WHERE clerk_event_id IS NOT NULL
                 AND UPPER(COALESCE(status,'')) IN ('SCHEDULED','ACTIVE')
                 AND UPPER(COALESCE(publish_status,'DRAFT'))='DRAFT'""")
    rows=fetch_all("""SELECT o.* FROM operations o
                       LEFT JOIN battalion_events e ON e.operation_id=o.id
                         AND UPPER(COALESCE(e.status,'')) IN ('SCHEDULED','ACTIVE')
                       WHERE o.start_at IS NOT NULL
                         AND UPPER(COALESCE(o.publish_status,'DRAFT'))='PUBLISHED'
                         AND UPPER(COALESCE(o.status,'')) IN ('SCHEDULED','ACTIVE')
                         AND (o.start_at + make_interval(mins => COALESCE(o.duration_minutes,90)))>NOW()
                         AND e.id IS NULL""")
    repaired=0
    for op in rows:
        try:
            schedule_operation_event(op,authority)
            repaired += 1
        except Exception:
            log.exception("Failed to repair Clerk event mirror for Operation %s",op.get("id"))
    return repaired


def public_scheduled_operations(limit=5):
    """Published website Operations are the sole public schedule source."""
    try:
        reconcile_operation_schedule_states()
        ensure_published_operation_events("PUBLIC SCHEDULE RECONCILIATION")
        rows = fetch_all("""
            SELECT o.id::text AS source_id,o.id AS operation_id,o.title,
                   COALESCE(o.operation_type,'OFFICIAL OPERATION') AS event_type,
                   o.start_at AS starts_at,
                   (o.start_at + make_interval(mins => COALESCE(o.duration_minutes,90))) AS ends_at,
                   'WEBSITE' AS schedule_source,COALESCE(o.area_of_operations,o.location) AS area_of_operations,
                   o.operation_number,o.status,o.duration_minutes
            FROM operations o
            WHERE o.start_at IS NOT NULL
              AND UPPER(COALESCE(o.publish_status,'DRAFT'))='PUBLISHED'
              AND UPPER(COALESCE(o.status,'')) IN ('SCHEDULED','ACTIVE')
              AND (o.start_at + make_interval(mins => COALESCE(o.duration_minutes,90))) > NOW()
            ORDER BY o.start_at ASC LIMIT %s
        """,(int(limit),))
        return decorate_operation_times(rows)
    except Exception:
        log.exception("Public Operation schedule query failed")
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
    try:
        detachment_rows = replacement_detachment_rows()
        processing = len(detachment_rows)
    except Exception:
        log.exception("Public Replacement Detachment count unavailable")
        processing = int(row.get("processing") or 0)
    return {
        "applications_pending": int(row.get("applications_pending") or 0),
        "command_review": int(row.get("command_review") or 0),
        "processing": int(processing or 0),
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
        SELECT ribbon_code,ribbon_name,image_filename,requirement_text,description_text,earning_text,award_type_label
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
        reconcile_operation_schedule_states()
        rows = fetch_all("""
            SELECT o.id,o.operation_number,o.title,o.operation_type,
                   COALESCE(o.completed_at,o.start_at,o.created_at) completed_at,
                   o.result,o.status,o.lifecycle_status,o.duration_minutes
            FROM operations o
            WHERE UPPER(COALESCE(o.publish_status,'DRAFT')) IN ('PUBLISHED','CLOSED')
              AND (UPPER(COALESCE(o.status,'')) IN ('COMPLETED','CLOSED','ARCHIVED')
                   OR UPPER(COALESCE(o.lifecycle_status,'')) IN ('COMPLETED','CLOSED','ARCHIVED','AAR FILED'))
            ORDER BY COALESCE(o.completed_at,o.start_at,o.created_at) DESC NULLS LAST
            LIMIT 24
        """)
        return decorate_operation_times(rows)
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
                try: welcome_complete_task(p["id"],"STANDING_ORDERS","SOLDIER")
                except Exception: log.exception("Welcome Packet Standing Orders milestone failed")
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
                try:
                    welcome_complete_task(card["personnel_id"],"WEBSITE_LOGIN","SOLDIER")
                except Exception:
                    log.exception("Welcome Packet first-login milestone failed for %s",card["personnel_id"])
                flash(f"DUTY STATUS CONFIRMED — {card['rank_code']} {card['last_name'].upper()}.", "success")
                wp=fetch_one("SELECT status FROM welcome_packets WHERE personnel_id=%s",(card["personnel_id"],))
                return redirect(url_for(member_landing_endpoint(card["personnel_id"])))
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


def safe_member_fetch_one(label, sql, params=()):
    """Run an optional Wall Locker query without allowing one module to take down the record."""
    return safe_member_panel(label, None, fetch_one, sql, params)


def safe_member_fetch_all(label, sql, params=()):
    """Run an optional Wall Locker list query with an empty-list fallback on schema/data drift."""
    return safe_member_panel(label, [], fetch_all, sql, params)


def _json_safe_value(value):
    """Convert PostgreSQL/native Python values into browser-safe JSON primitives."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", "replace")
    if isinstance(value, dict):
        return {str(k): _json_safe_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe_value(v) for v in value]
    return str(value)


def member_json_payload(value):
    """Never allow a member page to fail while serializing optional interactive data."""
    try:
        return json.dumps(_json_safe_value(value), ensure_ascii=False).replace("</", "<\\/")
    except Exception:
        ref=secrets.token_hex(4).upper()
        log.exception("MEMBER JSON SERIALIZATION FAILURE [%s]", ref)
        return "[]"


def _safe_member_endpoint(endpoint, fallback="my_action_center"):
    endpoint=str(endpoint or "").strip()
    return endpoint if endpoint in app.view_functions else fallback


def sanitize_member_nav_rows(rows, fallback="my_action_center"):
    out=[]
    for row in rows or []:
        item=dict(row or {})
        item["endpoint"]=_safe_member_endpoint(item.get("endpoint"), fallback)
        out.append(item)
    return out

def clean_gamertag(value):
    """Display-safe gamertag formatting: battalion style omits commas and periods."""
    return str(value or "").replace(",", "").replace(".", "").strip()

def clean_soldier_name(last_name, first_name=None, initials=False):
    """Render personnel names without legacy comma/period punctuation."""
    last = clean_gamertag(last_name).upper()
    first = clean_gamertag(first_name)
    if initials and first:
        first = first[:1].upper()
    elif first:
        first = first.upper()
    return " ".join(part for part in (last, first) if part).strip()

app.jinja_env.filters["gamertag"] = clean_gamertag
app.jinja_env.filters["soldier_name"] = clean_soldier_name
app.jinja_env.globals["member_json_payload"] = member_json_payload

def current_mos_proficiency(person):
    """Always present the live server-derived MOS grade, repairing stale legacy rows."""
    if not person or not person.get("id") or not person.get("mos_code"):
        return None
    try:
        return sync_mos_proficiency(person)
    except Exception:
        log.exception("MOS proficiency synchronization failed for %s", person.get('id'))
        return None



def _hll_json_dict(value):
    """Normalize JSON/JSONB read values without allowing telemetry display to break a member page."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed=json.loads(value)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _hll_mode_from_layer(map_id):
    raw=str(map_id or '').lower()
    if 'offensivenva' in raw: return 'NVA Offensive'
    if 'offensiveus' in raw: return 'US Offensive'
    if 'warfare' in raw: return 'Warfare'
    if 'domination' in raw: return 'Domination'
    if 'conquest' in raw: return 'Conquest'
    return ''


HLL_KNOWN_ROLE_MAPPINGS = {
    # Confirmed HLL: Vietnam role IDs mapped to the 1/5 Cavalry battlefield MOS
    # catalog. MOS progression only credits seconds in a verified matching role.
    "0": {"verified_role_name": "RIFLEMAN", "role_category": "INFANTRY", "mos_code": "11R", "verified": True},
    "3": {"verified_role_name": "MEDIC", "role_category": "MEDICAL", "mos_code": "91M", "verified": True},
    "5": {"verified_role_name": "SPECIALIST", "role_category": "INFANTRY", "mos_code": "", "verified": True},
    "6": {"verified_role_name": "MACHINE GUNNER", "role_category": "INFANTRY", "mos_code": "11M", "verified": True},
    "7": {"verified_role_name": "GRENADIER", "role_category": "INFANTRY", "mos_code": "11G", "verified": True},
    "8": {"verified_role_name": "ENGINEER", "role_category": "SUPPORT", "mos_code": "12E", "verified": True},
    "9": {"verified_role_name": "SQUAD LEADER", "role_category": "LEADERSHIP", "mos_code": "11L", "verified": True},
    "11": {"verified_role_name": "CREWMAN", "role_category": "ARMOR", "mos_code": "19K", "verified": True},
    "12": {"verified_role_name": "TANK COMMANDER", "role_category": "ARMOR", "mos_code": "19C", "verified": True},
    "16": {"verified_role_name": "PILOT", "role_category": "AVIATION", "mos_code": "67P", "verified": True},
    "17": {"verified_role_name": "LOGISTICS OFFICER", "role_category": "AVIATION", "mos_code": "67L", "verified": True},
}

def hll_role_display_name(role_id, mappings=None):
    if role_id is None:
        return "—"
    key=str(role_id)
    mapping=(mappings or {}).get(key) or HLL_KNOWN_ROLE_MAPPINGS.get(key) or {}
    if mapping.get("verified_role_name"):
        return str(mapping.get("verified_role_name"))
    return f"UNVERIFIED ROLE {key}"


def hll_service_statistics(personnel_id):
    """Fault-isolated HLL: Vietnam telemetry summary for one Soldier.

    Battalion Clerk owns collection/storage. The website only reads those tables.
    Missing RCON tables or collector downtime must never affect personnel pages.
    """
    empty={
        "available":False,"linked":False,"steam_id":None,"player_name":None,
        "totals":{"matches":0,"connected_seconds":0,"distance_meters":0.0,"altitude_gain_meters":0.0,
                  "infantry_kills":0,"deaths":0,"team_kills":0,"vehicle_kills":0,"vehicles_destroyed":0,
                  "combat_score":0,"defense_score":0,"offense_score":0,"support_score":0,
                  "max_observed_speed_mps":0.0,"high_speed_seconds":0},
        "recent":[],"live":None,"server":None,"role_seconds":{},"role_distance_meters":{},
        "role_max_speed_mps":{},"role_high_speed_seconds":{},"role_airmobile_seconds":{},"role_airmobile_distance_meters":{},"map_ledger":[],
        "role_mappings":{k:dict(v) for k,v in HLL_KNOWN_ROLE_MAPPINGS.items()},"role_ledger":[],"primary_role":None,
        "armor_service":{"crewman_seconds":0,"tank_commander_seconds":0,"total_seconds":0,"crewman_distance_meters":0.0,"tank_commander_distance_meters":0.0,"total_distance_meters":0.0,"rounds":0,"maps":[],"active":False},
        "aviation_service":{"pilot_verified":False,"pilot_seconds":0,"pilot_distance_meters":0.0,"slick_seconds":0,"slick_distance_meters":0.0},
        "leadership_experience":{"total_seconds":0,"roles":[],"role_ids":["9","12","17"]},
    }
    if personnel_id in (None, ""):
        return empty
    try:
        link=fetch_one("SELECT steam_id,hll_player_name,verified,linked_at FROM hll_personnel_links WHERE personnel_id=%s LIMIT 1",(str(personnel_id),))
        empty["available"]=True
        if not link:
            return empty
        empty["linked"]=True
        empty["steam_id"]=link.get("steam_id")
        empty["player_name"]=link.get("hll_player_name")
        total=fetch_one("""SELECT COUNT(*) AS matches,
                    COALESCE(SUM(connected_seconds),0) AS connected_seconds,
                    COALESCE(SUM(distance_meters),0) AS distance_meters,
                    COALESCE(SUM(altitude_gain_meters),0) AS altitude_gain_meters,
                    COALESCE(SUM(infantry_kills),0) AS infantry_kills,
                    COALESCE(SUM(deaths),0) AS deaths,
                    COALESCE(SUM(team_kills),0) AS team_kills,
                    COALESCE(SUM(vehicle_kills),0) AS vehicle_kills,
                    COALESCE(SUM(vehicles_destroyed),0) AS vehicles_destroyed,
                    COALESCE(SUM(combat_score),0) AS combat_score,
                    COALESCE(SUM(defense_score),0) AS defense_score,
                    COALESCE(SUM(offense_score),0) AS offense_score,
                    COALESCE(SUM(support_score),0) AS support_score,
                    COALESCE(MAX(max_observed_speed_mps),0) AS max_observed_speed_mps,
                    COALESCE(SUM(high_speed_seconds),0) AS high_speed_seconds,
                    MAX(last_seen_at) AS last_seen_at
                FROM hll_player_match_stats WHERE personnel_id=%s""",(str(personnel_id),)) or {}
        t=empty["totals"]
        for k in list(t):
            v=total.get(k)
            if k in {"distance_meters","altitude_gain_meters","max_observed_speed_mps"}: t[k]=float(v or 0)
            else: t[k]=int(v or 0)
        rows=fetch_all("""SELECT ps.*,ms.server_name,ms.map_id,ms.map_name,ms.game_mode,ms.started_at,ms.ended_at,ms.last_seen_at AS match_last_seen
                          FROM hll_player_match_stats ps
                          JOIN hll_match_sessions ms ON ms.id=ps.match_id
                          WHERE ps.personnel_id=%s
                          ORDER BY ps.last_seen_at DESC LIMIT 20""",(str(personnel_id),)) or []
        recent=[]; roles={}; role_dist={}; role_max_speed={}; role_high_speed={}; maps={}
        for raw in rows:
            r=dict(raw)
            recent.append(r)
            if not r.get("game_mode"):
                r["game_mode"]=_hll_mode_from_layer(r.get("map_id"))
            for k,v in _hll_json_dict(r.get("role_seconds")).items(): roles[str(k)]=roles.get(str(k),0)+int(v or 0)
            for k,v in _hll_json_dict(r.get("role_distance_meters")).items(): role_dist[str(k)]=role_dist.get(str(k),0.0)+float(v or 0)
            for k,v in _hll_json_dict(r.get("role_max_speed_mps")).items(): role_max_speed[str(k)]=max(role_max_speed.get(str(k),0.0),float(v or 0))
            for k,v in _hll_json_dict(r.get("role_high_speed_seconds")).items(): role_high_speed[str(k)]=role_high_speed.get(str(k),0)+int(v or 0)
            mk=str(r.get("map_name") or r.get("map_id") or "UNKNOWN")
            m=maps.setdefault(mk,{"map_name":mk,"matches":0,"seconds":0,"distance_meters":0.0,"kills":0,"deaths":0})
            m["matches"]+=1; m["seconds"]+=int(r.get("connected_seconds") or 0); m["distance_meters"]+=float(r.get("distance_meters") or 0); m["kills"]+=int(r.get("infantry_kills") or 0); m["deaths"]+=int(r.get("deaths") or 0)
        empty["recent"]=recent

        # Lifetime role aggregation. Recent rows remain only for the latest-contact display.
        roles={}; role_dist={}; role_max_speed={}; role_high_speed={}; role_airmobile={}; role_airmobile_dist={}
        try:
            lifetime_role_rows=fetch_all("""SELECT role_seconds,role_distance_meters,role_max_speed_mps,role_high_speed_seconds,
                                                    role_airmobile_seconds,role_airmobile_distance_meters
                                             FROM hll_player_match_stats WHERE personnel_id=%s""",(str(personnel_id),)) or []
        except Exception:
            # Allows the website to remain usable during a staggered deploy before
            # Battalion Clerk creates the new Air Cav telemetry columns.
            lifetime_role_rows=fetch_all("""SELECT role_seconds,role_distance_meters,role_max_speed_mps,role_high_speed_seconds
                                             FROM hll_player_match_stats WHERE personnel_id=%s""",(str(personnel_id),)) or []
        for rr in lifetime_role_rows:
            for k,v in _hll_json_dict(rr.get('role_seconds')).items(): roles[str(k)]=roles.get(str(k),0)+int(v or 0)
            for k,v in _hll_json_dict(rr.get('role_distance_meters')).items(): role_dist[str(k)]=role_dist.get(str(k),0.0)+float(v or 0)
            for k,v in _hll_json_dict(rr.get('role_max_speed_mps')).items(): role_max_speed[str(k)]=max(role_max_speed.get(str(k),0.0),float(v or 0))
            for k,v in _hll_json_dict(rr.get('role_high_speed_seconds')).items(): role_high_speed[str(k)]=role_high_speed.get(str(k),0)+int(v or 0)
            for k,v in _hll_json_dict(rr.get('role_airmobile_seconds')).items(): role_airmobile[str(k)]=role_airmobile.get(str(k),0)+int(v or 0)
            for k,v in _hll_json_dict(rr.get('role_airmobile_distance_meters')).items(): role_airmobile_dist[str(k)]=role_airmobile_dist.get(str(k),0.0)+float(v or 0)

        mapping_rows=fetch_all("""SELECT role_id,verified_role_name,role_category,mos_code,verified
                                  FROM hll_role_mappings""") or []
        mappings={str(x.get('role_id')):dict(x) for x in mapping_rows}
        # Confirmed HLL: Vietnam role IDs remain available immediately even during
        # staggered website/bot deploys before the mapping seed reaches PostgreSQL.
        for known_id, known in HLL_KNOWN_ROLE_MAPPINGS.items():
            current=mappings.get(known_id,{})
            current.update(known)
            mappings[known_id]=current

        # True lifetime Area-of-Operations ledger, one row per map.
        map_rows=fetch_all("""SELECT COALESCE(ms.map_name,ms.map_id,'UNKNOWN') AS map_name,
                    COUNT(*) AS matches,COALESCE(SUM(ps.connected_seconds),0) AS seconds,
                    COALESCE(SUM(ps.distance_meters),0) AS distance_meters,
                    COALESCE(SUM(ps.infantry_kills),0) AS kills,COALESCE(SUM(ps.deaths),0) AS deaths,
                    COALESCE(SUM(ps.vehicle_kills),0) AS vehicle_kills,COALESCE(SUM(ps.vehicles_destroyed),0) AS vehicles_destroyed,
                    COALESCE(SUM(ps.combat_score+ps.offense_score+ps.defense_score+ps.support_score),0) AS score_total
                 FROM hll_player_match_stats ps JOIN hll_match_sessions ms ON ms.id=ps.match_id
                 WHERE ps.personnel_id=%s GROUP BY COALESCE(ms.map_name,ms.map_id,'UNKNOWN')
                 ORDER BY seconds DESC,matches DESC""",(str(personnel_id),)) or []
        empty["role_seconds"]=roles
        empty["role_distance_meters"]=role_dist
        empty["role_max_speed_mps"]=role_max_speed
        empty["role_high_speed_seconds"]=role_high_speed
        empty["role_airmobile_seconds"]=role_airmobile
        empty["role_airmobile_distance_meters"]=role_airmobile_dist
        empty["role_mappings"]=mappings
        empty["map_ledger"]=[dict(m) for m in map_rows]
        role_ledger=[]
        for role_id, seconds in sorted(roles.items(), key=lambda item: int(item[1] or 0), reverse=True):
            mapping=mappings.get(str(role_id),{})
            verified=bool(mapping.get('verified'))
            role_name=(mapping.get('verified_role_name') if verified else None)
            role_ledger.append({
                "role_id": str(role_id),
                "role_name": role_name,
                "display_name": role_name or f"UNVERIFIED ROLE {role_id}",
                "role_category": (mapping.get('role_category') if verified else None),
                "mos_code": (mapping.get('mos_code') if verified else None),
                "verified": verified,
                "seconds": int(seconds or 0),
                "distance_meters": float(role_dist.get(str(role_id),0.0) or 0.0),
                "max_speed_mps": float(role_max_speed.get(str(role_id),0.0) or 0.0),
                "high_speed_seconds": int(role_high_speed.get(str(role_id),0) or 0),
                "airmobile_seconds": int(role_airmobile.get(str(role_id),0) or 0),
                "airmobile_distance_meters": float(role_airmobile_dist.get(str(role_id),0.0) or 0.0),
            })
        empty["role_ledger"]=role_ledger
        empty["primary_role"]=(role_ledger[0] if role_ledger else None)

        # Armored service is derived from the same authoritative per-role ledger
        # already used by the rest of the HLL service record. No second collector
        # or competing clock is introduced. Confirmed mappings:
        # Role 11 = Crewman; Role 12 = Tank Commander.
        crewman_seconds=int(roles.get("11",0) or 0)
        commander_seconds=int(roles.get("12",0) or 0)
        crewman_distance=float(role_dist.get("11",0.0) or 0.0)
        commander_distance=float(role_dist.get("12",0.0) or 0.0)
        armor_map_rows=[]
        try:
            armor_rows=fetch_all("""SELECT COALESCE(ms.map_name,ms.map_id,'UNKNOWN') AS map_name,
                                      ps.role_seconds,ps.role_distance_meters
                               FROM hll_player_match_stats ps
                               JOIN hll_match_sessions ms ON ms.id=ps.match_id
                               WHERE ps.personnel_id=%s""",(str(personnel_id),)) or []
            armor_maps={}
            for ar in armor_rows:
                rs=_hll_json_dict(ar.get('role_seconds'))
                rd=_hll_json_dict(ar.get('role_distance_meters'))
                sec=int(rs.get('11',0) or 0)+int(rs.get('12',0) or 0)
                if sec <= 0:
                    continue
                name=str(ar.get('map_name') or 'UNKNOWN')
                rec=armor_maps.setdefault(name,{'map_name':name,'seconds':0,'distance_meters':0.0,'rounds':0})
                rec['seconds']+=sec
                rec['distance_meters']+=float(rd.get('11',0.0) or 0.0)+float(rd.get('12',0.0) or 0.0)
                rec['rounds']+=1
            armor_map_rows=sorted(armor_maps.values(),key=lambda x:(int(x['seconds']),int(x['rounds'])),reverse=True)
        except Exception:
            armor_map_rows=[]
        empty['armor_service']={
            'crewman_seconds':crewman_seconds,
            'tank_commander_seconds':commander_seconds,
            'total_seconds':crewman_seconds+commander_seconds,
            'crewman_distance_meters':crewman_distance,
            'tank_commander_distance_meters':commander_distance,
            'total_distance_meters':crewman_distance+commander_distance,
            'rounds':sum(int(x.get('rounds') or 0) for x in armor_map_rows),
            'maps':armor_map_rows,
            'active':bool(crewman_seconds or commander_seconds),
        }

        empty["mobility_observation"]={
            "max_speed_mps":float(t.get("max_observed_speed_mps") or 0),
            "high_speed_seconds":int(t.get("high_speed_seconds") or 0),
            "altitude_gain_meters":float(t.get("altitude_gain_meters") or 0),
            "note":"OBSERVATION ONLY — high-speed movement may be vehicle or aircraft travel and is not yet filed as flight time."
        }
        # Air Cav classification: confirmed helicopter-flight roles are
        # Role 16 = Pilot and Role 17 = Logistics Officer. Aircraft-signature
        # movement recorded in either role earns Airmobile Flight Time. Every
        # other role remains a Slick Ride so passengers/gunners are not promoted
        # into flight time merely because they were aboard the aircraft.
        pilot_seconds=pilot_distance=slick_seconds=slick_distance=0
        flight_role_ids={"16","17"}
        flight_role_verified=all(bool((mappings.get(rid) or {}).get('verified')) for rid in flight_role_ids)
        for role in role_ledger:
            a_sec=int(role.get('airmobile_seconds') or 0)
            a_dist=float(role.get('airmobile_distance_meters') or 0.0)
            if str(role.get('role_id')) in flight_role_ids and role.get('verified'):
                pilot_seconds += a_sec; pilot_distance += a_dist
            else:
                slick_seconds += a_sec; slick_distance += a_dist
        empty['aviation_service']={
            # Keep legacy pilot_* keys because templates/routes already consume them.
            'pilot_verified':flight_role_verified,'pilot_seconds':pilot_seconds,'pilot_distance_meters':pilot_distance,
            'flight_role_ids':sorted(flight_role_ids),'slick_seconds':slick_seconds,'slick_distance_meters':slick_distance,
            # Compatibility aliases for older templates/routes.
            'passenger_verified':bool(slick_seconds or slick_distance),'passenger_seconds':slick_seconds,'passenger_distance_meters':slick_distance,
            'status':'FLIGHT ROLES VERIFIED' if flight_role_verified else 'FLIGHT ROLE VERIFICATION PENDING',
            'note':'Pilot (Role 16) and Logistics Officer (Role 17) earn flight time when Air Cav movement is observed. All other roles are filed as Slick Rides.'
        }
        leadership_role_ids=("9","12","17")
        leadership_roles=[]
        leadership_total=0
        for leadership_role_id in leadership_role_ids:
            seconds=int(roles.get(leadership_role_id,0) or 0)
            leadership_total += seconds
            leadership_roles.append({'role_id':leadership_role_id,'display_name':hll_role_display_name(leadership_role_id,mappings),'seconds':seconds})
        empty['leadership_experience']={'total_seconds':leadership_total,'roles':leadership_roles,'role_ids':list(leadership_role_ids),
            'note':'Verified time served as Squad Leader, Tank Commander, or Logistics Officer. Experience only; no automatic personnel action.'}
        service_hours=float(t.get("connected_seconds") or 0)/3600.0
        match_count=int(t.get("matches") or 0)
        if service_hours >= 40 or match_count >= 25:
            field_level="VETERAN"
            field_note="Extensive RCON-verified field service on the battalion server."
        elif service_hours >= 15 or match_count >= 10:
            field_level="COMBAT TESTED"
            field_note="Sustained RCON-verified field service across multiple rounds."
        elif service_hours >= 5 or match_count >= 3:
            field_level="FIELD EXPERIENCED"
            field_note="Established RCON-verified field service beyond initial orientation."
        else:
            field_level="NEWLY ARRIVED"
            field_note="Early RCON-verified field service; experience develops through continued battalion play."
        empty["field_experience"]={"level":field_level,"hours":round(service_hours,2),"matches":match_count,"note":field_note}
        deaths=max(0,int(t.get("deaths") or 0))
        kills=max(0,int(t.get("infantry_kills") or 0))
        t["kd_ratio"]=round(kills / deaths, 2) if deaths else float(kills)
        t["score_total"]=int(t.get("combat_score") or 0)+int(t.get("offense_score") or 0)+int(t.get("defense_score") or 0)+int(t.get("support_score") or 0)
        t["blue_on_blue"]=int(t.get("team_kills") or 0)
        t["avg_distance_per_match_meters"]=(float(t.get("distance_meters") or 0)/int(t.get("matches") or 1)) if int(t.get("matches") or 0) else 0.0
        if rows:
            newest=dict(rows[0])
            if not newest.get("game_mode"):
                newest["game_mode"]=_hll_mode_from_layer(newest.get("map_id"))
            # A sample is considered live only when the collector saw it very recently.
            try:
                seen=newest.get("last_seen_at")
                if seen and (datetime.now(timezone.utc)-seen).total_seconds() <= max(30, int(os.getenv("HLL_RCON_POLL_SECONDS","5") or 5)*4):
                    empty["live"]=newest
            except Exception:
                pass
        try:
            lead=fetch_one("""SELECT COUNT(DISTINCT o.id) AS operations,COALESCE(SUM(ps.connected_seconds),0) AS seconds
                              FROM operation_participation op
                              JOIN operations o ON o.id=op.operation_id
                              JOIN hll_match_sessions ms ON ms.started_at <= o.start_at + make_interval(mins => COALESCE(o.duration_minutes,90)+30)
                                                        AND COALESCE(ms.ended_at,ms.last_seen_at) >= o.start_at - INTERVAL '30 minutes'
                              JOIN hll_player_match_stats ps ON ps.match_id=ms.id AND ps.personnel_id=%s
                              WHERE op.personnel_id=%s
                                AND (UPPER(COALESCE(op.duty_role,'')) LIKE '%%LEADER%%' OR UPPER(COALESCE(op.duty_role,'')) LIKE '%%SERGEANT%%'
                                  OR UPPER(COALESCE(op.duty_role,'')) LIKE '%%COMMAND%%' OR UPPER(COALESCE(op.duty_role,'')) LIKE '%%PLATOON%%')
                                AND COALESCE(ps.connected_seconds,0)>=300""",(str(personnel_id),str(personnel_id))) or {}
            empty["leadership_evidence"]={"operations":int(lead.get("operations") or 0),"seconds":int(lead.get("seconds") or 0),
                "note":"Verified server presence while officially assigned to a leadership duty. Evidence only; it does not grant appointment or promotion."}
        except Exception:
            empty["leadership_evidence"]={"operations":0,"seconds":0,"note":"Leadership evidence unavailable."}
        health=fetch_one("SELECT * FROM hll_rcon_health WHERE id=1")
        empty["server"]=dict(health) if health else None
        if empty["server"] and not empty["server"].get("last_game_mode"):
            if recent:
                empty["server"]["last_game_mode"]=recent[0].get("game_mode") or _hll_mode_from_layer(recent[0].get("map_id"))
        return empty
    except Exception:
        log.exception("HLL telemetry read unavailable for personnel %s",personnel_id)
        return empty


def hll_m16_service_statistics(personnel_id, weapon=None):
    """RCON-backed M16 service evidence for the currently issued rifle.

    Exact: M16-attributed kill / Blue on Blue log events.
    Estimated: ammunition expenditure, because HLLV does not expose shots fired.
    For a linked Soldier with an issued M16, verified active-server time is treated
    as issued-rifle field service. Exact M16 kills/Blue on Blue remain weapon-event
    evidence and are not inferred from presence.
    """
    result={
        'available':False,'verified_field_seconds':0,'verified_distance_meters':0.0,
        'm16_kills':0,'blue_on_blue':0,'last_verified_use':None,'recent_events':[],
        'operations_carried':0,'maps_carried':[],'map_count':0,'estimated_rounds_expended':0,'estimated_rounds_since_cleaning':0,'verified_field_seconds_since_cleaning':0,
        'estimate_rate_per_hour':300,
        'estimate_note':'ESTIMATE — HLL: Vietnam does not expose a shots-fired counter. For a linked Soldier with an issued M16, verified active-server time is treated as issued-rifle field service at 300 rounds/hour.'
    }
    try:
        pid=str(personnel_id)
        agg=fetch_one("""SELECT COALESCE(SUM(connected_seconds),0) AS seconds,COALESCE(SUM(distance_meters),0) AS distance
                         FROM hll_player_match_stats WHERE personnel_id=%s""",(pid,)) or {}
        result['verified_field_seconds']=int(agg.get('seconds') or 0)
        result['verified_distance_meters']=float(agg.get('distance') or 0)
        ev=fetch_one("""SELECT
              COUNT(*) FILTER (WHERE is_m16=TRUE AND event_type='KILL') AS kills,
              COUNT(*) FILTER (WHERE is_m16=TRUE AND event_type='BLUE_ON_BLUE') AS blue_on_blue,
              MAX(event_at) FILTER (WHERE is_m16=TRUE) AS last_use
            FROM hll_weapon_events WHERE personnel_id=%s""",(pid,)) or {}
        result['m16_kills']=int(ev.get('kills') or 0)
        result['blue_on_blue']=int(ev.get('blue_on_blue') or 0)
        result['recent_events']=safe_member_fetch_all('M16 RCON weapon events',"""SELECT event_at,event_type,attacker_name,victim_name,weapon_id,weapon_name
             FROM hll_weapon_events WHERE personnel_id=%s AND is_m16=TRUE ORDER BY event_at DESC LIMIT 30""",(pid,))
        sample_last=fetch_one("""SELECT MAX(observed_at) AS last_use FROM hll_research_samples
             WHERE personnel_id=%s AND COALESCE(connected_delta_seconds,0)>0""",(pid,)) or {}
        candidates=[x for x in (ev.get('last_use'),sample_last.get('last_use')) if x]
        result['last_verified_use']=max(candidates) if candidates else None
        result['maps_carried']=safe_member_fetch_all('M16 map service',"""SELECT COALESCE(ms.map_name,ms.map_id,'UNKNOWN') AS map_name,
                    COUNT(*) AS rounds,COALESCE(SUM(ps.connected_seconds),0)::bigint AS seconds,
                    COALESCE(SUM(ps.distance_meters),0)::double precision AS distance_meters
             FROM hll_player_match_stats ps JOIN hll_match_sessions ms ON ms.id=ps.match_id
             WHERE ps.personnel_id=%s AND COALESCE(ps.connected_seconds,0)>0
             GROUP BY COALESCE(ms.map_name,ms.map_id,'UNKNOWN') ORDER BY seconds DESC,map_name""",(pid,))
        result['map_count']=len(result['maps_carried'])
        rate=int(result['estimate_rate_per_hour'])
        result['estimated_rounds_expended']=int(round(result['verified_field_seconds']*rate/3600.0))
        clean_at=(weapon or {}).get('last_cleaned_at') if weapon else None
        if clean_at:
            row=fetch_one("""SELECT COALESCE(SUM(connected_delta_seconds),0) AS seconds
                 FROM hll_research_samples WHERE personnel_id=%s AND observed_at>%s
                   AND COALESCE(connected_delta_seconds,0)>0""",(pid,clean_at)) or {}
            since_seconds=int(row.get('seconds') or 0)
        else:
            since_seconds=result['verified_field_seconds']
        result['verified_field_seconds_since_cleaning']=since_seconds
        result['estimated_rounds_since_cleaning']=int(round(since_seconds*rate/3600.0))
        try:
            op=fetch_one("""SELECT COUNT(DISTINCT o.id) AS total
                FROM operation_participation op
                JOIN operations o ON o.id=op.operation_id
                JOIN hll_match_sessions ms ON ms.started_at <= o.start_at + make_interval(mins => COALESCE(o.duration_minutes,90)+30)
                  AND COALESCE(ms.ended_at,ms.last_seen_at) >= o.start_at - INTERVAL '30 minutes'
                JOIN hll_player_match_stats ps ON ps.match_id=ms.id AND ps.personnel_id=%s AND COALESCE(ps.connected_seconds,0)>0
                WHERE op.personnel_id=%s""",(pid,pid)) or {}
            result['operations_carried']=int(op.get('total') or 0)
        except Exception:
            pass
        result['available']=True
    except Exception:
        log.exception('HLL M16 service evidence unavailable for personnel %s',personnel_id)
    return result


def hll_live_server_snapshot():
    try:
        row=fetch_one("SELECT * FROM hll_rcon_health WHERE id=1")
        return dict(row) if row else {"enabled":False,"connected":False}
    except Exception:
        return {"enabled":False,"connected":False}


def public_hll_server_snapshot():
    """Return only public-safe HLL server state for the recruiting homepage.

    Deliberately excludes host/port, RCON errors, player identities, coordinates,
    and every other collector/internal field.  A stale collector heartbeat is
    treated as offline so the public page never advertises old data as live.
    """
    raw = hll_live_server_snapshot() or {}
    now = datetime.now(timezone.utc)
    last_success = raw.get("last_success_at")
    heartbeat = last_success or raw.get("updated_at")
    fresh = False
    try:
        if heartbeat:
            if getattr(heartbeat, "tzinfo", None) is None:
                heartbeat = heartbeat.replace(tzinfo=timezone.utc)
            # Collector normally updates every few seconds. Give production deploys /
            # reconnects a generous grace window so a still-valid snapshot is not
            # discarded while the bot reconnects.
            fresh = (now - heartbeat).total_seconds() <= 300
    except Exception:
        fresh = False

    # A single transient RCON failure should not make the public board flash offline.
    # If the collector had a successful sample very recently, keep that sample live
    # while the next poll/reconnect completes.
    success_fresh = False
    try:
        if last_success:
            success_stamp = last_success
            if getattr(success_stamp, "tzinfo", None) is None:
                success_stamp = success_stamp.replace(tzinfo=timezone.utc)
            success_fresh = (now - success_stamp).total_seconds() <= 45
    except Exception:
        success_fresh = False

    connected = fresh and (bool(raw.get("connected")) or success_fresh)
    players = max(0, int(raw.get("last_player_count") or 0)) if connected else 0

    # Secondary live-data path: use the most recently touched match row even if a
    # deploy/reconnect has already marked it ended. Player sightings are restricted
    # to the last 45 seconds, so this cannot advertise an old populated server.
    session_fallback = None
    if not connected:
        try:
            session_fallback = fetch_one("""
                SELECT ms.id, ms.server_name, ms.map_name, ms.game_mode, ms.last_seen_at,
                       (SELECT COUNT(DISTINCT ps.steam_id)
                          FROM hll_player_match_stats ps
                         WHERE ps.match_id=ms.id
                           AND ps.last_seen_at >= NOW() - INTERVAL '45 seconds') AS recent_players
                  FROM hll_match_sessions ms
                 WHERE ms.last_seen_at >= NOW() - INTERVAL '90 seconds'
                 ORDER BY ms.last_seen_at DESC
                 LIMIT 1
            """)
            if session_fallback:
                connected = True
                players = max(0, int(session_fallback.get("recent_players") or 0))
        except Exception:
            session_fallback = None

    max_players = max(1, int(os.getenv("HLL_PUBLIC_MAX_PLAYERS", "100") or 100))
    if not connected:
        state = "COMMUNICATIONS DOWN"
        state_key = "offline"
    elif players <= 0:
        state = "AWAITING PERSONNEL"
        state_key = "standby"
    elif players < 10:
        state = "REINFORCEMENTS REQUESTED"
        state_key = "seeding"
    else:
        state = "IN ACTION"
        state_key = "live"

    server_name = (
        raw.get("last_server_name")
        or (session_fallback or {}).get("server_name")
        or "1st Battalion, 5th Cavalry | Vietnam 1965 | US East"
    ).strip()
    map_name = (
        raw.get("last_map_name")
        or (session_fallback or {}).get("map_name")
        or "FIELD STATUS PENDING"
    ).strip() if connected else "FIELD STATUS UNAVAILABLE"
    game_mode = (
        raw.get("last_game_mode")
        or (session_fallback or {}).get("game_mode")
        or "STANDING BY"
    ).strip() if connected else "STANDING BY"

    # Count only members whose live HLL rows are already tied to a personnel record.
    # No names or identifiers are exposed publicly; the API returns only the count.
    cav_members_active = 0
    if connected:
        try:
            member_row = fetch_one("""
                SELECT COUNT(DISTINCT COALESCE(NULLIF(BTRIM(ps.personnel_id::text),''), l.personnel_id::text)) AS member_count
                  FROM hll_player_match_stats ps
                  JOIN hll_match_sessions ms ON ms.id=ps.match_id
                  LEFT JOIN hll_personnel_links l ON l.steam_id=ps.steam_id
                 WHERE ms.last_seen_at >= NOW() - INTERVAL '90 seconds'
                   AND ps.last_seen_at >= NOW() - INTERVAL '45 seconds'
                   AND (NULLIF(BTRIM(ps.personnel_id::text),'') IS NOT NULL OR l.personnel_id IS NOT NULL)
            """) or {}
            cav_members_active = max(0, int(member_row.get("member_count") or 0))
        except Exception:
            cav_members_active = 0

    return {
        "online": connected,
        "state": state,
        "state_key": state_key,
        "server_name": server_name,
        "map_name": map_name,
        "game_mode": game_mode,
        "player_count": players,
        "max_players": max_players,
        "cav_members_active": cav_members_active,
        "last_success_at": last_success.isoformat() if connected and last_success else None,
    }


@app.get("/api/public/server-status")
def public_server_status_api():
    fallback = {
        "online": False, "state": "COMMUNICATIONS DOWN", "state_key": "offline",
        "server_name": "1st Battalion, 5th Cavalry | Vietnam 1965 | US East",
        "map_name": "FIELD STATUS UNAVAILABLE", "game_mode": "STANDING BY",
        "player_count": 0, "max_players": 100, "cav_members_active": 0, "last_success_at": None,
    }
    try:
        payload = public_hll_server_snapshot() if database_ready() else fallback
        if not isinstance(payload, dict):
            payload = fallback
    except Exception:
        log.exception("Public HLL server status unavailable")
        payload = fallback
    response = Response(json.dumps(payload), mimetype="application/json")
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


def hll_operation_telemetry(operation, personnel_id=None):
    """Match telemetry overlapping a scheduled Operation window.

    Uses time overlap rather than inventing a second operation-match mapping table.
    This is deterministic and remains read-only. A future explicit match binding can
    replace it without changing the display contract.
    """
    result={"available":False,"matches":[],"players":[],"totals":{},"viewer":None}
    if not operation or not operation.get("start_at"):
        return result
    try:
        start=operation.get("start_at")
        end=start+timedelta(minutes=int(operation.get("duration_minutes") or 90))
        matches=fetch_all("""SELECT * FROM hll_match_sessions
                             WHERE started_at <= %s + INTERVAL '30 minutes'
                               AND COALESCE(ended_at,last_seen_at) >= %s - INTERVAL '30 minutes'
                             ORDER BY started_at""",(end,start)) or []
        result["available"]=True
        result["matches"]=[dict(x) for x in matches]
        if not matches:
            return result
        ids=[m["id"] for m in matches]
        rows=fetch_all("""SELECT ps.*,p.rank_code,p.first_name,p.last_name
                          FROM hll_player_match_stats ps
                          LEFT JOIN personnel p ON p.id::text=ps.personnel_id
                          WHERE ps.match_id = ANY(%s::bigint[])
                          ORDER BY ps.connected_seconds DESC,ps.player_name""",(ids,)) or []
        # Collapse multiple game rounds into one Operation row per Soldier/Steam identity.
        grouped={}
        numeric=("connected_seconds","distance_meters","altitude_gain_meters","infantry_kills","deaths","team_kills","vehicle_kills","vehicles_destroyed","combat_score","defense_score","offense_score","support_score")
        for raw in rows:
            r=dict(raw); key=str(r.get("personnel_id") or r.get("steam_id"))
            g=grouped.setdefault(key,{"personnel_id":r.get("personnel_id"),"steam_id":r.get("steam_id"),"player_name":r.get("player_name"),"rank_code":r.get("rank_code"),"first_name":r.get("first_name"),"last_name":r.get("last_name"),"matches":0})
            g["matches"]+=1
            for f in numeric: g[f]=float(g.get(f,0) or 0)+float(r.get(f,0) or 0)
            rr=g.setdefault("role_seconds",{})
            for role_id,seconds in _hll_json_dict(r.get("role_seconds")).items(): rr[str(role_id)]=int(rr.get(str(role_id),0) or 0)+int(seconds or 0)
        players=list(grouped.values())
        operation_role_rows=fetch_all("SELECT role_id,verified_role_name,verified FROM hll_role_mappings") or []
        operation_mappings={str(x.get('role_id')):dict(x) for x in operation_role_rows}
        for known_id,known in HLL_KNOWN_ROLE_MAPPINGS.items():
            current=operation_mappings.get(known_id,{})
            current.update(known)
            operation_mappings[known_id]=current
        for p in players:
            roles=p.get("role_seconds") or {}
            p["primary_role_id"]=max(roles,key=lambda k:int(roles.get(k) or 0)) if roles else None
            p["primary_role_name"]=hll_role_display_name(p.get("primary_role_id"),operation_mappings) if p.get("primary_role_id") is not None else None
            p["verified_contact"]=int(p.get("connected_seconds") or 0)>=300
        result["players"]=players
        totals={f:sum(float(p.get(f,0) or 0) for p in players) for f in numeric}
        totals["players"]=len(players); totals["matches"]=len(matches)
        result["totals"]=totals
        if personnel_id is not None:
            result["viewer"]=next((p for p in players if str(p.get("personnel_id"))==str(personnel_id)),None)
        return result
    except Exception:
        log.exception("HLL operation telemetry unavailable operation=%s",(operation or {}).get("id"))
        return result


def member_record_context(personnel):
    if not personnel:
        return {
            "personnel": None, "roster_card": None, "weapon": None, "uniform_issue": None,
            "awards": [], "assignments": [], "appointments": [], "qualifications": [],
            "duty_quals": [], "personal_ops": [], "operation_credit_ledger": [], "service_history": [], "current_orders": [],
            "chain_of_command": [], "replacement_training": {"complete":False,"requirements":[]},
            "promotion_eligibility": [], "training_programs": [], "progress_control": {},
            "can_recommend_awards": False, "award_candidates": [], "award_recommendations": [], "documents": [], "action_items": [], "notifications": [], "next_step": "Report to S-1.", "weapon_inspection": None, "mos_records": [], "timeline": [], "current_story": {}, "mos_proficiency": [], "instructor_quals": [], "leadership_records": [], "acting_appointments": [], "tour_book_preview": [], "recognitions": [], "ribbon_progress": [], "ribbon_details": [], "earned_ribbons": [], "uniform_ribbon_rows": [], "career_stats": {}, "combat_experience": {}, "career_tour": {}, "career_milestones": [], "assignment_history_full": [], "buddy_history": [], "weekly_report": {}, "squad_snapshot": None, "where_you_stand": {}, "record_warning": None, "record_error_reference": None, "hll_stats": hll_service_statistics(None), "m16_service": hll_m16_service_statistics(None,None),
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
    # Keep the issued rifle current from verified server field use before rendering.
    safe_member_panel("HLL M16 RECONCILIATION", {}, reconcile_hll_m16_rounds, pid, 365)
    weapon = safe_member_panel("CURRENT WEAPON", None, current_weapon_for, personnel)
    weapon_last_maintenance = safe_member_panel("WEAPON LAST MAINTENANCE", None,
        lambda: fetch_one("SELECT * FROM weapon_maintenance_log WHERE weapon_id=%s ORDER BY performed_at DESC LIMIT 1",(weapon["id"],)) if weapon else None)

    return {
        "personnel": personnel,
        "roster_card": safe_member_panel("BATTLE ROSTER CARD", None, battle_roster_for, personnel),
        "weapon": weapon,
        "weapon_last_maintenance": weapon_last_maintenance,
        "uniform_issue": safe_member_fetch_one("UNIFORM ISSUE",
            """SELECT eih.issued_at,ei.condition_state,sic.item_name
               FROM equipment_issue_history eih
               JOIN equipment_inventory ei ON ei.id=eih.equipment_id
               JOIN supply_item_catalog sic ON sic.item_code=ei.item_code
               WHERE eih.personnel_id=%s AND eih.is_current=TRUE AND ei.item_code='AG44' LIMIT 1""",
            (pid,)),
        "awards": safe_member_fetch_all("AWARDS",
            """SELECT pa.*, pr.id AS ribbon_id, pr.is_worn, rc.ribbon_code, rc.ribbon_name
               FROM personnel_awards pa
               LEFT JOIN ribbon_catalog rc ON LOWER(TRIM(rc.ribbon_name))=LOWER(TRIM(pa.award_name))
               LEFT JOIN personnel_ribbons pr ON pr.personnel_id=pa.personnel_id AND pr.ribbon_code=rc.ribbon_code
               WHERE pa.personnel_id=%s
               ORDER BY pa.award_date DESC
               LIMIT 20""", (pid,)),
        "assignments": safe_member_fetch_all("ASSIGNMENT HISTORY", "SELECT * FROM assignment_history WHERE personnel_id=%s ORDER BY effective_date DESC,created_at DESC LIMIT 20", (pid,)),
        "appointments": safe_member_fetch_all("APPOINTMENTS",
            """SELECT pa.*,ac.appointment_name FROM personnel_appointments pa
               JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code
               WHERE pa.personnel_id=%s ORDER BY pa.effective_date DESC,pa.created_at DESC LIMIT 20""",
            (pid,)),
        "qualifications": safe_member_fetch_all("QUALIFICATIONS", "SELECT * FROM qualifications WHERE personnel_id=%s ORDER BY expires_at NULLS LAST,qualification_name LIMIT 20", (pid,)),
        "duty_quals": safe_member_panel("DUTY QUALIFICATIONS", [], personnel_duty_qualifications, pid),
        "personal_ops": safe_member_panel("PERSONAL OPERATIONS", [], personal_operations, pid),
        "operation_credit_ledger": safe_member_panel("OPERATION CREDIT LEDGER", [], member_operation_credit_ledger, pid),
        "service_history": safe_member_fetch_all("SERVICE HISTORY", "SELECT * FROM personnel_service_history WHERE personnel_id=%s ORDER BY entry_date DESC,created_at DESC LIMIT 30", (pid,)),
        "documents": safe_member_fetch_all("PERSONNEL DOCUMENTS", "SELECT * FROM personnel_documents WHERE personnel_id=%s ORDER BY effective_date DESC,created_at DESC LIMIT 40", (pid,)),
        "current_orders": safe_member_fetch_all("CURRENT OPERATIONS",
            """SELECT * FROM operations
               WHERE UPPER(COALESCE(status,'')) NOT IN ('CLOSED','COMPLETE','COMPLETED','CANCELLED','CANCELED','ARCHIVED')
                 AND UPPER(COALESCE(lifecycle_status,'PLANNING')) NOT IN ('CLOSED','COMPLETE','COMPLETED','CANCELLED','CANCELED','ARCHIVED','AAR FILED')
                 AND (start_at IS NULL OR (start_at + make_interval(mins => COALESCE(duration_minutes,90))) > NOW())
               ORDER BY CASE WHEN start_at IS NULL THEN 1 ELSE 0 END,start_at ASC,created_at DESC LIMIT 8"""),
        "chain_of_command": safe_member_panel("CHAIN OF COMMAND", [], chain_of_command_for, personnel),
        "replacement_training": safe_member_panel("REPLACEMENT TRAINING", {"complete":False,"requirements":[]}, replacement_training_status, personnel),
        "promotion_eligibility": safe_member_panel("PROMOTION ELIGIBILITY", [], promotion_eligibility, personnel),
        "promotion_onboarding_open": safe_member_panel("PROMOTION ONBOARDING GATE", True, welcome_packet_promotion_open, pid),
        "training_programs": safe_member_fetch_all("TRAINING PROGRAMS", "SELECT * FROM training_program_catalog WHERE is_active=TRUE ORDER BY sort_order"),
        "progress_control": safe_member_panel("PROGRESS CONTROL", {}, personnel_progress, pid),
        "can_recommend_awards": member_is_nco(personnel),
        "award_candidates": safe_member_fetch_all("AWARD CANDIDATES", "SELECT id,rank_code,last_name,first_name,unit_code,platoon,squad FROM personnel ORDER BY unit_code,last_name,first_name") if member_is_nco(personnel) else [],
        "award_recommendations": safe_member_fetch_all("AWARD RECOMMENDATIONS",
            """SELECT pr.*,p.rank_code,p.last_name,p.first_name FROM personnel_recommendations pr
               JOIN personnel p ON p.id=pr.personnel_id
               WHERE pr.recommending_personnel_id=%s AND UPPER(pr.recommendation_type)='AWARD'
               ORDER BY pr.created_at DESC LIMIT 12""", (pid,)) if member_is_nco(personnel) else [],
        "action_items": safe_member_panel("ACTION ITEMS", [], soldier_action_items, personnel),
        "notifications": safe_member_panel("NOTIFICATIONS", [], current_notifications, pid),
        "next_step": safe_member_panel("NEXT STEP", "Report to S-1.", soldier_next_step, personnel),
        "weapon_inspection": safe_member_panel("WEAPON INSPECTION", None, weapon_inspection_status, pid),
        "mos_records": safe_member_panel("MOS RECORDS", [], personnel_mos_for, pid),
        "timeline": safe_member_fetch_all("SOLDIER TIMELINE", "SELECT * FROM personnel_service_history WHERE personnel_id=%s ORDER BY entry_date DESC,created_at DESC LIMIT 80", (pid,)),
        "current_story": safe_member_panel("CURRENT STORY", {}, soldier_current_story, personnel),
        "mos_proficiency": safe_member_fetch_all("MOS PROFICIENCY HISTORY", "SELECT pmp.*,bmc.mos_title FROM personnel_mos_proficiency pmp JOIN battalion_mos_catalog bmc ON bmc.mos_code=pmp.mos_code WHERE pmp.personnel_id=%s AND pmp.is_current=TRUE ORDER BY pmp.proficiency_order DESC,bmc.sort_order",(pid,)),
        "instructor_quals": safe_member_fetch_all("INSTRUCTOR QUALIFICATIONS", "SELECT * FROM instructor_qualifications WHERE personnel_id=%s AND status='CURRENT' ORDER BY effective_date DESC",(pid,)),
        "leadership_score": leadership_score,
        "leadership_records": safe_member_fetch_all("LEADERSHIP RECORDS", "SELECT * FROM leadership_performance_records WHERE personnel_id=%s ORDER BY record_date DESC,created_at DESC LIMIT 12",(pid,)),
        "acting_appointments": safe_member_fetch_all("ACTING APPOINTMENTS", "SELECT * FROM acting_appointments WHERE personnel_id=%s AND is_current=TRUE ORDER BY effective_date DESC",(pid,)),
        "tour_book_preview": safe_member_fetch_all("TOUR BOOK", "SELECT * FROM soldier_tour_book WHERE personnel_id=%s ORDER BY entry_date DESC,created_at DESC LIMIT 8",(pid,)),
        "recognitions": safe_member_fetch_all("RECOGNITIONS", "SELECT * FROM soldier_recognitions WHERE personnel_id=%s ORDER BY effective_date DESC",(pid,)),
        "ribbon_progress": safe_member_panel("RIBBON PROGRESS", [], ribbon_progress_for, pid, award_completed=False),
        "ribbon_details": safe_member_panel("RIBBON DETAILS", [], ribbon_details_for_member, pid),
        "earned_ribbons": safe_member_panel("EARNED RIBBONS", [], lambda: worn_ribbon_rows(pid)[1]),
        "uniform_ribbon_rows": safe_member_panel("UNIFORM RIBBONS", [], lambda: worn_ribbon_rows(pid)[0]),
        "current_situation": safe_member_panel("CURRENT SITUATION", {}, current_situation_snapshot, personnel),
        "field_reputation": safe_member_panel("FIELD REPUTATION", [], field_reputation, personnel),
        "personal_action_center": safe_member_panel("PERSONAL ACTION CENTER", [], member_personal_action_center, personnel),
        "duty_desk": (lambda d: {**(d or {}), "items": sanitize_member_nav_rows((d or {}).get("items"), "my_action_center")})(safe_member_panel("DUTY DESK", {"items":[],"count":0,"all_clear":True}, member_duty_desk, personnel)),
        "next_milestones": sanitize_member_nav_rows(safe_member_panel("NEXT MILESTONES", [], member_next_milestones, personnel), "my_soldier_record"),
        "recommended_action": safe_member_panel("NEXT RECOMMENDED ACTION", {"title":"MAINTAIN READINESS","detail":"No immediate deficiency on file."}, next_recommended_action, personnel),
        "most_served_with": safe_member_panel("MOST SERVED WITH", [], most_served_with, pid, 5),
        **career_context,
        "where_you_stand": where_you_stand,
        "welcome_packet": safe_member_panel("WELCOME PACKET", {"packet":None,"percent":0,"phases":[]}, welcome_packet_context, pid),
        "org_context": safe_member_panel("MEMBER ORGANIZATION", {}, member_organization_context, personnel),
        "hll_stats": hll_service_statistics(pid),
        "m16_service": safe_member_panel("M16 SERVICE RECORD", {}, hll_m16_service_statistics, pid, weapon),
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
        if weapon:
            weapon=derive_weapon_state(weapon,p)
    except Exception:
        log.exception("MEMBER RECORD FALLBACK WEAPON LOOKUP FAILED [%s]",error_reference)

    return {
        "personnel":p,
        "roster_card":roster,
        "weapon":weapon,
        "record_error_reference":error_reference,
        "record_warning":"The extended Soldier Record encountered a server-side data error. Your core personnel record is available below while Headquarters logs the failing module.",
        "hll_stats": hll_service_statistics(p.get("id")),
        "m16_service": hll_m16_service_statistics(p.get("id"),weapon),
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
                try: welcome_complete_task(p["id"],"STANDING_ORDERS","SOLDIER")
                except Exception: log.exception("Welcome Packet Standing Orders milestone failed")
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
        try:
            welcome_complete_task(card["personnel_id"],"WEBSITE_LOGIN","SOLDIER")
        except Exception:
            log.exception("Welcome Packet first-login milestone failed for %s",card["personnel_id"])
        flash(f"SOLDIER RECORD OPENED — {card['rank_code']} {card['last_name'].upper()}.", "success")
        wp=fetch_one("SELECT status FROM welcome_packets WHERE personnel_id=%s",(card["personnel_id"],))
        return redirect(url_for(member_landing_endpoint(card["personnel_id"])))

    if session.get("access_role") in {"member","nco","company_hq"} and session.get("user_id"):
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
            # Wall Locker access is authoritative; onboarding visit bookkeeping is optional.
            # A stale/missing Welcome Packet table or column must never lock a Soldier out.
            try:
                welcome_visit(personnel["id"],"VIEW_WALL_LOCKER")
            except Exception:
                visit_ref=secrets.token_hex(4).upper()
                log.exception("WALL LOCKER OPTIONAL VISIT TRACKING FAILURE [%s] personnel=%s",visit_ref,personnel.get("id"))
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
    """Compatibility staff URL. Always render the dedicated staff portal directly.

    Keeping this independent of the member-login route prevents old bookmarks,
    cached public pages, or reverse-proxy redirect handling from ever falling
    through to Soldier Record authentication.
    """
    return _staff_login_response()


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

    A battalion/root placeholder is never enough. A real Company assignment (or
    Headquarters/Section destination) ends Replacement status immediately.
    Platoon and Squad remain separate assignment-detail fields that may be filed
    after the Soldier has joined the Company. Legacy fields remain supported.
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
    # A line-company Soldier becomes a battalion Member when a real Platoon
    # assignment is filed. Squad/Team placement may continue afterward.
    line_ok=bool(company_ok and platoon_ok)
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
    """Canonical Replacement release gate.

    The modern accession workflow has exactly two release requirements:
    Command-accepted Welcome Packet and a permanent formation assignment.
    Training, MOS development, M16 maintenance, readiness, and other Soldier
    requirements remain active suspense after assignment and never strand a recruit
    in Replacement Detachment.
    """
    person=fetch_one("SELECT * FROM personnel WHERE id=%s AND archived=FALSE AND separated_at IS NULL",(personnel_id,))
    if not person:
        return None, []
    formation=permanent_formation_state(person)
    pkt=fetch_one("SELECT status,approved_at FROM welcome_packets WHERE personnel_id=%s",(personnel_id,))
    case=fetch_one("SELECT id,approved_at FROM recruiting_cases WHERE personnel_id=%s ORDER BY approved_at DESC NULLS LAST,created_at DESC LIMIT 1",(personnel_id,))
    packet_required=bool(case and case.get('approved_at'))
    packet_accepted=(not packet_required) or bool(pkt and str(pkt.get('status') or '').upper() in {'COMPLETE','CLOSED','ARCHIVED'} and pkt.get('approved_at'))
    requirements=[
        ('Welcome Packet accepted by Command',packet_accepted),
        ('Permanent formation assigned',bool(formation.get('complete'))),
    ]
    return {'person':person,'formation':formation,'packet':pkt,'packet_accepted':packet_accepted},requirements



def release_replacement_on_company_assignment(personnel_id, authority='BATTALION S-1'):
    """Activate battalion membership on the first real Platoon/HQ assignment.

    The legacy function name is retained so older call sites remain compatible.
    Approved recruits remain in Replacement Detachment until Command accepts the
    Welcome Packet. Only then may permanent formation assignment activate membership.
    """
    person=fetch_one("SELECT * FROM personnel WHERE id=%s AND archived=FALSE AND separated_at IS NULL",(personnel_id,))
    if not person:
        return False
    formation=permanent_formation_state(person)
    if not (formation.get('platoon_ok') or formation.get('special_assignment')):
        return False
    # Defense in depth: even if an older/legacy path managed to establish formation
    # data, an approved recruit cannot leave Replacement Detachment until Command
    # has formally accepted the Welcome Packet.
    packet_gate=onboarding_assignment_gate(personnel_id)
    if packet_gate.get('packet') and not packet_gate.get('allowed'):
        record_automation_event('PERSONNEL','REPLACEMENT_RELEASE','BLOCKED',
            'Replacement release blocked — Welcome Packet has not been accepted by Command.',
            personnel_id=personnel_id,source_key=f'REPLACEMENT-RELEASE-BLOCK:{personnel_id}')
        return False

    duty=str(person.get('duty_status') or '').upper()
    next_duty='PRESENT FOR DUTY' if duty in {'','REPLACEMENT — UNASSIGNED','REPLACEMENT - UNASSIGNED','IN PROCESSING','REPLACEMENT'} else person.get('duty_status')
    execute("UPDATE personnel SET field_status='Assigned',duty_status=COALESCE(%s,duty_status),updated_at=NOW() WHERE id=%s",(next_duty,personnel_id))

    case=fetch_one("""SELECT * FROM recruiting_cases
                       WHERE personnel_id=%s
                         AND status IN ('APPROVED_AWAITING_DISCORD','REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING')
                       ORDER BY approved_at DESC NULLS LAST,created_at DESC LIMIT 1""",(personnel_id,))
    movement_number=None
    if case:
        refreshed=fetch_one("SELECT * FROM personnel WHERE id=%s",(personnel_id,)) or person
        destination=' / '.join(x for x in [refreshed.get('unit_code'),refreshed.get('platoon'),refreshed.get('squad')] if x)
        doc=create_personnel_order(
            personnel_id,'REPLACEMENT','MOVEMENT ORDERS — REPLACEMENT DETACHMENT',
            f"The Soldier named herein is released from the 1/5 Cavalry Replacement Detachment and assigned to {destination}.",
            effective_date=date.today(),authority=authority,
            details={'recruiting_case':case.get('case_number'),'destination':destination,'release_basis':'PLATOON ASSIGNMENT'},
            source_key=f"REPLACEMENT-PLATOON-RELEASE:{case['id']}"
        )
        movement_number=(doc or {}).get('document_number') if doc else None
        execute("""UPDATE recruiting_cases SET status='ENLISTED',movement_order_number=COALESCE(%s,movement_order_number),
                   movement_order_filed_at=CASE WHEN %s IS NOT NULL THEN NOW() ELSE movement_order_filed_at END,
                   movement_unit_code=COALESCE(%s,movement_unit_code),updated_at=NOW()
                   WHERE id=%s""",(movement_number,movement_number,refreshed.get('unit_code'),case['id']))
        write_service_entry(
            personnel_id,'ADMIN','RELEASED FROM REPLACEMENT DETACHMENT',
            f"Platoon assignment filed. Soldier activated as a battalion Member and released from Replacement Detachment to {destination}. Remaining onboarding or training requirements continue as normal personnel suspense.",
            authority,movement_number,date.today()
        )
    enqueue_discord_role_sync(personnel_id,'PLATOON ASSIGNMENT — ACTIVATE MEMBER / REMOVE REPLACEMENT STATUS')
    return True


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
        if ('IN-PROCESS' in subj or 'ONBOARD' in subj or src.startswith('REPLACEMENT-INPROCESS:')) and not src.startswith('REPLACEMENT-TRAINING:'):
            transition_personnel_action(a['id'],'COMPLETE',authority,'Replacement Detachment administrative processing complete. Training suspense remains independent.')
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

    A Soldier stays in the detachment until a permanent Platoon/HQ destination is
    filed after Welcome Packet acceptance. Remaining training items follow them as ordinary
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
        # Replacement Detachment is an UNASSIGNED holding roster. The moment a
        # Soldier leaves Replacement Detachment only after a real Platoon assignment
        # (or authorized HQ/Section destination). Company-only placeholders remain visible.
        permanent_destination=bool(formation.get('special_assignment') or formation.get('platoon_ok'))
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
            {'code':'FORMATION','label':'Permanent formation assigned','complete':formation_complete,'detail':' / '.join(x for x in [person.get('unit_code'),person.get('platoon'),person.get('squad')] if x) if formation_complete else 'PLATOON ASSIGNMENT REQUIRED'},
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
        # Company assignment ends Replacement status. Missing Platoon/Squad detail
        # may still be surfaced as an assignment-completeness issue where appropriate,
        # but it does not return the Soldier to Replacement Detachment.
        try:
            _formation=permanent_formation_state(p)
        except Exception:
            _formation={}
        if _formation.get('company_ok') and not _formation.get('special_assignment') and (not _formation.get('platoon_ok') or not _formation.get('squad_ok')):
            missing=[]
            if not _formation.get('platoon_ok'): missing.append('platoon')
            if not _formation.get('squad_ok'): missing.append('squad')
            rows.append({'person':p,'type':'INCOMPLETE LINE ASSIGNMENT','severity':'amber','detail':'Permanent line assignment incomplete; '+ ' and '.join(missing) +' assignment still requires S-1 action. Company assignment has already released the Soldier from Replacement Detachment.'})
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
    """Role-aware Battalion Action Queue built from authoritative live records.

    This is intentionally derived instead of copied into another workflow table:
    when the underlying issue is resolved, it disappears from the Command Post.
    """
    section=_staff_section(role)
    items=[]
    def add(level,count,title,endpoint,detail):
        count=int(count or 0)
        if count:
            items.append({"level":level,"count":count,"title":title,"endpoint":endpoint,"detail":detail})
    def count(sql,params=()):
        try:
            return int((fetch_one(sql,params) or {"total":0}).get("total") or 0)
        except Exception:
            log.exception('Staff action queue query failed: %s',sql[:120])
            return 0

    if role in {"battalion_hq","commander","admin","s1"}:
        add("red", count("SELECT COUNT(*) total FROM recruiting_cases WHERE status IN ('SUBMITTED','DISCORD_VERIFIED','PENDING_COMMAND','MORE_INFO_REQUIRED')"),
            "Recruit applications awaiting review","recruiting_control","Open the recruiting case queue and make the next S-1 / Command decision")
        try:
            rc=len(replacement_detachment_rows())
        except Exception:
            log.exception('Replacement Detachment attention item failed; continuing Action Center load.')
            rc=0
        add("amber",rc,"Soldiers awaiting permanent assignment","replacement_detachment","Process Replacement Detachment and file the next assignment action")

        # Vacancy counts are computed from the same billet-strength source shown to Command.
        try:
            vacant=sum(int(r.get('vacant') or 0) for r in billet_strength_rows())
        except Exception:
            log.exception('Billet vacancy queue item failed.')
            vacant=0
        add("amber",vacant,"Authorized billets vacant","billet_strength_page","Review the vacancy board and fill priority leadership / MOS billets")

    if role in {"battalion_hq","commander","admin","s3","training"}:
        add("amber", count("SELECT COUNT(*) total FROM qualifications WHERE status='CURRENT' AND expires_at BETWEEN CURRENT_DATE AND CURRENT_DATE+30"),
            "Qualifications expiring within 30 days","training","Open Training and schedule requalification")

    if role in {"battalion_hq","commander","admin","s4"}:
        add("amber", count("""SELECT COUNT(*) total FROM weapon_issue_history wih JOIN weapon_inventory wi ON wi.id=wih.weapon_id
                              WHERE wih.is_current=TRUE AND (wi.last_inspected_at IS NULL OR wi.last_inspected_at<NOW()-INTERVAL '14 days')"""),
            "M16 inspections due","arms_room","Open the Arms Room directly to the current inspection workload")

    if role in {"battalion_hq","commander","admin","s3","training"}:
        # Completed operations without an AAR remain a review item. Attendance can be finalized from the operation detail.
        add("amber", count("""SELECT COUNT(*) total FROM operations o
                              WHERE UPPER(COALESCE(o.status,'')) IN ('COMPLETED','CLOSED')
                                AND NOT EXISTS(SELECT 1 FROM after_action_reports aar WHERE aar.operation_id=o.id)"""),
            "Operations awaiting attendance / AAR review","operations","Open completed operations, verify participation, then file the AAR")

    if role in {"battalion_hq","commander","admin","s1","s3"}:
        # The HLL personnel link is the authoritative game-identity link used by telemetry.
        add("amber", count("""SELECT COUNT(*) total FROM personnel p
                              WHERE p.archived=FALSE AND p.separated_at IS NULL
                                AND UPPER(COALESCE(p.field_status,''))='ASSIGNED'
                                AND NOT EXISTS(SELECT 1 FROM hll_personnel_links h WHERE h.personnel_id::text=p.id::text AND COALESCE(h.verified,TRUE)=TRUE)
                                AND NOT EXISTS(SELECT 1 FROM hll_identity_claims c WHERE c.personnel_id::text=p.id::text AND UPPER(COALESCE(c.status,'')) IN ('PENDING','VERIFIED','LINKED'))"""),
            "Members without linked game identity","hll_telemetry_lab_page","Resolve the game identity link so server service can be credited automatically")

    action_where="" if not section else " AND owning_section=%s"
    params=() if not section else (section,)
    add("red", count(f"SELECT COUNT(*) total FROM personnel_actions WHERE status NOT IN ('COMPLETE','CLOSED','DENIED') AND due_date<CURRENT_DATE{action_where}",params),
        "Overdue staff actions","personnel_actions","Resolve or reroute overdue suspense")
    add("amber", count(f"SELECT COUNT(*) total FROM personnel_actions WHERE status NOT IN ('COMPLETE','CLOSED','DENIED'){action_where}",params),
        "Personnel actions pending","personnel_actions","Open the active action ledger and complete the next pending personnel action")

    if role in {"battalion_hq","commander","admin","s1"}:
        add("amber", count("""SELECT COUNT(*) total FROM personnel WHERE archived=FALSE AND separated_at IS NULL
                              AND UPPER(COALESCE(field_status,''))='ASSIGNED'
                              AND COALESCE(activity_last_seen_at,activity_last_duty_at,created_at)<NOW()-INTERVAL '7 days'"""),
            "Personnel inactivity watch","personnel_office","Review Soldiers with 7+ days since recorded activity")
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




def battalion_integrity_scan(limit=200):
    """Read-only cross-system consistency scan. Never mutates authoritative records."""
    issues=[]
    checks=[]
    def add(code,severity,title,detail,endpoint='battalion_control',personnel_id=None,reference=None):
        issues.append({'code':code,'severity':severity,'title':title,'detail':detail,'endpoint':endpoint,
                       'personnel_id':personnel_id,'reference':reference})
    def qcount(label,sql,params=()):
        try:
            n=int((fetch_one(sql,params) or {'total':0}).get('total') or 0)
            checks.append({'label':label,'state':'CURRENT' if n==0 else 'ACTION REQUIRED','count':n})
            return n
        except Exception as exc:
            log.exception('Integrity check failed: %s',label)
            checks.append({'label':label,'state':'CHECK FAILED','count':0})
            add('CHECK_FAILED','red',label,'Integrity query could not complete. Review Railway/database logs.','battalion_control')
            return None
    try:
        people=fetch_all("""SELECT p.* FROM personnel p WHERE p.archived=FALSE AND p.separated_at IS NULL ORDER BY p.last_name,p.first_name""")
        linked={str(r['personnel_id']) for r in fetch_all("SELECT personnel_id FROM website_member_links")}
        weapons={str(r['personnel_id']) for r in fetch_all("SELECT personnel_id FROM weapon_issue_history WHERE is_current=TRUE")}
        failed_sync={str(r['personnel_id']):r for r in fetch_all("""SELECT DISTINCT ON (personnel_id) personnel_id,status,error_text FROM discord_role_sync_queue ORDER BY personnel_id,requested_at DESC""")}
        packets={str(r['personnel_id']):r for r in fetch_all("SELECT personnel_id,status,current_phase,updated_at FROM welcome_packets")}
        for row in people:
            pid=str(row['id']); company=(row.get('unit_code') or '').strip(); platoon=(row.get('platoon') or '').strip(); squad=(row.get('squad') or '').strip()
            if squad and not platoon:
                add('FORMATION_SQUAD_WITHOUT_PLATOON','red','FORMATION CONFLICT',f"{row.get('rank_code','')} {row.get('last_name','')} has a Squad but no Platoon.",'personnel_office',pid)
            if company and company.upper() not in {'REPLACEMENT','REPLACEMENT DETACHMENT','REPLACEMENT DEPOT'} and str(row.get('field_status') or '').upper() in {'REPLACEMENT','REPLACEMENT DETACHMENT','REPLACEMENT DEPOT'}:
                add('ASSIGNED_STILL_REPLACEMENT','red','REPLACEMENT STATUS CONFLICT',f"{row.get('rank_code','')} {row.get('last_name','')} has a permanent unit but still carries Replacement field status.",'personnel_office',pid)
            if str(row.get('field_status') or '').upper()=='ASSIGNED' and pid not in weapons:
                add('ASSIGNED_NO_M16','amber','PROPERTY EXCEPTION',f"{row.get('rank_code','')} {row.get('last_name','')} is assigned but has no current M16 issue.",'arms_room',pid)
            if pid not in linked:
                add('NO_DISCORD_LINK','amber','DISCORD LINK MISSING',f"{row.get('rank_code','')} {row.get('last_name','')} has no Website↔Discord member link.",'personnel_office',pid)
            sync=failed_sync.get(pid)
            if sync and str(sync.get('status') or '').upper()=='FAILED':
                add('DISCORD_SYNC_FAILED','red','DISCORD ROLE SYNC FAILED',sync.get('error_text') or f"Role synchronization failed for {row.get('last_name','')}.",'personnel_office',pid)
            packet=packets.get(pid)
            if packet and str(packet.get('status') or '').upper() not in {'COMPLETE','CLOSED'} and company and company.upper() not in {'REPLACEMENT','REPLACEMENT DETACHMENT','REPLACEMENT DEPOT'}:
                # Active packet is valid after company assignment while later phases are unfinished; only flag impossible replacement-orientation state.
                if str(packet.get('current_phase') or '').upper()=='REPLACEMENT_ORIENTATION':
                    add('PACKET_PHASE_STALE','amber','WELCOME PACKET PHASE STALE',f"{row.get('rank_code','')} {row.get('last_name','')} is permanently assigned but the packet is still in Replacement Orientation.",'staff_onboarding',pid)
    except Exception:
        log.exception('Personnel integrity scan failed')
        add('PERSONNEL_SCAN_FAILED','red','PERSONNEL INTEGRITY SCAN FAILED','The personnel consistency scan could not complete.','battalion_control')

    qcount('AWARD ORDERS',"SELECT COUNT(*) total FROM personnel_awards WHERE COALESCE(BTRIM(order_number),'')='' OR COALESCE(BTRIM(citation),'')=''")
    qcount('DISCORD ROLE QUEUE',"SELECT COUNT(*) total FROM discord_role_sync_queue WHERE UPPER(COALESCE(status,''))='FAILED'")
    qcount('OPERATION / CLERK LINK',"""SELECT COUNT(*) total FROM operations WHERE UPPER(COALESCE(status,'')) NOT IN ('CANCELLED','CANCELED','CLOSED','COMPLETE','COMPLETED','ARCHIVED','DELETED') AND UPPER(COALESCE(publish_status,'DRAFT'))='PUBLISHED' AND clerk_event_id IS NULL""")
    qcount('WELCOME PACKET STALL',"SELECT COUNT(*) total FROM welcome_packets WHERE status NOT IN ('COMPLETE','CLOSED') AND updated_at<NOW()-INTERVAL '24 hours'")
    qcount('DUPLICATE CURRENT M16 ISSUE',"SELECT COUNT(*) total FROM (SELECT personnel_id FROM weapon_issue_history WHERE is_current=TRUE GROUP BY personnel_id HAVING COUNT(*)>1) x")
    qcount('DUPLICATE PENDING DISCORD SYNC',"SELECT COUNT(*) total FROM (SELECT personnel_id FROM discord_role_sync_queue WHERE status='PENDING' GROUP BY personnel_id HAVING COUNT(*)>1) x")
    qcount('TRAINING RECORD EXPIRATIONS',"SELECT COUNT(*) total FROM qualifications WHERE status='CURRENT' AND expires_at<CURRENT_DATE")

    # M16 counter integrity: compare stored post-cleaning counter with ledger events after last cleaning.
    try:
        rows=fetch_all("""SELECT wi.id,wi.serial_number,wi.rounds_since_cleaning,wi.total_rounds,
                     COALESCE((SELECT MAX(wml.performed_at) FROM weapon_maintenance_log wml WHERE wml.weapon_id=wi.id AND UPPER(COALESCE(wml.action_type,'')) LIKE '%%CLEAN%%'),wi.last_cleaned_at,(SELECT MIN(wih.issued_at)::timestamptz FROM weapon_issue_history wih WHERE wih.weapon_id=wi.id AND wih.is_current=TRUE)) AS cleaned_at,
                     COALESCE((SELECT SUM(wre.rounds_fired) FROM weapon_round_events wre WHERE wre.weapon_id=wi.id),0) AS ledger_total
                     FROM weapon_inventory wi
                     WHERE EXISTS(SELECT 1 FROM weapon_issue_history wih WHERE wih.weapon_id=wi.id AND wih.is_current=TRUE)""")
        bad=0
        for w in rows:
            after=fetch_one("SELECT COALESCE(SUM(rounds_fired),0) total FROM weapon_round_events WHERE weapon_id=%s AND recorded_at>=COALESCE(%s::timestamptz,'epoch'::timestamptz)",(w['id'],w.get('cleaned_at'))) or {'total':0}
            expected=int(after.get('total') or 0); stored=int(w.get('rounds_since_cleaning') or 0)
            if expected!=stored:
                bad+=1; add('M16_COUNTER_DRIFT','red','M16 CLEANING / ROUND COUNTER DRIFT',f"M16 {w.get('serial_number')} shows {stored} rounds since cleaning; ledger evidence shows {expected}.",'arms_room',reference=w.get('serial_number'))
        checks.append({'label':'M16 CLEANING LEDGER','state':'CURRENT' if bad==0 else 'ACTION REQUIRED','count':bad})
    except Exception:
        log.exception('M16 integrity scan failed')
        checks.append({'label':'M16 CLEANING LEDGER','state':'CHECK FAILED','count':0})
        add('M16_SCAN_FAILED','red','M16 INTEGRITY CHECK FAILED','Weapon ledger comparison could not complete.','arms_room')

    red=sum(1 for i in issues if i['severity']=='red'); amber=sum(1 for i in issues if i['severity']=='amber')
    return {'issues':issues[:limit],'checks':checks,'red':red,'amber':amber,'total':len(issues),'all_clear':not issues}


def staff_change_feed(role,limit=12):
    """Meaningful battalion changes; read-only and role-aware."""
    section=_staff_section(role)
    try:
        if section:
            rows=fetch_all("""SELECT created_at,section,action_type AS category,summary AS title,actor,reference_number,personnel_id
                              FROM staff_duty_log WHERE section=%s ORDER BY created_at DESC LIMIT %s""",(section,limit))
        else:
            rows=fetch_all("""SELECT created_at,section,action_type AS category,summary AS title,actor,reference_number,personnel_id
                              FROM staff_duty_log ORDER BY created_at DESC LIMIT %s""",(limit,))
        return rows
    except Exception:
        log.exception('Staff change feed failed')
        return []


def member_next_milestones(person):
    """Compact member progress board sourced only from existing authoritative systems."""
    if not person: return []
    pid=person['id']; out=[]
    try:
        for row in ribbon_progress_for(pid,award_completed=False):
            if row.get('earned') or row.get('pending_system'): continue
            remaining=max(0,int(row.get('target') or 0)-int(row.get('current') or 0))
            detail=row.get('detail') or ''
            if row.get('secondary_target'):
                detail=f"{detail} • {max(0,int(row.get('secondary_target') or 0)-int(row.get('secondary_current') or 0))} secondary requirement remaining"
            out.append({'section':'NEXT RIBBON','title':row.get('name'),'detail':detail,'percent':row.get('percent',0),'endpoint':'my_201_file','anchor':'awards'})
            break
    except Exception: log.exception('Member next-ribbon milestone failed')
    try:
        for m in member_career_milestones(person)[:3]:
            out.append({'section':m.get('section') or 'CAREER','title':m.get('title'),'detail':m.get('detail'),'percent':None,'endpoint':'my_201_file','anchor':'promotion-eligibility'})
    except Exception: log.exception('Member career milestone build failed')
    return out[:4]

def _command_service_health(role):
    """Five-line command health strip built from existing authoritative heartbeats/queues.

    Every check is fail-soft: a secondary status query must never take down Command Desk.
    """
    out=[]
    def add(label,state,detail,endpoint):
        out.append({'label':label,'state':state,'detail':detail,'endpoint':endpoint})
    add('WEBSITE','CURRENT' if database_ready() else 'ERROR',
        'Application and database are responding.' if database_ready() else 'Database is not ready.',
        'staff_action_center')
    try:
        ch=clerk_health_snapshot() or {}
        state='CURRENT' if ch.get('state')=='CONNECTED' else ('WATCH' if ch.get('state') in {'STALE','UNKNOWN'} else 'ERROR')
        add('BATTALION CLERK',state,
            'Clerk heartbeat connected.' if state=='CURRENT' else f"Clerk heartbeat {str(ch.get('state') or 'unknown').lower()}.",
            'personnel_sync_control' if role in {'s1','battalion_hq','commander','admin'} else 'staff_reliability')
    except Exception:
        log.exception('Command Clerk health strip failed')
        add('BATTALION CLERK','WATCH','Heartbeat check unavailable.','personnel_sync_control')
    try:
        failed=int((fetch_one("""SELECT COUNT(*) total FROM (SELECT DISTINCT ON (personnel_id) personnel_id,status FROM discord_role_sync_queue ORDER BY personnel_id,requested_at DESC) latest WHERE UPPER(COALESCE(status,'')) IN ('FAILED','BLOCKED')""") or {'total':0}).get('total') or 0)
        add('DISCORD SYNC','ERROR' if failed else 'CURRENT',
            f'{failed} blocked/failed sync item(s).' if failed else 'Personnel mirror has no blocked jobs.',
            'personnel_sync_control')
    except Exception:
        log.exception('Command Discord health strip failed')
        add('DISCORD SYNC','WATCH','Sync queue check unavailable.','personnel_sync_control')
    try:
        raw=hll_live_server_snapshot() or {}
        heartbeat=raw.get('last_success_at') or raw.get('updated_at')
        stale=True
        if heartbeat:
            hb=heartbeat if getattr(heartbeat,'tzinfo',None) else heartbeat.replace(tzinfo=timezone.utc)
            stale=(datetime.now(timezone.utc)-hb).total_seconds()>120
        add('HLL TELEMETRY','WATCH' if stale else 'CURRENT',
            'Collector heartbeat is stale.' if stale else 'Collector heartbeat is current.',
            'hll_telemetry_lab_page' if role in {'s3','training','battalion_hq','commander','admin'} else 'staff_reliability')
    except Exception:
        log.exception('Command HLL health strip failed')
        add('HLL TELEMETRY','WATCH','Telemetry heartbeat check unavailable.','hll_telemetry_lab_page' if role in {'s3','training','battalion_hq','commander','admin'} else 'staff_reliability')
    try:
        errs=int((fetch_one("""SELECT COUNT(*) total FROM recruiting_cases WHERE status IN ('REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING') AND discord_join_error IS NOT NULL AND discord_joined_at IS NULL""") or {'total':0}).get('total') or 0)
        add('RECRUITING','ERROR' if errs else 'CURRENT',
            f'{errs} accession intake error(s).' if errs else 'Recruiting intake has no recorded Discord errors.',
            'accession_pipeline')
    except Exception:
        log.exception('Command recruiting health strip failed')
        add('RECRUITING','WATCH','Recruiting health check unavailable.','accession_pipeline')
    return out


def _command_workflow_lanes(role, brief, attention, open_rows):
    """Command-facing work buckets. Counts are derived from existing workflows, never copied."""
    lanes=[]
    def lane(label,count,detail,endpoint,state='normal'):
        lanes.append({'label':label,'count':int(count or 0),'detail':detail,'endpoint':endpoint,'state':state})
    if role in {'battalion_hq','commander','admin'}:
        lane('WAITING ON COMMAND', brief.get('recruit_review') or 0, 'Applications needing a command decision.', 'accession_pipeline', 'attention')
        try:
            arrivals=len(prospective_replacements_rows())
        except Exception:
            log.exception('Command new-arrivals lane failed'); arrivals=0
        lane('NEW DISCORD ARRIVALS',arrivals,'Unlinked Discord arrivals entering Accessions.','prospective_replacements','attention' if arrivals else 'normal')
        try:
            replacement_rows=replacement_detachment_rows()
            ready_assign=sum(1 for r in replacement_rows if r.get('stage') in {'READY FOR ASSIGNMENT','ASSIGNMENT PENDING'})
        except Exception:
            log.exception('Command assignment-ready lane failed'); ready_assign=0
        lane('WAITING ON ASSIGNMENT',ready_assign,'Replacements ready for a permanent formation.','staff_assign_soldier','attention' if ready_assign else 'normal')
        try:
            eligible=0
            people=fetch_all("SELECT * FROM personnel WHERE archived=FALSE AND separated_at IS NULL AND lifecycle_state NOT IN ('APPLICANT','PROSPECT')")
            for person in people:
                paths=promotion_eligibility(person)
                if paths and bool(paths[0].get('eligible')): eligible+=1
        except Exception:
            log.exception('Command promotion-ready lane failed'); eligible=0
        lane('READY FOR PROMOTION',eligible,'Soldiers meeting the published promotion requirements.','promotion_board','good' if eligible else 'normal')
        lane('M16 ACTION DUE',brief.get('weapons_due') or 0,'Issued rifles currently due for S-4 inspection.','arms_room','attention' if brief.get('weapons_due') else 'normal')
    if role in {'s1','battalion_hq','commander','admin'}:
        try:
            sync_failed=int((fetch_one("""SELECT COUNT(*) total FROM (SELECT DISTINCT ON (personnel_id) personnel_id,status FROM discord_role_sync_queue ORDER BY personnel_id,requested_at DESC) latest WHERE UPPER(COALESCE(status,'')) IN ('FAILED','BLOCKED')""") or {'total':0}).get('total') or 0)
        except Exception:
            sync_failed=0
        lane('SYNC PROBLEMS',sync_failed,'Discord personnel jobs requiring repair.','personnel_sync_control','danger' if sync_failed else 'normal')
    overdue=sum(1 for a in (open_rows or []) if a.get('due_date') and a.get('due_date') < date.today())
    lane('OVERDUE STAFF WORK', max(overdue,int(brief.get('overdue') or 0)), 'Expired suspense that should be worked first.', 'personnel_actions','danger' if (overdue or brief.get('overdue')) else 'normal')
    return lanes


def _staff_automation_health(role):
    """Compact exception-only health readout; automation stays invisible when healthy."""
    health=[]
    def count(sql,params=()):
        try:
            return int((fetch_one(sql,params) or {'total':0}).get('total') or 0)
        except Exception:
            log.exception('Staff automation health check failed')
            return None
    if role in {'s1','battalion_hq','commander','admin'}:
        failed=count("""SELECT COUNT(*) total FROM (SELECT DISTINCT ON (personnel_id) personnel_id,status FROM discord_role_sync_queue ORDER BY personnel_id,requested_at DESC) latest WHERE UPPER(COALESCE(status,'')) IN ('FAILED','BLOCKED')""")
        pending=count("""SELECT COUNT(*) total FROM (SELECT DISTINCT ON (personnel_id) personnel_id,status FROM discord_role_sync_queue ORDER BY personnel_id,requested_at DESC) latest WHERE UPPER(COALESCE(status,'')) IN ('PENDING','QUEUED')""")
        health.append({'label':'DISCORD PERSONNEL SYNC','state':'ERROR' if failed else ('PENDING' if pending else 'CURRENT'),'count':failed or pending or 0,
                       'endpoint':'personnel_sync_control' if failed else 'staff_action_center'})
        intake_errors=count("""SELECT COUNT(*) total FROM recruiting_cases WHERE status IN ('REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING') AND discord_join_error IS NOT NULL AND discord_joined_at IS NULL""")
        health.append({'label':'RECRUIT DISCORD INTAKE','state':'ERROR' if intake_errors else 'CURRENT','count':intake_errors or 0,'endpoint':'recruiting_control'})
        stalled=count("""SELECT COUNT(*) total FROM welcome_packets WHERE status NOT IN ('COMPLETE','CLOSED') AND updated_at < NOW()-INTERVAL '24 hours'""")
        health.append({'label':'WELCOME PACKETS','state':'WATCH' if stalled else 'CURRENT','count':stalled or 0,'endpoint':'staff_onboarding'})
    if role in {'s3','training','battalion_hq','commander','admin'}:
        unsynced=count("""SELECT COUNT(*) total FROM operations WHERE UPPER(COALESCE(status,'')) NOT IN ('CANCELLED','CANCELED','CLOSED','COMPLETE','COMPLETED','ARCHIVED','DELETED') AND UPPER(COALESCE(publish_status,'DRAFT'))='PUBLISHED' AND clerk_event_id IS NULL""")
        health.append({'label':'OPERATION / CLERK LINK','state':'ERROR' if unsynced else 'CURRENT','count':unsynced or 0,'endpoint':'operations'})
    if role in {'s4','battalion_hq','commander','admin'}:
        health.append({'label':'HLL M16 SERVICE','state':'CURRENT','count':0,'endpoint':'arms_room'})
    return health


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
    workflow_lanes=_command_workflow_lanes(role,brief,attention,open_rows)
    assignment_queue=[]
    if role in {'s1','battalion_hq','commander','admin'}:
        for item in replacement_rows:
            if item.get('stage') in {'READY FOR ASSIGNMENT','ASSIGNMENT PENDING'}:
                assignment_queue.append(item)
    # The next task is selected from live attention items, with red items always first.
    ranked_attention=sorted(enumerate(attention),key=lambda pair:(0 if pair[1].get('level')=='red' else 1,pair[0]))
    do_next=ranked_attention[0][1] if ranked_attention else None
    command_service_health=_command_service_health(role)
    role_focus={
        's1':'PERSONNEL / REPLACEMENTS / ONBOARDING',
        's2':'INTELLIGENCE / STAFF SUSPENSE',
        's3':'OPERATIONS / ATTENDANCE / READINESS',
        'training':'TRAINING / QUALIFICATIONS / READINESS',
        's4':'M16 / INSPECTIONS / LOGISTICS',
        'battalion_hq':'BATTALION-WIDE ACTION REQUIRED',
        'commander':'BATTALION-WIDE ACTION REQUIRED',
        'admin':'SYSTEM / BATTALION-WIDE CONTROL',
    }.get(role,'STAFF ACTION REQUIRED')
    return render_template('staff_action_center.html',role=role,section=section,brief=brief,attention=attention,
                           search_query=q,search_rows=search_rows,recent_actions=recent,open_actions=open_rows,
                           personnel_choices=personnel_choices,command_watchlist=watchlist,suspense_summary=suspense_summary,
                           replacement_rows=replacement_rows,priority_work=s1_priority_work(replacement_rows) if replacement_rows else [],
                           personnel_exceptions=personnel_exceptions,automation_health=_staff_automation_health(role),integrity=battalion_integrity_scan(40),change_feed=staff_change_feed(role,12),role_focus=role_focus,server_seed=staff_server_seed_snapshot(),reliability=automation_reliability_report(),workflow_lanes=workflow_lanes,do_next=do_next,command_service_health=command_service_health,assignment_queue=assignment_queue)

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
        elif not onboarding_assignment_gate(personnel_id).get('allowed'):
            flash('WELCOME PACKET MUST BE COMPLETED BY THE SOLDIER AND ACCEPTED BY COMMAND BEFORE PERMANENT FORMATION ASSIGNMENT.','warning')
        else:
            file_primary_mos_change(personnel_id,mos['mos_code'],date.today(),authority,request.form.get('remarks') or None)
            assignment_result=process_assignment_action(personnel_id,node['id'],duty['value'],date.today(),authority,None,request.form.get('remarks') or None)
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
                flash('PERMANENT FORMATION ASSIGNMENT FILED — ORDERS '+str(((assignment_result or {}).get('document') or {}).get('document_number') or 'FILED')+' — SOLDIER RELEASED FROM REPLACEMENT DETACHMENT.','success')
            else:
                flash('PERMANENT FORMATION ASSIGNMENT FILED — ORDERS '+str(((assignment_result or {}).get('document') or {}).get('document_number') or 'FILED')+'; REMAINING REPLACEMENT ACTIONS: '+', '.join(missing),'warning')
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
    staff_log('S-1','REPLACEMENT BATCH ACTION',f"{action} — {processed} Soldier{'s' if processed != 1 else ''}",authority,
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
    staff_log(section,'BATCH ACTION',f"{subject} — {created} Soldier{'s' if created != 1 else ''}",authority,details={'count':created,'action_type':kind})
    flash(f"BATCH ACTION FILED FOR {created} SOLDIER{'S' if created != 1 else ''}.",'success')
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
    # The Action Center is a member navigation hub. Optional subsystem failures
    # must degrade individual cards, never make the tab appear dead.
    try:
        person=linked_personnel()
        if not person:
            return redirect(url_for('login'))
        p=soldier_view(person)
        notifications=safe_member_panel('Member notifications', [], refresh_member_notices, p)
        history=safe_member_panel('Member action history', [], member_notification_history, p['id'], 40)
        items=safe_member_panel('My Actions list', [], member_personal_action_center, p)
        recommended=safe_member_panel('My Actions recommendation',
            {'section':'HEADQUARTERS','title':'MAINTAIN READINESS','detail':'No immediate deficiency is on file.','priority':'ROUTINE'},
            next_recommended_action, p)
        situation=safe_member_panel('My Actions situation', {}, current_situation_snapshot, p)
        reputation=safe_member_panel('My Actions reputation', [], field_reputation, p)
        return render_template('member_action_center.html',personnel=p,items=items,recommended=recommended,situation=situation,reputation=reputation,notifications=notifications,history=history)
    except Exception:
        error_reference=secrets.token_hex(4).upper()
        log.exception('MY ACTIONS ROUTE FAILURE [%s]',error_reference)
        # Preserve a usable member destination instead of sending the user to the
        # global 500 screen. The Wall Locker remains the safe return point.
        try:
            person=linked_personnel()
            if person:
                p=soldier_view(person)
                return render_template('member_action_center.html', personnel=p, items=[], notifications=[], history=[],
                    recommended={'section':'HEADQUARTERS','title':'ACTION DATA TEMPORARILY LIMITED',
                                 'detail':'Your Action Center is available, but one optional data source could not be read. Headquarters has logged reference '+error_reference+'.',
                                 'priority':'WATCH'}, situation={}, reputation=[]), 200
        except Exception:
            log.exception('MY ACTIONS FALLBACK FAILURE [%s]',error_reference)
        return redirect(url_for('my_soldier_record'))


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
    params=[]; where="WHERE q.status='PENDING' AND (q.next_retry_at IS NULL OR q.next_retry_at<=NOW())"
    if guild_id:
        where += ' AND q.guild_id=%s'; params.append(guild_id)
    rows=fetch_all(f"""SELECT q.id,q.personnel_id,q.guild_id,q.discord_user_id,q.reason,q.requested_at,
                        p.rank_code,p.mos_code,p.unit_code,p.platoon,p.squad,p.fire_team
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
    row=fetch_one("SELECT personnel_id,reason,attempt_count FROM discord_role_sync_queue WHERE id=%s",(queue_id,))
    attempts=int((row or {}).get('attempt_count') or 0)+1
    blocked=bool(err and any(x in str(err).lower() for x in ('hierarchy','permission','member not found','not found in guild')))
    if ok:
        final_status='COMPLETE'; next_retry=None
    elif blocked or attempts>=5:
        final_status='BLOCKED' if blocked else 'FAILED'; next_retry=None
    else:
        final_status='PENDING'; next_retry=datetime.now(timezone.utc)+timedelta(minutes=min(2**attempts,30))
    execute("""UPDATE discord_role_sync_queue SET status=%s,processed_at=CASE WHEN %s IN ('COMPLETE','FAILED','BLOCKED') THEN NOW() ELSE NULL END,
               error_text=%s,attempt_count=%s,last_attempt_at=NOW(),next_retry_at=%s WHERE id=%s""",
            (final_status,final_status,err,attempts,next_retry,queue_id))
    if row and ok:
        execute("""UPDATE discord_role_sync_queue SET status='SUPERSEDED'
                   WHERE personnel_id=%s AND id<>%s AND UPPER(COALESCE(status,'')) IN ('FAILED','BLOCKED')""",
                (row.get('personnel_id'),queue_id))
    if row:
        ledger_status='COMPLETE' if ok else ('BLOCKED' if final_status=='BLOCKED' else ('FAILED' if final_status=='FAILED' else 'PENDING'))
        summary='Discord personnel roles reconciled to website authority.' if ok else (f"Discord reconciliation blocked: {err}" if final_status=='BLOCKED' else (f"Discord reconciliation queued for automatic retry: {err}" if final_status=='PENDING' else f"Discord reconciliation failed after {attempts} attempts: {err}"))
        record_automation_event('DISCORD','ROLE_SYNC',ledger_status,summary,
            personnel_id=row.get('personnel_id'),source_key=f"DISCORD-SYNC:{row.get('personnel_id')}",details={'reason':row.get('reason'),'error':err,'attempts':attempts,'next_retry_at':str(next_retry) if next_retry else None})
    return {'ok':True,'status':final_status,'attempts':attempts,'next_retry_at':next_retry.isoformat() if next_retry else None}


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
            reminders.append({'personnel_id':str(pid),'guild_id':p['guild_id'],'discord_user_id':p['discord_user_id'],'type':'M16_INSPECTION','stage':str(insp.get('days')),'reminder_key':f"M16:{insp.get('days')}",'message':f"**1/5 CAV — S-4 NOTICE**\nYour assigned M16 inspection is due in {max(0,int(insp.get('days')))} day{'s' if max(0,int(insp.get('days'))) != 1 else ''}."})
        q=fetch_one("""SELECT MIN(due) due FROM (
                         SELECT expires_at due FROM qualifications WHERE personnel_id=%s AND expires_at IS NOT NULL
                         UNION ALL SELECT expiration_date due FROM personnel_duty_qualifications WHERE personnel_id=%s AND expiration_date IS NOT NULL) x
                         WHERE due BETWEEN CURRENT_DATE AND CURRENT_DATE+INTERVAL '7 days'""",(pid,pid))
        if q and q.get('due'):
            days=(q['due']-date.today()).days
            if days in {7,3,1,0}: reminders.append({'personnel_id':str(pid),'guild_id':p['guild_id'],'discord_user_id':p['discord_user_id'],'type':'QUALIFICATION_EXPIRATION','stage':str(days),'reminder_key':f"QUAL:{q.get('due')}:{days}",'message':f"**1/5 CAV — TRAINING NOTICE**\nA qualification on your Soldier Record expires in {days} day{'s' if days != 1 else ''}."})
    return {'ok':True,'reminders':reminders}

def _member_requirement_progress(requirement):
    """Best-effort percentage for a promotion requirement without changing authority."""
    if not requirement:
        return 0
    if requirement.get('complete'):
        return 100
    detail=str(requirement.get('detail') or '')
    import re
    m=re.search(r'(\d+(?:\.\d+)?)\s*(?:%|)\s*/\s*(\d+(?:\.\d+)?)',detail)
    if m:
        cur=float(m.group(1)); target=max(1.0,float(m.group(2)))
        return max(0,min(99,round((cur/target)*100)))
    if 'NOT SUBMITTED' in detail.upper() or 'NOT ON FILE' in detail.upper() or 'NOT COMPLETE' in detail.upper() or 'NOT CURRENT' in detail.upper():
        return 0
    return 0

def member_home_context(personnel):
    """Read-only, member-friendly home context built from existing authoritative systems."""
    if not personnel:
        return {
            'situation':{},'actions':[],'next_move':{'section':'S-1','title':'REPORT TO S-1','detail':'Your personnel record is not linked yet.','priority':'HIGH'},
            'promotion':None,'ribbons':[],'notifications':[],'recent':[],'weapon':None,'inspection':None,'welcome':None,
            'server':{},'tour':{},'organization':{},'qualification_watch':[]
        }
    p=soldier_view(personnel); pid=p['id']
    situation=safe_member_panel('HOME SITUATION',{},current_situation_snapshot,p)
    actions=safe_member_panel('HOME ACTIONS',[],member_personal_action_center,p)
    recommended=safe_member_panel('HOME NEXT MOVE',{},next_recommended_action,p) or {}
    # Prefer an actually member-clearable action. If none exists, show the next career milestone.
    actionable=[a for a in actions if str(a.get('title') or '').upper() not in {'UPCOMING OPERATION','DEROS'}]
    next_move=(actionable[0] if actionable else recommended)
    promotion_rows=safe_member_panel('HOME PROMOTION',[],promotion_eligibility,p) or []
    promotion=None
    if promotion_rows:
        row=dict(promotion_rows[0]); reqs=[]
        for r in row.get('requirements') or []:
            rr=dict(r); rr['percent']=_member_requirement_progress(rr)
            # Passive requirements are progress; recommendations are command-owned.
            if rr.get('complete'): rr['owner_state']='COMPLETE'
            elif rr.get('kind')=='recommendation': rr['owner_state']='AWAITING COMMAND'
            elif rr.get('kind') in {'tig','ops','readiness'}: rr['owner_state']='IN PROGRESS'
            else: rr['owner_state']='ACTION REQUIRED'
            reqs.append(rr)
        row['requirements']=reqs
        row['percent']=round(sum(x['percent'] for x in reqs)/len(reqs)) if reqs else (100 if row.get('eligible') else 0)
        promotion=row
    ribbons=[]
    for r in safe_member_panel('HOME RIBBONS',[],ribbon_progress_for,pid,award_completed=False) or []:
        if r.get('earned') or r.get('pending_system'): continue
        ribbons.append(dict(r))
    ribbons.sort(key=lambda x:(-int(x.get('percent') or 0),str(x.get('name') or '')))
    notifications=safe_member_panel('HOME NOTIFICATIONS',[],current_notifications,pid) or []
    recent=safe_member_fetch_all('HOME RECENT CHANGES',
        "SELECT * FROM personnel_service_history WHERE personnel_id=%s ORDER BY entry_date DESC,created_at DESC LIMIT 7",(pid,)) or []
    weapon=safe_member_panel('HOME WEAPON',None,current_weapon_for,p)
    inspection=safe_member_panel('HOME INSPECTION',None,weapon_inspection_status,pid)
    welcome=safe_member_panel('HOME WELCOME',{},welcome_packet_context,pid)
    server=safe_member_panel('HOME SERVER SERVICE',{},verified_server_service_snapshot,pid)
    tour=safe_member_panel('HOME TOUR',{},member_tour_phase,p)
    organization=safe_member_panel('HOME ORGANIZATION',{},member_organization_context,p)
    qualification_watch=[]
    try:
        qualification_watch=fetch_all("""SELECT qualification_name,expires_at,status FROM qualifications
            WHERE personnel_id=%s AND expires_at IS NOT NULL ORDER BY expires_at ASC LIMIT 4""",(pid,)) or []
    except Exception:
        qualification_watch=[]
    return {'situation':situation,'actions':actions,'next_move':next_move,'promotion':promotion,'ribbons':ribbons[:3],
            'notifications':notifications[:5],'recent':recent,'weapon':weapon,'inspection':inspection,'welcome':welcome,
            'server':server,'tour':tour,'organization':organization,'qualification_watch':qualification_watch}

@app.get("/dashboard")
@login_required
def dashboard():
    personnel = soldier_view(linked_personnel())
    ctx=member_home_context(personnel)
    return render_template("dashboard.html", personnel=personnel, **ctx)



def personnel_record_context(personnel):
    """Build the complete read-only 201 File / Service Record view."""
    if not personnel:
        return {
            "personnel": None, "qualifications": [], "equipment": [], "awards": [], "award_catalog": [],
            "activity": [], "service_history": [], "assignments": [], "promotions": [],
            "appointments": [], "roster_card": None, "weapon": None,
            "chain_of_command": [], "readiness": {}, "tour_phase_record": ("NO RECORD", None),
            "duty_quals": [], "personal_ops": [], "issued_equipment": [],
            "replacement_training": {"complete":False,"requirements":[]}, "promotion_eligibility": [], "documents": [],
            "leadership_service": {"history":[],"totals":[],"total_days":0}, "leadership_score": {"score":0,"rating":"NOT RATED","breakdown":{}}, "mos_proficiency": None,
            "current_situation": {}, "field_reputation": [], "service_timeline": [], "weapon_personality": None,
            "award_evidence": {}, "promotion_packet": {}, "most_served_with": [], "member_action_center": [],
            "next_recommended_action": {}, "journal_entries": [], "command_watchlist": [],
            "uniform_issue": None, "earned_ribbons": [], "uniform_ribbon_rows": [], "personal_action_center": [],
            "recommended_action": {}, "discord_sync": None, "recruit_login_case": None, "welcome_packet": {"packet":None,"tasks":[],"percent":0}, "hll_stats": hll_service_statistics(None),
        }

    personnel = soldier_view(personnel)
    pid = personnel["id"]
    # 201 File GET is read-oriented. Background/event actions own synchronization;
    # opening a personnel jacket must not trigger a chain of database writes.
    mos_proficiency = safe_member_panel('201 MOS proficiency', None, current_mos_proficiency, personnel)
    leadership_service = safe_member_panel('201 leadership service', {'history':[],'totals':[],'total_days':0}, leadership_service_summary, pid)
    leadership_score = safe_member_panel('201 leadership score', {'score':0,'rating':'NOT RATED','breakdown':{}}, combat_leadership_score, pid)
    weapon = safe_member_panel('201 weapon', None, current_weapon_for, personnel)
    current_situation = safe_member_panel('201 current situation', {}, current_situation_snapshot, personnel)
    reputation = safe_member_panel('201 field reputation', [], field_reputation, personnel)
    service_timeline = safe_member_panel('201 service timeline', [], active_service_timeline, pid)
    weapon_story = safe_member_panel('201 weapon story', None, weapon_personality, weapon["id"]) if weapon else None
    award_evidence = safe_member_panel('201 award evidence', {}, award_recommendation_evidence, pid)
    promotion_packet = safe_member_panel('201 promotion packet', {}, promotion_board_packet, pid)
    served_with = safe_member_panel('201 most served with', [], most_served_with, pid, 5)
    member_actions = safe_member_panel('201 member actions', [], member_personal_action_center, personnel)
    recommended_action = safe_member_panel('201 recommended action', {'section':'HEADQUARTERS','title':'MAINTAIN READINESS','detail':'No immediate deficiency is on file.','priority':'ROUTINE'}, next_recommended_action, personnel)
    journal_entries = safe_member_fetch_all('201 unit journal', """SELECT sje.*,o.operation_number,o.title AS operation_title FROM soldier_journal_entries sje
                                  LEFT JOIN operations o ON o.id=sje.operation_id
                                  WHERE sje.personnel_id=%s AND sje.visibility='UNIT'
                                  ORDER BY sje.entry_date DESC,sje.created_at DESC LIMIT 40""",(pid,))
    watchlist = safe_member_fetch_all('201 command watchlist', "SELECT * FROM command_watchlist WHERE personnel_id=%s AND resolved_at IS NULL ORDER BY created_at DESC",(pid,))

    promotions = safe_member_fetch_all("201 promotions",
        """SELECT ph.*,rc.rank_name,rc.pay_grade
           FROM promotion_history ph
           LEFT JOIN rank_catalog rc ON rc.rank_code=ph.new_rank_code
           WHERE ph.personnel_id=%s
           ORDER BY ph.effective_date DESC,ph.created_at DESC""", (pid,)
    )
    appointments = safe_member_fetch_all("201 appointments",
        """SELECT pa.*,ac.appointment_name
           FROM personnel_appointments pa
           JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code
           WHERE pa.personnel_id=%s
           ORDER BY pa.effective_date DESC,pa.created_at DESC""", (pid,)
    )
    recruit_login_case = safe_member_fetch_one("201 recruit login delivery",
        """SELECT id,case_number,status,discord_user_id,discord_verified_username,
                  credentials_sent_at,credentials_delivery_error,credentials_last_attempt_at,
                  credentials_pending_field_code_enc,credentials_resend_requested_at,
                  credentials_resend_requested_by,credentials_resend_rotate
           FROM recruiting_cases
           WHERE personnel_id=%s
           ORDER BY approved_at DESC NULLS LAST,created_at DESC
           LIMIT 1""", (pid,))
    return {
        "personnel": personnel,
        "qualifications": safe_member_fetch_all("201 qualifications", "SELECT * FROM qualifications WHERE personnel_id=%s ORDER BY qualification_name", (pid,)),
        "equipment": safe_member_fetch_all("201 equipment", "SELECT * FROM equipment_issues WHERE personnel_id=%s ORDER BY item_type,nomenclature", (pid,)),
        "awards": safe_member_fetch_all("201 awards", "SELECT * FROM personnel_awards WHERE personnel_id=%s ORDER BY award_date DESC", (pid,)),
        "award_catalog": safe_member_fetch_all("201 award catalog", "SELECT ribbon_code,ribbon_name FROM ribbon_catalog WHERE is_active=TRUE ORDER BY sort_order,ribbon_name"),
        "activity": safe_member_fetch_all("201 activity", "SELECT * FROM personnel_activity_credit WHERE personnel_id=%s ORDER BY activity_date DESC,created_at DESC LIMIT 100", (pid,)),
        "service_history": safe_member_fetch_all("201 service history", "SELECT * FROM personnel_service_history WHERE personnel_id=%s ORDER BY entry_date DESC,created_at DESC LIMIT 150", (pid,)),
        "documents": safe_member_fetch_all("201 documents", "SELECT * FROM personnel_documents WHERE personnel_id=%s ORDER BY effective_date DESC,created_at DESC LIMIT 100", (pid,)),
        "assignments": safe_member_fetch_all("201 assignments", "SELECT * FROM assignment_history WHERE personnel_id=%s ORDER BY effective_date DESC,created_at DESC", (pid,)),
        "promotions": promotions, "appointments": appointments,
        "roster_card": safe_member_panel("201 roster card", None, battle_roster_for, personnel), "weapon": weapon,
        "chain_of_command": safe_member_panel("201 chain of command", [], chain_of_command_for, personnel),
        "readiness": safe_member_panel("201 readiness", {}, soldier_readiness, personnel), "tour_phase_record": safe_member_panel("201 tour phase", ("NO RECORD",None), tour_phase, personnel),
        "inactivity": safe_member_panel("201 inactivity", {}, inactivity_snapshot, personnel),
        "inactivity_contacts": safe_member_fetch_all("201 inactivity contacts", "SELECT * FROM inactivity_contact_log WHERE personnel_id=%s ORDER BY contacted_at DESC LIMIT 12", (pid,)),
        "duty_quals": safe_member_panel("201 duty qualifications", [], personnel_duty_qualifications, pid), "personal_ops": safe_member_panel("201 personal operations", [], personal_operations, pid),
        "issued_equipment": safe_member_panel("201 issued equipment", [], current_equipment_for, pid),
        "replacement_training": safe_member_panel("201 replacement training", {"complete":False,"requirements":[],"program_title":"IN-PROCESSING","initial_rank":personnel.get("rank_code") or "PVT","replacement_required":False,"record":None}, replacement_training_status, personnel),
        "promotion_eligibility": safe_member_panel("201 promotion eligibility", [], promotion_eligibility, personnel),
        "promotion_onboarding_open": safe_member_panel("201 promotion onboarding gate", True, welcome_packet_promotion_open, pid),
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
        "ribbon_details": safe_member_panel("201 ribbon details", [], ribbon_details_for_member, pid),
        "uniform_issue": safe_member_fetch_one("201 uniform issue",
            """SELECT eih.issued_at,ei.condition_state,sic.item_name
               FROM equipment_issue_history eih
               JOIN equipment_inventory ei ON ei.id=eih.equipment_id
               JOIN supply_item_catalog sic ON sic.item_code=ei.item_code
               WHERE eih.personnel_id=%s AND eih.is_current=TRUE AND ei.item_code='AG44'
               LIMIT 1""",
            (pid,),
        ),
        "earned_ribbons": (safe_member_panel("201 worn ribbons", ([],[]), worn_ribbon_rows, pid) or ([],[]))[1],
        "uniform_ribbon_rows": (safe_member_panel("201 worn ribbon rows", ([],[]), worn_ribbon_rows, pid) or ([],[]))[0],
        "personal_action_center": safe_member_panel("201 personal action center", [], member_personal_action_center, personnel),
        # IMPORTANT: use the already fault-isolated recommendation computed above.
        # Calling next_recommended_action() directly here can touch optional weapon,
        # qualification, operation, tour, or promotion tables and must never be able
        # to take down the entire 201 File.
        "recommended_action": recommended_action,
        "field_reputation": reputation,
        "current_situation": current_situation,
        "most_served_with": served_with,
        "discord_sync": safe_member_fetch_one("201 Discord sync", "SELECT status,reason,requested_at,processed_at,error_text FROM discord_role_sync_queue WHERE personnel_id=%s ORDER BY requested_at DESC LIMIT 1", (pid,)),
        "recruit_login_case": recruit_login_case,
        "welcome_packet": safe_member_panel("201 Welcome Packet", {"packet":None,"tasks":[],"percent":0}, welcome_packet_context, pid),
        "hll_stats": hll_service_statistics(pid),
        "m16_service": safe_member_panel("201 M16 service", {}, hll_m16_service_statistics, pid, weapon),
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
    # Nothing in the member 201 route is allowed to escape to the global 500 page.
    # This includes the shared personnel-link lookup itself.
    person=None
    try:
        person=linked_personnel()
        if not person:
            return redirect(url_for("my_soldier_record"))
        safe_member_panel("201 visit tracking", None, welcome_visit, person["id"], "VIEW_201")
        context=personnel_record_context(person)
        return render_template("personnel_file.html", **context)
    except Exception:
        error_reference=secrets.token_hex(4).upper()
        log.exception("MEMBER 201 FILE TOP-LEVEL FAILURE [%s] personnel=%s session_user=%s",
                      error_reference,(person or {}).get("id"),session.get("user_id"))
        try:
            if not person:
                try:
                    # Session personnel_id is the authoritative member-login link and avoids
                    # depending on user_personnel_links when that optional linkage is stale.
                    pid=session.get("personnel_id")
                    if pid:
                        person=fetch_one("SELECT * FROM personnel WHERE id=%s",(pid,))
                except Exception:
                    log.exception("MEMBER 201 DIRECT PERSONNEL RECOVERY FAILURE [%s]",error_reference)
            if person:
                context=member_record_fallback_context(person,error_reference)
                context["record_warning"]="The extended 201 File could not load one data source. Your core personnel and issued-weapon record remains available while Headquarters logs the failing module."
                return render_template("member_record_core.html",**context),200
            return render_template("member_record_core.html",personnel=None,roster_card=None,weapon=None,
                                   record_error_reference=error_reference,
                                   record_warning="Your authenticated session is active, but Headquarters could not resolve the personnel link for this request."),200
        except Exception:
            # Last-resort response deliberately avoids the normal base template/context processor.
            log.exception("MEMBER 201 ABSOLUTE FALLBACK FAILURE [%s]",error_reference)
            return Response(
                "<!doctype html><html><head><meta charset='utf-8'><title>201 File Recovery</title></head>"
                "<body><main><h1>201 FILE — RECOVERY MODE</h1>"
                f"<p>Headquarters logged diagnostic reference <b>{error_reference}</b>.</p>"
                "<p><a href='/my-soldier-record'>Return to Wall Locker</a></p></main></body></html>",
                status=200,mimetype="text/html")



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
    """Public-safe active personnel records with canonical structured assignment labels."""
    rows=[]
    try:
        rows=fetch_all("""
            SELECT p.id,p.rank_code,p.last_name,p.first_name,p.unit_code,p.platoon,p.squad,p.fire_team,
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
            rows=fetch_all("""
                SELECT p.id,p.rank_code,p.last_name,p.first_name,p.unit_code,p.platoon,p.squad,p.fire_team,
                       p.duty_position,p.field_status,p.unit_node_id,p.readiness_percent,
                       0 AS rank_precedence
                FROM personnel p
                WHERE p.separated_at IS NULL
                ORDER BY p.last_name,p.first_name
            """) or []
        except Exception:
            log.exception("Public active roster compatibility query unavailable")
            rows=[]
    for row in rows:
        row["platoon"]=canonical_formation_label(row.get("platoon"),"PLATOON") if row.get("platoon") else None
        row["squad"]=canonical_formation_label(row.get("squad"),"SQUAD") if row.get("squad") else None
        row["fire_team"]=canonical_formation_label(row.get("fire_team"),"TEAM") if row.get("fire_team") else None
    return rows

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
    # Backward-compatible URL: the member workspace is now uniformly named MY SQUAD.
    return redirect(url_for("my_squad"))




@app.route("/operations", methods=["GET","POST"])
def operations():
    """Single-screen S-3 Operations Control.

    The website is authoritative. Scheduling, editing, duty assignment, closeout,
    cancel/delete, public display, Discord notices, attendance and M16 tracking all
    originate from the same operations row.
    """
    # Members get a deliberately read-only, failure-isolated view.  Staff reconciliation
    # jobs must never prevent a Soldier from opening the Operations tab.
    if request.method == "GET" and session.get("access_role") in {"member","nco","company_hq"}:
        viewer = linked_personnel() if session.get("user_id") else None
        if not viewer:
            return redirect(url_for("report_for_duty"))
        try:
            welcome_visit(viewer["id"], "VIEW_OPERATIONS")
        except Exception:
            log.exception("Welcome Packet Operations milestone failed for %s", viewer.get("id"))
        try:
            rows = fetch_all("""SELECT o.*,op.attendance_status AS my_attendance_status,op.duty_role AS my_duty_role,
                                      op.rounds_expended AS my_rounds_expended,op.credited_at AS my_credited_at
                               FROM operations o
                               LEFT JOIN operation_participation op ON op.operation_id=o.id AND op.personnel_id=%s
                               WHERE UPPER(COALESCE(o.status,'')) <> 'DELETED'
                                 AND (UPPER(COALESCE(o.publish_status,''))='PUBLISHED' OR op.personnel_id IS NOT NULL)
                               ORDER BY COALESCE(o.start_at,o.created_at) DESC
                               LIMIT 100""", (viewer["id"],))
        except Exception:
            log.exception("MEMBER OPERATIONS READ FAILED personnel=%s", viewer.get("id"))
            rows = []
        now = datetime.now(timezone.utc)
        upcoming=[]; completed=[]
        for op in rows:
            op=dict(op)
            status=str(op.get("status") or op.get("lifecycle_status") or "SCHEDULED").upper()
            start=op.get("start_at")
            end=(start + timedelta(minutes=int(op.get("duration_minutes") or 90))) if start else None
            if status in {"COMPLETED","CLOSED","ARCHIVED","CANCELLED","CANCELED"} or (end and end < now):
                completed.append(op)
            else:
                upcoming.append(op)
        decorate_operation_times(upcoming); decorate_operation_times(completed)
        # Attach the viewer's RCON-verified field record to each visible operation.
        # This is read-only evidence and never grants personnel credit by itself.
        for op in upcoming + completed[:30]:
            try:
                tel=hll_operation_telemetry(op,viewer["id"])
                op["hll_viewer"]=(tel or {}).get("viewer")
                op["hll_match_count"]=len((tel or {}).get("matches") or [])
            except Exception:
                op["hll_viewer"]=None
                op["hll_match_count"]=0
        return render_template("member_operations.html", personnel=viewer, upcoming=upcoming, completed=completed[:30], hll_server=hll_live_server_snapshot(), hll_stats=hll_service_statistics(viewer["id"]))

    staff_roles={"s3","training","battalion_hq","commander","admin"}
    can_control=bool(session.get("user_id") and session.get("access_role") in staff_roles)
    if request.method == "POST":
        if not session.get("user_id"):
            return redirect(url_for("report_for_duty"))
        if not can_control:
            abort(403)
        action=(request.form.get("action") or "").strip().lower()
        authority=session.get("display_name") or session.get("username") or "S-3 OPERATIONS"

        if action=="create_operation":
            title=(request.form.get("title") or "").strip()
            start_at=parse_operation_local_datetime(request.form.get("start_at"))
            channel_id=(request.form.get("credit_channel_id") or "").strip() or None
            if not title:
                flash("OPERATION TITLE IS REQUIRED.","danger")
                return redirect(url_for("operations"))
            if not start_at:
                flash("STEP-OFF DATE / TIME IS REQUIRED.","danger")
                return redirect(url_for("operations"))
            duration=max(45,min(720,int(request.form.get("duration_minutes") or 90)))
            if start_at + timedelta(minutes=duration) <= datetime.now(timezone.utc):
                flash("STEP-OFF / END TIME IS ALREADY IN THE PAST.","danger")
                return redirect(url_for("operations"))
            if not channel_id:
                flash("SELECT A DISCORD OPERATION VOICE CHANNEL.","danger")
                return redirect(url_for("operations"))
            ch=fetch_one("SELECT channel_name FROM discord_channel_directory WHERE channel_id=%s AND active=TRUE",(channel_id,))
            if not ch:
                flash("THE SELECTED DISCORD VOICE CHANNEL IS NOT IN THE CURRENT BATTALION CLERK DIRECTORY. WAIT FOR CLERK SYNC OR SELECT ANOTHER CHANNEL.","danger")
                return redirect(url_for("operations"))
            opnum=(request.form.get("operation_number") or "").strip() or next_operation_number()
            threshold=max(5,min(duration,int(request.form.get("credit_threshold_minutes") or 45)))
            op=None
            try:
                op=fetch_one(
                    """INSERT INTO operations
                       (operation_code,title,operation_number,operation_type,area_of_operations,commander,h_hour,
                        situation,mission,execution,service_support,command_signal,status,lifecycle_status,start_at,operation_date,
                        duration_minutes,credit_threshold_minutes,rounds_per_soldier,credit_channel_id,credit_channel_name,
                        reminder_minutes,formation_scope,formation_unit_node_id,publish_status)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PLANNING','PLANNING',%s,%s,%s,%s,300,%s,%s,%s,%s,%s,'DRAFT') RETURNING *""",
                    (opnum,title,opnum,request.form.get("operation_type") or "OFFICIAL OPERATION",
                     request.form.get("area_of_operations") or None,request.form.get("commander") or None,request.form.get("h_hour") or None,
                     request.form.get("situation") or None,request.form.get("mission") or None,request.form.get("execution") or None,
                     request.form.get("service_support") or None,request.form.get("command_signal") or None,start_at,start_at.date(),duration,threshold,
                     channel_id,ch.get("channel_name"),request.form.get("reminder_minutes") or "1440,120,30",
                     request.form.get("formation_scope") or "BATTALION",request.form.get("formation_unit_node_id") or None),
                )
                event=schedule_operation_event(op,authority)
                staff_log("S-3","OPERATION SCHEDULED",f"{opnum} — {title}",authority,details={"event_id":str(event['id'])})
                flash(f"{opnum} SCHEDULED. WEBSITE, DISCORD NOTICE, ATTENDANCE, AND HLL TELEMETRY ARE ACTIVE.","success")
            except Exception as exc:
                # A failed publish must never leave a half-created Operation visible.
                if op:
                    try: execute("DELETE FROM battalion_events WHERE operation_id=%s",(op["id"],))
                    except Exception: pass
                    try: execute("DELETE FROM operations WHERE id=%s",(op["id"],))
                    except Exception: pass
                log.exception("Operation scheduling failed")
                flash(f"OPERATION WAS NOT SCHEDULED: {exc}","danger")
            return redirect(url_for("operations"))

        operation_id=request.form.get("operation_id")
        op=operation_record(operation_id) if operation_id else None
        if not op:
            flash("OPERATION RECORD NOT FOUND.","danger")
            return redirect(url_for("operations"))

        if action=="update_operation":
            start_at=parse_operation_local_datetime(request.form.get("start_at"))
            channel_id=(request.form.get("credit_channel_id") or "").strip() or None
            duration=max(45,min(720,int(request.form.get("duration_minutes") or op.get("duration_minutes") or 90)))
            threshold=max(5,min(duration,int(request.form.get("credit_threshold_minutes") or op.get("credit_threshold_minutes") or 45)))
            if not start_at or not channel_id:
                flash("STEP-OFF AND DISCORD VOICE CHANNEL ARE REQUIRED.","danger")
                return redirect(url_for("operations"))
            ch=fetch_one("SELECT channel_name FROM discord_channel_directory WHERE channel_id=%s AND active=TRUE",(channel_id,))
            if not ch:
                flash("SELECTED DISCORD VOICE CHANNEL IS NOT AVAILABLE.","danger")
                return redirect(url_for("operations"))
            execute("""UPDATE operations SET title=%s,start_at=%s,operation_date=%s,duration_minutes=%s,
                      credit_threshold_minutes=%s,rounds_per_soldier=300,credit_channel_id=%s,credit_channel_name=%s,
                      formation_scope=%s,formation_unit_node_id=%s,area_of_operations=%s,commander=%s,
                      reminder_minutes=%s,updated_at=NOW() WHERE id=%s""",
                    ((request.form.get("title") or op.get("title") or "Operation").strip(),start_at,start_at.date(),duration,threshold,
                     channel_id,ch.get("channel_name"),request.form.get("formation_scope") or op.get("formation_scope") or "BATTALION",
                     request.form.get("formation_unit_node_id") or None,request.form.get("area_of_operations") or None,
                     request.form.get("commander") or None,request.form.get("reminder_minutes") or op.get("reminder_minutes") or "1440,120,30",operation_id))
            try:
                schedule_operation_event(operation_record(operation_id),authority)
                execute("UPDATE operation_duty_assignments SET discord_published_at=NULL WHERE operation_id=%s",(operation_id,))
                flash("OPERATION UPDATED. DISCORD / ATTENDANCE / HLL TELEMETRY AUTOMATION HAS BEEN RESYNCHRONIZED.","success")
            except Exception as exc:
                log.exception("Operation reschedule failed")
                flash(f"OPERATION SAVED, BUT CLERK RESYNC FAILED: {exc}","danger")

        elif action=="assign_duty":
            pid=request.form.get("personnel_id")
            duty=(request.form.get("duty_role") or "").strip()
            if not pid or not duty:
                flash("SELECT A SOLDIER AND DUTY ROLE.","danger")
            else:
                person=canonical_personnel_snapshot(pid) or {}
                execute("""INSERT INTO operation_duty_assignments(operation_id,personnel_id,duty_role,mos_code,element,assigned_by,remarks)
                           VALUES(%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT(operation_id,personnel_id) DO UPDATE SET duty_role=EXCLUDED.duty_role,
                           mos_code=EXCLUDED.mos_code,element=EXCLUDED.element,assigned_by=EXCLUDED.assigned_by,
                           assigned_at=NOW(),discord_published_at=NULL,remarks=EXCLUDED.remarks""",
                        (operation_id,pid,duty,person.get("mos_code"),request.form.get("element") or None,authority,request.form.get("remarks") or None))
                execute("""INSERT INTO operation_participation(operation_id,personnel_id,unit_node_id,duty_role,attendance_status,rounds_expended,credited_by)
                           VALUES(%s,%s,%s,%s,'ASSIGNED',0,%s)
                           ON CONFLICT(operation_id,personnel_id) DO UPDATE SET duty_role=EXCLUDED.duty_role""",
                        (operation_id,pid,person.get("unit_node_id"),duty,authority))
                flash("OPERATION DUTY ASSIGNMENT SAVED. BATTALION CLERK WILL DISTRIBUTE THE UPDATED DUTY ROSTER IN DISCORD.","success")

        elif action=="remove_duty":
            pid=request.form.get("personnel_id")
            execute("DELETE FROM operation_duty_assignments WHERE operation_id=%s AND personnel_id=%s",(operation_id,pid))
            execute("DELETE FROM operation_participation WHERE operation_id=%s AND personnel_id=%s AND UPPER(COALESCE(attendance_status,''))='ASSIGNED' AND COALESCE(rounds_expended,0)=0",(operation_id,pid))
            flash("OPERATION DUTY ASSIGNMENT REMOVED.","success")

        elif action=="close_operation":
            event=operation_live_event(operation_id)
            if event:
                result=finalize_operation_event(event["id"],authority,request.form.get("result") or "COMPLETED",request.form.get("commander_remarks") or None)
                flash(f"OPERATION COMPLETED — {result['credited']} CREDITED; {result['weapon_rounds_applied']} M16 ROUNDS RECONCILED.","success")
            else:
                # Still allow S-3 to close a record when Clerk telemetry was unavailable.
                complete_operation(operation_id,request.form.get("result") or "COMPLETED",request.form.get("commander_remarks") or None,authority)
                execute("UPDATE operations SET status='COMPLETED',lifecycle_status='COMPLETED',publish_status='CLOSED',completed_at=COALESCE(completed_at,NOW()),updated_at=NOW() WHERE id=%s",(operation_id,))
                flash("OPERATION COMPLETED. NO LIVE CLERK EVENT WAS AVAILABLE; THE WEBSITE RECORD WAS CLOSED.","warning")

        elif action=="cancel_operation":
            execute("UPDATE battalion_events SET status='CANCELLED',ends_at=LEAST(ends_at,NOW()) WHERE operation_id=%s",(operation_id,))
            execute("UPDATE operations SET status='CANCELLED',lifecycle_status='CANCELLED',publish_status='CLOSED',completed_at=COALESCE(completed_at,NOW()),updated_at=NOW() WHERE id=%s",(operation_id,))
            staff_log("S-3","OPERATION CANCELLED",f"{op.get('operation_number') or 'OPERATION'} — {op.get('title')}",authority)
            flash("OPERATION CANCELLED. DISCORD TRACKING AND FUTURE VOICE CREDIT ARE STOPPED.","success")

        return redirect(url_for("operations"))

    if database_ready():
        reconcile_operation_schedule_states()
        ensure_published_operation_events("S-3 SINGLE-SCREEN RECONCILIATION")
        run_operation_maintenance("S-3 SINGLE-SCREEN RECONCILIATION")

    current=fetch_all("""SELECT * FROM operations
        WHERE UPPER(COALESCE(status,'')) NOT IN ('COMPLETED','CLOSED','CANCELLED','CANCELED','ARCHIVED','DELETED')
          AND UPPER(COALESCE(lifecycle_status,'PLANNING')) NOT IN ('CLOSED','COMPLETED','CANCELLED','CANCELED','ARCHIVED','AAR FILED','DELETED')
          AND (start_at IS NULL OR (start_at + make_interval(mins => COALESCE(duration_minutes,90))) > NOW())
        ORDER BY CASE WHEN start_at IS NULL THEN 1 ELSE 0 END,start_at ASC,created_at DESC""") if database_ready() else []
    completed=fetch_all("""SELECT o.*,aar.ammunition_expended,aar.filed_at
        FROM operations o LEFT JOIN after_action_reports aar ON aar.operation_id=o.id
        WHERE (UPPER(COALESCE(o.status,'')) IN ('COMPLETED','CLOSED','ARCHIVED','CANCELLED','CANCELED')
           OR UPPER(COALESCE(o.lifecycle_status,'')) IN ('COMPLETED','CLOSED','ARCHIVED','CANCELLED','CANCELED'))
          AND UPPER(COALESCE(o.status,''))<>'DELETED'
        ORDER BY COALESCE(o.completed_at,o.start_at,o.created_at) DESC LIMIT 50""") if database_ready() else []

    viewer=linked_personnel() if session.get("user_id") else None
    member_mode=bool(viewer and session.get("access_role") not in COMMAND_ROLES and session.get("access_role") not in {"s3","company_hq"})
    if member_mode:
        def _relevant(op):
            if any(str(x.get("personnel_id"))==str(viewer["id"]) for x in operation_participants(op["id"])):
                return True
            try:
                return any(str(x.get("id"))==str(viewer["id"]) for x in operation_expected_roster(op))
            except Exception:
                return str(op.get("publish_status") or "").upper()=="PUBLISHED"
        current=[op for op in current if _relevant(op)]
        completed=[op for op in completed if _relevant(op)]

    decorate_operation_times(current); decorate_operation_times(completed)
    personnel_list=[] if member_mode else (fetch_all("SELECT * FROM personnel WHERE separated_at IS NULL AND COALESCE(archived,FALSE)=FALSE ORDER BY last_name,first_name") if database_ready() else [])
    units=fetch_all("SELECT * FROM unit_nodes WHERE is_active=TRUE ORDER BY sort_order,display_name") if database_ready() else []
    assignments={}
    live_map={}
    for op in current:
        assignments[str(op["id"])]=fetch_all("""SELECT oda.*,p.rank_code,p.first_name,p.last_name,p.unit_code,p.platoon,p.squad
            FROM operation_duty_assignments oda JOIN personnel p ON p.id=oda.personnel_id
            WHERE oda.operation_id=%s ORDER BY oda.element NULLS LAST,p.last_name,p.first_name""",(op["id"],))
        event,attendance=operation_live_attendance(op["id"])
        expected=operation_expected_roster(op)
        live_map[str(op["id"])]= {"event":event,"attendance":attendance,"expected":len(expected),
                                   "ready":sum(1 for p in expected if int(p.get("readiness_percent") or 0)>=80),
                                   "sync_state":"SYNCED" if event else "REPAIRING"}
    return render_template("operations.html",current=current,completed=completed,personnel_list=personnel_list,units=units,
                           duty_assignments=assignments,live_map=live_map,discord_voice_channels=(fetch_all("SELECT * FROM discord_channel_directory WHERE active=TRUE AND channel_type='VOICE' ORDER BY category_name NULLS FIRST,channel_name") if database_ready() else []),
                           clerk_health=clerk_health_snapshot(),member_mode=member_mode,viewer=viewer,can_control=can_control,
                           hll_server=hll_live_server_snapshot())


@app.get("/operations/<operation_id>")
def operation_detail(operation_id):
    reconcile_operation_schedule_states()
    ensure_published_operation_events("OPERATION CONTROL BOARD")
    op=operation_record(operation_id)
    if not op:
        abort(404)
    decorate_operation_times([op])
    participants=operation_participants(operation_id)
    units=operation_units_for(operation_id)
    aar=fetch_one("SELECT * FROM after_action_reports WHERE operation_id=%s",(operation_id,))
    journal=fetch_all("SELECT * FROM operation_journal_entries WHERE operation_id=%s ORDER BY entry_date,created_at",(operation_id,))
    photos=fetch_all("SELECT * FROM operation_photographs WHERE operation_id=%s ORDER BY sort_order,uploaded_at",(operation_id,))
    live_event,live_attendance=operation_live_attendance(operation_id)
    expected=operation_expected_roster(op)
    discord_voice_channels=fetch_all("SELECT * FROM discord_channel_directory WHERE active=TRUE AND channel_type='VOICE' ORDER BY category_name NULLS FIRST,channel_name") if database_ready() else []
    viewer=linked_personnel() if session.get("user_id") else None
    hll_telemetry=hll_operation_telemetry(op,(viewer or {}).get("id"))
    return render_template("operation_detail.html",op=op,participants=participants,units=units,aar=aar,journal=journal,photos=photos,
                           live_event=live_event,live_attendance=live_attendance,expected_roster=expected,
                           duty_suggestions=operation_duty_suggestions(operation_id),discord_voice_channels=discord_voice_channels,
                           clerk_health=clerk_health_snapshot(),hll_telemetry=hll_telemetry)


@app.post("/operations/<operation_id>/schedule")
@login_required
def operation_schedule_action(operation_id):
    if session.get("access_role") not in {"s3","company_hq","battalion_hq","commander","admin"}: abort(403)
    op=operation_record(operation_id)
    if not op: abort(404)
    start_at=parse_operation_local_datetime(request.form.get("start_at")) if request.form.get("start_at") else op.get("start_at")
    duration=max(45,int(request.form.get("duration_minutes") or op.get("duration_minutes") or 90))
    threshold=max(5,min(duration,int(request.form.get("credit_threshold_minutes") or op.get("credit_threshold_minutes") or 45)))
    rounds=300
    selected_channel_id=request.form.get("credit_channel_id") or op.get("credit_channel_id")
    channel_directory=fetch_one("SELECT channel_name FROM discord_channel_directory WHERE channel_id=%s AND active=TRUE",(selected_channel_id,)) if selected_channel_id else None
    selected_channel_name=(channel_directory or {}).get("channel_name") or request.form.get("credit_channel_name") or op.get("credit_channel_name") or "Operation Voice"
    execute("""UPDATE operations SET start_at=%s,operation_date=%s,duration_minutes=%s,credit_threshold_minutes=%s,
              rounds_per_soldier=%s,credit_channel_id=%s,credit_channel_name=%s,reminder_minutes=%s,
              formation_scope=%s,formation_unit_node_id=%s,updated_at=NOW() WHERE id=%s""",
            (start_at,(start_at.date() if start_at else None),duration,threshold,rounds,
             selected_channel_id,
             selected_channel_name,
             request.form.get("reminder_minutes") or op.get("reminder_minutes") or "1440,120,30",
             request.form.get("formation_scope") or op.get("formation_scope") or "BATTALION",
             request.form.get("formation_unit_node_id") or op.get("formation_unit_node_id"),operation_id))
    op=operation_record(operation_id)
    authority=session.get("display_name") or session.get("username") or "S-3"
    try:
        if not selected_channel_id:
            raise ValueError("Select a Discord voice channel before publishing.")
        if not start_at:
            raise ValueError("Step-off date/time is required.")
        if start_at + timedelta(minutes=duration) <= datetime.now(timezone.utc):
            raise ValueError("The Operation end time is already in the past. Check the step-off date/time.")
        event=schedule_operation_event(op,authority)
        staff_log("S-3","OPERATION PUBLISHED",f"{op.get('operation_number')} — {op.get('title')}",authority,details={"event_id":str(event['id'])})
        flash("OPERATION SCHEDULE UPDATED. DISCORD NOTIFICATION, ATTENDANCE CREDIT, AND HLL TELEMETRY ARE ARMED.","success")
    except Exception as exc:
        flash(f"OPERATION SCHEDULE UPDATE FAILED: {exc}","danger")
    return redirect(url_for("operations"))


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
    repaired=0; credited=0
    operation_id=event.get("operation_id")
    if str(event.get("event_type") or '').upper()=="OPERATION" and operation_id:
        for a in attendance:
            secs=int(a.get("qualifying_seconds") or 0)
            if secs<=0:
                continue
            result_row=sync_operation_presence_from_attendance(event,a["personnel_id"],secs,authority)
            repaired+=int(result_row.get("rounds_applied") or 0)
            status,percent=operation_presence_status(event,secs)
            if result_row.get("full_credit"):
                credited+=1
                execute("""UPDATE battalion_event_attendance SET credited_at=COALESCE(credited_at,NOW()),attendance_grade='FULL CREDIT',attendance_percent=100,updated_at=NOW()
                           WHERE event_id=%s AND personnel_id=%s""",(event_id,a["personnel_id"]))
            else:
                execute("""UPDATE battalion_event_attendance SET attendance_grade=%s,attendance_percent=%s,updated_at=NOW()
                           WHERE event_id=%s AND personnel_id=%s""",(status,percent,event_id,a["personnel_id"]))
        complete_operation(operation_id,result,remarks,authority)
        execute("UPDATE operations SET lifecycle_status='CLOSED',publish_status='CLOSED',status='COMPLETED',updated_at=NOW() WHERE id=%s",(operation_id,))
    participated=sum(1 for row in attendance if int(row.get("qualifying_seconds") or 0)>0)
    return {"tracked":len(attendance),"participated":participated,"credited":credited,"weapon_rounds_applied":repaired,"threshold":threshold}


@app.post("/operations/<operation_id>/close")
@login_required
def operation_close_action(operation_id):
    if session.get("access_role") not in {"s3","battalion_hq","commander","admin"}: abort(403)
    op=operation_record(operation_id)
    if not op: abort(404)
    if str(op.get('status') or '').upper() in {'COMPLETED','CLOSED','ARCHIVED'}:
        flash("OPERATION CLOSEOUT BLOCKED — THIS OPERATION IS ALREADY A PERMANENT COMPLETED RECORD.","warning")
        return redirect(url_for("operation_detail",operation_id=operation_id))
    event=operation_live_event(operation_id)
    if not event:
        flash("NO ACTIVE BATTALION CLERK EVENT IS LINKED TO THIS OPERATION.","danger")
        return redirect(url_for("operation_detail",operation_id=operation_id))
    authority=session.get("display_name") or session.get("username") or "S-3"
    result=finalize_operation_event(event["id"],authority,request.form.get("result") or None,request.form.get("commander_remarks") or None)
    staff_log("S-3","OPERATION CLOSED",f"{op.get('operation_number') or op.get('operation_code')} — {op.get('title')}",authority,details={"operation_id":str(operation_id),"credited":result.get('credited'),"participants":result.get('participated'),"rounds":result.get('weapon_rounds_applied'),"result":request.form.get('result')})
    flash(f"OPERATION CLOSED — {result['credited']} SOLDIER{'S' if int(result['credited']) != 1 else ''} CREDITED; {result['weapon_rounds_applied']} M16 ROUNDS RECONCILED.","success")
    return redirect(url_for("operation_detail",operation_id=operation_id))


@app.post("/operations/<operation_id>/delete")
@login_required
def operation_delete_action(operation_id):
    if session.get("access_role") not in {"s3","company_hq","battalion_hq","commander","admin"}: abort(403)
    op=operation_record(operation_id)
    if not op: abort(404)
    credited=int((fetch_one("""SELECT COUNT(*) AS total FROM operation_participation
                              WHERE operation_id=%s AND (UPPER(COALESCE(attendance_status,'')) IN ('FULL CREDIT','CREDITED','PARTICIPATED','PRESENT') OR COALESCE(rounds_expended,0)>0)""",(operation_id,)) or {'total':0})['total'] or 0)
    aar=fetch_one("SELECT id FROM after_action_reports WHERE operation_id=%s",(operation_id,))
    if credited or aar or str(op.get('status') or '').upper() in {'COMPLETED','CLOSED','ARCHIVED'}:
        flash('THIS OPERATION ALREADY CONTAINS PERMANENT SERVICE HISTORY. USE COMPLETE / CANCEL INSTEAD OF DELETE.','warning')
        return redirect(url_for('operations'))
    authority=session.get('display_name') or session.get('username') or 'S-3'
    # Soft-delete the authoritative Website row so foreign-key/audit history can never make
    # the user-facing DELETE button fail. Remove the Clerk event so reminders, Discord
    # announcements, attendance, and M16 tracking stop immediately.
    execute("DELETE FROM battalion_events WHERE operation_id=%s OR id=%s",(operation_id,op.get('clerk_event_id')))
    execute("UPDATE operations SET status='DELETED',lifecycle_status='DELETED',publish_status='DRAFT',clerk_event_id=NULL,completed_at=COALESCE(completed_at,NOW()),updated_at=NOW() WHERE id=%s",(operation_id,))
    staff_log('S-3','OPERATION DELETED',f"{op.get('operation_number') or op.get('operation_code')} — {op.get('title')}",authority,
              details={'operation_id':str(operation_id),'reason':request.form.get('reason') or 'S-3 scheduled operation removed'})
    flash('OPERATION DELETED FROM THE ACTIVE SYSTEM. DISCORD NOTICES, ATTENDANCE CREDIT, AND HLL TELEMETRY ARE STOPPED.','success')
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

    weapon = safe_member_panel('201 weapon', None, current_weapon_for, personnel) if personnel else None
    if weapon:
        refresh_weapon_condition(weapon["id"])
        weapon = safe_member_panel('201 weapon', None, current_weapon_for, personnel)
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
        """SELECT p.id,p.rank_code,p.last_name,p.first_name,p.unit_code,p.platoon,p.squad,
                  p.duty_position,p.field_status,p.readiness_status,p.readiness_percent,p.mos_code
           FROM personnel p LEFT JOIN rank_catalog rc ON rc.rank_code=p.rank_code WHERE p.archived=FALSE AND p.separated_at IS NULL
           ORDER BY COALESCE(rc.precedence,0) DESC,p.last_name,p.first_name
           LIMIT 250"""
    ) if database_ready() else []
    return render_template("personnel.html", personnel=personnel, roster=roster)




@app.get("/training")
def training():
    raw_personnel = linked_personnel() if database_ready() else None
    if raw_personnel:
        safe_member_panel("Training visit tracking", None, welcome_visit, raw_personnel["id"], "VIEW_TRAINING")
    personnel = soldier_view(raw_personnel) if raw_personnel else None
    qualifications = []
    duty_qualifications = []
    catalog = safe_member_panel("Training catalog", [], duty_qualification_catalog) if database_ready() else []
    if personnel:
        qualifications = safe_member_fetch_all("Member training qualifications", "SELECT * FROM qualifications WHERE personnel_id=%s ORDER BY qualification_name", (personnel["id"],))
        duty_qualifications = safe_member_panel("Member duty qualifications", [], personnel_duty_qualifications, personnel["id"])
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
    personnel=linked_personnel() if session.get("user_id") else None
    documents=[]; ops=[]
    if personnel and session.get("access_role") in {"member","nco","company_hq"}:
        try:
            welcome_visit(personnel["id"],"VIEW_OPERATIONS")
            pkt=fetch_one("SELECT current_phase FROM welcome_packets WHERE personnel_id=%s",(personnel["id"],))
            if pkt and pkt.get("current_phase") in {"MOVEMENT_ASSIGNMENT","UNIT_ORIENTATION","COMPLETE"}:
                welcome_visit(personnel["id"],"REVIEW_MOVEMENT_ORDERS")
        except Exception:
            log.exception("Welcome Packet Orders milestone failed for %s", personnel.get("id"))
        try:
            documents=fetch_all("SELECT * FROM personnel_documents WHERE personnel_id=%s ORDER BY effective_date DESC,created_at DESC",(personnel["id"],))
        except Exception:
            log.exception("MEMBER PERSONAL ORDERS READ FAILED personnel=%s",personnel.get("id"))
            documents=[]
        try:
            ops=fetch_all("""SELECT DISTINCT o.* FROM operations o
                              LEFT JOIN operation_participation op ON op.operation_id=o.id AND op.personnel_id=%s
                              WHERE UPPER(COALESCE(o.status,'')) <> 'DELETED'
                                AND (op.personnel_id IS NOT NULL OR UPPER(COALESCE(o.publish_status,''))='PUBLISHED')
                              ORDER BY COALESCE(o.start_at,o.created_at) DESC LIMIT 100""",(personnel["id"],))
        except Exception:
            log.exception("MEMBER BATTALION ORDERS READ FAILED personnel=%s",personnel.get("id"))
            ops=[]
        return render_template("orders.html", operations=ops, documents=documents, personnel=personnel)

    if database_ready():
        try:
            archive_expired_operations()
        except Exception:
            log.exception("Staff Orders archive maintenance failed")
        try:
            ops=fetch_all("SELECT * FROM operations WHERE UPPER(COALESCE(status,'')) <> 'DELETED' ORDER BY COALESCE(start_at,created_at) DESC")
        except Exception:
            log.exception("Staff Orders operation read failed")
            ops=[]
    return render_template("orders.html", operations=ops, documents=documents, personnel=personnel)


@app.get("/why-join-us")
def why_join():
    return render_template("why_join.html")




@app.get("/1-5-awards-and-decorations")
def awards_decorations():
    ribbons=[]
    try:
        if database_ready():
            ribbons = fetch_all("""SELECT ribbon_code,ribbon_name,automation_mode,requirement_text,description_text,earning_text,award_type_label,sort_order,image_filename
                                   FROM ribbon_catalog WHERE is_active=TRUE ORDER BY sort_order,ribbon_name""")
    except Exception:
        app.logger.exception("Public awards catalog unavailable")
    recent_decorations=[]
    try:
        if database_ready():
            recent_decorations=fetch_all("""SELECT pa.award_name,pa.award_date,pa.order_number,p.rank_code,p.first_name,p.last_name
                                           FROM personnel_awards pa JOIN personnel p ON p.id=pa.personnel_id
                                           WHERE p.separated_at IS NULL AND COALESCE(p.archived,FALSE)=FALSE
                                           ORDER BY pa.award_date DESC,pa.id DESC LIMIT 12""")
    except Exception:
        app.logger.exception("Recent public decorations unavailable")
    return render_template("awards_decorations.html", ribbons=ribbons, medals=[], recent_decorations=recent_decorations)

@app.get("/about-1-5-cav")
def about():
    return render_template("about.html")


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
        session["recruiting_fallback"] = True
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
        session["recruiting_fallback"] = True
        return redirect(url_for("recruiting"))
    if request.args.get("error"):
        flash("DISCORD IDENTITY VERIFICATION WAS CANCELLED.", "warning")
        return redirect(url_for("recruiting"))
    code = request.args.get("code", "").strip()
    if not code:
        flash("DISCORD DID NOT RETURN AN AUTHORIZATION CODE.", "danger")
        session["recruiting_fallback"] = True
        return redirect(url_for("recruiting"))
    try:
        identity = _discord_oauth_exchange(code)
    except Exception as exc:
        log.warning("Recruit Discord OAuth callback failed: %s", exc)
        flash("DISCORD IDENTITY VERIFICATION FAILED. PLEASE TRY AGAIN.", "danger")
        session["recruiting_fallback"] = True
        return redirect(url_for("recruiting"))
    if not identity.get("id") or not identity.get("username"):
        flash("DISCORD IDENTITY COULD NOT BE VERIFIED.", "danger")
        session["recruiting_fallback"] = True
        return redirect(url_for("recruiting"))
    existing_case_token=session.pop("recruit_oauth_case_token",None)
    if existing_case_token:
        case=fetch_one("SELECT * FROM recruiting_cases WHERE public_token=%s",(existing_case_token,))
        if not case:
            flash("THE RECRUITING CASE COULD NOT BE FOUND.","danger")
            session["recruiting_fallback"] = True
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
    recruiting_fallback = bool(session.pop("recruiting_fallback", False))
    recruit_discord_invite = CONFIG.discord_invite_url or RECRUIT_DISCORD_FALLBACK_URL
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
        game_platform = (request.form.get("game_platform") or "").strip().upper()
        game_identity = (request.form.get("game_identity") or "").strip()
        steam_id64 = game_identity if game_platform == "STEAM" else None
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
            return render_template("recruiting.html", discord_invite_url=recruit_discord_invite, form=request.form, discord_identity=identity, oauth_ready=_discord_oauth_ready(), active_members=active_members, recruiting_counts=recruiting_counts, recruit_step=recruit_step, current_date=date.today(), recruiting_fallback=recruiting_fallback)
        age_raw = (request.form.get("age") or "").strip()
        if game_platform not in {"STEAM","XBOX","PS5"}:
            flash("SELECT STEAM, XBOX, OR PLAYSTATION 5.", "danger")
            return render_template("recruiting.html", discord_invite_url=recruit_discord_invite, form=request.form, discord_identity=identity, oauth_ready=_discord_oauth_ready(), active_members=active_members, recruiting_counts=recruiting_counts, recruit_step=recruit_step, current_date=date.today(), recruiting_fallback=recruiting_fallback)
        if game_platform == "STEAM" and not (game_identity.isdigit() and len(game_identity) == 17):
            flash("STEAM PLAYERS MUST ENTER A 17-DIGIT STEAMID64.", "danger")
            return render_template("recruiting.html", discord_invite_url=recruit_discord_invite, form=request.form, discord_identity=identity, oauth_ready=_discord_oauth_ready(), active_members=active_members, recruiting_counts=recruiting_counts, recruit_step=recruit_step, current_date=date.today(), recruiting_fallback=recruiting_fallback)
        if game_platform in {"XBOX","PS5"} and len(game_identity) < 2:
            flash("ENTER YOUR CONSOLE GAMERTAG / ONLINE ID EXACTLY AS IT APPEARS IN-GAME.", "danger")
            return render_template("recruiting.html", discord_invite_url=recruit_discord_invite, form=request.form, discord_identity=identity, oauth_ready=_discord_oauth_ready(), active_members=active_members, recruiting_counts=recruiting_counts, recruit_step=recruit_step, current_date=date.today(), recruiting_fallback=recruiting_fallback)
        if not all([timezone_name, game_identity, hll_experience, role_interest, looking_for, play_style, follows_chain, participation]):
            flash("COMPLETE ALL REQUIRED APPLICATION FIELDS BEFORE SUBMITTING.", "danger")
            return render_template("recruiting.html", discord_invite_url=recruit_discord_invite, form=request.form, discord_identity=identity, oauth_ready=_discord_oauth_ready(), active_members=active_members, recruiting_counts=recruiting_counts, recruit_step=recruit_step, current_date=date.today(), recruiting_fallback=recruiting_fallback)
        try:
            age = int(age_raw) if age_raw else None
        except ValueError:
            flash("AGE MUST BE A NUMBER OR LEFT BLANK.", "danger")
            return render_template("recruiting.html", discord_invite_url=recruit_discord_invite, form=request.form, discord_identity=identity, oauth_ready=_discord_oauth_ready(), active_members=active_members, recruiting_counts=recruiting_counts, recruit_step=recruit_step, current_date=date.today(), recruiting_fallback=recruiting_fallback)
        discord_user_id = int(identity["id"])
        duplicate = fetch_one("""SELECT case_number,public_token,status FROM recruiting_cases
                                 WHERE discord_user_id=%s
                                   AND status NOT IN ('DENIED','CLOSED','ENLISTED')
                                 ORDER BY created_at DESC LIMIT 1""", (discord_user_id,))
        if duplicate:
            flash(f"AN ACTIVE APPLICATION IS ALREADY ON FILE: {duplicate['case_number']}", "warning")
            return redirect(url_for("recruiting_status", token=duplicate["public_token"]))
        identity_duplicate=fetch_one("""SELECT case_number,public_token FROM recruiting_cases
                                      WHERE UPPER(COALESCE(game_platform,''))=%s
                                        AND LOWER(COALESCE(game_identity,steam_id64,''))=LOWER(%s)
                                        AND status NOT IN ('DENIED','CLOSED','ENLISTED')
                                      ORDER BY created_at DESC LIMIT 1""",(game_platform,game_identity))
        if identity_duplicate:
            flash(f"THAT GAME ACCOUNT IS ALREADY ATTACHED TO ACTIVE APPLICATION {identity_duplicate['case_number']}.","danger")
            return render_template("recruiting.html", discord_invite_url=recruit_discord_invite, form=request.form, discord_identity=identity, oauth_ready=_discord_oauth_ready(), active_members=active_members, recruiting_counts=recruiting_counts, recruit_step=recruit_step, current_date=date.today(), recruiting_fallback=recruiting_fallback)
        case_number = _recruit_case_number()
        public_token = secrets.token_urlsafe(24)
        verified_name = identity.get("global_name") or identity.get("username")
        oauth_expires_at=datetime.now(timezone.utc)+timedelta(seconds=max(60,int(identity.get("expires_in") or 604800)))
        try:
            execute("""INSERT INTO recruiting_cases
                       (case_number,public_token,discord_username_input,discord_user_id,discord_verified_username,
                        discord_avatar_hash,discord_oauth_linked_at,discord_oauth_access_token_enc,discord_oauth_refresh_token_enc,
                        discord_oauth_expires_at,discord_oauth_scope,age,timezone_name,steam_id64,game_platform,game_identity,hll_experience,role_interest,
                        looking_for,play_style,follows_chain,participation,applicant_notes,recruited_by_personnel_id,status)
                       VALUES(%s,%s,%s,%s,%s,%s,NOW(),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING_COMMAND')""",
                    (case_number,public_token,identity.get("username"),discord_user_id,verified_name,identity.get("avatar") or None,
                     identity.get("access_token_enc"),identity.get("refresh_token_enc"),oauth_expires_at,identity.get("scope"),
                     age,timezone_name,steam_id64,game_platform,game_identity,hll_experience,role_interest,looking_for,play_style,follows_chain=='YES',participation,applicant_notes,recruited_by_personnel_id))
        except Exception:
            ref=secrets.token_hex(4).upper()
            log.exception("RECRUITING APPLICATION FILING FAILED [%s] discord_user_id=%s platform=%s",ref,discord_user_id,game_platform)
            flash(f"YOUR APPLICATION COULD NOT BE FILED. YOUR ENTRIES HAVE BEEN PRESERVED. PLEASE TRY AGAIN. REFERENCE: {ref}", "danger")
            return render_template("recruiting.html", discord_invite_url=recruit_discord_invite, form=request.form,
                                   discord_identity=identity, oauth_ready=_discord_oauth_ready(), active_members=active_members,
                                   recruiting_counts=recruiting_counts, recruit_step=recruit_step, current_date=date.today(), recruiting_fallback=True), 503
        session['recruiting_case_token'] = public_token
        session.pop("recruit_discord_identity", None)
        flash(f"APPLICATION {case_number} FILED WITH BATTALION HEADQUARTERS.", "success")
        return redirect(url_for("recruiting_status", token=public_token))
    return render_template("recruiting.html", discord_invite_url=recruit_discord_invite, form={}, discord_identity=identity, oauth_ready=_discord_oauth_ready(), active_members=active_members, recruiting_counts=recruiting_counts, recruit_step=recruit_step, current_date=date.today(), recruiting_fallback=recruiting_fallback)


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
    return render_template("recruiting_status.html", case=case, discord_invite_url=(CONFIG.discord_invite_url or RECRUIT_DISCORD_FALLBACK_URL))


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
    duplicate = fetch_one(
        """SELECT id,status FROM personnel_recommendations
           WHERE personnel_id=%s AND recommendation_type='AWARD'
             AND UPPER(COALESCE(recommended_action,''))=UPPER(%s)
             AND UPPER(COALESCE(status,'')) NOT IN ('APPROVED','DENIED','CANCELLED','CANCELED','CLOSED','COMPLETE','COMPLETED')
           ORDER BY created_at DESC LIMIT 1""",
        (personnel_id, award_name),
    )
    if duplicate:
        flash(f"AWARD RECOMMENDATION NOT DUPLICATED — {award_name.upper()} IS ALREADY PENDING ({str(duplicate.get('status') or 'OPEN').replace('_',' ')}).", "warning")
        return redirect(request.referrer or url_for("my_soldier_record"))
    execute(
        """INSERT INTO personnel_recommendations
           (personnel_id,recommendation_type,recommended_action,justification,recommending_personnel_id,status)
           VALUES(%s,'AWARD',%s,%s,%s,'PENDING_S1')""",
        (personnel_id, award_name, justification, recommender.get("id") if recommender else None),
    )
    flash("AWARD RECOMMENDATION FORWARDED TO S-1 PERSONNEL FOR ADMINISTRATIVE REVIEW.", "success")
    return redirect(request.referrer or url_for("my_soldier_record"))




_FIELD_BILLET_ALIASES={
    'PLATOON SERGEANT':'PSG',
    'SQUAD LEADER':'SL',
    'ASSISTANT SQUAD LEADER':'ASST_SL',
    'TEAM LEADER':'FTL',
    'FIRE TEAM LEADER':'FTL',
}

def _formation_node_for_billet(person, appointment_code):
    """Resolve the billet formation, including older records that only stored text labels."""
    if not person:
        return None
    wanted='Platoon' if appointment_code=='PSG' else 'Squad'
    if person.get('unit_node_id'):
        ancestry=unit_ancestry(person['unit_node_id'])
        found=next((n for n in ancestry if str(n.get('unit_type') or '').lower()==wanted.lower()),None)
        if found:
            return found
    unit=str(person.get('unit_code') or '').upper()
    company_letter=''
    m=re.search(r'\b([ABC])\b',unit)
    if m: company_letter=m.group(1)
    if wanted=='Squad' and company_letter and person.get('platoon') and person.get('squad'):
        found=fetch_one("""SELECT s.* FROM unit_nodes s
                            JOIN unit_nodes pl ON pl.id=s.parent_id
                            JOIN unit_nodes co ON co.id=pl.parent_id
                            WHERE s.unit_type='Squad' AND s.is_active=TRUE
                              AND UPPER(s.display_name)=UPPER(%s)
                              AND UPPER(pl.display_name)=UPPER(%s)
                              AND co.unit_code=%s LIMIT 1""",
                        (person.get('squad'),person.get('platoon'),f'{company_letter}-1-5'))
        if found:
            execute("UPDATE personnel SET unit_node_id=%s,updated_at=NOW() WHERE id=%s",(found['id'],person['id']))
            execute("UPDATE assignment_history SET unit_node_id=%s WHERE personnel_id=%s AND is_current=TRUE",(found['id'],person['id']))
            person['unit_node_id']=found['id']
            return found
    if wanted=='Platoon' and company_letter and person.get('platoon'):
        found=fetch_one("""SELECT pl.* FROM unit_nodes pl JOIN unit_nodes co ON co.id=pl.parent_id
                            WHERE pl.unit_type='Platoon' AND pl.is_active=TRUE
                              AND UPPER(pl.display_name)=UPPER(%s) AND co.unit_code=%s LIMIT 1""",
                        (person.get('platoon'),f'{company_letter}-1-5'))
        if found:
            return found
    return None

def reconcile_formation_billets():
    """One-time-safe migration of clear legacy duty positions into authoritative billets."""
    people=fetch_all("""SELECT * FROM personnel WHERE separated_at IS NULL AND COALESCE(archived,FALSE)=FALSE""") or []
    for person in people:
        duty=re.sub(r'[^A-Z0-9 ]+',' ',str(person.get('duty_position') or '').upper()).strip()
        code=_FIELD_BILLET_ALIASES.get(duty)
        # resolve prior exceptions each pass; re-open only if still ambiguous
        execute("UPDATE formation_migration_exceptions SET is_active=FALSE,resolved_at=NOW() WHERE personnel_id=%s AND is_active=TRUE",(person['id'],))
        if not code:
            continue
        node=_formation_node_for_billet(person,code)
        if not node:
            execute("""INSERT INTO formation_migration_exceptions(personnel_id,exception_code,detail,is_active,detected_at,resolved_at)
                       VALUES(%s,'MISSING_FORMATION',%s,TRUE,NOW(),NULL)
                       ON CONFLICT(personnel_id,exception_code) DO UPDATE SET detail=EXCLUDED.detail,is_active=TRUE,detected_at=NOW(),resolved_at=NULL""",
                    (person['id'],f"{person.get('rank_code') or ''} {person.get('last_name') or ''} is marked {person.get('duty_position') or code} but has no compatible structured formation assignment."))
            continue
        # Always normalize to a string. Non-FTL billets intentionally have no
        # fire-team value, but the normalization below must never call string
        # methods on None during startup reconciliation.
        team=(person.get('fire_team') or '').upper().strip() if code=='FTL' else ''
        if code=='FTL' and team not in {'ALPHA','BRAVO','ALPHA TEAM','BRAVO TEAM'}:
            execute("""INSERT INTO formation_migration_exceptions(personnel_id,exception_code,detail,is_active,detected_at,resolved_at)
                       VALUES(%s,'TEAM_LEADER_WITHOUT_TEAM',%s,TRUE,NOW(),NULL)
                       ON CONFLICT(personnel_id,exception_code) DO UPDATE SET detail=EXCLUDED.detail,is_active=TRUE,detected_at=NOW(),resolved_at=NULL""",
                    (person['id'],f"{person.get('rank_code') or ''} {person.get('last_name') or ''} is a Team Leader but Alpha/Bravo Team is not defined."))
            continue
        team='ALPHA TEAM' if team.startswith('ALPHA') else ('BRAVO TEAM' if team.startswith('BRAVO') else None)
        existing=fetch_one("""SELECT id,unit_node_id,fire_team FROM personnel_appointments WHERE personnel_id=%s AND appointment_code=%s AND is_current=TRUE""",(person['id'],code))
        if existing:
            if str(existing.get('unit_node_id') or '')!=str(node['id']) or str(existing.get('fire_team') or '').upper()!=str(team or '').upper():
                execute("UPDATE personnel_appointments SET unit_node_id=%s,fire_team=%s,organization=%s,remarks=COALESCE(remarks,'') || ' | RECONCILED TO STRUCTURED FORMATION' WHERE id=%s",
                        (node['id'],team,format_assignment_node(node['id']),existing['id']))
        else:
            appt_name=fetch_one("SELECT appointment_name FROM appointment_catalog WHERE appointment_code=%s",(code,)) or {'appointment_name':code}
            execute("""INSERT INTO personnel_appointments(personnel_id,appointment_code,unit_node_id,fire_team,organization,appointment_status,effective_date,authority,remarks,is_current)
                       VALUES(%s,%s,%s,%s,%s,'PERMANENT',CURRENT_DATE,'SYSTEM MIGRATION','AUTO-MIGRATED FROM CURRENT DUTY POSITION',TRUE)""",
                    (person['id'],code,node['id'],team,format_assignment_node(node['id'])))
            log.info("Formation billet migrated: %s -> %s / %s",person['id'],appt_name.get('appointment_name'),node.get('display_name'))

def formation_control_snapshot(squad_id):
    squad=unit_node(squad_id)
    if not squad or str(squad.get('unit_type') or '').lower()!='squad':
        return None
    ancestry=unit_ancestry(squad_id)
    platoon=next((n for n in ancestry if str(n.get('unit_type') or '').lower()=='platoon'),None)
    company=next((n for n in ancestry if str(n.get('unit_type') or '').lower()=='company'),None)
    members=fetch_all("""SELECT p.*,COALESCE(rc.precedence,0) rank_precedence
                         FROM personnel p LEFT JOIN rank_catalog rc ON rc.rank_code=p.rank_code
                         WHERE p.unit_node_id=%s AND p.separated_at IS NULL AND COALESCE(p.archived,FALSE)=FALSE
                         ORDER BY COALESCE(rc.precedence,0) DESC,p.last_name,p.first_name""",(squad_id,)) or []
    appts=fetch_all("""SELECT pa.*,p.rank_code,p.first_name,p.last_name FROM personnel_appointments pa JOIN personnel p ON p.id=pa.personnel_id
                       WHERE pa.unit_node_id=%s AND pa.is_current=TRUE AND pa.appointment_code IN ('SL','ASST_SL','FTL')
                       ORDER BY pa.appointment_code,pa.fire_team,p.last_name""",(squad_id,)) or []
    bykey={}
    for a in appts:
        key=a['appointment_code']
        if key=='FTL': key=f"FTL_{str(a.get('fire_team') or '').upper().replace(' TEAM','')}"
        bykey[key]=a
    return {'squad':squad,'platoon':platoon,'company':company,'members':members,'appointments':bykey}

def reconcile_all_mos_proficiency():
    """Repair legacy/manual MOS grades from the authoritative role_seconds ledger."""
    ready=fetch_one("SELECT to_regclass('public.hll_player_match_stats') AS stats_table") or {}
    if not ready.get('stats_table'):
        return {'reconciled':0,'skipped':True}
    people=fetch_all("SELECT * FROM personnel WHERE separated_at IS NULL AND COALESCE(archived,FALSE)=FALSE AND COALESCE(mos_code,'')<>'' AND UPPER(COALESCE(mos_code,'')) NOT IN ('00R','00','PENDING','UNASSIGNED')") or []
    count=0
    for person in people:
        try:
            if sync_mos_proficiency(person):
                count+=1
        except Exception:
            log.exception('MOS proficiency reconcile failed for %s',person.get('id'))
    return {'reconciled':count,'skipped':False}


# Automatic billet and MOS reconciliation after all helpers are available.
if database_ready():
    try:
        reconcile_formation_billets()
    except Exception:
        log.exception('Automatic formation billet reconciliation failed')
    try:
        reconcile_all_mos_proficiency()
    except Exception:
        log.exception('Automatic MOS proficiency reconciliation failed')

@app.route('/staff/formation-control',methods=['GET','POST'])
@login_required
def staff_formation_control():
    role=session.get('access_role')
    if role not in {'s1','battalion_hq','commander','admin'}: abort(403)
    authority=session.get('display_name') or session.get('username') or role.upper()
    squads=fetch_all("""SELECT s.*,p.display_name platoon_name,c.display_name company_name
                         FROM unit_nodes s
                         JOIN unit_nodes p ON p.id=s.parent_id
                         JOIN unit_nodes c ON c.id=p.parent_id
                         WHERE s.unit_type='Squad' AND s.is_active=TRUE AND p.is_active=TRUE AND c.is_active=TRUE
                         ORDER BY c.sort_order,p.sort_order,s.sort_order""") or []
    squad_id=request.values.get('squad_id') or (str(squads[0]['id']) if squads else None)
    if request.method=='POST':
        action=(request.form.get('action') or '').upper()
        if action=='RECONCILE':
            reconcile_formation_billets(); flash('FORMATION BILLET RECONCILIATION COMPLETE. REVIEW EXCEPTIONS BELOW.','success')
            return redirect(url_for('staff_formation_control',squad_id=squad_id))
        if action=='ASSIGN_SLOT':
            snap=formation_control_snapshot(squad_id)
            if not snap: abort(400)
            person_id=request.form.get('personnel_id') or ''
            slot=(request.form.get('slot_code') or '').upper()
            person=fetch_one("SELECT * FROM personnel WHERE id=%s AND separated_at IS NULL",(person_id,)) if person_id else None
            if not person: abort(400)
            gate=onboarding_assignment_gate(person_id)
            if not gate.get('allowed'):
                flash(gate.get('reason') or 'WELCOME PACKET MUST BE ACCEPTED BEFORE FORMATION ASSIGNMENT.','warning')
                return redirect(url_for('staff_formation_control',squad_id=squad_id))
            mapping={
                'SL':('SL',None,'Squad Leader'),
                'ASST_SL':('ASST_SL',None,'Assistant Squad Leader'),
                'FTL_ALPHA':('FTL','ALPHA TEAM','Team Leader'),
                'FTL_BRAVO':('FTL','BRAVO TEAM','Team Leader'),
                'ALPHA_RIFLEMAN':(None,'ALPHA TEAM','Rifleman'),
                'ALPHA_GRENADIER':(None,'ALPHA TEAM','Grenadier'),
                'ALPHA_AUTOMATIC_RIFLEMAN':(None,'ALPHA TEAM','Automatic Rifleman'),
                'BRAVO_RIFLEMAN':(None,'BRAVO TEAM','Rifleman'),
                'BRAVO_GRENADIER':(None,'BRAVO TEAM','Grenadier'),
                'BRAVO_AUTOMATIC_RIFLEMAN':(None,'BRAVO TEAM','Automatic Rifleman'),
            }
            if slot not in mapping: abort(400)
            appt_code,team,duty=mapping[slot]
            # Relieve the current occupant of a singleton leadership slot.
            if appt_code:
                rows=fetch_all("""SELECT id,personnel_id FROM personnel_appointments WHERE unit_node_id=%s AND appointment_code=%s AND is_current=TRUE""",(squad_id,appt_code)) or []
                for row in rows:
                    if appt_code!='FTL' or str((fetch_one('SELECT fire_team FROM personnel_appointments WHERE id=%s',(row['id'],)) or {}).get('fire_team') or '').upper()==str(team or '').upper():
                        if str(row['personnel_id'])!=str(person_id): relieve_appointment(row['id'],date.today(),authority,None,'Reassigned through Formation Assignment Control.')
            process_assignment_action(person_id,squad_id,duty,date.today(),authority,None,'Formation Assignment Control',fire_team=team)
            if appt_code:
                current=fetch_all("SELECT id,appointment_code,fire_team FROM personnel_appointments WHERE personnel_id=%s AND is_current=TRUE AND appointment_code IN ('SL','ASST_SL','FTL')",(person_id,)) or []
                for row in current:
                    if row['appointment_code']!=appt_code or (appt_code=='FTL' and str(row.get('fire_team') or '').upper()!=str(team or '').upper()):
                        relieve_appointment(row['id'],date.today(),authority,None,'Reassigned through Formation Assignment Control.')
                already=fetch_one("SELECT id FROM personnel_appointments WHERE personnel_id=%s AND appointment_code=%s AND unit_node_id=%s AND is_current=TRUE AND COALESCE(fire_team,'')=COALESCE(%s,'')",(person_id,appt_code,squad_id,team))
                if not already: process_appointment_action(person_id,appt_code,None,'PERMANENT',date.today(),authority,None,'Formation Assignment Control',squad_id,fire_team=team)
            enqueue_discord_role_sync(person_id,'FORMATION ASSIGNMENT CONTROL')
            flash('FORMATION SLOT ASSIGNED. WEBSITE RECORD AND DISCORD ROLE MIRROR QUEUED.','success')
            return redirect(url_for('staff_formation_control',squad_id=squad_id))
    snapshot=formation_control_snapshot(squad_id) if squad_id else None
    candidates=fetch_all("""SELECT p.*,COALESCE(rc.precedence,0) rank_precedence FROM personnel p LEFT JOIN rank_catalog rc ON rc.rank_code=p.rank_code
                           WHERE p.separated_at IS NULL AND COALESCE(p.archived,FALSE)=FALSE AND COALESCE(p.field_status,'')<>'Replacement'
                           ORDER BY COALESCE(rc.precedence,0) DESC,p.last_name,p.first_name""") or []
    exceptions=fetch_all("""SELECT e.*,p.rank_code,p.first_name,p.last_name FROM formation_migration_exceptions e JOIN personnel p ON p.id=e.personnel_id
                             WHERE e.is_active=TRUE ORDER BY e.detected_at DESC,p.last_name""") or []
    return render_template('staff_formation_control.html',squads=squads,snapshot=snapshot,candidates=candidates,exceptions=exceptions)


def validate_appointment_compatibility(person, appointment, unit_id=None, fire_team=None, allow_override=False):
    """Block structurally impossible field-leadership choices before they reach history/Discord.

    Command can explicitly override rank guidance, but formation/echelon requirements remain
    hard constraints so a Squad Leader cannot silently exist without a Squad, etc.
    """
    if not appointment:
        return True, None
    code=str(appointment.get('appointment_code') or '').upper()
    node=unit_node(unit_id) if unit_id else None
    node_type=str((node or {}).get('unit_type') or '').upper()
    required={'PSG':'PLATOON','SL':'SQUAD','ASST_SL':'SQUAD','FTL':'SQUAD'}
    if code in required:
        if not node:
            return False, f"{code} REQUIRES A STRUCTURED {required[code]} ASSIGNMENT."
        if node_type!=required[code]:
            return False, f"{code} MUST BE ATTACHED TO A {required[code]}, NOT {node_type or 'AN UNSPECIFIED FORMATION'}."
    if code=='FTL':
        team=str(fire_team or person.get('fire_team') or '').upper().replace(' TEAM','').strip()
        if team not in {'ALPHA','BRAVO'}:
            return False, "TEAM LEADER REQUIRES ALPHA OR BRAVO FIRE TEAM."
    suggested=str(appointment.get('suggested_rank') or '').upper().strip()
    current=str(person.get('rank_code') or '').upper().strip()
    if suggested and current and current!=suggested and not allow_override:
        return False, f"{code} IS NORMALLY AUTHORIZED FOR {suggested}; {current} REQUIRES COMMAND OVERRIDE."
    return True, None



def _assignment_wizard_data(personnel_id=None):
    """Build the single-screen Command assignment workflow from authoritative unit/personnel data."""
    nodes=fetch_all("""SELECT id,parent_id,unit_code,display_name,unit_type,sort_order
                       FROM unit_nodes WHERE is_active=TRUE
                       ORDER BY sort_order,display_name""")
    by_id={str(n['id']):n for n in nodes}
    companies=[n for n in nodes if str(n.get('unit_type') or '').upper()=='COMPANY']
    platoons=[n for n in nodes if str(n.get('unit_type') or '').upper()=='PLATOON']
    squads=[n for n in nodes if str(n.get('unit_type') or '').upper()=='SQUAD']
    # Only active, non-applicant Soldier records are valid assignment targets.
    people=fetch_all("""SELECT id,rank_code,first_name,last_name,service_number,unit_node_id,unit_code,platoon,squad,
                               fire_team,duty_position,mos_code,field_status,duty_status
                        FROM personnel
                        WHERE archived=FALSE AND separated_at IS NULL
                          AND COALESCE(lifecycle_state,'') NOT IN ('APPLICANT','PROSPECT')
                        ORDER BY last_name,first_name""")
    person=next((x for x in people if personnel_id and str(x['id'])==str(personnel_id)),None)
    # Current strength is deliberately simple and transparent: active Soldiers assigned to each exact node.
    strength_rows=fetch_all("""SELECT unit_node_id,COUNT(*)::int AS total
                               FROM personnel
                               WHERE archived=FALSE AND separated_at IS NULL AND unit_node_id IS NOT NULL
                               GROUP BY unit_node_id""")
    strengths={str(r['unit_node_id']):int(r.get('total') or 0) for r in strength_rows}
    team_rows=fetch_all("""SELECT unit_node_id,UPPER(COALESCE(fire_team,'')) AS fire_team,COUNT(*)::int AS total
                           FROM personnel
                           WHERE archived=FALSE AND separated_at IS NULL AND unit_node_id IS NOT NULL
                           GROUP BY unit_node_id,UPPER(COALESCE(fire_team,''))""")
    teams={}
    for r in team_rows:
        teams[(str(r['unit_node_id']),str(r.get('fire_team') or ''))]=int(r.get('total') or 0)
    # Recommend the least-populated active squad, then the lighter Alpha/Bravo team inside it.
    recommendation=None
    candidates=[]
    for squad in squads:
        platoon=by_id.get(str(squad.get('parent_id')))
        company=by_id.get(str((platoon or {}).get('parent_id')))
        if not platoon or not company: continue
        score=strengths.get(str(squad['id']),0)
        candidates.append((score,int(company.get('sort_order') or 0),int(platoon.get('sort_order') or 0),int(squad.get('sort_order') or 0),company,platoon,squad))
    if candidates:
        _,_,_,_,company,platoon,squad=sorted(candidates,key=lambda x:x[:4])[0]
        alpha=teams.get((str(squad['id']),'ALPHA TEAM'),0)
        bravo=teams.get((str(squad['id']),'BRAVO TEAM'),0)
        recommendation={'company':company,'platoon':platoon,'squad':squad,
                        'fire_team':'ALPHA TEAM' if alpha<=bravo else 'BRAVO TEAM',
                        'strength':strengths.get(str(squad['id']),0),'alpha':alpha,'bravo':bravo}
    duty_positions=personnel_form_catalogs()['duty_positions']
    return {'person':person,'people':people,'nodes':nodes,'companies':companies,'platoons':platoons,'squads':squads,
            'strengths':strengths,'recommendation':recommendation,'duty_positions':duty_positions}


@app.route('/staff/assign',methods=['GET','POST'])
@login_required
def staff_assign_soldier():
    role=session.get('access_role')
    if role not in {'s1','battalion_hq','commander','admin'}: abort(403)
    personnel_id=(request.values.get('personnel_id') or '').strip() or None
    data=_assignment_wizard_data(personnel_id)
    if request.method=='POST':
        if not data.get('person'):
            flash('SELECT A SOLDIER BEFORE FILING AN ASSIGNMENT.','danger')
            return redirect(url_for('staff_assign_soldier'))
        person=data['person']
        gate=onboarding_assignment_gate(person['id'])
        if not gate.get('allowed'):
            flash(gate.get('reason') or 'WELCOME PACKET MUST BE ACCEPTED BEFORE PERMANENT ASSIGNMENT.','warning')
            return redirect(url_for('staff_assign_soldier',personnel_id=person['id']))
        company_id=(request.form.get('company_id') or '').strip()
        platoon_id=(request.form.get('platoon_id') or '').strip()
        squad_id=(request.form.get('squad_id') or '').strip()
        fire_team=(request.form.get('fire_team') or '').upper().strip()
        if not squad_id:
            fire_team=''
        duty=(request.form.get('duty_position') or '').strip() or person.get('duty_position') or 'Rifleman'
        remarks=(request.form.get('remarks') or '').strip() or 'Filed through Command Assign Soldier workflow.'
        # New replacements must reach at least platoon level. Squad is preferred when selected.
        target_id=squad_id or platoon_id
        if not company_id or not platoon_id or not target_id:
            flash('SELECT A COMPANY AND PLATOON. SQUAD IS OPTIONAL, BUT RECOMMENDED.','danger')
            return redirect(url_for('staff_assign_soldier',personnel_id=person['id']))
        company=unit_node(company_id); platoon=unit_node(platoon_id); target=unit_node(target_id)
        if not company or str(company.get('unit_type') or '').upper()!='COMPANY': abort(400)
        if not platoon or str(platoon.get('unit_type') or '').upper()!='PLATOON' or str(platoon.get('parent_id'))!=str(company_id): abort(400)
        if squad_id:
            if not target or str(target.get('unit_type') or '').upper()!='SQUAD' or str(target.get('parent_id'))!=str(platoon_id): abort(400)
        if fire_team not in {'','ALPHA TEAM','BRAVO TEAM'}: abort(400)
        authority=session.get('display_name') or session.get('username') or role.upper()
        try:
            result=process_assignment_action(person['id'],target_id,duty,date.today(),authority,None,remarks,fire_team=fire_team or None)
            staff_log('S-1','GUIDED ASSIGNMENT',f"{person.get('rank_code','')} {person.get('last_name','')} assigned to {result.get('assignment') or format_assignment_node(target_id)}",authority,person['id'],details={'company_id':company_id,'platoon_id':platoon_id,'squad_id':squad_id or None,'fire_team':fire_team or None,'duty_position':duty})
            flash(f"ASSIGNMENT FILED — {result.get('assignment') or format_assignment_node(target_id)}. ORDERS FILED AND DISCORD SYNC QUEUED.",'success')
            return redirect(url_for('staff_assign_soldier',personnel_id=person['id'],complete='1'))
        except Exception as exc:
            log.exception('Guided assignment failed for %s',person['id'])
            flash(str(exc) or 'ASSIGNMENT COULD NOT BE FILED.','danger')
            return redirect(url_for('staff_assign_soldier',personnel_id=person['id']))
    return render_template('staff_assign_soldier.html',**data,complete=request.args.get('complete')=='1')

@app.route('/staff/personnel/<personnel_id>/manage',methods=['GET','POST'])
@login_required
def staff_personnel_manage(personnel_id):
    role=session.get('access_role')
    if role not in {'s1','s2','s3','s4','training','battalion_hq','commander','admin'}: abort(403)
    person=fetch_one('SELECT * FROM personnel WHERE id=%s AND separated_at IS NULL',(personnel_id,))
    if not person: abort(404)
    onboarding_gate=onboarding_assignment_gate(personnel_id)
    catalogs=personnel_form_catalogs(); authority=session.get('display_name') or session.get('username') or role.upper(); today=date.today()
    awards=fetch_all("SELECT ribbon_code,ribbon_name FROM ribbon_catalog WHERE is_active=TRUE ORDER BY sort_order,ribbon_name")
    operations_list=fetch_all("SELECT id,operation_number,title,start_at,status FROM operations ORDER BY COALESCE(start_at,created_at) DESC LIMIT 100")
    qual_types=fetch_all("SELECT id,code,display_name,battlefield_unit AS category FROM duty_qualification_types WHERE is_active=TRUE ORDER BY battlefield_unit,sort_order,display_name")
    training_programs=fetch_all("SELECT program_code,program_name,'TRAINING' AS category FROM training_program_catalog WHERE is_active=TRUE ORDER BY sort_order,program_name")
    current_weapon=current_weapon_for(person)
    evidence=award_recommendation_evidence(personnel_id)
    promotion_packet=promotion_board_packet(personnel_id)
    situation=current_situation_snapshot(person)
    current_leadership=fetch_one("""SELECT pa.appointment_code,ac.appointment_name FROM personnel_appointments pa JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code WHERE pa.personnel_id=%s AND pa.is_current=TRUE AND pa.appointment_code IN ('PSG','SL','ASST_SL','FTL') ORDER BY pa.effective_date DESC LIMIT 1""",(personnel_id,))
    action_permissions={
        'PERSONNEL_CONTROL': {'s1','battalion_hq','commander','admin'},
        'AWARD': {'s1','battalion_hq','commander','admin'},
        'FIELD_CITATION': {'s1','s3','battalion_hq','commander','admin'},
        'QUALIFICATION': {'s3','training','battalion_hq','commander','admin'},
        'TRAINING': {'s3','training','battalion_hq','commander','admin'},
        'LEAVE': {'s1','battalion_hq','commander','admin'},
        'WEAPON': {'s4','battalion_hq','commander','admin'},
        'COMMAND_REMARK': {'s1','s3','s4','battalion_hq','commander','admin'},
        'WATCHLIST': {'battalion_hq','commander','admin'},
    }
    allowed_actions=[k for k,v in action_permissions.items() if role in v]
    requested_action=((request.args.get('action') if request.method=='GET' else request.form.get('action')) or '').upper()
    initial_action=requested_action if requested_action in allowed_actions else (allowed_actions[0] if allowed_actions else '')
    if request.method=='POST':
        action=(request.form.get('action') or '').upper()
        eff_raw=request.form.get('effective_date') or today.isoformat()
        eff=date.fromisoformat(str(eff_raw)[:10]) if not isinstance(eff_raw,date) else eff_raw
        remarks=(request.form.get('remarks') or '').strip() or None
        if action=='PERSONNEL_CONTROL':
            if role not in {'s1','battalion_hq','commander','admin'}: abort(403)
            rank=validate_system_choice((request.form.get('rank_code') or '').upper(),catalogs['ranks'],'rank_code')
            node=validate_system_choice(request.form.get('unit_node_id'),catalogs['assignment_options'],'id')
            duty=validate_system_choice(request.form.get('duty_position'),catalogs['duty_positions'],'value')
            mos=validate_system_choice((request.form.get('mos_code') or '').upper(),catalogs['mos_catalog'],'mos_code')
            fire_team=(request.form.get('fire_team') or '').upper().strip()
            valid_teams={x['value'] for x in catalogs['fire_teams']}
            if fire_team not in valid_teams: abort(400)
            duty_status=(request.form.get('duty_status') or 'PRESENT FOR DUTY').upper().strip()
            valid_status={x['value'] for x in catalogs['duty_statuses']}
            if duty_status not in valid_status: abort(400)
            appointment_code=(request.form.get('appointment_code') or '').upper().strip()
            if appointment_code and appointment_code!='NONE':
                appt=validate_system_choice(appointment_code,catalogs['appointment_catalog'],'appointment_code')
            else:
                appt=None
            if not rank or not node or not duty or not mos:
                flash('RANK, ASSIGNMENT, MOS, AND DUTY POSITION ARE REQUIRED.','danger')
            else:
                current=fetch_one('SELECT * FROM personnel WHERE id=%s',(personnel_id,)) or person
                assignment_changed=(str(current.get('unit_node_id') or '')!=str(node['id']) or
                                    (current.get('duty_position') or '')!=duty['value'] or
                                    (current.get('fire_team') or '').upper()!=fire_team)
                if assignment_changed and not onboarding_gate.get('allowed'):
                    flash(onboarding_gate.get('reason') or 'WELCOME PACKET MUST BE ACCEPTED BEFORE PERMANENT ASSIGNMENT.','warning')
                    return redirect(url_for('staff_personnel_manage',personnel_id=personnel_id,action='PERSONNEL_CONTROL'))
                changes=[]
                if (current.get('rank_code') or '').upper()!=rank['rank_code']:
                    process_rank_action(personnel_id,rank['rank_code'],eff,authority,None,remarks); changes.append(f"Rank → {rank['rank_code']}")
                if (current.get('mos_code') or '').upper()!=mos['mos_code']:
                    file_primary_mos_change(personnel_id,mos['mos_code'],eff,authority,remarks); changes.append(f"MOS → {mos['mos_code']}")
                assignment_result=None
                if assignment_changed:
                    assignment_result=process_assignment_action(personnel_id,node['id'],duty['value'],eff,authority,None,remarks,fire_team=fire_team or None); changes.append('Formation / billet updated')
                else:
                    assignment_result=ensure_assignment_artifacts(personnel_id,authority,None,remarks)
                    if assignment_result.get('repaired'):
                        changes.append('Assignment paperwork repaired')
                if assignment_result and assignment_result.get('document') and (assignment_changed or assignment_result.get('repaired')):
                    order_no=assignment_result['document'].get('document_number')
                    if order_no:
                        changes.append(f'Orders {order_no}')
                if (current.get('duty_status') or 'PRESENT FOR DUTY').upper()!=duty_status:
                    execute("UPDATE personnel SET duty_status=%s,updated_at=NOW() WHERE id=%s",(duty_status,personnel_id))
                    write_service_entry(personnel_id,'STATUS','DUTY STATUS CHANGED',f'Duty status changed to {duty_status}.',authority,None,eff); changes.append(f"Duty status → {duty_status}")
                managed_codes={'PSG','SL','ASST_SL','FTL'}
                current_managed=fetch_all("SELECT id,appointment_code FROM personnel_appointments WHERE personnel_id=%s AND is_current=TRUE AND appointment_code=ANY(%s)",(personnel_id,list(managed_codes)))
                desired_code=appt['appointment_code'] if appt and appt['appointment_code'] in managed_codes else None
                for row in current_managed:
                    if row['appointment_code']!=desired_code:
                        relieve_appointment(row['id'],eff,authority,None,remarks); changes.append(f"Relieved {row['appointment_code']}")
                if desired_code and not any(row['appointment_code']==desired_code for row in current_managed):
                    process_appointment_action(personnel_id,desired_code,None,'PERMANENT',eff,authority,None,remarks,node['id'],fire_team=fire_team or None); changes.append(f"Appointment → {desired_code}")
                if changes:
                    enqueue_discord_role_sync(personnel_id,'STAFF PERSONNEL CONTROL — AUTHORITATIVE SYNC')
                    staff_log('S-1','PERSONNEL CONTROL',f"{rank['rank_code']} {current.get('last_name','')} — " + '; '.join(changes),authority,personnel_id,details={'unit_node_id':str(node['id']),'fire_team':fire_team or None,'duty_position':duty['value'],'mos_code':mos['mos_code'],'rank_code':rank['rank_code'],'appointment_code':desired_code,'duty_status':duty_status,'changes':changes})
                    flash('PERSONNEL CONTROL FILED. WEBSITE UPDATED AND DISCORD ROLE MIRROR QUEUED.','success')
                else:
                    flash('NO PERSONNEL CHANGES WERE DETECTED. NOTHING WAS FILED OR QUEUED.','warning')
        elif action=='ASSIGNMENT':
            if role not in {'s1','battalion_hq','commander','admin'}: abort(403)
            node=validate_system_choice(request.form.get('unit_node_id'),catalogs['assignment_options'],'id'); duty=validate_system_choice(request.form.get('duty_position'),catalogs['duty_positions'],'value'); mos=validate_system_choice((request.form.get('mos_code') or '').upper(),catalogs['mos_catalog'],'mos_code')
            if not node or not duty or not mos: flash('SELECT AN ASSIGNMENT, MOS, AND DUTY POSITION FROM THE AUTHORIZED LISTS.','danger')
            elif not onboarding_gate.get('allowed'):
                flash(onboarding_gate.get('reason') or 'WELCOME PACKET MUST BE ACCEPTED BEFORE PERMANENT ASSIGNMENT.','warning')
            else:
                file_primary_mos_change(personnel_id,mos['mos_code'],eff,authority,remarks); assignment_result=process_assignment_action(personnel_id,node['id'],duty['value'],eff,authority,None,remarks,fire_team=(request.form.get('fire_team') or None)); order_no=((assignment_result or {}).get('document') or {}).get('document_number'); flash(f'ASSIGNMENT / TRANSFER COMPLETE — ORDERS {order_no or "FILED"} — DISCORD ROLE MIRROR QUEUED.','success')
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
            override=role in {'battalion_hq','commander','admin'} and request.form.get('command_override')=='YES'
            ok, reason=validate_appointment_compatibility(person,appt,unit_id,request.form.get('fire_team'),allow_override=override)
            if not ok:
                flash(f'APPOINTMENT BLOCKED — {reason}','danger')
            elif appt:
                process_appointment_action(personnel_id,appt['appointment_code'],None,status,eff,authority,None,remarks,unit_id,fire_team=(request.form.get('fire_team') or None)); flash('APPOINTMENT FILED AND DISCORD ROLE MIRROR QUEUED.','success')
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
            notify_soldier(personnel_id,'S-1 PERSONNEL',f"Award orders — {award['ribbon_name']}",
                f"Headquarters filed {award['ribbon_name']} in your permanent record. Open the Awards tab to review the award and citation.",
                notification_type='AWARD',priority='HIGH',source_key=f"AWARD-NOTICE:{personnel_id}:{award['ribbon_code']}:{order_no or eff}",target_endpoint='my_201_file',target_anchor='awards')
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
        return redirect(url_for('staff_personnel_manage',personnel_id=personnel_id,action=action))
    discord_sync=fetch_one("SELECT status,reason,requested_at,processed_at,error_text FROM discord_role_sync_queue WHERE personnel_id=%s ORDER BY requested_at DESC LIMIT 1",(personnel_id,))
    assignment_order=fetch_one("SELECT document_number,effective_date,title FROM personnel_documents WHERE personnel_id=%s AND UPPER(COALESCE(document_type,''))='ASSIGNMENT' ORDER BY effective_date DESC,created_at DESC LIMIT 1",(personnel_id,))
    return render_template('staff_personnel_manage.html',personnel=person,authority=authority,today=today.isoformat(),
                           award_catalog=awards,operations_list=operations_list,qualification_types=qual_types,
                           training_programs=training_programs,current_weapon=current_weapon,evidence=evidence,
                           promotion_packet=promotion_packet,current_situation=situation,current_leadership=current_leadership,
                           initial_action=initial_action,allowed_actions=allowed_actions,discord_sync=discord_sync,assignment_order=assignment_order,
                           onboarding_gate=onboarding_gate,**catalogs)

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
                assignment_change=(str(old.get("unit_node_id") or "")!=str(node["id"]) or (old.get("duty_position") or "")!=duty["value"])
                gate=onboarding_assignment_gate(pid)
                if assignment_change and not gate.get('allowed'):
                    flash(gate.get('reason') or 'WELCOME PACKET MUST BE ACCEPTED BEFORE FORMATION ASSIGNMENT.','warning')
                    return redirect(url_for('s1'))
                if assignment_change: process_assignment_action(pid,node["id"],duty["value"],date.today(),authority,None,"S-1 structured roster correction.")
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
                       ON CONFLICT(personnel_id,ribbon_code) DO UPDATE SET
                         source_reference=EXCLUDED.source_reference,
                         notes=EXCLUDED.notes""",
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
                    notification_type="AWARD", source_key=f"AWARD-NOTICE:{pid}:{ribbon_code}:{order_number}", target_endpoint="my_201_file", target_anchor="awards",
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
            elif not onboarding_assignment_gate(pid).get('allowed'):
                flash('WELCOME PACKET MUST BE COMPLETED BY THE SOLDIER AND ACCEPTED BY COMMAND BEFORE PERMANENT FORMATION ASSIGNMENT.','warning')
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
    recent = fetch_all("""SELECT p.*,COALESCE(rc.precedence,0) AS rank_precedence FROM personnel p LEFT JOIN rank_catalog rc ON rc.rank_code=p.rank_code WHERE p.archived=FALSE AND p.separated_at IS NULL ORDER BY COALESCE(rc.precedence,0) DESC,p.last_name,p.first_name LIMIT 300""")
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
    award_eligibility = award_eligibility_board(recent)
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
    inactivity_counts={"READY":0,"WATCH":0,"AT RISK":0,"INACTIVE":0,"ADMIN REVIEW":0,"EXCUSED ABSENCE":0}
    for row in recent:
        snap=inactivity_snapshot(row)
        inactivity_counts[snap["state"]]=inactivity_counts.get(snap["state"],0)+1
        # Automatically surface meaningful inactivity stages to the member and staff.
        state=str(snap.get("state") or "").upper()
        last_server=snap.get("last_activity")
        if hasattr(last_server,"date"):
            episode=str(last_server.date())
        else:
            joined=row.get("date_joined") or row.get("created_at") or date.today()
            episode=str(joined.date() if hasattr(joined,"date") and not isinstance(joined,date) else joined)
        if state in {"AT RISK","INACTIVE","ADMIN REVIEW"}:
            notify_soldier(row["id"],"S-1 PERSONNEL",f"SERVER ACTIVITY — {state}",
                f"{int(snap.get('days') or 0)} days since last verified HLL server activity. {snap.get('time_7d','0H 00M')} recorded in the last 7 days.",
                notification_type="READINESS",priority="HIGH" if state in {"INACTIVE","ADMIN REVIEW"} else "WATCH",
                source_key=f"SERVER-INACTIVITY:{row['id']}:{episode}:{state}",target_endpoint="my_201_file",target_anchor="readiness")
        if state == "INACTIVE":
            open_personnel_action(row["id"],"PERSONNEL","21-day server inactivity review","S-1","HIGH","BATTALION SYSTEM",
                {"days_inactive":snap.get("days"),"server_time_7d":snap.get("seconds_7d")},source_key=f"SERVER-INACTIVITY-S1:{row['id']}:{episode}")
        elif state == "ADMIN REVIEW":
            open_personnel_action(row["id"],"COMMAND REVIEW","30-day server inactivity administrative review","HQ","URGENT","BATTALION SYSTEM",
                {"days_inactive":snap.get("days"),"server_time_7d":snap.get("seconds_7d")},source_key=f"SERVER-INACTIVITY-HQ:{row['id']}:{episode}")
        elif state in {"READY","WATCH","EXCUSED ABSENCE"}:
            # Any meaningful return to activity closes old automated inactivity actions.
            stale=fetch_all("""SELECT id FROM personnel_actions WHERE personnel_id=%s
                              AND source_key LIKE 'SERVER-INACTIVITY-%%'
                              AND status NOT IN ('COMPLETE','CLOSED','APPROVED','DENIED','CANCELLED')""",(row["id"],)) or []
            for old_action in stale:
                transition_personnel_action(old_action["id"],"COMPLETE","BATTALION SYSTEM","Verified server activity resumed; automated inactivity action cleared.")
        weapon=weapon_map.get(str(row["id"]))
        inactivity_board.append({"person":row,"status":snap,"weapon":weapon})
    inactivity_board.sort(key=lambda x: (-int(x["status"].get("days") or 0), str(x["person"].get("last_name") or "")))
    s1_suspense = fetch_all("""SELECT pa.*,p.rank_code,p.first_name,p.last_name,p.unit_code,
                              CASE WHEN pa.due_date IS NULL THEN NULL ELSE (pa.due_date-CURRENT_DATE) END AS days_remaining
                       FROM personnel_actions pa LEFT JOIN personnel p ON p.id=pa.personnel_id
                       WHERE pa.owning_section='S-1' AND pa.status NOT IN ('COMPLETE','CLOSED','DENIED')
                       ORDER BY pa.due_date NULLS LAST,pa.priority DESC,pa.created_at LIMIT 100""")
    return render_template("s1_personnel.html", counts=counts, recent=recent, card_map=card_map, weapon_map=weapon_map, issued_packet=issued_packet, communications_roster=communications_roster, ranks=ranks, mos_catalog=mos_catalog, duty_positions=duty_positions, assignment_options=assignment_options, staff_authority=staff_authority, appointment_catalog=appointment_catalog, appointment_map=appointment_map, organization_nodes=organization_nodes, replacement_map=replacement_map, promotion_map=promotion_map, progress_map=progress_map, training_programs=training_programs, award_catalog=award_catalog, award_eligibility=award_eligibility, award_queue=award_queue, forwarded_awards=forwarded_awards, inactivity_board=inactivity_board, inactivity_counts=inactivity_counts, s1_suspense=s1_suspense, s1_today=date.today().isoformat(), workload=staff_workload("S-1"))


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


def hll_battalion_field_report(hours=24):
    """Read-only S-3 field return from RCON telemetry for the recent window."""
    try:
        row=fetch_one("""SELECT COUNT(DISTINCT ps.personnel_id) FILTER (WHERE ps.personnel_id IS NOT NULL) AS linked_soldiers,
                                 COUNT(DISTINCT ps.steam_id) AS players,COUNT(DISTINCT ps.match_id) AS matches,
                                 COALESCE(SUM(ps.connected_seconds),0) AS seconds,COALESCE(SUM(ps.distance_meters),0) AS distance_meters,
                                 COALESCE(SUM(ps.infantry_kills),0) AS infantry_kills,COALESCE(SUM(ps.deaths),0) AS deaths,
                                 COALESCE(SUM(ps.vehicle_kills),0) AS vehicle_kills
                          FROM hll_player_match_stats ps JOIN hll_match_sessions ms ON ms.id=ps.match_id
                          WHERE ps.last_seen_at >= NOW() - make_interval(hours => %s)""",(int(hours),)) or {}
        maps=fetch_all("""SELECT COALESCE(ms.map_name,ms.map_id,'UNKNOWN') AS map_name,COUNT(DISTINCT ms.id) matches,
                                COALESCE(SUM(ps.connected_seconds),0) seconds
                         FROM hll_player_match_stats ps JOIN hll_match_sessions ms ON ms.id=ps.match_id
                         WHERE ps.last_seen_at >= NOW() - make_interval(hours => %s)
                         GROUP BY COALESCE(ms.map_name,ms.map_id,'UNKNOWN') ORDER BY seconds DESC LIMIT 6""",(int(hours),)) or []
        return {"available":True,"hours":hours,"linked_soldiers":int(row.get('linked_soldiers') or 0),"players":int(row.get('players') or 0),
                "matches":int(row.get('matches') or 0),"seconds":int(row.get('seconds') or 0),"distance_meters":float(row.get('distance_meters') or 0),
                "infantry_kills":int(row.get('infantry_kills') or 0),"deaths":int(row.get('deaths') or 0),"vehicle_kills":int(row.get('vehicle_kills') or 0),
                "maps":[dict(x) for x in maps]}
    except Exception:
        return {"available":False,"hours":hours,"linked_soldiers":0,"players":0,"matches":0,"seconds":0,"distance_meters":0.0,"infantry_kills":0,"deaths":0,"vehicle_kills":0,"maps":[]}


def hll_telemetry_research_lab():
    """Staff-facing evidence board for role/MOS/vehicle/aviation research.

    Nothing returned here grants a qualification or changes a personnel record.
    Verified mappings are human-reviewed labels applied to observed RCON role IDs.
    """
    try:
        roles=fetch_all("""SELECT rm.*,COUNT(DISTINCT rlo.loadout)::int AS loadout_count,
                           COALESCE(MAX(rlo.max_speed_mps),0) AS max_speed_mps,
                           COALESCE(MAX(rlo.max_vertical_speed_mps),0) AS max_vertical_speed_mps,
                           COALESCE(SUM(rlo.high_speed_seconds),0) AS high_speed_seconds,
                           COALESCE(SUM(rlo.altitude_gain_meters),0) AS altitude_gain_meters,
                           COALESCE(SUM(rlo.infantry_kills_delta),0) AS infantry_kills_delta,
                           COALESCE(SUM(rlo.vehicle_kills_delta),0) AS vehicle_kills_delta
                    FROM hll_role_mappings rm LEFT JOIN hll_role_loadout_observations rlo ON rlo.role_id=rm.role_id
                    GROUP BY rm.role_id ORDER BY rm.verified DESC,rm.sample_count DESC,rm.role_id""") or []
        loadouts=fetch_all("""SELECT * FROM hll_role_loadout_observations ORDER BY last_seen_at DESC LIMIT 100""") or []
        samples=fetch_all("""SELECT rs.*,p.rank_code,p.first_name,p.last_name,ms.map_name,ms.game_mode
                       FROM hll_research_samples rs LEFT JOIN personnel p ON p.id::text=rs.personnel_id
                       LEFT JOIN hll_match_sessions ms ON ms.id=rs.match_id
                       ORDER BY rs.observed_at DESC LIMIT 80""") or []
        # Named-role/MOS progression recommendations only become possible after a
        # staff member verifies a role mapping. Never auto-certify INSTRUCTOR.
        verified=[r for r in roles if r.get('verified') and r.get('mos_code')]
        recommendations=[]
        for mapping in verified:
            rid=str(mapping.get('role_id')); mos=str(mapping.get('mos_code') or '').upper()
            rows=fetch_all("""SELECT ps.personnel_id,ps.role_seconds,p.rank_code,p.first_name,p.last_name
                               FROM hll_player_match_stats ps JOIN personnel p ON p.id::text=ps.personnel_id
                               WHERE ps.personnel_id IS NOT NULL AND ps.role_seconds ? %s""",(rid,)) or []
            totals={}
            for row in rows:
                raw=row.get('role_seconds') or {}
                if isinstance(raw,str):
                    try: raw=json.loads(raw)
                    except Exception: raw={}
                sec=int((raw or {}).get(rid,0) or 0); pid=str(row.get('personnel_id'))
                if sec<=0: continue
                item=totals.setdefault(pid,{'seconds':0,'rank_code':row.get('rank_code'),'first_name':row.get('first_name'),'last_name':row.get('last_name')})
                item['seconds']+=sec
            for pid,item in totals.items():
                hours=item['seconds']/3600
                suggested='SENIOR' if hours>=30 else ('I' if hours>=15 else ('II' if hours>=5 else ('III' if hours>=1 else None)))
                if suggested:
                    recommendations.append({'personnel_id':pid,'rank_code':item['rank_code'],'first_name':item['first_name'],'last_name':item['last_name'],
                                            'role_id':rid,'role_name':mapping.get('verified_role_name') or mapping.get('observed_label'),'mos_code':mos,'hours':hours,'suggested_level':suggested})
        recommendations.sort(key=lambda x:(x['mos_code'],-x['hours'],x.get('last_name') or ''))
        return {'available':True,'roles':[dict(r) for r in roles],'loadouts':[dict(r) for r in loadouts],'samples':[dict(r) for r in samples],
                'recommendations':recommendations,
                'ammo_status':'RCON V2 exposes player loadout, kills, deaths, score and world position, but no ammunition counter. Exact rounds remain unverified.',
                'aviation_status':'Collecting role-specific speed, vertical-rate and altitude evidence. Flight hours remain disabled until a pilot/aircrew role mapping is verified.',
                'vehicle_status':'Collecting role/loadout movement signatures. RCON V2 does not expose a direct vehicle-seat field in the player record.'}
    except Exception as exc:
        return {'available':False,'roles':[],'loadouts':[],'samples':[],'recommendations':[],'error':str(exc),
                'ammo_status':'Research tables are not available yet. Deploy Battalion Clerk Research Phase first.',
                'aviation_status':'Waiting for research collector tables.','vehicle_status':'Waiting for research collector tables.'}


@app.get('/s3/hll-telemetry-lab')
@login_required
@role_required('s3')
def hll_telemetry_lab_page():
    return render_template('hll_telemetry_lab.html',lab=hll_telemetry_research_lab())


@app.post('/s3/hll-role-mapping')
@login_required
@role_required('s3')
def hll_role_mapping_action():
    role_id=(request.form.get('role_id') or '').strip(); role_name=(request.form.get('verified_role_name') or '').strip().upper()
    category=(request.form.get('role_category') or '').strip().upper(); mos=(request.form.get('mos_code') or '').strip().upper() or None
    if not role_id or not role_name: abort(400)
    if category not in {'INFANTRY','LEADERSHIP','AVIATION','VEHICLE','MEDICAL','SUPPORT','OTHER'}: category='OTHER'
    authority=session.get('display_name') or session.get('username') or 'S-3'
    execute("""INSERT INTO hll_role_mappings(role_id,verified_role_name,role_category,mos_code,verified,verified_by,verified_at,notes,last_seen_at)
               VALUES(%s,%s,%s,%s,TRUE,%s,NOW(),%s,NOW())
               ON CONFLICT(role_id) DO UPDATE SET verified_role_name=EXCLUDED.verified_role_name,role_category=EXCLUDED.role_category,
               mos_code=EXCLUDED.mos_code,verified=TRUE,verified_by=EXCLUDED.verified_by,verified_at=NOW(),notes=EXCLUDED.notes""",
            (role_id,role_name,category,mos,authority,request.form.get('notes')))
    flash(f'HLL ROLE {role_id} VERIFIED AS {role_name}.','success')
    return redirect(url_for('hll_telemetry_lab_page'))


@app.post('/s3/hll-role-mapping/<role_id>/unverify')
@login_required
@role_required('s3')
def hll_role_mapping_unverify(role_id):
    execute("UPDATE hll_role_mappings SET verified=FALSE,verified_by=NULL,verified_at=NULL WHERE role_id=%s",(role_id,))
    flash(f'HLL ROLE {role_id} RETURNED TO OBSERVATION STATUS.','warning')
    return redirect(url_for('hll_telemetry_lab_page'))


@app.get("/s3")
@login_required
@role_required("s3")
def s3():
    recent = fetch_all("SELECT * FROM operations ORDER BY created_at DESC LIMIT 10")
    counts=section_action_counts("S-3")
    training_due=fetch_one("SELECT COUNT(*) total FROM qualifications WHERE expires_at BETWEEN CURRENT_DATE AND CURRENT_DATE + 30") or {"total":0}
    operation_board=fetch_all("SELECT * FROM operations WHERE lifecycle_status NOT IN ('AAR FILED','CANCELLED') ORDER BY COALESCE(start_at,created_at),created_at LIMIT 20")
    deficiencies=training_deficiencies()
    return render_template("section.html", section="S-3 OPERATIONS & TRAINING", section_code="s3", subtitle="Operations ledger, training, attendance, qualifications, readiness and after-action workflow.", counts={"total":counts.get("open",0),"ready":training_due.get("total",0)}, recent=recent, action_counts=counts, operation_board=operation_board, deficiencies=deficiencies, workload=staff_workload("S-3"), hll_field_report=hll_battalion_field_report(24))


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
                    notify_soldier(rec["personnel_id"],'BATTALION HEADQUARTERS',f"Award orders — {award_name}",
                        f"Battalion Headquarters approved and filed {award_name}. Open the Awards tab in your 201 File to review it.",
                        notification_type='AWARD',priority='HIGH',source_key=f"AWARD-REC-NOTICE:{rec_id}",target_endpoint='my_201_file',target_anchor='awards')
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
    return redirect(request.referrer or url_for("my_soldier_record"))


@app.post("/my-soldier-record/weapon/clean")
@login_required
def member_clean_weapon():
    p = linked_personnel()
    if not p:
        abort(403)
    weapon = fetch_one(
        """SELECT wi.* FROM weapon_issue_history wih
           JOIN weapon_inventory wi ON wi.id=wih.weapon_id
           WHERE wih.personnel_id=%s AND wih.is_current=TRUE ORDER BY wih.issued_at DESC LIMIT 1""",
        (p["id"],),
    )
    if not weapon:
        flash("NO INDIVIDUAL M16 IS CURRENTLY ISSUED TO YOUR RECORD.", "warning")
        return redirect(url_for("my_soldier_record") + "#equipment")
    if str(weapon.get("status") or "").upper() == "MAINTENANCE":
        flash("THIS M16 IS IN S-4 MAINTENANCE AND CANNOT BE MEMBER-CLEANED UNTIL RELEASED.", "warning")
        return redirect(url_for("my_soldier_record") + "#equipment")
    performer = f"{p.get('rank_code') or ''} {p.get('last_name') or ''}".strip() or "SOLDIER"
    pre_clean_rounds=max(0,int(weapon.get("rounds_since_cleaning") or 0))
    try:
        weapon_maintenance_action(weapon["id"], "CLEANED", p["id"], performer,
                                  f"Operator cleaning performed by assigned Soldier; {pre_clean_rounds} rounds in fouling cycle before cleaning.")
        verified=fetch_one("SELECT rounds_since_cleaning,last_cleaned_at,condition_state,condition_percent FROM weapon_inventory WHERE id=%s",(weapon["id"],)) or {}
        latest=fetch_one("""SELECT id,performed_at FROM weapon_maintenance_log
                            WHERE weapon_id=%s AND personnel_id=%s AND UPPER(action_type)='CLEANED'
                            ORDER BY performed_at DESC LIMIT 1""",(weapon["id"],p["id"]))
        if int(verified.get("rounds_since_cleaning") or -1)!=0 or not verified.get("last_cleaned_at") or not latest:
            raise RuntimeError("post-clean verification failed")
    except Exception:
        log.exception("Member weapon-clean authoritative path failed personnel_id=%s weapon_id=%s",p.get("id"),weapon.get("id"))
        # Last-resort recovery keeps the Soldier-facing state truthful even if an
        # optional history subsystem is temporarily unavailable.
        execute("""UPDATE weapon_inventory SET rounds_since_cleaning=0,last_cleaned_at=NOW(),
                   condition_percent=100,condition_state='SERVICEABLE',updated_at=NOW() WHERE id=%s""",(weapon["id"],))
        try:
            reconcile_weapon_rounds_since_cleaning(weapon["id"]); refresh_weapon_condition(weapon["id"])
        except Exception:
            log.exception("Member weapon-clean recovery refresh failed weapon_id=%s",weapon.get("id"))
        flash("M16 CLEANING WAS APPLIED, BUT THE MAINTENANCE LEDGER NEEDS S-4 REVIEW.","warning")
        return redirect(url_for("my_soldier_record") + "#equipment")
    execute("UPDATE personnel SET activity_last_seen_at=NOW(),updated_at=NOW() WHERE id=%s", (p["id"],))
    flash(f"M16 OPERATOR CLEANING COMPLETE — {pre_clean_rounds} ROUND FOULING CYCLE CLEARED; MAINTENANCE ENTRY VERIFIED.", "success")
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


@app.get('/staff/reliability')
@login_required
def staff_reliability():
    if session.get('access_role') not in STAFF_ROLES: abort(403)
    report=automation_reliability_report()
    return render_template('staff_reliability.html',report=report,details=automation_reliability_details(),integrity=battalion_integrity_scan(100),server_seed=staff_server_seed_snapshot())


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
    integrity=battalion_integrity_scan(200)
    return render_template("battalion_control.html",health=health,lifecycle=lifecycle,due_weapons=due_weapons,logs=logs,history=history,personnel_list=personnel_list,daily=daily,milestones=milestones,company_history=company_history,integrity=integrity)


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

    # Every verified OPERATION presence chunk is now mirrored immediately into
    # the member's operation history and M16 ledger. The row begins as TRACKED
    # PRESENCE, advances to PARTIAL / LATE, and upgrades to FULL CREDIT only when
    # the configured threshold is met.
    live_presence=sync_operation_presence_from_attendance(event,personnel_id,total,"BATTALION CLERK")
    live_rounds={"target":live_presence.get("rounds_target",0),"applied":live_presence.get("rounds_applied",0)}
    if total < threshold_seconds or already:
        return False, total

    execute("""UPDATE battalion_event_attendance SET credited_at=NOW(),attendance_grade=%s,attendance_percent=%s,updated_at=NOW()
               WHERE event_id=%s AND personnel_id=%s""", (grade,attendance_percent,event["id"], personnel_id))
    execute("""INSERT INTO personnel_activity_credit
               (personnel_id,source,source_reference,activity_type,activity_date,duration_seconds,credited)
               SELECT %s,'BATTALION DUTY',%s,%s,%s,%s,TRUE
               WHERE NOT EXISTS (
                 SELECT 1 FROM personnel_activity_credit
                 WHERE personnel_id=%s AND source='BATTALION DUTY' AND source_reference=%s AND activity_type=%s
               )""",
            (personnel_id, str(event["id"]), event["event_type"], event["starts_at"].date(), total,
             personnel_id,str(event["id"]),event["event_type"]))
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
        # sync_operation_presence_from_attendance already upgraded the same
        # idempotent operation row and weapon ledger to FULL CREDIT above.
        operation_credit_cascade(
            event["operation_id"], personnel_id,
            int((live_rounds or {}).get("target") or operation_round_target_for_time(event,total)),
            "BATTALION CLERK"
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
        if m: platoon=canonical_formation_label(m.group(1), "PLATOON")
        m=re.search(r"\b(1ST|2ND|3RD|4TH)\s+(?:SQUAD|SQD)\b",cleaned)
        if m: squad=canonical_formation_label(m.group(1), "SQUAD")
    return unit,platoon,squad

# Legacy Discord-to-formation writeback removed. Website personnel assignments are authoritative;
# Battalion Clerk only mirrors the canonical Website state into Discord.

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
            # AUTHORITY LOCK: once a 201 File exists, Discord is presentation/access only.
            # No rank, MOS, Company, Platoon, Squad, Team, or billet value is written back
            # from Discord. Manual Discord edits are treated as drift and reconciled to the
            # website record by Battalion Clerk.
            discord_rank = _rank_from_discord_roles(roles)
            discord_mos, _discord_mos_title = _mos_from_discord_roles(roles)
            drift=[]
            if discord_rank and discord_rank != person.get("rank_code"):
                drift.append({"field":"rank","discord":discord_rank,"canonical":person.get("rank_code")})
            if discord_mos and discord_mos != person.get("mos_code"):
                drift.append({"field":"mos","discord":discord_mos,"canonical":person.get("mos_code")})
            discord_unit,discord_platoon,discord_squad=_assignment_from_discord_roles(roles)
            for field,dvalue,cvalue in (("company",discord_unit,person.get("unit_code")),("platoon",discord_platoon,person.get("platoon")),("squad",discord_squad,person.get("squad"))):
                if dvalue and str(dvalue).upper()!=str(cvalue or '').upper():
                    drift.append({"field":field,"discord":dvalue,"canonical":cvalue})
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
        # Existing 201 Files remain authoritative. Discord rank/MOS/formation values
        # are never allowed to rewrite an existing personnel record during link repair.
        candidate=fetch_one("SELECT * FROM personnel WHERE id=%s",(candidate["id"],))
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
               VALUES(%s,NULL,%s,CURRENT_DATE,'HEADQUARTERS — BATTALION CLERK','Initial rank recorded when the Soldier was entered on the Battle Roster.')""",
            (person["id"],rank))
    execute("""INSERT INTO assignment_history(personnel_id,unit_code,duty_position,effective_date)
               VALUES(%s,'1-5 CAV',%s,CURRENT_DATE)""",(person["id"],mos_title))
    execute("""INSERT INTO personnel_mos_records(personnel_id,mos_code,mos_title,mos_kind,effective_date,qualified_by,remarks)
               VALUES(%s,%s,%s,'PRIMARY',CURRENT_DATE,'HEADQUARTERS — BATTALION CLERK','Initial battlefield MOS read from Discord role set.')
               ON CONFLICT(personnel_id,mos_code,mos_kind) DO NOTHING""",(person["id"],mos_code,mos_title))
    # New Discord-linked records enter Replacement Detachment without importing
    # organizational roles. Command/S-1 must file the authoritative assignment on
    # the website; membership activates when a Platoon is assigned.
    execute("""INSERT INTO website_member_links(guild_id,discord_user_id,personnel_id)
               VALUES(%s,%s,%s)
               ON CONFLICT(guild_id,discord_user_id)
               DO UPDATE SET personnel_id=EXCLUDED.personnel_id,linked_at=NOW()""",
            (guild_id,discord_user_id,str(person["id"])))
    card,field_code=issue_battle_roster_card(person["id"])
    weapon=issue_m16(person["id"])
    _assigned_on_entry = bool(person.get("unit_code") not in {None,"","1-5 CAV","REPLACEMENT DETACHMENT"})
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
    staff_log("S-1","NEW SOLDIER",f"{rank} {person.get('last_name','')} entered on the Battle Roster", "BATTALION CLERK",person["id"],initial_order.get("document_number") if initial_order else None,{"mos":mos_code})
    battalion_history_entry("ARRIVAL",f"{rank} {person.get('last_name','')} entered the Battle Roster",f"Primary MOS {mos_code} — {mos_title}.",person["id"],reference_number=initial_order.get("document_number") if initial_order else None)
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
            "fire_team":person.get("fire_team") if person else None,
            "field_status":person.get("field_status") if person else None,
            "lifecycle_state":person.get("lifecycle_state") if person else None,
            "membership_active":bool(person and str(person.get("field_status") or "").upper()=="ASSIGNED" and (person.get("platoon") or str(person.get("unit_code") or "").upper().startswith("HHC"))),
            "appointment_roles":discord_appointment_roles,
            "role_drift":result.get("role_drift") or [],
            "roster_number":card.get("roster_number") if card else None,
            "field_code":result.get("field_code"),
            "weapon_serial":weapon.get("serial_number") if weapon else None,
            "initial_order_id":str((result.get("initial_order") or {}).get("id")) if result.get("initial_order") else None}


@app.get("/internal/clerk/personnel/canonical-roster")
def clerk_personnel_canonical_roster():
    """Return authoritative linked personnel state for safe Discord role maintenance.

    This endpoint is read-only. Battalion Clerk uses it during managed-role cleanup so
    Discord is repaired to match the website rather than allowing transient Discord
    role combinations to rewrite personnel assignments.
    """
    if not _clerk_authorized():
        return {"ok":False,"error":"authorization required"},401
    guild_id=request.args.get("guild_id")
    if not guild_id:
        return {"ok":False,"error":"guild_id required"},400
    rows=fetch_all("""SELECT wml.discord_user_id,p.id AS personnel_id,p.rank_code,p.mos_code,
                             p.unit_code,p.platoon,p.squad,p.fire_team,p.field_status,p.lifecycle_state,
                             p.first_name,p.last_name,
                             h.steam_id AS hll_player_id,h.hll_player_name,h.platform AS hll_platform,h.verified AS hll_verified
                      FROM website_member_links wml
                      JOIN personnel p ON p.id::text=wml.personnel_id
                      LEFT JOIN hll_personnel_links h ON h.personnel_id::text=p.id::text
                      WHERE wml.guild_id=%s AND p.archived=FALSE
                      ORDER BY p.last_name,p.first_name""",(str(guild_id),))
    appointment_role_map={
        "PSG":"Platoon Sergeant","SL":"Squad Leader",
        "ASST_SL":"Assistant Squad Leader","FTL":"Team Leader",
    }
    out=[]
    for row in rows:
        appts=fetch_all("""SELECT appointment_code FROM personnel_appointments
                           WHERE personnel_id=%s AND is_current=TRUE
                           ORDER BY effective_date,created_at""",(row.get("personnel_id"),))
        item=dict(row)
        item["personnel_id"]=str(item.get("personnel_id"))
        item["guild_id"]=str(guild_id)
        item["linked"]=True
        item["appointment_roles"]=[appointment_role_map[a["appointment_code"]]
                                   for a in appts if a.get("appointment_code") in appointment_role_map]
        field_status=str(item.get("field_status") or "").upper().strip()
        lifecycle=str(item.get("lifecycle_state") or "").upper().strip()
        item["vip_eligible"]=bool(field_status=="ASSIGNED" and lifecycle not in {"SEPARATED","ARCHIVED"} and item.get("hll_player_id") and item.get("hll_verified") is not False)
        item["vip_comment"]=(f"1/5 CAV | {item.get('rank_code') or ''} {item.get('last_name') or item.get('hll_player_name') or item.get('personnel_id')}".strip())[:120]
        out.append(item)
    return {"ok":True,"items":out,"count":len(out)}


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
    reconcile_operation_schedule_states()
    # Guild is represented by configured channel binding; events themselves are global to this battalion site.
    rows = fetch_all("""SELECT e.*, COUNT(a.id) AS tracked_count,
                     COALESCE(SUM(CASE WHEN a.credited_at IS NOT NULL THEN 1 ELSE 0 END),0) AS qualified_count
                     FROM battalion_events e LEFT JOIN battalion_event_attendance a ON a.event_id=e.id
                     WHERE e.status IN ('SCHEDULED','ACTIVE') AND e.ends_at > NOW() - INTERVAL '12 hours'
                     GROUP BY e.id ORDER BY e.starts_at""")
    for event in rows:
        event["attendance"] = fetch_all("""SELECT p.rank_code,p.first_name,p.last_name,a.qualifying_seconds,a.credited_at,a.last_seen_at
            FROM battalion_event_attendance a JOIN personnel p ON p.id=a.personnel_id
            WHERE a.event_id=%s ORDER BY a.qualifying_seconds DESC,p.last_name""", (event["id"],))
    return {"ok": True, "events": rows}


@app.post("/internal/clerk/operations/maintenance")
def clerk_operation_maintenance():
    """Reconcile verified historical attendance, M16 rounds, and stale schedules."""
    if not _clerk_authorized():
        return {"ok":False,"error":"authorization required"},401
    reconcile_operation_schedule_states()
    result=run_operation_maintenance("BATTALION CLERK MAINTENANCE")
    return {"ok":True,"summary":result}


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
    # Refresh server-driven readiness/MOS/ribbon state before promotion evidence is read.
    # This keeps promotion eligibility synchronized with actual HLL service instead
    # of waiting for a Soldier to open a Website page.
    reconcile_career_systems(award_ribbons=True)
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


@app.post("/internal/clerk/weapons/reconcile-hll-rounds")
def clerk_reconcile_hll_weapon_rounds():
    """Advance/backfill issued M16 rounds from verified HLL: Vietnam field telemetry."""
    if not _clerk_authorized():
        return {"ok":False,"error":"authorization required"},401
    data=request.get_json(silent=True) or {}
    personnel_id=data.get("personnel_id")
    try: days=max(1,min(730,int(data.get("days") or 365)))
    except (TypeError,ValueError): days=365
    return {"ok":True,**reconcile_hll_m16_rounds(personnel_id,days)}


@app.post("/internal/clerk/weapons/voice-rounds")
def clerk_voice_weapon_rounds():
    """Legacy compatibility endpoint. Discord voice no longer advances M16 service."""
    if not _clerk_authorized():
        return {"ok":False,"error":"authorization required"},401
    return {"ok":True,"applied":0,"deprecated":True,"reason":"M16 service is HLL server-authoritative; Discord voice credit is disabled."}


@app.post("/internal/clerk/weapons/reconcile-voice-rounds")
def clerk_reconcile_voice_rounds():
    """Legacy compatibility endpoint. Historical Discord voice backfill is disabled."""
    if not _clerk_authorized():
        return {"ok":False,"error":"authorization required"},401
    return {"ok":True,"sessions":0,"blocks_checked":0,"rounds_applied":0,"deprecated":True,"reason":"M16 service is HLL server-authoritative; Discord voice reconciliation is disabled."}

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

# Legacy compatibility only. MOS grade is now calculated from verified HLL role time.
MOS_PROFICIENCY_ORDER = {"UNRATED":0,"III":1,"II":2,"I":3,"SENIOR":4}


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
    """Legacy S-3 control now recalculates the server-authoritative grade only."""
    pid=request.form.get('personnel_id')
    person=fetch_one("SELECT * FROM personnel WHERE id=%s",(pid,)) if pid else None
    if not person: abort(404)
    result=sync_mos_proficiency(person)
    if result:
        flash(f"MOS PROFICIENCY RECALCULATED — {result.get('level')} / {result.get('server_hours',0):.1f} VERIFIED ROLE HOURS.",'success')
    else:
        flash('MOS PROFICIENCY COULD NOT BE CALCULATED. VERIFY THE SOLDIER MOS AND HLL ROLE MAPPING.','warning')
    return redirect(request.referrer or url_for('s3'))


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
    flash('LEADERSHIP PERFORMANCE RECORD FILED.','success'); return redirect(request.referrer or url_for('my_squad'))


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
    current_issue=fetch_one('SELECT personnel_id,issued_at FROM weapon_issue_history WHERE weapon_id=%s AND is_current=TRUE',(weapon_id,))
    holder=fetch_one('SELECT * FROM personnel WHERE id=%s',(current_issue['personnel_id'],)) if current_issue and current_issue.get('personnel_id') else None
    if current_issue and current_issue.get('issued_at'):
        weapon['_issued_at']=current_issue['issued_at']
    weapon=derive_weapon_state(weapon,holder)
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
    p=soldier_view(linked_personnel())
    if not p: abort(403)
    ctx=member_home_context(p)
    ctx["milestones"]=safe_member_panel("career milestones", [], member_career_milestones, p)
    ctx["formation_legacy"]=safe_member_panel("career formation legacy", [], member_formation_legacy, p["id"])
    ctx["promotion_readiness"]=safe_member_panel(
        "career promotion readiness",
        {"target":None,"overall":0,"time":0,"training":0,"operations":0,"leadership":0,"readiness":0,"status":"NOT TRACKED"},
        member_promotion_readiness,p)
    ctx["service_goals"]=safe_member_panel("career service goals", [], member_service_goals, p)
    return render_template("member_career.html",personnel=p,**ctx)

@app.get("/my-service-statistics")
@login_required
def my_service_statistics():
    p=linked_personnel()
    if not p: abort(403)
    hll=safe_member_panel("service statistics HLL telemetry", hll_service_statistics(None), hll_service_statistics, p["id"])
    return render_template("member_service_statistics.html",personnel=soldier_view(p),hll_stats=hll,**member_career_context(p))

@app.get("/my-armored-service-record")
@login_required
def my_armored_service_record():
    p=linked_personnel()
    if not p: abort(403)
    # This page must open even if telemetry is temporarily unavailable or the
    # database is between collector migrations.
    hll=safe_member_panel("armored service HLL telemetry", hll_service_statistics(None), hll_service_statistics, p["id"])
    return render_template("member_armored_service.html",personnel=soldier_view(p),hll_stats=hll)

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
    safe_member_panel("M16 history visit tracking", None, welcome_visit, p["id"], "VIEW_M16")
    weapon=safe_member_panel("M16 history current weapon", None, current_weapon_for, p)
    if not weapon: return render_template("member_weapon_history.html",personnel=soldier_view(p),weapon=None,issue_history=[],maintenance=[],inspections=[],operations=[],m16_service=hll_m16_service_statistics(p["id"],None))
    issue_history=safe_member_fetch_all("M16 issue history", """SELECT wih.*,wi.serial_number,wi.rack_number FROM weapon_issue_history wih JOIN weapon_inventory wi ON wi.id=wih.weapon_id
      WHERE wih.personnel_id=%s ORDER BY wih.issued_at DESC,wih.created_at DESC""",(p["id"],))
    maintenance=safe_member_fetch_all("M16 cleaning history", "SELECT * FROM weapon_maintenance_log WHERE weapon_id=%s AND UPPER(COALESCE(action_type,'')) LIKE '%%CLEAN%%' ORDER BY performed_at DESC",(weapon["id"],))
    inspections=safe_member_fetch_all("M16 inspection history", "SELECT * FROM weapon_inspections WHERE weapon_id=%s ORDER BY inspection_date DESC,created_at DESC",(weapon["id"],))
    operations=safe_member_fetch_all("M16 operation history", """SELECT DISTINCT op.*,o.operation_code,o.title,o.operation_date
      FROM operation_participation op
      JOIN operations o ON o.id=op.operation_id
      JOIN hll_match_sessions ms ON ms.started_at <= o.start_at + make_interval(mins => COALESCE(o.duration_minutes,90)+30)
        AND COALESCE(ms.ended_at,ms.last_seen_at) >= o.start_at - INTERVAL '30 minutes'
      JOIN hll_player_match_stats ps ON ps.match_id=ms.id AND ps.personnel_id=%s AND COALESCE(ps.connected_seconds,0)>0
      JOIN weapon_issue_history wih ON wih.personnel_id=op.personnel_id AND wih.weapon_id=%s
        AND wih.issued_at<=COALESCE(o.operation_date,o.start_at::date)
        AND (wih.turned_in_at IS NULL OR wih.turned_in_at>=COALESCE(o.operation_date,o.start_at::date))
      WHERE op.personnel_id=%s ORDER BY COALESCE(o.operation_date,CURRENT_DATE) DESC""",(p["id"],weapon["id"],p["id"]))
    m16_service=hll_m16_service_statistics(p["id"],weapon)
    return render_template("member_weapon_history.html",personnel=soldier_view(p),weapon=weapon,issue_history=issue_history,maintenance=maintenance,inspections=inspections,operations=operations,m16_service=m16_service)

@app.route("/my-squad", methods=["GET","POST"])
@login_required
def my_squad():
    raw=linked_personnel()
    if not raw: abort(403)
    safe_member_panel("My Squad visit tracking", None, welcome_visit, raw["id"], "VIEW_MY_SQUAD")
    personnel=soldier_view(raw)
    roster, scope, team_filter, leadership_appt, actionable_ids=safe_member_panel("My Squad roster", ([],None,None,None,[]), squad_roster_for, raw)
    rank=str(raw.get("rank_code") or "").upper()
    # Presentation is billet-driven: a recognized leadership appointment gets the leader workspace.
    # Mutating quick actions remain governed by the existing rank + appointment + subordinate scope checks.
    leader_mode=bool(leadership_appt)
    leadership_title=(leadership_appt.get("appointment_name") or leadership_appt.get("appointment_code") or "LEADERSHIP") if leadership_appt else ""
    can_act=rank in SQUAD_LEADERSHIP_RANKS and bool(leadership_appt) and bool(actionable_ids)
    allowed_ids=set(actionable_ids)

    if request.method=="POST":
        if not can_act: abort(403)
        target_id=str(request.form.get("personnel_id") or "")
        if target_id not in allowed_ids or target_id==str(raw.get("id")): abort(403)
        action=(request.form.get("quick_action") or "").upper().strip()
        justification=(request.form.get("justification") or "").strip()
        target=next((x for x in roster if str(x.get("id"))==target_id),None)
        authority=f"{rank} {raw.get('last_name','')}".strip()
        if not target or not justification: abort(400)
        if action=="PROMOTION":
            target_rank=(request.form.get("target_rank") or "").upper().strip()
            if not target_rank: abort(400)
            execute("""INSERT INTO personnel_recommendations(personnel_id,recommendation_type,recommended_action,justification,promotion_narrative,recommending_personnel_id,status)
                       VALUES(%s,'PROMOTION',%s,%s,%s,%s,'PENDING')""",(target_id,f"PROMOTION TO {target_rank}",justification,justification,raw["id"]))
            open_personnel_action(target_id,"PROMOTION",f"Promotion recommendation — {target_rank}","S-1","HIGH",authority,{"target_rank":target_rank,"justification":justification},source_key=f"SQUAD-PROMO:{target_id}:{target_rank}:{date.today()}")
            flash("PROMOTION RECOMMENDATION FORWARDED TO S-1.","success")
        elif action=="AWARD":
            award_name=(request.form.get("award_name") or "").strip()
            if not award_name: abort(400)
            execute("""INSERT INTO personnel_recommendations(personnel_id,recommendation_type,recommended_action,justification,recommending_personnel_id,status)
                       VALUES(%s,'AWARD',%s,%s,%s,'PENDING_S1')""",(target_id,award_name,justification,raw["id"]))
            open_personnel_action(target_id,"AWARD",f"Award recommendation — {award_name}","S-1","NORMAL",authority,{"award_name":award_name,"justification":justification},source_key=f"SQUAD-AWARD:{target_id}:{award_name}:{date.today()}")
            flash("AWARD RECOMMENDATION FORWARDED TO S-1.","success")
        elif action=="LEADERSHIP_NOTE":
            execute("""INSERT INTO leadership_performance_records(personnel_id,record_date,leadership_type,title,narrative,recorded_by)
                       VALUES(%s,%s,'SQUAD LEADERSHIP','NCO LEADERSHIP NOTE',%s,%s)""",(target_id,date.today(),justification,authority))
            write_service_entry(target_id,"LEADERSHIP","NCO LEADERSHIP NOTE",justification,authority)
            flash("LEADERSHIP NOTE FILED.","success")
        else:
            abort(400)
        return redirect(url_for("my_squad",soldier=target_id))

    selected_id=str(request.args.get("soldier") or "")
    selected=next((x for x in roster if str(x.get("id"))==selected_id),None) if selected_id else None
    selected_view=soldier_view(selected) if selected else None
    uniform_rows=[]; earned=[]
    if selected:
        uniform_rows, earned=safe_member_panel("My Squad ribbon rack", ([],[]), worn_ribbon_rows, selected["id"])
    awards=safe_member_fetch_all("My Squad award catalog", "SELECT ribbon_name FROM ribbon_catalog WHERE is_active=TRUE ORDER BY sort_order,ribbon_name") or []
    ranks=safe_member_fetch_all("My Squad rank catalog", "SELECT rank_code,rank_name,precedence FROM rank_catalog ORDER BY precedence") or []
    org=safe_member_panel("My Squad organization", {}, member_organization_context, raw) or {}
    team_leaders=org.get("team_leaders") or {}
    leader_metrics={
        "assigned": len(roster or []),
        "readiness": int(org.get("squad_readiness") or 0),
        "actionable": len(actionable_ids or []),
        "team_leader_vacancies": sum(1 for team in ("Alpha Team","Bravo Team") if not team_leaders.get(team)),
    }
    return render_template("my_squad.html",personnel=personnel,soldiers=roster,scope=scope,team_filter=team_filter,
                           leadership_appt=leadership_appt,leadership_title=leadership_title,leader_mode=leader_mode,leader_metrics=leader_metrics,
                           actionable_ids=actionable_ids,can_act=can_act,selected=selected_view,
                           uniform_ribbon_rows=uniform_rows,earned_ribbons=earned,awards=awards,ranks=ranks,org=org)

@app.get("/my-squad/combat-record/<personnel_id>")
@login_required
def squad_soldier_combat_record(personnel_id):
    """Read-only member-facing combat record for Soldiers visible in MY SQUAD.

    Visibility is deliberately derived from the same squad/platoon roster scope as
    MY SQUAD. This does not expose the staff 201 File or any private account /
    identity fields. The uniform uses the exact existing squad uniform/ribbon CSS
    classes so ribbon geometry is not duplicated or repositioned.
    """
    raw=linked_personnel()
    if not raw: abort(403)
    roster, scope, team_filter, leadership_appt, actionable_ids=safe_member_panel(
        "Squad Combat Record roster", ([],None,None,None,[]), squad_roster_for, raw)
    visible={str(x.get("id")):x for x in (roster or [])}
    target=visible.get(str(personnel_id))
    if not target:
        abort(404)

    target_view=soldier_view(target)
    uniform_rows, earned=safe_member_panel(
        "Squad Combat Record ribbon rack", ([],[]), worn_ribbon_rows, target["id"])
    hll_stats=safe_member_panel(
        "Squad Combat Record HLL stats", hll_service_statistics(None), hll_service_statistics, target["id"])
    weapon=safe_member_panel("Squad Combat Record weapon", None, current_weapon_for, target)
    m16_service=safe_member_panel(
        "Squad Combat Record M16 service", hll_m16_service_statistics(None,None),
        hll_m16_service_statistics, target["id"], weapon)
    service_stats=safe_member_panel(
        "Squad Combat Record service statistics", {}, member_service_statistics, target) or {}
    combat_experience=safe_member_panel(
        "Squad Combat Record experience", {}, member_combat_experience, target["id"]) or {}

    ordered=[str(x.get("id")) for x in (roster or [])]
    idx=ordered.index(str(personnel_id)) if str(personnel_id) in ordered else -1
    previous_id=ordered[idx-1] if idx>0 else None
    next_id=ordered[idx+1] if idx>=0 and idx<len(ordered)-1 else None

    rank=str(raw.get("rank_code") or "").upper()
    can_act=rank in SQUAD_LEADERSHIP_RANKS and bool(leadership_appt) and str(personnel_id) in {str(x) for x in (actionable_ids or [])} and str(personnel_id)!=str(raw.get("id"))

    # Canonical Soldier Service View: every Soldier click uses the same 201 File shell.
    # Peer/member viewers receive the complete service presentation in read-only mode;
    # staff controls continue to be governed by the viewer's existing access role.
    context=personnel_record_context(target)
    context.update({
        "read_only_peer": str(target.get("id")) != str(raw.get("id")),
        "record_return_url": url_for("my_squad"),
        "record_return_label": "BACK TO MY SQUAD",
        "previous_id": previous_id,
        "next_id": next_id,
        "record_view_scope": "SQUAD",
    })
    return render_template("personnel_file.html", **context)


@app.get("/my-platoon")
@login_required
def my_platoon_identity():
    p=linked_personnel()
    if not p: abort(403)
    return render_template("member_formation.html",personnel=soldier_view(p),snapshot=member_formation_snapshot(p,"platoon"),formation_type="PLATOON",org=member_organization_context(p))


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
    safe_member_panel("My Unit visit tracking", None, welcome_visit, p["id"], "VIEW_MY_UNIT")
    org=safe_member_panel("My Unit organization", {}, member_organization_context, p)
    members=safe_member_fetch_all("My Unit company roster", """SELECT p.id,p.rank_code,p.last_name,p.first_name,p.unit_code,p.platoon,p.squad,p.fire_team,p.duty_position,p.mos_code,p.readiness_percent,COALESCE(rc.precedence,0) AS rank_precedence
      FROM personnel p LEFT JOIN rank_catalog rc ON rc.rank_code=p.rank_code
      WHERE p.separated_at IS NULL AND COALESCE(p.archived,FALSE)=FALSE AND p.unit_code=%s
      ORDER BY COALESCE(rc.precedence,0) DESC,p.platoon,p.squad,p.last_name,p.first_name""",(p['unit_code'],))
    for m in members:
        m['platoon']=canonical_formation_label(m.get('platoon'),'PLATOON') if m.get('platoon') else None
        m['squad']=canonical_formation_label(m.get('squad'),'SQUAD') if m.get('squad') else None
        m['fire_team']=canonical_formation_label(m.get('fire_team'),'TEAM') if m.get('fire_team') else None
    return render_template('my_unit.html',personnel=soldier_view(p),members=members,org=org)

@app.get('/my-company')
@login_required
def my_company_identity():
    p=linked_personnel()
    if not p: abort(403)
    return render_template("member_formation.html",personnel=soldier_view(p),snapshot=member_formation_snapshot(p,"company"),formation_type="COMPANY",org=member_organization_context(p))


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
            identity_conflict=_recruit_game_identity_conflict(case,personnel_id=case.get('personnel_id'))
            if case.get('status') in {'DENIED','CLOSED','ENLISTED'}:
                flash('THIS RECRUITING CASE IS ALREADY CLOSED AND CANNOT BE APPROVED.','warning')
            elif identity_conflict:
                flash('GAME IDENTITY CONFLICT — '+identity_conflict['error'],'danger')
            elif case.get('discord_user_id'):
                execute("""UPDATE recruiting_cases SET status='REPLACEMENT_DEPOT',replacement_depot_entered_at=COALESCE(replacement_depot_entered_at,NOW()),
                           command_notes=%s,reviewed_by=%s,reviewed_at=NOW(),approved_at=COALESCE(approved_at,NOW()),
                           discord_join_error=NULL,credentials_delivery_error=NULL,updated_at=NOW() WHERE id=%s""",(remarks,authority,case_id))
                # Approval itself opens the Website personnel/onboarding record immediately.
                # Battalion Clerk still owns Discord guild join, role mirroring, and DM delivery.
                try:
                    approved_case=fetch_one('SELECT * FROM recruiting_cases WHERE id=%s',(case_id,))
                    provision=_provision_replacement_personnel(approved_case,discord_user_id=approved_case.get('discord_user_id'),username=approved_case.get('discord_verified_username') or approved_case.get('discord_username_input'),display_name=approved_case.get('discord_verified_username'))
                    if provision.get('ok'):
                        _ensure_recruit_login_delivery(approved_case,provision)
                except Exception:
                    log.exception('Immediate Welcome Packet provisioning failed after approval case=%s',case_id)
                flash('APPLICATION APPROVED. THE REPLACEMENT 201 FILE AND WELCOME PACKET ARE OPEN. BATTALION CLERK WILL COMPLETE DISCORD JOIN, ROLES, AND PRIVATE LOGIN DELIVERY AUTOMATICALLY.','success')
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
    for row in rows:
        counts[row['status']]=counts.get(row['status'],0)+1
        if row.get('personnel_id'):
            try: row['journey']=_recruit_journey_status(row['personnel_id'])
            except Exception:
                log.exception('Recruiting Control journey build failed case=%s',row.get('case_number')); row['journey']={}
        else:
            row['journey']={}
    prospective_rows=prospective_replacements_rows()
    prospective_counts={
      'total':len(prospective_rows),
      'no_application':sum(1 for r in prospective_rows if not r.get('case_id')),
      'review':sum(1 for r in prospective_rows if r.get('recruiting_status') in {'SUBMITTED','DISCORD_VERIFIED','PENDING_COMMAND','MORE_INFO_REQUIRED'}),
      'approved':sum(1 for r in prospective_rows if r.get('recruiting_status') in {'APPROVED_AWAITING_DISCORD','REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING'}),
    }
    return render_template('recruiting_control.html',cases=rows,counts=counts,
                           prospective_rows=prospective_rows,prospective_counts=prospective_counts,
                           vacancy_rows=[x for x in billet_strength_rows() if int(x.get('vacant') or 0)>0],recruiting_needs=mos_recruiting_needs())





@app.post('/hq/recruiting/<case_id>/delete')
@login_required
@role_required('battalion_hq')
def recruiting_case_delete(case_id):
    """Permanently remove an unprovisioned recruiting application.

    Once a case owns a personnel record, history is preserved and Command must use
    normal personnel/separation workflows instead of destructive deletion.
    """
    case=fetch_one("SELECT * FROM recruiting_cases WHERE id=%s",(case_id,))
    if not case:
        abort(404)
    confirm=(request.form.get('confirm_case_number') or '').strip().upper()
    expected=str(case.get('case_number') or '').strip().upper()
    if not expected or confirm != expected:
        flash('DELETE CANCELLED. TYPE THE EXACT CASE NUMBER TO PERMANENTLY REMOVE THIS APPLICATION.','danger')
        return redirect(request.referrer or url_for('recruiting_control'))
    if case.get('personnel_id'):
        flash('THIS APPLICATION IS LINKED TO AN OFFICIAL PERSONNEL RECORD AND CANNOT BE HARD-DELETED. USE THE PERSONNEL / SEPARATION WORKFLOW TO PRESERVE THE SERVICE RECORD.','warning')
        return redirect(url_for('recruiting_case_archive',case_id=case_id))
    authority=session.get('display_name') or session.get('username') or 'BATTALION HEADQUARTERS'
    # A pre-provisioning case should have no official personnel dependencies. If a
    # partially-created Welcome Packet exists without personnel, detach/remove it first.
    safe_member_panel('Recruiting delete orphan Welcome Packet', None, execute, 'DELETE FROM welcome_packets WHERE recruiting_case_id=%s AND personnel_id IS NULL', (case_id,))
    deleted=fetch_one('DELETE FROM recruiting_cases WHERE id=%s AND personnel_id IS NULL RETURNING case_number',(case_id,))
    if not deleted:
        flash('APPLICATION COULD NOT BE REMOVED BECAUSE IT IS NOW LINKED TO A PERSONNEL RECORD.','warning')
        return redirect(url_for('recruiting_control'))
    try:
        staff_log('HQ','RECRUITING APPLICATION DELETED',f"{deleted.get('case_number')} permanently removed before personnel provisioning.",authority,details={'case_number':deleted.get('case_number')})
    except Exception:
        log.exception('Recruiting delete audit log failed for %s',case_id)
    flash(f"APPLICATION {deleted.get('case_number')} PERMANENTLY REMOVED.",'success')
    return redirect(url_for('recruiting_control'))


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
    credential_events=fetch_all("""SELECT event_type,event_status,discord_user_id,detail,authority,created_at
                                  FROM recruit_credential_delivery_events
                                  WHERE recruiting_case_id=%s ORDER BY created_at DESC LIMIT 20""",(case_id,))
    return render_template('recruiting_case_archive.html',case=case,credential_events=credential_events)

@app.post('/hq/recruiting/<case_id>/resend-login')
@login_required
@role_required('battalion_hq')
def recruiting_case_resend_login(case_id):
    case=fetch_one("SELECT * FROM recruiting_cases WHERE id=%s",(case_id,))
    if not case:
        abort(404)
    if not case.get('personnel_id'):
        flash('LOGIN INFORMATION CANNOT BE SENT UNTIL THE SOLDIER RECORD HAS BEEN PROVISIONED.','warning')
        return redirect(url_for('recruiting_case_archive',case_id=case_id))
    if not case.get('discord_user_id'):
        flash('LOGIN INFORMATION CANNOT BE SENT UNTIL A DISCORD ACCOUNT IS LINKED TO THIS RECRUITING CASE.','warning')
        return redirect(url_for('recruiting_case_archive',case_id=case_id))
    recoverable=bool(case.get('credentials_pending_field_code_enc'))
    previously_sent=bool(case.get('credentials_sent_at'))
    rotate=bool(request.form.get('rotate_field_code') == 'YES')
    if previously_sent and not recoverable and not rotate:
        flash('THE ORIGINAL FIELD CODE IS NO LONGER RECOVERABLE AFTER A SUCCESSFUL DM. CHECK “ROTATE FIELD CODE” TO ISSUE A NEW CODE AND RESEND SAFELY.','warning')
        return redirect(url_for('recruiting_case_archive',case_id=case_id))
    authority=session.get('display_name') or session.get('username') or 'BATTALION HEADQUARTERS'
    execute("""UPDATE recruiting_cases SET credentials_resend_requested_at=NOW(),credentials_resend_requested_by=%s,
               credentials_resend_rotate=%s,credentials_delivery_error=NULL,credentials_last_attempt_at=NULL,updated_at=NOW() WHERE id=%s""",
            (authority,rotate,case_id))
    execute("""INSERT INTO recruit_credential_delivery_events(recruiting_case_id,event_type,event_status,discord_user_id,detail,authority)
               VALUES(%s,'RESEND','REQUESTED',%s,%s,%s)""",
            (case_id,case.get('discord_user_id'),'Field Code rotation authorized.' if rotate else 'Resend requested using pending credentials.',authority))
    try:
        staff_log('HQ','LOGIN CREDENTIAL RESEND REQUESTED',f"{case.get('case_number')} queued for Battalion Clerk login DM resend.",authority,
                  case.get('personnel_id'),details={'rotate_field_code':rotate,'discord_user_id':case.get('discord_user_id')})
    except Exception:
        log.exception('Credential resend audit log failed for %s',case_id)
    flash('LOGIN INFORMATION RESEND QUEUED. BATTALION CLERK SHOULD PROCESS IT WITHIN ABOUT 10 SECONDS.','success')
    return redirect(url_for('recruiting_case_archive',case_id=case_id))

@app.post('/staff/personnel/<personnel_id>/send-login-info')
@login_required
def staff_personnel_send_login_info(personnel_id):
    """Command/S-1 convenience action: queue the canonical recruiting credential DM."""
    if session.get('access_role') not in {'s1','battalion_hq','commander','admin'}:
        abort(403)
    person=fetch_one("SELECT id,rank_code,first_name,last_name FROM personnel WHERE id=%s",(personnel_id,))
    if not person:
        abort(404)
    case=fetch_one("""SELECT * FROM recruiting_cases WHERE personnel_id=%s
                      ORDER BY approved_at DESC NULLS LAST,created_at DESC LIMIT 1""",(personnel_id,))
    if not case:
        flash('NO RECRUITING CASE IS LINKED TO THIS SOLDIER. LOGIN DELIVERY CANNOT BE SENT THROUGH BATTALION CLERK.','warning')
        return redirect(request.referrer or url_for('personnel_service_record',personnel_id=personnel_id))
    if not case.get('discord_user_id'):
        flash('LOGIN INFORMATION CANNOT BE SENT UNTIL A DISCORD ACCOUNT IS LINKED TO THIS SOLDIER.','warning')
        return redirect(request.referrer or url_for('personnel_service_record',personnel_id=personnel_id))
    recoverable=bool(case.get('credentials_pending_field_code_enc'))
    previously_sent=bool(case.get('credentials_sent_at'))
    rotate=bool(request.form.get('rotate_field_code') == 'YES')
    if previously_sent and not recoverable and not rotate:
        flash('THE PREVIOUS FIELD CODE WAS SECURELY DISCARDED AFTER DELIVERY. CHECK ROTATE FIELD CODE AND SEND AGAIN TO ISSUE A NEW LOGIN.','warning')
        return redirect(request.referrer or url_for('personnel_service_record',personnel_id=personnel_id))
    authority=session.get('display_name') or session.get('username') or 'BATTALION HEADQUARTERS'
    execute("""UPDATE recruiting_cases SET credentials_resend_requested_at=NOW(),credentials_resend_requested_by=%s,
               credentials_resend_rotate=%s,credentials_delivery_error=NULL,credentials_last_attempt_at=NULL,updated_at=NOW() WHERE id=%s""",
            (authority,rotate,case['id']))
    execute("""INSERT INTO recruit_credential_delivery_events(recruiting_case_id,event_type,event_status,discord_user_id,detail,authority)
               VALUES(%s,'MANUAL_SEND','REQUESTED',%s,%s,%s)""",
            (case['id'],case.get('discord_user_id'),'Field Code rotation authorized from 201 File.' if rotate else 'Manual login delivery requested from 201 File.',authority))
    try:
        staff_log('HQ','MANUAL LOGIN CREDENTIAL DELIVERY REQUESTED',
                  f"{case.get('case_number')} queued from the Soldier 201 File for Battalion Clerk DM delivery.",
                  authority,personnel_id,details={'rotate_field_code':rotate,'discord_user_id':case.get('discord_user_id')})
    except Exception:
        log.exception('201 File manual credential delivery audit failed for %s',personnel_id)
    flash('LOGIN INFORMATION QUEUED FOR BATTALION CLERK DELIVERY. STATUS WILL UPDATE AFTER THE BOT ATTEMPTS THE DM.','success')
    return redirect(request.referrer or url_for('personnel_service_record',personnel_id=personnel_id))

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


@app.post('/internal/clerk/progression/recheck')
def clerk_progression_recheck():
    """Reconcile HLL-driven progression for every active Soldier in one idempotent pass.

    This is the bridge between durable HLL telemetry and the Website systems that
    consume it.  It never automatically promotes a Soldier.
    """
    if not _clerk_authorized():
        return {"ok":False,"error":"authorization required"},401
    data=request.get_json(silent=True) or {}
    requested=data.get('personnel_id')
    if requested:
        people=fetch_all("SELECT * FROM personnel WHERE id=%s AND separated_at IS NULL AND archived=FALSE",(requested,))
    else:
        people=fetch_all("""SELECT * FROM personnel
                           WHERE separated_at IS NULL AND archived=FALSE
                             AND COALESCE(lifecycle_state,'') NOT IN ('SEPARATED','ARCHIVED')
                           ORDER BY last_name,first_name""")
    summary={"checked":0,"readiness_rechecked":0,"mos_rechecked":0,"ribbons_rechecked":0,
             "ribbons_awarded":0,"promotion_paths_rechecked":0,"errors":[]}
    for person in people:
        pid=person.get('id')
        summary['checked']+=1
        try:
            sync_readiness(person)
            summary['readiness_rechecked']+=1
            person=fetch_one("SELECT * FROM personnel WHERE id=%s",(pid,)) or person
        except Exception as exc:
            summary['errors'].append({"personnel_id":str(pid),"system":"readiness","error":str(exc)[:180]})
        try:
            if person.get('mos_code'):
                sync_mos_proficiency(person)
                summary['mos_rechecked']+=1
        except Exception as exc:
            summary['errors'].append({"personnel_id":str(pid),"system":"mos","error":str(exc)[:180]})
        try:
            before=int((fetch_one("SELECT COUNT(*) total FROM personnel_ribbons WHERE personnel_id=%s",(pid,)) or {}).get('total') or 0)
            ribbon_progress_for(pid,award_completed=True)
            after=int((fetch_one("SELECT COUNT(*) total FROM personnel_ribbons WHERE personnel_id=%s",(pid,)) or {}).get('total') or 0)
            summary['ribbons_rechecked']+=1
            summary['ribbons_awarded']+=max(0,after-before)
        except Exception as exc:
            summary['errors'].append({"personnel_id":str(pid),"system":"ribbons","error":str(exc)[:180]})
        try:
            person=fetch_one("SELECT * FROM personnel WHERE id=%s",(pid,)) or person
            promotion_eligibility(soldier_view(person))
            summary['promotion_paths_rechecked']+=1
        except Exception as exc:
            summary['errors'].append({"personnel_id":str(pid),"system":"promotion","error":str(exc)[:180]})
    summary['ok']=len(summary['errors'])==0
    summary['error_count']=len(summary['errors'])
    try:
        linked=int((fetch_one("SELECT COUNT(*) total FROM hll_personnel_links") or {'total':0}).get('total') or 0)
        summary['hll_linked']=linked
        execute("""CREATE TABLE IF NOT EXISTS career_reconciliation_status(
                    id INTEGER PRIMARY KEY DEFAULT 1,last_run_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    checked INTEGER NOT NULL DEFAULT 0,linked INTEGER NOT NULL DEFAULT 0,
                    readiness_updates INTEGER NOT NULL DEFAULT 0,mos_updates INTEGER NOT NULL DEFAULT 0,
                    ribbons_awarded INTEGER NOT NULL DEFAULT 0,error_count INTEGER NOT NULL DEFAULT 0)""")
        execute("""INSERT INTO career_reconciliation_status(id,last_run_at,checked,linked,readiness_updates,mos_updates,ribbons_awarded,error_count)
                   VALUES(1,NOW(),%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(id) DO UPDATE SET last_run_at=NOW(),checked=EXCLUDED.checked,linked=EXCLUDED.linked,
                     readiness_updates=EXCLUDED.readiness_updates,mos_updates=EXCLUDED.mos_updates,
                     ribbons_awarded=EXCLUDED.ribbons_awarded,error_count=EXCLUDED.error_count""",
                (summary['checked'],linked,summary['readiness_rechecked'],summary['mos_rechecked'],summary['ribbons_awarded'],summary['error_count']))
    except Exception:
        log.exception('Progression reconciliation status write failed')
    return summary


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
    people=fetch_all("SELECT id FROM personnel WHERE separated_at IS NULL AND archived=FALSE")
    automatic_ribbon_recheck([p['id'] for p in people])
    return {'ok':True,'checked':len(people)}


@app.get('/internal/clerk/automation/server-inactivity')
def clerk_server_inactivity():
    """Return HLL-authoritative inactivity state for linked Discord Soldiers.

    Discord voice timestamps are intentionally not consulted.
    """
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    guild_id=request.args.get('guild_id')
    if not guild_id: return {'ok':False,'error':'guild_id required'},400
    rows=fetch_all("""SELECT p.*,w.discord_user_id FROM personnel p
                      JOIN website_member_links w ON w.personnel_id=p.id::text
                      WHERE w.guild_id=%s AND p.archived=FALSE AND p.separated_at IS NULL
                      ORDER BY p.last_name,p.first_name""",(str(guild_id),))
    out=[]
    for p in rows:
        snap=server_activity_snapshot(p)
        out.append({'personnel_id':str(p['id']),'discord_user_id':str(p.get('discord_user_id') or ''),
                    'rank_code':p.get('rank_code'),'first_name':p.get('first_name'),'last_name':p.get('last_name'),
                    'state':snap.get('state'),'days':int(snap.get('days') or 0),'seconds_7d':int(snap.get('seconds_7d') or 0),
                    'last_activity':snap.get('last_activity').isoformat() if hasattr(snap.get('last_activity'),'isoformat') else None,
                    'excused':bool(snap.get('excused')),'linked':bool(snap.get('linked'))})
    return {'ok':True,'items':out,'count':len(out),'source':'HLL: VIETNAM SERVER'}


def _discord_recruit_active_case(discord_user_id, guild_id=None):
    if not discord_user_id:
        return None
    return fetch_one("""SELECT id,case_number,public_token,status,application_source,created_at
                      FROM recruiting_cases
                      WHERE discord_user_id=%s
                        AND status NOT IN ('DENIED','CLOSED','ENLISTED')
                        AND (%s IS NULL OR guild_id=%s OR guild_id IS NULL)
                      ORDER BY created_at DESC LIMIT 1""",(discord_user_id,guild_id,guild_id))


def _resolve_recruiter_text(value, guild_id=None):
    """Best-effort referral resolution. Ambiguous text is retained for staff review."""
    raw=str(value or '').strip()
    if not raw or raw.upper() in {'NONE','NO','N/A','NA','NOT APPLICABLE'}:
        return None,None
    mention=raw.strip('<@!>')
    if mention.isdigit():
        row=fetch_one("""SELECT p.id,p.rank_code,p.first_name,p.last_name
                         FROM website_member_links w JOIN personnel p ON p.id::text=w.personnel_id::text
                         WHERE w.discord_user_id=%s AND (%s IS NULL OR w.guild_id=%s)
                           AND p.separated_at IS NULL LIMIT 1""",(int(mention),guild_id,guild_id))
        if row: return row['id'], raw
    q=raw.lower().lstrip('@')
    rows=fetch_all("""SELECT DISTINCT p.id,p.rank_code,p.first_name,p.last_name
                      FROM personnel p
                      LEFT JOIN website_member_links w ON w.personnel_id::text=p.id::text
                      LEFT JOIN discord_members dm ON dm.guild_id=w.guild_id AND dm.discord_user_id=w.discord_user_id
                      WHERE p.separated_at IS NULL AND (
                        LOWER(COALESCE(p.first_name,'')||' '||COALESCE(p.last_name,''))=%s OR
                        LOWER(COALESCE(p.last_name,''))=%s OR LOWER(COALESCE(dm.username,''))=%s OR
                        LOWER(COALESCE(dm.display_name,''))=%s)
                      LIMIT 2""",(q,q,q,q)) or []
    return (rows[0]['id'],raw) if len(rows)==1 else (None,raw)


@app.post('/internal/clerk/recruiting/intake/start')
def clerk_recruiting_intake_start():
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    data=request.get_json(silent=True) or {}
    uid=data.get('discord_user_id'); gid=data.get('guild_id')
    if not uid or not gid: return {'ok':False,'error':'discord_user_id and guild_id required'},400
    linked=fetch_one("SELECT personnel_id FROM website_member_links WHERE guild_id=%s AND discord_user_id=%s",(gid,uid))
    if linked:
        return {'ok':True,'existing_member':True,'message':'This Discord account is already linked to an active Soldier Record.'}
    case=_discord_recruit_active_case(uid,gid)
    if case:
        return {'ok':True,'existing_case':True,'case':case}
    execute("""INSERT INTO discord_recruiting_intake(guild_id,discord_user_id,discord_username,discord_display_name,status,current_step,updated_at)
               VALUES(%s,%s,%s,%s,'IN_PROGRESS',1,NOW())
               ON CONFLICT(guild_id,discord_user_id) DO UPDATE SET
                 discord_username=EXCLUDED.discord_username,discord_display_name=EXCLUDED.discord_display_name,
                 status=CASE WHEN discord_recruiting_intake.status='SUBMITTED' THEN discord_recruiting_intake.status ELSE 'IN_PROGRESS' END,
                 updated_at=NOW()""",(gid,uid,(data.get('username') or '')[:100],(data.get('display_name') or '')[:100]))
    draft=fetch_one("SELECT current_step,status,answers FROM discord_recruiting_intake WHERE guild_id=%s AND discord_user_id=%s",(gid,uid)) or {}
    return {'ok':True,'draft':draft}


@app.post('/internal/clerk/recruiting/intake/save')
def clerk_recruiting_intake_save():
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    data=request.get_json(silent=True) or {}; uid=data.get('discord_user_id'); gid=data.get('guild_id')
    answers=data.get('answers') or {}; step=int(data.get('current_step') or 1)
    if not uid or not gid or not isinstance(answers,dict): return {'ok':False,'error':'invalid intake payload'},400
    import json as _json
    execute("""UPDATE discord_recruiting_intake
               SET answers=COALESCE(answers,'{}'::jsonb) || %s::jsonb,current_step=%s,status='IN_PROGRESS',updated_at=NOW()
               WHERE guild_id=%s AND discord_user_id=%s""",(_json.dumps(answers),step,gid,uid))
    return {'ok':True,'current_step':step}


@app.get('/internal/clerk/recruiting/intake/status')
def clerk_recruiting_intake_status():
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    uid=request.args.get('discord_user_id',type=int); gid=request.args.get('guild_id',type=int)
    if not uid or not gid: return {'ok':False,'error':'discord_user_id and guild_id required'},400
    case=_discord_recruit_active_case(uid,gid)
    if case: return {'ok':True,'existing_case':True,'case':case}
    row=fetch_one("SELECT current_step,status,answers,updated_at FROM discord_recruiting_intake WHERE guild_id=%s AND discord_user_id=%s",(gid,uid))
    return {'ok':True,'draft':row,'exists':bool(row)}


@app.post('/internal/clerk/recruiting/intake/connect-existing')
def clerk_recruiting_intake_connect_existing():
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    data=request.get_json(silent=True) or {}
    uid=data.get('discord_user_id'); gid=data.get('guild_id')
    case_number=str(data.get('case_number') or '').strip().upper()
    verification_code=str(data.get('verification_code') or '').strip().upper()
    username=str(data.get('username') or '').strip()[:100]
    if not uid or not gid or not case_number or not verification_code:
        return {'ok':False,'error':'case number and verification code are required'},400
    linked=fetch_one("SELECT personnel_id FROM website_member_links WHERE guild_id=%s AND discord_user_id=%s",(gid,uid))
    if linked: return {'ok':False,'error':'this Discord account is already linked to a Soldier Record'},409
    case=fetch_one("SELECT * FROM recruiting_cases WHERE UPPER(case_number)=UPPER(%s)",(case_number,))
    if not case: return {'ok':False,'error':'recruiting case not found'},404
    if str(case.get('status') or '').upper() in {'DENIED','CLOSED'}:
        return {'ok':False,'error':'that recruiting case is closed'},409
    if case.get('discord_user_id') and int(case.get('discord_user_id')) != int(uid):
        return {'ok':False,'error':'that recruiting case is already linked to another Discord account'},409
    stored=str(case.get('verification_code') or '').strip().upper()
    if not stored or stored != verification_code:
        return {'ok':False,'error':'verification code does not match that recruiting case'},403
    if case.get('verification_expires_at') and case['verification_expires_at'] < datetime.now(timezone.utc):
        return {'ok':False,'error':'verification code expired; use the website status page or contact Recruiting'},410
    conflict=_discord_recruit_active_case(uid,gid)
    if conflict and str(conflict.get('id')) != str(case.get('id')):
        return {'ok':False,'error':f"this Discord account is already linked to active application {conflict.get('case_number')}"},409
    new_status='PENDING_COMMAND' if str(case.get('status') or '').upper() in {'SUBMITTED','DISCORD_VERIFICATION_PENDING','DISCORD_VERIFIED'} else case.get('status')
    execute("""UPDATE recruiting_cases SET discord_user_id=%s,guild_id=%s,discord_verified_username=%s,verification_used_at=COALESCE(verification_used_at,NOW()),discord_joined_at=COALESCE(discord_joined_at,NOW()),status=%s,updated_at=NOW() WHERE id=%s""",
            (uid,gid,username or case.get('discord_username_input'),new_status,case['id']))
    return {'ok':True,'case':{'id':str(case['id']),'case_number':case['case_number'],'public_token':case.get('public_token'),'status':new_status}}


@app.post('/internal/clerk/recruiting/intake/submit')
def clerk_recruiting_intake_submit():
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    data=request.get_json(silent=True) or {}; uid=data.get('discord_user_id'); gid=data.get('guild_id')
    if not uid or not gid: return {'ok':False,'error':'discord_user_id and guild_id required'},400
    existing=_discord_recruit_active_case(uid,gid)
    if existing: return {'ok':True,'existing_case':True,'case':existing}
    row=fetch_one("SELECT * FROM discord_recruiting_intake WHERE guild_id=%s AND discord_user_id=%s",(gid,uid))
    if not row: return {'ok':False,'error':'application draft not found'},404
    a=row.get('answers') or {}
    required=['timezone_name','game_platform','game_identity','hll_experience','role_interest','looking_for','play_style','follows_chain','participation']
    missing=[k for k in required if str(a.get(k) or '').strip()=='']
    if missing: return {'ok':False,'error':'missing required answers: '+', '.join(missing)},400
    platform=str(a.get('game_platform') or '').strip().upper()
    identity=str(a.get('game_identity') or '').strip()
    if platform not in {'STEAM','XBOX','PS5'}: return {'ok':False,'error':'platform must be STEAM, XBOX, or PS5'},400
    if platform=='STEAM' and not (identity.isdigit() and len(identity)==17): return {'ok':False,'error':'SteamID64 must be exactly 17 digits'},400
    if platform in {'XBOX','PS5'} and len(identity)<2: return {'ok':False,'error':'console Gamertag / PSN ID is required'},400
    duplicate=fetch_one("""SELECT case_number,public_token FROM recruiting_cases
                         WHERE UPPER(COALESCE(game_platform,''))=%s
                           AND LOWER(COALESCE(game_identity,steam_id64,''))=LOWER(%s)
                           AND status NOT IN ('DENIED','CLOSED','ENLISTED') LIMIT 1""",(platform,identity))
    if duplicate: return {'ok':False,'error':f"That game identity is already attached to active application {duplicate['case_number']}."},409
    identity_conflict=_recruit_game_identity_conflict({'game_platform':platform,'game_identity':identity,'steam_id64':identity if platform=='STEAM' else None})
    if identity_conflict: return {'ok':False,'error':identity_conflict['error']},409
    age=None
    try:
        if str(a.get('age') or '').strip(): age=int(str(a.get('age')).strip())
    except ValueError: return {'ok':False,'error':'age must be a number or blank'},400
    chain=str(a.get('follows_chain') or '').strip().upper()
    follows=chain in {'YES','Y','TRUE','I WILL FOLLOW THE CHAIN OF COMMAND'}
    if chain not in {'YES','Y','TRUE','I WILL FOLLOW THE CHAIN OF COMMAND','NO','N','FALSE'}:
        return {'ok':False,'error':'chain of command answer must be YES or NO'},400
    recruiter_id,recruiter_text=_resolve_recruiter_text(a.get('recruited_by'),gid)
    case_number=_recruit_case_number(); public_token=secrets.token_urlsafe(24)
    steam=identity if platform=='STEAM' else None
    username=row.get('discord_username') or str(uid); display=row.get('discord_display_name') or username
    notes=str(a.get('applicant_notes') or '').strip() or None
    case=fetch_one("""INSERT INTO recruiting_cases
        (case_number,public_token,discord_username_input,discord_user_id,discord_verified_username,guild_id,
         age,timezone_name,steam_id64,game_platform,game_identity,hll_experience,role_interest,looking_for,play_style,
         follows_chain,participation,applicant_notes,recruited_by_personnel_id,recruited_by_text,status,application_source,
         discord_oauth_linked_at,discord_joined_at)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING_COMMAND','DISCORD',NOW(),NOW())
        RETURNING id,case_number,public_token,status""",
        (case_number,public_token,username,uid,display,gid,age,str(a['timezone_name']).strip(),steam,platform,identity,
         str(a['hll_experience']).strip(),str(a['role_interest']).strip(),str(a['looking_for']).strip(),str(a['play_style']).strip(),
         follows,str(a['participation']).strip(),notes,recruiter_id,recruiter_text))
    execute("""UPDATE discord_recruiting_intake SET status='SUBMITTED',submitted_case_id=%s,submitted_at=NOW(),current_step=4,updated_at=NOW()
               WHERE guild_id=%s AND discord_user_id=%s""",(case['id'],gid,uid))
    return {'ok':True,'case':case,'status_url':url_for('recruiting_status',token=public_token,_external=True,_scheme='https')}


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


@app.get('/internal/clerk/recruiting/pending-entry')
def clerk_recruiting_pending_entry():
    """Applicants are admitted to Discord as soon as the website application is filed.

    This endpoint deliberately returns only pre-approval Recruiting Cases. No 201 File,
    Battle Roster credential, Replacement status, rank, MOS, or unit assignment is created
    here. Discord is the communications bridge while Command reviews the application.
    """
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    guild_id=request.args.get('guild_id',type=int)
    rows=fetch_all("""SELECT id,case_number,public_token,discord_user_id,discord_verified_username,status,
                             discord_joined_at,discord_join_error,discord_join_last_attempt_at
                      FROM recruiting_cases
                      WHERE status IN ('SUBMITTED','DISCORD_VERIFIED','PENDING_COMMAND','MORE_INFO_REQUIRED')
                        AND discord_user_id IS NOT NULL
                        AND (guild_id=%s OR guild_id IS NULL)
                        AND (discord_joined_at IS NULL
                             AND (discord_join_last_attempt_at IS NULL OR discord_join_last_attempt_at < NOW()-INTERVAL '2 minutes'))
                      ORDER BY created_at ASC""",(guild_id,))
    return {'ok':True,'cases':rows}


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




def _normalized_game_identity(platform: str, identity: str) -> str:
    platform=str(platform or '').strip().upper()
    identity=str(identity or '').strip()
    return identity if platform == 'STEAM' else identity.casefold()


def _recruit_game_identity_conflict(case: dict, *, personnel_id=None) -> dict | None:
    """Return a human-readable identity conflict without mutating personnel data."""
    if not case:
        return None
    platform=str(case.get('game_platform') or '').strip().upper()
    identity=str(case.get('game_identity') or case.get('steam_id64') or '').strip()
    if platform not in {'STEAM','XBOX','PS5'} or not identity:
        return None
    normalized=_normalized_game_identity(platform,identity)
    pid=str(personnel_id or case.get('personnel_id') or '').strip()
    if platform == 'STEAM':
        try:
            linked=fetch_one("SELECT personnel_id FROM hll_personnel_links WHERE steam_id=%s LIMIT 1",(identity,))
            if linked and str(linked.get('personnel_id') or '') != pid:
                return {'error':f'SteamID64 {identity} is already linked to another Soldier Record.'}
            owned=fetch_one("SELECT steam_id FROM hll_personnel_links WHERE personnel_id::text=%s LIMIT 1",(pid,)) if pid else None
            if owned and str(owned.get('steam_id') or '') != identity:
                return {'error':'This Soldier Record already owns a different HLL identity. Staff must unlink or repair it before approval.'}
        except Exception:
            # Battalion Clerk creates this table. A pending claim still provides
            # duplicate protection if the telemetry table has not been initialized yet.
            pass
    try:
        claim=fetch_one("""SELECT personnel_id,platform,claimed_identity FROM hll_identity_claims
                           WHERE platform=%s AND normalized_identity=%s
                             AND status IN ('PENDING','LINKED','VERIFIED')
                           ORDER BY created_at DESC LIMIT 1""",(platform,normalized))
        if claim and str(claim.get('personnel_id') or '') != pid:
            return {'error':f'{platform} identity {identity} is already claimed by another Soldier Record.'}
    except Exception:
        pass
    return None


def _queue_recruit_game_identity(case: dict, personnel_id) -> dict:
    """Attach/queue the applicant game account when Command approves the case.

    SteamID64 is durable and can be linked immediately. Console names are filed
    as pending claims; Battalion Clerk promotes the claim to VERIFIED the first
    time the exact account is observed on the unit server.
    """
    platform=str(case.get('game_platform') or '').strip().upper()
    identity=str(case.get('game_identity') or case.get('steam_id64') or '').strip()
    if platform not in {'STEAM','XBOX','PS5'} or not identity:
        return {'ok':True,'status':'NOT PROVIDED'}
    if platform == 'STEAM' and not (identity.isdigit() and len(identity)==17):
        return {'ok':False,'error':'SteamID64 must be exactly 17 digits.'}
    conflict=_recruit_game_identity_conflict(case,personnel_id=personnel_id)
    if conflict:
        execute("UPDATE recruiting_cases SET game_identity_link_status='CONFLICT',game_identity_link_error=%s,updated_at=NOW() WHERE id=%s",(conflict['error'],case['id']))
        return {'ok':False,**conflict}
    normalized=_normalized_game_identity(platform,identity)
    discord_user_id=str(case.get('discord_user_id') or '') or None
    # Keep exactly one current application claim per Soldier.
    existing=fetch_one("""SELECT id FROM hll_identity_claims
                          WHERE personnel_id::text=%s AND status IN ('PENDING','LINKED','VERIFIED')
                          ORDER BY created_at DESC LIMIT 1""",(str(personnel_id),))
    if existing:
        execute("""UPDATE hll_identity_claims SET recruiting_case_id=%s,platform=%s,claimed_identity=%s,normalized_identity=%s,
                   discord_user_id=%s,status='PENDING',linked_player_key=NULL,error=NULL,linked_at=NULL,updated_at=NOW()
                   WHERE id=%s""",(case['id'],platform,identity,normalized,discord_user_id,existing['id']))
    else:
        execute("""INSERT INTO hll_identity_claims(recruiting_case_id,personnel_id,discord_user_id,platform,claimed_identity,normalized_identity,status)
                   VALUES(%s,%s,%s,%s,%s,%s,'PENDING')""",(case['id'],str(personnel_id),discord_user_id,platform,identity,normalized))
    status='PENDING SERVER VERIFICATION'
    if platform == 'STEAM':
        try:
            execute("""INSERT INTO hll_personnel_links(steam_id,personnel_id,discord_user_id,platform,platform_user_id,linked_by,verified,updated_at)
                       VALUES(%s,%s,%s,'STEAM',%s,'RECRUITING APPROVAL',TRUE,NOW())
                       ON CONFLICT(steam_id) DO UPDATE SET personnel_id=EXCLUDED.personnel_id,discord_user_id=EXCLUDED.discord_user_id,
                           platform='STEAM',platform_user_id=EXCLUDED.platform_user_id,linked_by='RECRUITING APPROVAL',verified=TRUE,updated_at=NOW()""",
                    (identity,str(personnel_id),discord_user_id,identity))
            execute("""UPDATE hll_identity_claims SET status='VERIFIED',linked_player_key=%s,linked_at=NOW(),updated_at=NOW()
                       WHERE personnel_id::text=%s AND normalized_identity=%s AND platform='STEAM'""",(identity,str(personnel_id),normalized))
            status='VERIFIED'
        except Exception as exc:
            # Leave the claim pending. Battalion Clerk will finish it on its next
            # telemetry cycle even if the RCON tables were not ready at approval time.
            log.warning('Steam identity queued for Clerk reconciliation case=%s: %s',case.get('id'),exc)
    execute("""UPDATE recruiting_cases SET game_identity_link_status=%s,game_identity_link_error=NULL,
               game_identity_linked_at=CASE WHEN %s='VERIFIED' THEN NOW() ELSE game_identity_linked_at END,updated_at=NOW() WHERE id=%s""",
            (status,status,case['id']))
    return {'ok':True,'status':status,'platform':platform,'identity':identity}


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
            try: ensure_welcome_packet(personnel_id,case.get('id'))
            except Exception: log.exception('Welcome Packet ensure failed for existing replacement %s',personnel_id)
            identity_link=_queue_recruit_game_identity(case,personnel_id)
            return {'ok':True,'created':False,'personnel_id':str(personnel_id),'personnel':person,'identity_link':identity_link}

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
        f"Approved recruit entered on the Battle Roster as PVT and placed in Replacement Detachment under Recruiting Case {case.get('case_number') or 'N/A'}. Permanent MOS and formation assignment pending.",
        'BATTALION HEADQUARTERS')
    initial_order=replacement_orders_for(person['id'])
    enqueue_discord_role_sync(person['id'],'APPROVED REPLACEMENT PROVISIONED')
    staff_log('S-1','NEW REPLACEMENT',f"PVT {person.get('last_name','')} entered Replacement Detachment",'BATTALION CLERK',person['id'],
              (initial_order or {}).get('document_number') if initial_order else None,{'case_number':case.get('case_number')})
    try:
        ensure_welcome_packet(person['id'],case.get('id'))
    except Exception:
        log.exception('Welcome Packet generation failed for new replacement %s',person['id'])
    identity_link=_queue_recruit_game_identity(case,person['id'])
    return {'ok':True,'created':True,'personnel_id':str(person['id']),'personnel':person,'roster':card,'field_code':field_code,
            'initial_order':initial_order,'identity_link':identity_link}


def _ensure_recruit_login_delivery(case: dict, provision: dict) -> dict:
    """Return stable one-time plaintext credentials until successful DM delivery is recorded."""
    if not provision.get("ok"):
        return provision
    current=fetch_one("SELECT credentials_sent_at,credentials_pending_field_code_enc,credentials_resend_requested_at,credentials_resend_rotate FROM recruiting_cases WHERE id=%s",(case["id"],)) or {}
    sent_at=current.get("credentials_sent_at")
    resend_at=current.get("credentials_resend_requested_at")
    resend_pending=bool(resend_at and (not sent_at or resend_at > sent_at))
    provision["credentials_resend_pending"]=resend_pending
    if sent_at and not resend_pending:
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
        # Plaintext is intentionally discarded after a successful DM. A resend
        # may rotate the Field Code only when Command explicitly authorized it.
        if sent_at and resend_pending and not current.get("credentials_resend_rotate"):
            provision["ok"]=False
            provision["error"]="Original Field Code is no longer recoverable; Command must authorize Field Code rotation before resend."
            return provision
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
    allowed_join_statuses={'SUBMITTED','DISCORD_VERIFIED','PENDING_COMMAND','MORE_INFO_REQUIRED','REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING'}
    if str(case.get('status') or '').upper() not in allowed_join_statuses:
        return {'ok':False,'error':'recruiting case is not eligible for Discord entry'},409
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
    case=fetch_one("SELECT discord_user_id,credentials_resend_requested_at,credentials_sent_at FROM recruiting_cases WHERE id=%s",(case_id,)) or {}
    execute("""UPDATE recruiting_cases SET credentials_sent_at=CASE WHEN %s THEN NOW() ELSE credentials_sent_at END,
               credentials_delivery_error=%s,credentials_last_attempt_at=NOW(),
               credentials_pending_field_code_enc=CASE WHEN %s THEN NULL ELSE credentials_pending_field_code_enc END,
               credentials_resend_requested_at=CASE WHEN %s THEN NULL ELSE credentials_resend_requested_at END,
               credentials_resend_requested_by=CASE WHEN %s THEN NULL ELSE credentials_resend_requested_by END,
               credentials_resend_rotate=CASE WHEN %s THEN FALSE ELSE credentials_resend_rotate END,
               discord_notified_at=CASE WHEN %s THEN COALESCE(discord_notified_at,NOW()) ELSE discord_notified_at END,updated_at=NOW() WHERE id=%s""",
            (sent,None if sent else error,sent,sent,sent,sent,sent,case_id))
    execute("""INSERT INTO recruit_credential_delivery_events(recruiting_case_id,event_type,event_status,discord_user_id,detail,authority)
               VALUES(%s,'DM',%s,%s,%s,'BATTALION CLERK')""",
            (case_id,'SENT' if sent else 'ERROR',case.get('discord_user_id'),None if sent else error))
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
    rows=fetch_all("""SELECT id,case_number,public_token,discord_user_id,discord_verified_username,status,discord_notified_at,discord_joined_at,discord_join_error,discord_join_last_attempt_at,credentials_sent_at,credentials_delivery_error,credentials_last_attempt_at,credentials_resend_requested_at,credentials_resend_requested_by,credentials_resend_rotate,
                             (credentials_resend_requested_at IS NOT NULL AND (credentials_sent_at IS NULL OR credentials_resend_requested_at > credentials_sent_at)) AS credentials_resend_pending,personnel_id
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


@app.get('/internal/clerk/recruiting/credential-resends')
def clerk_recruiting_credential_resends():
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    guild_id=request.args.get('guild_id',type=int)
    rows=fetch_all("""SELECT id,case_number,public_token,discord_user_id,discord_verified_username,status,personnel_id,credentials_sent_at,
                             credentials_resend_requested_at,credentials_resend_requested_by,credentials_resend_rotate,
                             TRUE AS credentials_resend_pending
                      FROM recruiting_cases
                      WHERE discord_user_id IS NOT NULL AND personnel_id IS NOT NULL
                        AND (guild_id=%s OR guild_id IS NULL)
                        AND credentials_resend_requested_at IS NOT NULL
                        AND (credentials_sent_at IS NULL OR credentials_resend_requested_at > credentials_sent_at)
                        AND (credentials_last_attempt_at IS NULL OR credentials_last_attempt_at < NOW()-INTERVAL '5 minutes')
                      ORDER BY credentials_resend_requested_at ASC""",(guild_id,))
    return {'ok':True,'cases':rows}

@app.post('/internal/clerk/recruiting/<case_id>/resend-credentials')
def clerk_recruiting_resend_credentials(case_id):
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    case=fetch_one("SELECT * FROM recruiting_cases WHERE id=%s",(case_id,))
    if not case: return {'ok':False,'error':'recruiting case not found'},404
    if not case.get('personnel_id') or not case.get('discord_user_id'):
        return {'ok':False,'error':'Soldier record and Discord identity are required'},409
    requested=case.get('credentials_resend_requested_at')
    sent_at=case.get('credentials_sent_at')
    if not requested or (sent_at and requested <= sent_at):
        return {'ok':False,'error':'No credential resend is currently pending'},409
    card=fetch_one("SELECT * FROM battle_roster_cards WHERE personnel_id=%s AND is_active=TRUE",(case.get('personnel_id'),))
    if not card:
        return {'ok':False,'error':'Active Battle Roster card not found'},409
    field_code=_oauth_decrypt(case.get('credentials_pending_field_code_enc'))
    rotated=False
    if not field_code:
        if not case.get('credentials_resend_rotate'):
            return {'ok':False,'error':'Original Field Code is not recoverable; Command must authorize Field Code rotation'},409
        field_code=_random_field_code()
        execute("UPDATE battle_roster_cards SET field_code_hash=%s WHERE id=%s",(generate_password_hash(field_code),card['id']))
        execute("UPDATE recruiting_cases SET credentials_pending_field_code_enc=%s,updated_at=NOW() WHERE id=%s",(_oauth_encrypt(field_code),case_id))
        rotated=True
    weapon=fetch_one("""SELECT wi.serial_number FROM weapon_issue_history wih JOIN weapon_inventory wi ON wi.id=wih.weapon_id
                        WHERE wih.personnel_id=%s AND wih.is_current=TRUE LIMIT 1""",(case.get('personnel_id'),))
    return {'ok':True,'personnel_id':str(case.get('personnel_id')),'roster_number':card.get('roster_number'),'field_code':field_code,
            'weapon_serial':(weapon or {}).get('serial_number'),'credentials_resend_pending':True,'field_code_rotated':rotated}

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

# Run one idempotent operation repair pass on every deployment/startup. This
# backfills historical verified attendance and missing M16 rounds from the existing
# Battalion Clerk attendance ledger, then removes yesterday's operations from the
# active schedule. Failures are logged but never prevent the website from starting.
try:
    if database_ready():
        _operation_boot_repair=run_operation_maintenance("DEPLOYMENT RECONCILIATION")
        log.info("Operation reconciliation complete: %s",_operation_boot_repair)
except Exception:
    log.exception("Operation reconciliation failed during startup; Clerk maintenance will retry.")

# Reconcile already-recorded HLL field sessions on deployment as well. This is
# idempotent and lets existing server time immediately update issued M16 records.
try:
    if database_ready():
        _hll_m16_boot_repair=reconcile_hll_m16_rounds(None,365)
        log.info("HLL M16 reconciliation complete: %s",_hll_m16_boot_repair)
except Exception:
    log.exception("HLL M16 reconciliation failed during startup; Clerk maintenance will retry.")


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


# ---------------------------------------------------------------------------
# Welcome Packet / New Soldier Onboarding
# ---------------------------------------------------------------------------
WELCOME_PHASES = [
    ("REPLACEMENT_ORIENTATION", "Replacement Detachment Orientation"),
    ("MOVEMENT_ASSIGNMENT", "Movement & Assignment"),
    ("UNIT_ORIENTATION", "Unit Orientation"),
]
WELCOME_TASK_DEFS = [
    ("REVIEW_ASSIGNMENT","COMPANY_ONBOARDING","Review Your Current Status","Confirm that you are currently attached to Replacement Detachment and understand that permanent Company / Platoon assignment occurs only after Command accepts this packet.","member_report_for_duty","SELF",10),
    ("READ_RULES","COMPANY_ONBOARDING","Read Unit Rules / Standing Orders","Read the battalion rules and standing orders, then certify that you understand them.","my_soldier_record","SELF",20),
    ("REVIEW_CHAIN","COMPANY_ONBOARDING","Review Your Chain of Command","Review the Replacement Detachment / Battalion Headquarters chain you use during in-processing. Your permanent Company / Platoon chain will appear after assignment.","member_report_for_duty","SELF",30),
    ("TALKED_TO_NCO","COMPANY_ONBOARDING","Talk to an NCO / Sponsor","Make contact with the Replacement Detachment NCO, assigned sponsor, or another designated 1/5 Cavalry NCO in Discord and confirm the contact here.","member_report_for_duty","SELF",40),
    ("FOUND_UNIT_SERVER","COMPANY_ONBOARDING","Find the 1/5 Cavalry Server","Locate and favorite the unit Hell Let Loose: Vietnam server so you know where to report for field service.",None,"SELF",50),
    ("REVIEW_EXPECTATIONS","COMPANY_ONBOARDING","Review Operations / Equipment Expectations","Review how operations, attendance, readiness, issued M16 responsibility, cleaning, and inspections work.","member_report_for_duty","SELF",60),
    ("DISCORD_LINKED","COMPANY_ONBOARDING","Discord Account Connected","Verified automatically from your linked battalion Discord account.",None,"SYSTEM",70),
    ("STEAM_LINKED","COMPANY_ONBOARDING","HLL / Game Identity Connected","Verified automatically from your linked HLL: Vietnam identity.",None,"SYSTEM",80),
]

WELCOME_TASK_CODES={row[0] for row in WELCOME_TASK_DEFS}

WELCOME_TASK_GUIDANCE = {
    "REVIEW_ASSIGNMENT": {
        "where": "Member Area → Report for Duty",
        "steps": [
            "Review the CURRENT ASSIGNMENT block on your Report for Duty page.",
            "Confirm that Replacement Detachment is your current status while onboarding is open; do not create a second application.",
            "Permanent Company / Platoon assignment will be unlocked only after this packet is completed and accepted by Command."
        ],
    },
    "READ_RULES": {
        "where": "Member Area → 201 File / Standing Orders",
        "steps": [
            "Read the current battalion rules and standing orders.",
            "Review conduct, attendance, operations, and chain-of-command expectations.",
            "Ask your NCO about anything unclear before certifying completion."
        ],
    },
    "REVIEW_CHAIN": {
        "where": "Member Area → Report for Duty / My Unit",
        "steps": [
            "Review the leadership shown with your current assignment.",
            "Know who your first point of contact is while you are in Replacement Detachment.",
            "After Command accepts this packet and S-1 assigns you, My Unit will show the permanent Company → Platoon → Squad → Team chain."
        ],
    },
    "TALKED_TO_NCO": {
        "where": "Report for Duty → Assigned NCO, then Discord",
        "steps": [
            "Contact the Replacement Detachment NCO, an assigned sponsor, or another NCO designated by Headquarters.",
            "Introduce yourself in the 1/5 Cavalry Discord as a new replacement and ask any onboarding questions you have.",
            "Return to the Welcome Packet and certify this item only after actual contact."
        ],
    },
    "FOUND_UNIT_SERVER": {
        "where": "Hell Let Loose: Vietnam → Server Browser",
        "steps": [
            "Open Hell Let Loose: Vietnam and go to the server browser.",
            "Find the 1/5 Cavalry community server and favorite it.",
            "If you cannot find it, ask your NCO before marking this item complete."
        ],
    },
    "REVIEW_EXPECTATIONS": {
        "where": "Member Area → Orders / Operations + Wall Locker → Issued M16",
        "steps": [
            "Review where scheduled operations and training are posted and how attendance/readiness credit works.",
            "Review your issued M16 record, cleaning responsibility, inspection status, and server-hour fouling system.",
            "Confirm you understand both field participation and equipment responsibility before completing this item."
        ],
    },
    "DISCORD_LINKED": {
        "where": "Automatic verification",
        "steps": [
            "No manual checkbox is required.",
            "Use the same Discord account attached to your Recruiting Case and Soldier Record.",
            "If it remains pending, use RECHECK VERIFICATION; contact S-1 only if it still does not clear."
        ],
    },
    "STEAM_LINKED": {
        "where": "Automatic application / server verification",
        "steps": [
            "PC: the SteamID64 filed on your application is linked automatically at approval when valid.",
            "Xbox/PS5: the filed Gamertag/PSN ID remains pending until Battalion Clerk observes and verifies it on the server.",
            "Use /hll-link or /hll-link-console only as repair/fallback commands if automatic linking does not complete."
        ],
    },
}

def _welcome_person(personnel_id):
    return fetch_one("SELECT * FROM personnel WHERE id=%s",(personnel_id,)) if personnel_id else None

def _welcome_company_assigned(person):
    """True only after a real Company assignment is on file.

    Do not trust unit_code alone: legacy personnel rows defaulted to A/1-5 CAV,
    which could activate onboarding before S-1 actually assigned the Soldier.
    Prefer structured organization ancestry and use platoon/squad evidence only as
    a compatibility fallback for older records.
    """
    if not person:
        return False
    node_id=person.get('unit_node_id')
    if node_id:
        try:
            ancestry=unit_ancestry(node_id) or []
            if any(str(n.get('unit_type') or '').strip().lower()=='company' for n in ancestry):
                return True
        except Exception:
            log.exception('Welcome Packet company ancestry check failed personnel=%s',person.get('id'))
    unit=str(person.get('unit_code') or '').strip().upper()
    if not unit or unit in {'1-5 CAV','1-5-CAV','1ST BATTALION, 5TH CAVALRY REGIMENT','REPLACEMENT','REPLACEMENT DETACHMENT','REPLACEMENT DEPOT','UNASSIGNED'}:
        return False
    companyish=(unit.startswith('A/') or unit.startswith('A-') or unit.startswith('B/') or unit.startswith('B-') or unit.startswith('C/') or unit.startswith('C-') or unit.startswith('HHC'))
    formation_evidence=bool(str(person.get('platoon') or '').strip() or str(person.get('squad') or '').strip())
    return bool(companyish and formation_evidence)

def welcome_packet_promotion_open(personnel_id):
    """Promotion eligibility stays closed until Command accepts onboarding."""
    if not personnel_id or not database_ready(): return True
    pkt=fetch_one("SELECT status,approved_at FROM welcome_packets WHERE personnel_id=%s",(personnel_id,))
    if not pkt: return True
    return str(pkt.get('status') or '').upper() in {'COMPLETE','CLOSED','ARCHIVED'} and bool(pkt.get('approved_at'))

def ensure_welcome_packet(personnel_id, recruiting_case_id=None):
    if not personnel_id or not database_ready(): return None
    pkt=fetch_one("SELECT * FROM welcome_packets WHERE personnel_id=%s",(personnel_id,))
    person=_welcome_person(personnel_id) or {}
    created=False
    if not pkt:
        initial_status='IN_PROGRESS'
        pkt=fetch_one("""INSERT INTO welcome_packets(personnel_id,recruiting_case_id,current_phase,status,activated_at)
                         VALUES(%s,%s,'COMPANY_ONBOARDING',%s,NOW()) RETURNING *""",
                      (personnel_id,recruiting_case_id,initial_status))
        created=True
    elif recruiting_case_id and not pkt.get('recruiting_case_id'):
        execute("UPDATE welcome_packets SET recruiting_case_id=%s,updated_at=NOW() WHERE id=%s",(recruiting_case_id,pkt['id']))
        pkt=fetch_one("SELECT * FROM welcome_packets WHERE id=%s",(pkt['id'],))
    for code,phase,title,desc,target,mode,order in WELCOME_TASK_DEFS:
        execute("""INSERT INTO welcome_packet_tasks(packet_id,task_code,phase_code,title,description,target_endpoint,completion_mode,sort_order,status,required)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE)
                   ON CONFLICT(packet_id,task_code) DO UPDATE SET phase_code=EXCLUDED.phase_code,title=EXCLUDED.title,
                   description=EXCLUDED.description,target_endpoint=EXCLUDED.target_endpoint,completion_mode=EXCLUDED.completion_mode,
                   sort_order=EXCLUDED.sort_order,required=TRUE,updated_at=NOW()""",
                (pkt['id'],code,phase,title,desc,target,mode,order,'OPEN'))
    # Retire legacy checklist rows without deleting historical timestamps.
    execute("UPDATE welcome_packet_tasks SET required=FALSE,status=CASE WHEN status='COMPLETE' THEN status ELSE 'RETIRED' END,updated_at=NOW() WHERE packet_id=%s AND NOT (task_code = ANY(%s))",
            (pkt['id'],list(WELCOME_TASK_CODES)))
    return reconcile_welcome_packet(personnel_id)

def _welcome_notify(packet, event_type, title, message, suffix):
    if not packet: return
    key=f"WELCOME:{packet['personnel_id']}:{suffix}"
    execute("""INSERT INTO welcome_packet_notifications(packet_id,personnel_id,event_key,event_type,title,message)
               VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(event_key) DO NOTHING""",
            (packet['id'],packet['personnel_id'],key,event_type,title,message))

def welcome_complete_task(personnel_id, task_code, completed_by='SYSTEM', *, reconcile=True):
    pkt=fetch_one("SELECT * FROM welcome_packets WHERE personnel_id=%s",(personnel_id,))
    if not pkt: return None
    execute("""UPDATE welcome_packet_tasks SET status='COMPLETE',completed_at=COALESCE(completed_at,NOW()),completed_by=COALESCE(completed_by,%s),updated_at=NOW()
               WHERE packet_id=%s AND task_code=%s AND required=TRUE AND status NOT IN ('COMPLETE','WAIVED')""",(completed_by,pkt['id'],task_code))
    execute("UPDATE welcome_packets SET last_activity_at=NOW(),updated_at=NOW() WHERE id=%s",(pkt['id'],))
    return reconcile_welcome_packet(personnel_id) if reconcile else pkt

def welcome_visit(personnel_id, task_code):
    # New onboarding uses explicit member certification instead of passive page visits.
    return

def _welcome_required_counts(packet_id):
    row=fetch_one("""SELECT COUNT(*) FILTER (WHERE required=TRUE) AS total,
                            COUNT(*) FILTER (WHERE required=TRUE AND status IN ('COMPLETE','WAIVED')) AS done
                     FROM welcome_packet_tasks WHERE packet_id=%s""",(packet_id,)) or {}
    return int(row.get('total') or 0),int(row.get('done') or 0)

def reconcile_welcome_packet(personnel_id):
    pkt=fetch_one("SELECT * FROM welcome_packets WHERE personnel_id=%s",(personnel_id,))
    if not pkt: return None
    person=_welcome_person(personnel_id) or {}
    status=str(pkt.get('status') or 'IN_PROGRESS').upper()

    if status=='PENDING_ASSIGNMENT':
        execute("UPDATE welcome_packets SET status='IN_PROGRESS',current_phase='COMPANY_ONBOARDING',activated_at=COALESCE(activated_at,NOW()),updated_at=NOW() WHERE id=%s",(pkt['id'],))
        execute("UPDATE welcome_packet_tasks SET status='OPEN' WHERE packet_id=%s AND required=TRUE AND status='LOCKED'",(pkt['id'],))
        _welcome_notify(pkt,'ACTIVATED','Welcome Packet Activated','Your Replacement Detachment Soldier Record is open. Complete the onboarding checklist while S-1 processes your permanent assignment.','ACTIVATED')
        status='IN_PROGRESS'

    # Automatic evidence checks. Keep these deliberately tolerant because older
    # deployments used slightly different telemetry/link schemas. A verification
    # query must never make the member's Welcome Packet unusable.
    discord_verified=False
    try:
        discord_verified=bool(fetch_one("SELECT 1 AS ok FROM website_member_links WHERE personnel_id::text=%s LIMIT 1",(str(personnel_id),)))
    except Exception:
        log.exception('Welcome Packet Discord link check failed for %s',personnel_id)
    if discord_verified:
        welcome_complete_task(personnel_id,'DISCORD_LINKED','SYSTEM',reconcile=False)

    steam_verified=False
    try:
        steam_verified=bool(fetch_one("SELECT 1 AS ok FROM hll_personnel_links WHERE personnel_id::text=%s AND COALESCE(verified,TRUE)=TRUE AND COALESCE(steam_id::text,'')<>'' LIMIT 1",(str(personnel_id),)))
    except Exception:
        # Compatibility fallback for legacy hll_personnel_links tables without a
        # verified column. A non-empty Steam identity linked to this personnel row
        # is sufficient evidence for onboarding.
        try:
            steam_verified=bool(fetch_one("SELECT 1 AS ok FROM hll_personnel_links WHERE personnel_id::text=%s AND COALESCE(steam_id::text,'')<>'' LIMIT 1",(str(personnel_id),)))
        except Exception:
            log.exception('Welcome Packet HLL link check failed for %s',personnel_id)
    if steam_verified:
        welcome_complete_task(personnel_id,'STEAM_LINKED','SYSTEM',reconcile=False)

    # Never auto-approve: all completed checklists wait for the member to submit and Command to accept.
    total,done=_welcome_required_counts(pkt['id'])
    current=fetch_one("SELECT * FROM welcome_packets WHERE id=%s",(pkt['id'],)) or pkt
    status=str(current.get('status') or 'IN_PROGRESS').upper()
    if status in {'COMPLETE','CLOSED','ARCHIVED','READY_FOR_REVIEW'}:
        return current
    if status=='RETURNED':
        return current
    execute("UPDATE welcome_packets SET current_phase='COMPANY_ONBOARDING',status='IN_PROGRESS',updated_at=NOW() WHERE id=%s",(pkt['id'],))
    return fetch_one("SELECT * FROM welcome_packets WHERE id=%s",(pkt['id'],))

def welcome_packet_context(personnel_id):
    pkt=fetch_one("SELECT * FROM welcome_packets WHERE personnel_id=%s",(personnel_id,))
    if not pkt: return {'packet':None,'tasks':[],'phases':[],'total':0,'done':0,'percent':0,'personnel':_welcome_person(personnel_id),'ready_to_submit':False}
    # Active packets may have been created by an older onboarding definition.
    # Normalize task titles/modes/targets on every open so legacy VISIT rows or
    # missing current tasks cannot break member check-off. Never mutate an
    # accepted archive; archived packets are permanent historical records.
    if str(pkt.get('status') or '').upper() not in {'COMPLETE','CLOSED','ARCHIVED'}:
        pkt=ensure_welcome_packet(personnel_id,pkt.get('recruiting_case_id')) or pkt
    else:
        pkt=reconcile_welcome_packet(personnel_id)
    tasks=[dict(t) for t in fetch_all("SELECT * FROM welcome_packet_tasks WHERE packet_id=%s AND required=TRUE ORDER BY sort_order",(pkt['id'],))]
    for task in tasks:
        task['guidance']=WELCOME_TASK_GUIDANCE.get(str(task.get('task_code') or '').upper(), {})
    total=sum(1 for t in tasks if t.get('required')); done=sum(1 for t in tasks if t.get('status') in {'COMPLETE','WAIVED'})
    phases=[{'code':'COMPANY_ONBOARDING','label':'REPLACEMENT ONBOARDING','tasks':tasks,'total':total,'done':done,'percent':int(done*100/total) if total else 0,'locked':False}]
    return {'packet':pkt,'tasks':tasks,'phases':phases,'total':total,'done':done,'percent':int(done*100/total) if total else 0,
            'personnel':_welcome_person(personnel_id),'ready_to_submit':bool(total and done==total and str(pkt.get('status') or '').upper() in {'IN_PROGRESS','RETURNED'})}

def _personnel_has_platoon_assignment(person):
    if not person: return False
    if str(person.get('platoon') or '').strip(): return True
    node_id=person.get('unit_node_id')
    if node_id:
        try:
            ancestry=unit_ancestry(node_id) or []
            return any(str(n.get('unit_type') or '').strip().lower()=='platoon' for n in ancestry)
        except Exception:
            log.exception('Report for Duty platoon ancestry check failed personnel=%s',person.get('id'))
    return False

def _recruit_journey_status(personnel_id):
    person=_welcome_person(personnel_id) or {}
    case=fetch_one("SELECT * FROM recruiting_cases WHERE personnel_id=%s ORDER BY approved_at DESC NULLS LAST,created_at DESC LIMIT 1",(personnel_id,)) or {}
    wp=welcome_packet_context(personnel_id) if personnel_id else {'packet':None,'percent':0}
    packet=wp.get('packet') or {}
    discord_ok=False; game_ok=False
    try:
        discord_ok=bool(fetch_one("SELECT 1 ok FROM website_member_links WHERE personnel_id::text=%s LIMIT 1",(str(personnel_id),)))
    except Exception: pass
    try:
        game_ok=bool(fetch_one("SELECT 1 ok FROM hll_personnel_links WHERE personnel_id::text=%s AND COALESCE(steam_id::text,'')<>'' LIMIT 1",(str(personnel_id),)))
    except Exception: pass
    assigned=_personnel_has_platoon_assignment(person)
    chain=[]
    try: chain=chain_of_command_for(person) or []
    except Exception: log.exception('Report for Duty chain lookup failed personnel=%s',personnel_id)
    assigned_nco=next((x for x in chain if str(x.get('chain_title') or '').upper() in {'TEAM LEADER','SQUAD LEADER','PLATOON SERGEANT'}),None)
    assignment='REPLACEMENT DETACHMENT — AWAITING PLATOON ASSIGNMENT'
    if assigned:
        bits=[str(person.get('unit_code') or '').strip(),str(person.get('platoon') or '').strip(),str(person.get('squad') or '').strip()]
        assignment=' → '.join([b for b in bits if b])
    return {
        'personnel':person,'case':case,'welcome':wp,'assigned':assigned,'assignment':assignment,
        'stages':[
            {'label':'APPLICATION','complete':bool(case),'detail':case.get('case_number') or 'FILED'},
            {'label':'COMMAND REVIEW','complete':str(case.get('status') or '').upper() in {'REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING','ENLISTED'},'detail':str(case.get('status') or 'APPROVED').replace('_',' ')},
            {'label':'GAME IDENTITY','complete':game_ok,'detail':'VERIFIED' if game_ok else str(case.get('game_identity_link_status') or 'PENDING').replace('_',' ')},
            {'label':'WEBSITE ACCESS','complete':True,'detail':'ISSUED'},
            {'label':'WELCOME PACKET','complete':str(packet.get('status') or '').upper() in {'COMPLETE','CLOSED','ARCHIVED'},'detail':f"{wp.get('percent',0)}% COMPLETE"},
            {'label':'ASSIGNMENT','complete':assigned,'detail':assignment},
        ],
        'discord_ok':discord_ok,'game_ok':game_ok,'chain':chain,'assigned_nco':assigned_nco,
    }

@app.route('/report-for-duty')
@login_required
def member_report_for_duty():
    if session.get('access_role') not in {'member','nco','company_hq'}: abort(403)
    person=linked_personnel()
    if not person: abort(404)
    journey=_recruit_journey_status(person['id'])
    if journey.get('assigned') and request.args.get('stay') != '1':
        return redirect(url_for('my_soldier_record'))
    return render_template('member_report_for_duty.html',journey=journey,personnel=person)


@app.route('/welcome-packet',methods=['GET','POST'])
@login_required
def welcome_packet():
    if session.get('access_role') not in {'member','nco','company_hq'}: abort(403)
    pid=session.get('personnel_id'); person=linked_personnel()
    if not pid or not person: abort(403)
    ctx=welcome_packet_context(pid)
    pkt=ctx.get('packet') or {}
    if not pkt:
        flash('NO ACTIVE WELCOME PACKET IS REQUIRED FOR THIS SOLDIER RECORD.','warning')
        return redirect(url_for('my_soldier_record'))
    if str(pkt.get('status') or '').upper() in {'COMPLETE','CLOSED','ARCHIVED'}:
        flash('YOUR WELCOME PACKET HAS BEEN COMPLETED AND ARCHIVED.','success')
        return redirect(url_for('my_soldier_record'))
    if str(pkt.get('status') or '').upper()=='PENDING_ASSIGNMENT':
        flash('YOUR WELCOME PACKET OPENS WHEN YOUR FIRST COMPANY ASSIGNMENT IS FILED.','warning')
        return redirect(url_for('my_soldier_record'))
    if request.method=='POST':
        action=(request.form.get('action') or '').strip().upper()
        if str(pkt.get('status') or '').upper()=='READY_FOR_REVIEW':
            flash('WELCOME PACKET IS ALREADY SUBMITTED TO COMMAND AND IS READ-ONLY.','warning')
            return redirect(url_for('welcome_packet'))

        # Allow the member to explicitly rerun automatic Discord/HLL evidence
        # checks without changing any checklist item themselves.
        if action=='REVERIFY':
            reconcile_welcome_packet(pid)
            refreshed=welcome_packet_context(pid)
            auto={str(t.get('task_code') or ''):str(t.get('status') or '') for t in refreshed.get('tasks',[])}
            verified=sum(1 for code in ('DISCORD_LINKED','STEAM_LINKED') if auto.get(code) in {'COMPLETE','WAIVED'})
            if verified==2:
                flash('AUTOMATIC VERIFICATION COMPLETE — DISCORD AND HLL / STEAM IDENTITY ARE BOTH ON FILE.','success')
            elif verified==1:
                flash('ONE AUTOMATIC IDENTITY CHECK IS VERIFIED. THE OTHER LINK IS NOT YET ON FILE; COMMAND CAN ALSO REVIEW IT.','warning')
            else:
                flash('DISCORD / HLL IDENTITY LINKS ARE NOT YET VISIBLE TO THE WEBSITE. YOUR MANUAL CHECKLIST PROGRESS WAS NOT LOST.','warning')
            return redirect(url_for('welcome_packet'))

        if action=='SUBMIT':
            refreshed=welcome_packet_context(pid)
            if not refreshed.get('ready_to_submit'):
                flash('COMPLETE EVERY REQUIRED ONBOARDING ITEM BEFORE SUBMITTING TO COMMAND.','warning')
                return redirect(url_for('welcome_packet'))
            execute("UPDATE welcome_packets SET status='READY_FOR_REVIEW',submitted_at=NOW(),returned_at=NULL,returned_by=NULL,return_note=NULL,last_activity_at=NOW(),updated_at=NOW() WHERE id=%s",(pkt['id'],))
            _welcome_notify(pkt,'COMMAND_REVIEW','Onboarding Ready for Command Review',f"{person.get('rank_code') or ''} {person.get('last_name') or ''} submitted the Welcome Packet for Command acceptance.",'SUBMITTED')
            try: notify_soldier(pid,'S-1 PERSONNEL','Welcome Packet submitted','Your onboarding checklist has been submitted to Command for review.',source_key=f'WELCOME-SUBMIT:{pid}',target_anchor='orientation-record')
            except Exception: log.exception('Welcome Packet submit notice failed for %s',pid)
            flash('WELCOME PACKET SUBMITTED TO COMMAND. YOUR CHECKLIST IS NOW READ-ONLY PENDING REVIEW.','success')
            return redirect(url_for('welcome_packet'))

        if action not in {'CHECK','UNCHECK'}:
            flash('THAT WELCOME PACKET ACTION COULD NOT BE UNDERSTOOD. THE PACKET WAS NOT CHANGED — PLEASE TRY AGAIN.','warning')
            return redirect(url_for('welcome_packet'))

        code=(request.form.get('task_code') or '').strip().upper()
        if not code:
            flash('NO CHECKLIST ITEM WAS RECEIVED. THE PACKET WAS NOT CHANGED — PLEASE TRY AGAIN.','warning')
            return redirect(url_for('welcome_packet'))
        task=fetch_one("SELECT t.* FROM welcome_packet_tasks t JOIN welcome_packets p ON p.id=t.packet_id WHERE p.personnel_id=%s AND t.task_code=%s AND t.required=TRUE",(pid,code))
        if not task:
            flash('THAT CHECKLIST ITEM IS NO LONGER ACTIVE. THE PACKET HAS BEEN REFRESHED.','warning')
            return redirect(url_for('welcome_packet'))
        if str(task.get('status') or '').upper()=='LOCKED':
            flash('THAT CHECKLIST ITEM IS STILL LOCKED UNTIL YOUR COMPANY ASSIGNMENT IS ACTIVE.','warning')
            return redirect(url_for('welcome_packet'))
        if str(task.get('completion_mode') or '').strip().upper()!='SELF':
            # System-verification rows must never generate a raw HTTP 400 page.
            reconcile_welcome_packet(pid)
            flash('THAT ITEM IS VERIFIED AUTOMATICALLY. WE RECHECKED THE LINK FOR YOU.','warning')
            return redirect(url_for('welcome_packet'))

        if action=='UNCHECK':
            execute("UPDATE welcome_packet_tasks SET status='OPEN',completed_at=NULL,completed_by=NULL,updated_at=NOW() WHERE id=%s",(task['id'],))
        else:
            welcome_complete_task(pid,code,f"{person.get('rank_code') or ''} {person.get('last_name') or ''}".strip(),reconcile=False)
        execute("UPDATE welcome_packets SET status=CASE WHEN status='RETURNED' THEN 'IN_PROGRESS' ELSE status END,last_activity_at=NOW(),updated_at=NOW() WHERE id=%s",(pkt['id'],))
        return redirect(url_for('welcome_packet'))
    return render_template('welcome_packet.html',**ctx)



def _staff_preview_recruit_state(state='approved'):
    state=(state or 'approved').strip().lower()
    presets={
        'approved': dict(packet=0, discord=True, game=True, assigned=False, packet_status='IN_PROGRESS', review='APPROVED'),
        'not_started': dict(packet=0, discord=True, game=False, assigned=False, packet_status='IN_PROGRESS', review='APPROVED'),
        'packet_2': dict(packet=25, discord=True, game=False, assigned=False, packet_status='IN_PROGRESS', review='APPROVED'),
        'identity_pending': dict(packet=50, discord=True, game=False, assigned=False, packet_status='IN_PROGRESS', review='APPROVED'),
        'ready_review': dict(packet=100, discord=True, game=True, assigned=False, packet_status='READY_FOR_REVIEW', review='APPROVED'),
        'returned': dict(packet=75, discord=True, game=True, assigned=False, packet_status='RETURNED', review='APPROVED'),
        'accepted': dict(packet=100, discord=True, game=True, assigned=False, packet_status='COMPLETE', review='APPROVED'),
        'awaiting_assignment': dict(packet=100, discord=True, game=True, assigned=False, packet_status='COMPLETE', review='APPROVED'),
        'assigned': dict(packet=100, discord=True, game=True, assigned=True, packet_status='COMPLETE', review='APPROVED'),
    }
    return state,presets.get(state,presets['approved'])

@app.get('/staff/preview-center')
@login_required
@role_required('s1')
def staff_preview_center():
    return render_template('staff_preview_center.html')

@app.get('/staff/preview/recruit')
@login_required
@role_required('s1')
def staff_preview_recruit():
    state,p=_staff_preview_recruit_state(request.args.get('state'))
    view=(request.args.get('view') or 'report').strip().lower()
    person={'id':'preview-recruit','rank_code':'PVT','first_name':'JOHN','last_name':'DOE','unit_code':'A CO','platoon':'1st Platoon' if p['assigned'] else None,'squad':'1st Squad' if p['assigned'] else None,'fire_team':'Alpha Team' if p['assigned'] else None}
    assignment='A CO → 1st Platoon → 1st Squad → Alpha Team' if p['assigned'] else 'REPLACEMENT DETACHMENT — AWAITING PLATOON ASSIGNMENT'
    nco={'rank_code':'SGT','first_name':'JAMES','last_name':'MILLER','chain_title':'SQUAD LEADER'} if p['assigned'] else None
    stages=[
        {'label':'APPLICATION','complete':True,'detail':'RC-1965-00125'},
        {'label':'COMMAND REVIEW','complete':True,'detail':'APPROVED'},
        {'label':'GAME IDENTITY','complete':p['game'],'detail':'VERIFIED' if p['game'] else 'PENDING'},
        {'label':'WEBSITE ACCESS','complete':True,'detail':'ISSUED'},
        {'label':'WELCOME PACKET','complete':p['packet_status'] in {'COMPLETE','CLOSED','ARCHIVED'},'detail':f"{p['packet']}% COMPLETE"},
        {'label':'ASSIGNMENT','complete':p['assigned'],'detail':assignment},
    ]
    journey={'stages':stages,'assignment':assignment,'assigned':p['assigned'],'discord_ok':p['discord'],'game_ok':p['game'],'assigned_nco':nco,'welcome':{'percent':p['packet']}}
    if view=='welcome':
        titles=[
            ('REVIEW_ASSIGNMENT','Review Your Assignment','Review your current Replacement Detachment or permanent formation assignment and confirm you know where you belong.','SELF'),
            ('READ_RULES','Read Unit Rules','Review the battalion rules and standing orders before reporting to your permanent formation.','SELF'),
            ('REVIEW_CHAIN','Review Your Chain of Command','Know who you report to now and who you will report to after permanent assignment.','SELF'),
            ('TALKED_TO_NCO','Talk to Your Assigned NCO','Once an NCO/sponsor is listed, make contact in Discord and confirm the contact here.','SELF'),
            ('FIND_SERVER','Find the 1/5 Cavalry Server','Locate and favorite the unit HLL: Vietnam server so you are ready for operations.','SELF'),
            ('REVIEW_EXPECTATIONS','Review Operations / Equipment Expectations','Review how operations, attendance, readiness, issued M16 responsibility, cleaning, and inspections work.','SELF'),
            ('DISCORD_LINKED','Discord Account Connected','Battalion Clerk verifies your Discord account automatically.','SYSTEM'),
            ('STEAM_LINKED','HLL / Game Identity Connected','Your SteamID64, Xbox Gamertag, or PSN ID is verified automatically.','SYSTEM'),
        ]
        done_target=round(len(titles)*p['packet']/100)
        tasks=[]
        for i,(code,title,desc,mode) in enumerate(titles):
            complete=i < done_target
            if code=='DISCORD_LINKED': complete=p['discord']
            if code=='STEAM_LINKED': complete=p['game']
            tasks.append({'task_code':code,'title':title,'description':desc,'completion_mode':mode,'status':'COMPLETE' if complete else 'OPEN','guidance':WELCOME_TASK_GUIDANCE.get(code,{})})
        done=sum(1 for t in tasks if t['status']=='COMPLETE'); total=len(tasks); percent=int(done*100/total)
        packet={'status':p['packet_status'],'return_note':'Command returned this packet so you can verify your NCO contact before resubmitting.' if p['packet_status']=='RETURNED' else None}
        return render_template('welcome_packet.html',packet=packet,personnel=person,tasks=tasks,phases=[{'label':'REPLACEMENT ONBOARDING','tasks':tasks,'done':done,'total':total,'percent':percent,'locked':False}],done=done,total=total,percent=percent,ready_to_submit=done==total,welcome_preview=True,simulation_preview=True,preview_return_endpoint=None,suppress_staff_chrome=True,preview_state=state)
    return render_template('member_report_for_duty.html',journey=journey,personnel=person,preview_mode=True,suppress_staff_chrome=True,preview_state=state)

@app.get('/staff/preview/member')
@login_required
@role_required('s1')
def staff_preview_member_state():
    state=(request.args.get('state') or 'clear').strip().lower()
    person={'id':'preview-member','rank_code':'SGT','first_name':'JAMES','last_name':'MILLER','unit_code':'A CO','platoon':'1st Platoon','squad':'1st Squad','fire_team':'Alpha Team'}
    presets={
        'clear':([],[]),
        'award':([{'id':'preview-award','section':'S-1 PERSONNEL','notification_type':'AWARD','title':'UNIT CITATION FILED','message':'The Unit Citation has been entered into your permanent service record.','priority':'ROUTINE','target_endpoint':'my_201_file','target_anchor':'awards'}],[]),
        'promotion':([{'id':'preview-promo','section':'S-1 PERSONNEL','notification_type':'PROMOTION','title':'PROMOTION ORDER POSTED','message':'Your promotion order has been filed in your 201 File.','priority':'ROUTINE','target_endpoint':'my_201_file','target_anchor':'career'}],[]),
        'required':([],[{'section':'S-4','title':'M16 INSPECTION REQUIRED','detail':'Your issued rifle inspection is overdue. Coordinate with S-4.','priority':'HIGH','target':'my_weapon_service_history','anchor':None}]),
        'multiple':([{'id':'preview-award','section':'S-1 PERSONNEL','notification_type':'AWARD','title':'AWARD FILED','message':'A new award has been entered into your record.','priority':'ROUTINE','target_endpoint':'my_201_file','target_anchor':'awards'},{'id':'preview-order','section':'HEADQUARTERS','notification_type':'ORDERS','title':'SPECIAL ORDER POSTED','message':'A new personnel order is available in your 201 File.','priority':'HIGH','target_endpoint':'my_201_file','target_anchor':'orders'}],[{'section':'S-4','title':'M16 INSPECTION','detail':'Inspection due in 2 days.','priority':'WATCH','target':'my_weapon_service_history','anchor':None}]),
        'readiness':([],[{'section':'S-1','title':'SERVER ACTIVITY — AT RISK','detail':'No verified server activity has been recorded for 16 days.','priority':'HIGH','target':'my_201_file','anchor':'readiness'}]),
    }
    notices,items=presets.get(state,presets['clear'])
    recommended=items[0] if items else {'section':'HEADQUARTERS','title':'MAINTAIN READINESS','detail':'No immediate deficiency is on file. Continue normal participation.','priority':'ROUTINE','target':None,'anchor':None}
    history=[{'section':'S-1 PERSONNEL','notification_type':'AWARD','title':'GOOD CONDUCT RIBBON FILED','message':'Previously acknowledged notice.','acknowledged_at':'24 AUG 1965'}]
    return render_template('member_action_center.html',personnel=person,items=items,recommended=recommended,situation={},reputation=[],notifications=notices,history=history,preview_mode=True,suppress_staff_chrome=True,preview_state=state)

@app.get('/staff/personnel/<personnel_id>/member-preview')
@login_required
@role_required('s1')
def staff_personnel_member_preview(personnel_id):
    person=fetch_one('SELECT * FROM personnel WHERE id=%s',(personnel_id,))
    if not person: abort(404)
    ctx=personnel_record_context(person)
    ctx['member_preview_mode']=True
    ctx['suppress_staff_chrome']=True
    return render_template('personnel_file.html',**ctx)

def reconcile_recent_recruit_onboarding_safeguards():
    """Repair missing Welcome Packets for recently approved recruits without changing assignments.

    This is deliberately non-destructive: existing Company/Platoon/Squad history is
    preserved. It only restores the missing onboarding record and records an observable
    automation event so Command can finish the packet before any further formation action.
    """
    rows=fetch_all("""SELECT rc.id AS case_id,rc.personnel_id,rc.case_number,rc.status
                      FROM recruiting_cases rc
                      JOIN personnel p ON p.id=rc.personnel_id
                      LEFT JOIN welcome_packets wp ON wp.personnel_id=p.id
                      WHERE rc.approved_at IS NOT NULL
                        AND rc.approved_at >= NOW()-INTERVAL '30 days'
                        AND rc.status IN ('REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING','ENLISTED')
                        AND p.archived=FALSE AND p.separated_at IS NULL
                        AND wp.id IS NULL
                      ORDER BY rc.approved_at DESC""")
    repaired=[]
    for row in rows:
        try:
            pkt=ensure_welcome_packet(row['personnel_id'],row['case_id'])
            if pkt:
                repaired.append(str(row['personnel_id']))
                record_automation_event('ONBOARDING','WELCOME_PACKET_REPAIR','COMPLETE',
                    f"Missing Welcome Packet restored for {row.get('case_number') or 'approved recruit'}; existing assignment preserved.",
                    personnel_id=row['personnel_id'],source_key=f"WELCOME-REPAIR:{row['personnel_id']}")
        except Exception:
            log.exception('Welcome Packet safeguard repair failed personnel=%s',row.get('personnel_id'))
    return repaired


@app.get('/staff/onboarding')
@login_required
@role_required('s1')
def staff_onboarding():
    reconcile_recent_recruit_onboarding_safeguards()
    people=fetch_all("""SELECT p.*,wp.id packet_id,wp.current_phase,wp.status onboarding_status,wp.last_activity_at,wp.submitted_at,wp.activated_at,
                       (SELECT COUNT(*) FROM welcome_packet_tasks t WHERE t.packet_id=wp.id AND t.required=TRUE) total_tasks,
                       (SELECT COUNT(*) FROM welcome_packet_tasks t WHERE t.packet_id=wp.id AND t.required=TRUE AND t.status IN ('COMPLETE','WAIVED')) done_tasks
                       FROM welcome_packets wp JOIN personnel p ON p.id=wp.personnel_id
                       WHERE p.separated_at IS NULL AND wp.status NOT IN ('COMPLETE','CLOSED','ARCHIVED','PENDING_ASSIGNMENT')
                       ORDER BY CASE wp.status WHEN 'READY_FOR_REVIEW' THEN 0 WHEN 'RETURNED' THEN 1 WHEN 'IN_PROGRESS' THEN 2 ELSE 3 END,wp.last_activity_at DESC""")
    return render_template('staff_onboarding.html',rows=people,archived=False)

@app.get('/staff/onboarding/archive')
@login_required
@role_required('s1')
def staff_onboarding_archive():
    people=fetch_all("""SELECT p.*,wp.id packet_id,wp.current_phase,wp.status onboarding_status,wp.last_activity_at,wp.completed_at,wp.approved_at,wp.approved_by,
                       (SELECT COUNT(*) FROM welcome_packet_tasks t WHERE t.packet_id=wp.id AND t.required=TRUE) total_tasks,
                       (SELECT COUNT(*) FROM welcome_packet_tasks t WHERE t.packet_id=wp.id AND t.required=TRUE AND t.status IN ('COMPLETE','WAIVED')) done_tasks
                       FROM welcome_packets wp JOIN personnel p ON p.id=wp.personnel_id
                       WHERE wp.status IN ('COMPLETE','CLOSED','ARCHIVED') ORDER BY COALESCE(wp.approved_at,wp.completed_at) DESC NULLS LAST""")
    return render_template('staff_onboarding.html',rows=people,archived=True)

@app.get('/staff/onboarding/<personnel_id>/member-preview')
@login_required
@role_required('s1')
def staff_welcome_packet_member_preview(personnel_id):
    ctx=welcome_packet_context(personnel_id)
    if not ctx.get('packet'): abort(404)
    ctx['welcome_preview']=True
    ctx['preview_return_endpoint']='staff_welcome_packet'
    return render_template('welcome_packet.html',**ctx)

@app.route('/staff/onboarding/<personnel_id>',methods=['GET','POST'])
@login_required
@role_required('s1')
def staff_welcome_packet(personnel_id):
    ctx=welcome_packet_context(personnel_id)
    if not ctx.get('packet'): abort(404)
    if request.method=='POST':
        action=(request.form.get('action') or '').upper(); authority=session.get('display_name') or session.get('username') or 'BATTALION HEADQUARTERS'
        pkt=ctx['packet']
        if action=='APPROVE':
            if str(pkt.get('status') or '').upper()!='READY_FOR_REVIEW':
                flash('PACKET MUST BE SUBMITTED BY THE SOLDIER BEFORE COMMAND CAN ACCEPT IT.','warning')
                return redirect(url_for('staff_welcome_packet',personnel_id=personnel_id))
            total,done=_welcome_required_counts(pkt['id'])
            if not total or done!=total:
                flash('PACKET CANNOT BE ACCEPTED WHILE REQUIRED ITEMS REMAIN OPEN.','warning')
                return redirect(url_for('staff_welcome_packet',personnel_id=personnel_id))
            execute("UPDATE welcome_packets SET status='COMPLETE',current_phase='COMPLETE',completed_at=NOW(),approved_at=NOW(),approved_by=%s,return_note=NULL,updated_at=NOW() WHERE id=%s",(authority,pkt['id']))
            try:
                write_service_entry(personnel_id,'ADMIN','WELCOME PACKET ACCEPTED','Welcome Packet onboarding checklist completed by the Soldier and accepted by Command.',authority)
            except Exception: log.exception('Welcome Packet archive service entry failed for %s',personnel_id)
            try:
                notify_soldier(personnel_id,'S-1 PERSONNEL','Welcome Packet accepted','Command accepted your Welcome Packet. Permanent Company / Platoon assignment is now authorized and S-1 will file your assignment orders.',source_key=f'WELCOME-APPROVED:{personnel_id}',target_anchor='assignment')
            except Exception: log.exception('Welcome Packet approval notice failed for %s',personnel_id)
            try:
                open_personnel_action(personnel_id,'ASSIGNMENT','Permanent Formation Assignment Required','S-1','HIGH',authority,
                    {'workflow':'POST-WELCOME-PACKET','welcome_packet_id':str(pkt['id'])},source_key=f'POST-WELCOME-ASSIGNMENT:{personnel_id}',due_date=date.today()+timedelta(days=3))
            except Exception: log.exception('Post-Welcome Packet assignment action failed for %s',personnel_id)
            flash('WELCOME PACKET ACCEPTED AND ARCHIVED. PERMANENT ASSIGNMENT IS NOW UNLOCKED AND QUEUED FOR S-1 / COMMAND.','success')
            return redirect(url_for('staff_onboarding'))
        if action=='RETURN':
            note=(request.form.get('return_note') or '').strip()
            if not note: note='Command returned the packet for correction.'
            execute("UPDATE welcome_packets SET status='RETURNED',returned_at=NOW(),returned_by=%s,return_note=%s,submitted_at=NULL,updated_at=NOW() WHERE id=%s",(authority,note,pkt['id']))
            try: notify_soldier(personnel_id,'S-1 PERSONNEL','Welcome Packet returned',note,source_key=f'WELCOME-RETURN:{personnel_id}:{date.today()}',target_anchor='orientation-record')
            except Exception: log.exception('Welcome Packet return notice failed for %s',personnel_id)
            flash('WELCOME PACKET RETURNED TO THE SOLDIER FOR CORRECTION.','success')
            return redirect(url_for('staff_welcome_packet',personnel_id=personnel_id))
        code=(request.form.get('task_code') or '').strip().upper()
        task=fetch_one("SELECT * FROM welcome_packet_tasks WHERE packet_id=%s AND task_code=%s AND required=TRUE",(pkt['id'],code))
        if not task:
            flash('THAT CHECKLIST ITEM IS NO LONGER ACTIVE. THE PACKET WAS REFRESHED AND NO RECORD WAS CHANGED.','warning')
            return redirect(url_for('staff_welcome_packet',personnel_id=personnel_id))
        if action not in {'COMPLETE','WAIVE','RESET'}:
            flash('THAT COMMAND WELCOME PACKET ACTION WAS NOT RECOGNIZED. NO RECORD WAS CHANGED.','warning')
            return redirect(url_for('staff_welcome_packet',personnel_id=personnel_id))
        if action=='COMPLETE': welcome_complete_task(personnel_id,code,authority)
        elif action=='WAIVE':
            execute("UPDATE welcome_packet_tasks SET status='WAIVED',waived_at=NOW(),waived_by=%s,updated_at=NOW() WHERE id=%s",(authority,task['id'])); reconcile_welcome_packet(personnel_id)
        elif action=='RESET':
            execute("UPDATE welcome_packet_tasks SET status='OPEN',completed_at=NULL,completed_by=NULL,waived_at=NULL,waived_by=NULL,updated_at=NOW() WHERE id=%s",(task['id'],)); reconcile_welcome_packet(personnel_id)
        staff_log('S-1','WELCOME PACKET',f"{action} — {task['title']}",authority,personnel_id,details={'task_code':code})
        return redirect(url_for('staff_welcome_packet',personnel_id=personnel_id))
    return render_template('staff_welcome_packet.html',**ctx)

@app.get('/internal/clerk/welcome-packet/notifications')
def clerk_welcome_packet_notifications():
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    guild_id=request.args.get('guild_id',type=int)
    # One useful reminder per stalled day; never nag Soldiers who already finished
    # their current phase and are waiting on S-1 assignment.
    execute("""INSERT INTO welcome_packet_notifications(packet_id,personnel_id,event_key,event_type,title,message)
               SELECT wp.id,wp.personnel_id,'WELCOME:'||wp.personnel_id::text||':REMINDER:'||CURRENT_DATE::text,'REMINDER',
                      'Welcome Packet Action Required','Your Welcome Packet still has required orientation items open. Sign in to the battalion website to continue.'
               FROM welcome_packets wp
               WHERE wp.status='IN_PROGRESS' AND wp.last_activity_at < NOW()-INTERVAL '24 hours'
               ON CONFLICT(event_key) DO NOTHING""")
    rows=fetch_all("""SELECT n.id,n.event_type,n.title,n.message,n.personnel_id,wml.discord_user_id,wml.guild_id
                      FROM welcome_packet_notifications n JOIN website_member_links wml ON wml.personnel_id=n.personnel_id::text
                      WHERE n.delivered_at IS NULL AND (wml.guild_id=%s OR %s IS NULL) ORDER BY n.created_at ASC LIMIT 100""",(guild_id,guild_id))
    return {'ok':True,'notifications':rows}

@app.post('/internal/clerk/welcome-packet/notifications/<notification_id>/delivered')
def clerk_welcome_packet_notification_delivered(notification_id):
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    data=request.get_json(silent=True) or {}; ok=bool(data.get('ok',True))
    execute("UPDATE welcome_packet_notifications SET delivered_at=NOW(),delivery_error=%s WHERE id=%s",((None if ok else (data.get('error') or 'Discord delivery failed')),notification_id))
    return {'ok':True}


@app.route('/hq/sync-control',methods=['GET','POST'])
@login_required
def personnel_sync_control():
    if session.get('access_role') not in {'s1','battalion_hq','commander','admin'}:
        abort(403)
    authority=session.get('username') or 'COMMAND'
    if request.method=='POST':
        action=(request.form.get('action') or '').upper().strip()
        if action=='RECONCILE_ONE':
            pid=request.form.get('personnel_id')
            person=fetch_one("SELECT id,rank_code,last_name FROM personnel WHERE id=%s AND archived=FALSE",(pid,))
            if not person:
                flash('PERSONNEL RECORD NOT FOUND.','error')
            else:
                enqueue_discord_role_sync(pid,f'MANUAL RECONCILIATION REQUESTED BY {authority}')
                record_automation_event('PERSONNEL','RECONCILE','PENDING',f"Full Soldier reconciliation requested for {person.get('rank_code','')} {person.get('last_name','')}.",personnel_id=pid,source_key=f'RECONCILE:{pid}',details={'authority':authority})
                flash('SOLDIER RECONCILIATION QUEUED.','success')
            return redirect(url_for('personnel_sync_control'))
        if action=='RECONCILE_ALL':
            rows=fetch_all("""SELECT p.id FROM personnel p JOIN website_member_links w ON w.personnel_id=p.id::text
                              WHERE p.archived=FALSE AND p.separated_at IS NULL""")
            for row in rows:
                enqueue_discord_role_sync(row['id'],f'BATTALION RECONCILIATION REQUESTED BY {authority}')
            record_automation_event('PERSONNEL','BATTALION_RECONCILE','PENDING',f'Battalion-wide personnel reconciliation queued for {len(rows)} linked Soldiers.',source_key='BATTALION-RECONCILE',details={'authority':authority,'count':len(rows)})
            flash(f'BATTALION RECONCILIATION QUEUED FOR {len(rows)} LINKED SOLDIERS.','success')
            return redirect(url_for('personnel_sync_control'))
        flash('SYNC CONTROL ACTION NOT RECOGNIZED.','warning')
        return redirect(url_for('personnel_sync_control'))

    rows=fetch_all("""SELECT p.id,p.rank_code,p.first_name,p.last_name,p.mos_code,p.unit_code,p.platoon,p.squad,p.fire_team,p.field_status,p.lifecycle_state,
                             w.guild_id,w.discord_user_id,
                             q.status AS sync_status,q.reason AS sync_reason,q.requested_at,q.processed_at,q.error_text,q.attempt_count,q.last_attempt_at,q.next_retry_at,
                             o.status AS observation_status,o.actual_roles_json,o.changes_json,o.error_text AS observation_error,o.observed_at
                      FROM personnel p
                      JOIN website_member_links w ON w.personnel_id=p.id::text
                      LEFT JOIN LATERAL (SELECT * FROM discord_role_sync_queue q0 WHERE q0.personnel_id=p.id ORDER BY requested_at DESC LIMIT 1) q ON TRUE
                      LEFT JOIN LATERAL (SELECT * FROM discord_sync_observations o0 WHERE o0.personnel_id=p.id ORDER BY observed_at DESC LIMIT 1) o ON TRUE
                      WHERE p.archived=FALSE AND p.separated_at IS NULL
                      ORDER BY p.unit_code,p.platoon NULLS FIRST,p.squad NULLS FIRST,p.last_name,p.first_name""")
    counts={'total':len(rows),'current':0,'pending':0,'error':0,'unlinked':0}
    for r in rows:
        state=str(r.get('observation_status') or r.get('sync_status') or 'UNKNOWN').upper()
        if state in {'COMPLETE','CURRENT','MATCHED'}: counts['current']+=1
        elif state in {'PENDING','QUEUED','PROCESSING'}: counts['pending']+=1
        elif state in {'FAILED','BLOCKED','ERROR','DRIFT'}: counts['error']+=1
    heartbeats=fetch_all("SELECT * FROM system_heartbeats ORDER BY component")
    role_registry=fetch_all("""SELECT guild_id,role_category,COUNT(*)::int AS role_count,COUNT(DISTINCT canonical_key)::int AS canonical_count,
                                     COUNT(*) FILTER (WHERE manageable=FALSE)::int AS blocked_count
                              FROM discord_managed_role_registry GROUP BY guild_id,role_category ORDER BY guild_id,role_category""")
    ledger=fetch_all("""SELECT a.*,p.rank_code,p.first_name,p.last_name FROM automation_ledger a
                         LEFT JOIN personnel p ON p.id=a.personnel_id ORDER BY a.updated_at DESC LIMIT 30""")
    return render_template('personnel_sync_control.html',rows=rows,counts=counts,heartbeats=heartbeats,role_registry=role_registry,ledger=ledger)


@app.post('/internal/clerk/heartbeat')
def clerk_heartbeat():
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    data=request.get_json(silent=True) or {}
    component=str(data.get('component') or 'BATTALION_CLERK').upper().strip()
    execute("""INSERT INTO system_heartbeats(component,status,version,details_json,last_seen_at)
               VALUES(%s,%s,%s,%s,NOW()) ON CONFLICT(component) DO UPDATE SET status=EXCLUDED.status,
               version=EXCLUDED.version,details_json=EXCLUDED.details_json,last_seen_at=NOW()""",
            (component,str(data.get('status') or 'ONLINE').upper(),data.get('version'),Json(data.get('details') or {})))
    return {'ok':True}


@app.post('/internal/clerk/role-registry')
def clerk_role_registry():
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    data=request.get_json(silent=True) or {}; guild_id=data.get('guild_id'); roles=data.get('roles') or []
    if not guild_id: return {'ok':False,'error':'guild_id required'},400
    execute("DELETE FROM discord_managed_role_registry WHERE guild_id=%s",(guild_id,))
    for role in roles[:500]:
        try:
            execute("""INSERT INTO discord_managed_role_registry(guild_id,role_id,role_name,role_category,canonical_key,manageable,updated_at)
                       VALUES(%s,%s,%s,%s,%s,%s,NOW())""",
                    (guild_id,int(role.get('role_id')),str(role.get('role_name') or ''),str(role.get('role_category') or 'UNKNOWN'),str(role.get('canonical_key') or ''),bool(role.get('manageable',True))))
        except Exception:
            log.exception('Role registry row rejected guild=%s role=%s',guild_id,role)
    return {'ok':True,'count':len(roles)}


@app.post('/internal/clerk/personnel/sync-observation')
def clerk_sync_observation():
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    data=request.get_json(silent=True) or {}; pid=data.get('personnel_id')
    if not pid: return {'ok':False,'error':'personnel_id required'},400
    execute("""INSERT INTO discord_sync_observations(personnel_id,guild_id,discord_user_id,status,expected_json,actual_roles_json,changes_json,error_text)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
            (pid,data.get('guild_id'),data.get('discord_user_id'),str(data.get('status') or 'UNKNOWN').upper(),Json(data.get('expected') or {}),Json(data.get('actual_roles') or []),Json(data.get('changes') or {}),data.get('error')))
    record_automation_event('DISCORD','ROLE_SYNC',str(data.get('status') or 'UNKNOWN').upper(),str(data.get('summary') or 'Discord personnel synchronization observed.'),personnel_id=pid,source_key=f'DISCORD-SYNC:{pid}',details={'changes':data.get('changes') or {},'error':data.get('error')})
    return {'ok':True}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=CONFIG.port, debug=True)




# ============================================================================
# 2026-08-26 COMMAND OPERATING SYSTEM — ACCESSION / PROMOTION / REPAIR / DELIVERY
# ============================================================================

def _staff_command_authorized(*roles):
    allowed=set(roles or ('s1','s3','battalion_hq','commander','admin'))
    return session.get('access_role') in allowed


def _accession_rows():
    # Keep Accessions fail-soft. This board summarizes several subsystems whose
    # optional tables/columns can be mid-migration on a live deployment; one
    # unavailable secondary metric must never take down Recruiting/S-1.
    try:
        rows=fetch_all("""SELECT rc.*,p.rank_code,p.first_name,p.last_name,p.unit_code,p.platoon,p.squad,p.lifecycle_state,p.field_status,
                                w.discord_user_id,w.guild_id,
                                wp.id AS packet_id,wp.status AS packet_status,wp.approved_at AS command_accepted_at,
                                (SELECT COUNT(*) FROM welcome_packet_tasks wi WHERE wi.packet_id=wp.id AND (wi.completed_at IS NOT NULL OR wi.waived_at IS NOT NULL OR UPPER(COALESCE(wi.status,''))='WAIVED')) AS packet_done,
                                (SELECT COUNT(*) FROM welcome_packet_tasks wi WHERE wi.packet_id=wp.id AND wi.required=TRUE) AS packet_total,
                                (SELECT document_number FROM personnel_documents po WHERE po.personnel_id=p.id AND UPPER(COALESCE(po.document_type,'')) IN ('ASSIGNMENT','TRANSFER') ORDER BY po.effective_date DESC NULLS LAST,po.created_at DESC LIMIT 1) AS assignment_order,
                                (SELECT status FROM discord_role_sync_queue q WHERE q.personnel_id=p.id ORDER BY requested_at DESC LIMIT 1) AS role_sync_status,
                                (SELECT error_text FROM discord_role_sync_queue q WHERE q.personnel_id=p.id ORDER BY requested_at DESC LIMIT 1) AS role_sync_error
                         FROM recruiting_cases rc
                         LEFT JOIN personnel p ON p.id=rc.personnel_id
                         LEFT JOIN website_member_links w ON w.personnel_id=p.id::text
                         LEFT JOIN LATERAL (SELECT * FROM welcome_packets w0 WHERE w0.personnel_id=p.id ORDER BY generated_at DESC NULLS LAST,updated_at DESC LIMIT 1) wp ON TRUE
                         WHERE rc.status NOT IN ('DENIED','CLOSED')
                         ORDER BY COALESCE(rc.approved_at,rc.created_at) DESC""")
    except Exception:
        log.exception('Accession pipeline enrichment failed; rendering core recruiting data')
        # Core fallback deliberately depends only on the two authoritative tables
        # required for the accession lifecycle. Optional packet/orders/Discord
        # values are filled with NULL so the page remains usable during a
        # partial migration or subsystem outage.
        rows=fetch_all("""SELECT rc.*,p.rank_code,p.first_name,p.last_name,p.unit_code,p.platoon,p.squad,p.lifecycle_state,p.field_status,
                                NULL::BIGINT AS discord_user_id,NULL::BIGINT AS guild_id,
                                NULL::UUID AS packet_id,NULL::TEXT AS packet_status,NULL::TIMESTAMPTZ AS command_accepted_at,
                                0::BIGINT AS packet_done,0::BIGINT AS packet_total,
                                NULL::TEXT AS assignment_order,NULL::TEXT AS role_sync_status,NULL::TEXT AS role_sync_error
                         FROM recruiting_cases rc
                         LEFT JOIN personnel p ON p.id=rc.personnel_id
                         WHERE rc.status NOT IN ('DENIED','CLOSED')
                         ORDER BY COALESCE(rc.approved_at,rc.created_at) DESC""")
    for r in rows:
        stages=[]
        stages.append(('APPLICATION', True, str(r.get('status') or 'FILED').replace('_',' ')))
        approved=bool(r.get('approved_at') or str(r.get('status') or '').upper() in {'REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING','ENLISTED'})
        stages.append(('APPROVED',approved,'APPROVED' if approved else 'COMMAND REVIEW'))
        stages.append(('DISCORD',bool(r.get('discord_user_id')),'LINKED' if r.get('discord_user_id') else 'WAITING'))
        dm=bool(r.get('credentials_sent_at')); dm_err=r.get('credentials_delivery_error')
        stages.append(('LOGIN DM',dm,'SENT' if dm else ('ERROR' if dm_err else 'QUEUED')))
        packet=bool(r.get('packet_id')); packet_accept=bool(r.get('command_accepted_at'))
        stages.append(('WELCOME PACKET',packet_accept,('ACCEPTED' if packet_accept else (str(r.get('packet_status') or 'NOT ISSUED').replace('_',' ')))))
        assigned=bool(r.get('unit_code') and r.get('platoon'))
        stages.append(('ASSIGNMENT',assigned,(f"{r.get('unit_code')} • {r.get('platoon')}" if assigned else 'LOCKED / PENDING')))
        stages.append(('ORDERS',bool(r.get('assignment_order')),r.get('assignment_order') or 'PENDING'))
        sync=str(r.get('role_sync_status') or '').upper(); stages.append(('DISCORD SYNC',sync in {'COMPLETE','CURRENT'},sync or 'NOT QUEUED'))
        score=sum(1 for _,ok,_ in stages if ok); r['accession_percent']=round(score/len(stages)*100); r['stages']=stages
        r['attention']= next((f'{name}: {detail}' for name,ok,detail in stages if not ok), 'COMPLETE')
    return rows

@app.get('/hq/accession-pipeline')
@login_required
def accession_pipeline():
    if not _staff_command_authorized('s1','battalion_hq','commander','admin'): abort(403)
    rows=_accession_rows()
    stats={'total':len(rows),'complete':sum(1 for r in rows if r['accession_percent']==100),'attention':sum(1 for r in rows if r['accession_percent']<100)}
    return render_template('accession_pipeline.html',rows=rows,stats=stats)

@app.post('/hq/accession-pipeline/<case_id>/repair')
@login_required
def accession_pipeline_repair(case_id):
    if not _staff_command_authorized('s1','battalion_hq','commander','admin'): abort(403)
    case=fetch_one('SELECT * FROM recruiting_cases WHERE id=%s',(case_id,))
    if not case: abort(404)
    actor=session.get('display_name') or session.get('username') or 'COMMAND'
    pid=case.get('personnel_id')
    if pid:
        try: ensure_welcome_packet(pid,case.get('id'))
        except Exception: log.exception('Accession repair packet check failed case=%s',case_id)
        try: ensure_assignment_artifacts(pid,actor)
        except Exception: log.exception('Accession repair assignment artifact check failed pid=%s',pid)
        enqueue_discord_role_sync(pid,f'ACCESSION PIPELINE REPAIR BY {actor}')
    if not case.get('credentials_sent_at') and case.get('discord_user_id'):
        execute("UPDATE recruiting_cases SET credentials_resend_requested_at=NOW(),credentials_resend_requested_by=%s,updated_at=NOW() WHERE id=%s",(actor,case_id))
    record_automation_event('ACCESSION','REPAIR','PENDING','Command requested full accession reconciliation.',personnel_id=pid,source_key=f'ACCESSION-REPAIR:{case_id}',details={'authority':actor})
    flash('ACCESSION REPAIR QUEUED — PACKET, ORDERS, LOGIN DELIVERY AND DISCORD SYNC WILL BE RECONCILED.','success')
    return redirect(url_for('accession_pipeline'))

@app.get('/hq/promotion-board')
@login_required
def promotion_board():
    if not _staff_command_authorized('s1','battalion_hq','commander','admin'): abort(403)
    people=fetch_all("SELECT * FROM personnel WHERE archived=FALSE AND separated_at IS NULL AND lifecycle_state NOT IN ('APPLICANT','PROSPECT') ORDER BY unit_code,platoon,squad,last_name")
    rows=[]
    for p in people:
        paths=promotion_eligibility(p)
        if not paths: continue
        path=paths[0]
        rows.append({'person':p,'path':path,'eligible':bool(path.get('eligible')),'requirements':path.get('requirements') or []})
    rows.sort(key=lambda x:(0 if x['eligible'] else 1, x['person'].get('last_name') or ''))
    return render_template('promotion_board.html',rows=rows)

@app.post('/hq/promotion-board/<personnel_id>/promote')
@login_required
def promotion_board_promote(personnel_id):
    if not _staff_command_authorized('s1','battalion_hq','commander','admin'): abort(403)
    p=fetch_one('SELECT * FROM personnel WHERE id=%s',(personnel_id,))
    if not p: abort(404)
    paths=promotion_eligibility(p); target=(request.form.get('target_rank') or '').strip().upper()
    chosen=next((x for x in paths if str(x.get('target') or '').upper()==target),None)
    if not chosen or not chosen.get('eligible'):
        flash('PROMOTION BLOCKED — CURRENT PUBLISHED REQUIREMENTS ARE NOT COMPLETE.','danger'); return redirect(url_for('promotion_board'))
    actor=session.get('display_name') or session.get('username') or 'COMMAND'
    process_rank_action(personnel_id,target,authority=actor,remarks=(request.form.get('remarks') or '').strip() or 'Approved by Command Promotion Board.')
    staff_log('S-1','PROMOTION_BOARD',f'Promotion to {target} approved from Command Promotion Board.',actor=actor,personnel_id=personnel_id)
    flash(f'PROMOTION COMPLETE — {target} ORDERS FILED, 201 UPDATED, DISCORD SYNC QUEUED.','success')
    return redirect(url_for('promotion_board'))

@app.post('/hq/sync-control/repair-stuck')
@login_required
def personnel_sync_repair_stuck():
    if not _staff_command_authorized('s1','battalion_hq','commander','admin'): abort(403)
    actor=session.get('display_name') or session.get('username') or 'COMMAND'
    stale=fetch_all("""SELECT id,personnel_id FROM discord_role_sync_queue
                       WHERE status='PENDING' AND requested_at < NOW()-INTERVAL '3 minutes'""")
    for q in stale:
        execute("UPDATE discord_role_sync_queue SET next_retry_at=NOW(),error_text=NULL WHERE id=%s",(q['id'],))
        record_automation_event('DISCORD','ROLE_SYNC','PENDING','Stale pending role sync released for immediate retry.',personnel_id=q['personnel_id'],source_key=f'DISCORD-SYNC:{q["personnel_id"]}',details={'authority':actor})
    flash(f'{len(stale)} STALE ROLE-SYNC JOB(S) RELEASED FOR IMMEDIATE BATTALION CLERK RETRY.','success')
    return redirect(url_for('personnel_sync_control'))

def _run_local_progression_reconcile():
    summary={'checked':0,'readiness_rechecked':0,'mos_rechecked':0,'ribbons_rechecked':0,'ribbons_awarded':0,'promotion_paths_rechecked':0,'errors':[]}
    people=fetch_all("SELECT * FROM personnel WHERE separated_at IS NULL AND archived=FALSE AND COALESCE(lifecycle_state,'') NOT IN ('SEPARATED','ARCHIVED')")
    for person in people:
        pid=person.get('id'); summary['checked']+=1
        try: sync_readiness(person); summary['readiness_rechecked']+=1
        except Exception as exc: summary['errors'].append(str(exc)[:160])
        try:
            person=fetch_one('SELECT * FROM personnel WHERE id=%s',(pid,)) or person
            if person.get('mos_code'): sync_mos_proficiency(person); summary['mos_rechecked']+=1
        except Exception as exc: summary['errors'].append(str(exc)[:160])
        try:
            before=int((fetch_one('SELECT COUNT(*) total FROM personnel_ribbons WHERE personnel_id=%s',(pid,)) or {}).get('total') or 0)
            ribbon_progress_for(pid,award_completed=True)
            after=int((fetch_one('SELECT COUNT(*) total FROM personnel_ribbons WHERE personnel_id=%s',(pid,)) or {}).get('total') or 0)
            summary['ribbons_rechecked']+=1; summary['ribbons_awarded']+=max(0,after-before)
        except Exception as exc: summary['errors'].append(str(exc)[:160])
        try:
            person=fetch_one('SELECT * FROM personnel WHERE id=%s',(pid,)) or person
            promotion_eligibility(soldier_view(person)); summary['promotion_paths_rechecked']+=1
        except Exception as exc: summary['errors'].append(str(exc)[:160])
    return summary

@app.post('/hq/system-health/repair')
@login_required
def system_health_repair():
    if not _staff_command_authorized('s1','s3','s4','training','battalion_hq','commander','admin'): abort(403)
    actor=session.get('display_name') or session.get('username') or 'COMMAND'; action=(request.form.get('repair') or 'ALL').upper()
    results=[]
    if action in {'ALL','SYNC'}:
        rows=fetch_all("SELECT id FROM personnel WHERE archived=FALSE AND separated_at IS NULL")
        for r in rows: enqueue_discord_role_sync(r['id'],f'SYSTEM HEALTH REPAIR BY {actor}')
        results.append(f'{len(rows)} role syncs queued')
    if action in {'ALL','PROGRESSION'}:
        try:
            prog=_run_local_progression_reconcile()
            results.append(f"progression checked {prog.get('checked',0)} Soldiers")
        except Exception as exc: results.append(f'progression repair error: {exc}')
    if action in {'ALL','ACCESSION'}:
        count=0
        for r in _accession_rows():
            if r.get('personnel_id') and r.get('accession_percent',100)<100:
                try: ensure_welcome_packet(r['personnel_id'],r.get('id'))
                except Exception: pass
                try: ensure_assignment_artifacts(r['personnel_id'],actor)
                except Exception: pass
                count+=1
        results.append(f'{count} accession cases inspected')
    if action in {'ALL','OPERATIONS'}:
        try:
            op=run_operation_maintenance(f'SYSTEM HEALTH / {actor}'); results.append(f"operation maintenance completed")
        except Exception as exc: results.append(f'operation repair error: {exc}')
    record_automation_event('SYSTEM','FIX_EVERYTHING','COMPLETE','; '.join(results),source_key=f'FIX-EVERYTHING:{date.today()}',details={'authority':actor,'repair':action})
    flash('SYSTEM REPAIR COMPLETE — '+ '; '.join(results),'success')
    return redirect(url_for('staff_reliability'))

@app.get('/internal/clerk/member-notifications/pending')
def clerk_member_notifications_pending():
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    guild_id=request.args.get('guild_id')
    rows=fetch_all("""SELECT sn.id,sn.personnel_id,sn.notification_type,sn.title,sn.message,sn.target_endpoint,sn.target_anchor,
                            w.discord_user_id,w.guild_id,p.rank_code,p.first_name,p.last_name
                     FROM soldier_notifications sn
                     JOIN personnel p ON p.id=sn.personnel_id
                     JOIN website_member_links w ON w.personnel_id=p.id::text
                     LEFT JOIN discord_notification_delivery d ON d.notification_id=sn.id
                     WHERE (UPPER(sn.notification_type) IN ('AWARD','PROMOTION') OR UPPER(sn.title) LIKE 'PROMOTION TO %')
                       AND (d.notification_id IS NULL OR (d.status='ERROR' AND COALESCE(d.attempt_count,0)<3 AND d.attempted_at<NOW()-INTERVAL '5 minutes'))
                       AND (%s IS NULL OR w.guild_id=%s)
                     ORDER BY sn.created_at LIMIT 50""",(guild_id,guild_id))
    return {'ok':True,'notifications':rows}

@app.post('/internal/clerk/member-notifications/<notification_id>/delivered')
def clerk_member_notification_delivered(notification_id):
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    data=request.get_json(silent=True) or {}; ok=bool(data.get('ok')); err=data.get('error')
    row=fetch_one('SELECT personnel_id,notification_type,title FROM soldier_notifications WHERE id=%s',(notification_id,))
    if not row: return {'ok':False,'error':'notification not found'},404
    execute("""INSERT INTO discord_notification_delivery(notification_id,personnel_id,status,error_text,attempted_at,attempt_count,delivered_at)
               VALUES(%s,%s,%s,%s,NOW(),1,CASE WHEN %s THEN NOW() ELSE NULL END)
               ON CONFLICT(notification_id) DO UPDATE SET status=EXCLUDED.status,error_text=EXCLUDED.error_text,
                    attempted_at=NOW(),attempt_count=discord_notification_delivery.attempt_count+1,delivered_at=CASE WHEN %s THEN NOW() ELSE discord_notification_delivery.delivered_at END""",
            (notification_id,row['personnel_id'],'SENT' if ok else 'ERROR',err,ok,ok))
    return {'ok':True}

@app.get('/internal/clerk/operations/lifecycle-pending')
def clerk_operation_lifecycle_pending():
    if not _clerk_authorized(): return {'ok':False,'error':'authorization required'},401
    rows=fetch_all("""SELECT id,operation_number,title,start_at,duration_minutes,status,lifecycle_status,aar_filed_at
                     FROM operations WHERE UPPER(COALESCE(status,'')) NOT IN ('CANCELLED','CANCELED','DELETED')
                       AND (UPPER(COALESCE(status,'')) IN ('COMPLETED','CLOSED') OR UPPER(COALESCE(lifecycle_status,''))='CLOSED')
                       AND aar_filed_at IS NULL ORDER BY COALESCE(start_at,created_at) ASC LIMIT 25""")
    return {'ok':True,'operations':rows}
