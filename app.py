from __future__ import annotations
from psycopg.types.json import Json

import logging
import re
import secrets
import string
from datetime import date, datetime, timezone

from flask import Flask, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from auth import login_required, role_required
from config import CONFIG
from database import execute, fetch_all, fetch_one, init_schema

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("5th-cavalry-web")

app = Flask(__name__)
app.secret_key = CONFIG.secret_key
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


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
        ON CONFLICT (username) DO NOTHING
        """,
        (CONFIG.admin_username, generate_password_hash(CONFIG.admin_password)),
    )
    log.info("Initial site administrator ensured: %s", CONFIG.admin_username)


try:
    bootstrap()
except Exception:
    log.exception("Website bootstrap failed. Check DATABASE_URL and PostgreSQL connectivity.")




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


def write_service_entry(personnel_id, entry_type: str, title: str, narrative: str = "", authority: str | None = None, reference_number: str | None = None, entry_date: date | None = None) -> None:
    execute(
        """
        INSERT INTO personnel_service_history
        (personnel_id, entry_date, entry_type, title, narrative, authority, reference_number)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        """,
        (personnel_id, entry_date or date.today(), entry_type, title, narrative, authority, reference_number),
    )


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
    # Battalion Clerk may provide an internal attendance signal. It is deliberately
    # never exposed as Discord/bot terminology in member-facing pages.
    clerk = fetch_one(
        """
        SELECT MAX(vs.ended_at) AS last_at
        FROM website_member_links wml
        JOIN voice_sessions vs ON vs.guild_id=wml.guild_id AND vs.discord_user_id=wml.discord_user_id
        WHERE wml.personnel_id=%s
        """,
        (str(personnel["id"]),),
    )
    if clerk and clerk.get("last_at"):
        clerk_date = clerk["last_at"].date()
        if not last_date or clerk_date > last_date:
            last_date = clerk_date
    return last_date or personnel.get("date_joined")


def derive_weapon_state(weapon: dict | None, personnel: dict | None):
    if not weapon:
        return None
    record = dict(weapon)
    last_duty = _last_duty_activity(personnel)
    inactive_days = max((date.today() - last_duty).days, 0) if last_duty else 0
    rounds = int(record.get("rounds_since_cleaning") or 0)
    stress = max(inactive_days, rounds // 45)
    if inactive_days >= 60 or rounds >= 1200:
        state, pct, stage = "UNSERVICEABLE", 20, 5
    elif inactive_days >= 40 or rounds >= 850:
        state, pct, stage = "MAINTENANCE REQUIRED", 42, 4
    elif inactive_days >= 25 or rounds >= 550:
        state, pct, stage = "CLEANING REQUIRED", 58, 3
    elif inactive_days >= 14 or rounds >= 300:
        state, pct, stage = "FOULED", 72, 2
    elif inactive_days >= 7 or rounds >= 150:
        state, pct, stage = "FIELD WORN", 86, 1
    else:
        state, pct, stage = "SERVICEABLE", 100, 0
    record.update({"display_state": state, "display_condition_percent": min(record.get("condition_percent") or 100, pct), "dirt_stage": stage, "inactive_days": inactive_days, "last_duty_date": last_duty, "stress_index": stress})
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
    return {
        "site_name": "5th Cavalry Regiment",
        "unit_name": "1st Battalion, 5th Cavalry Regiment",
        "today": date.today(),
    }



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


def process_appointment_action(personnel_id, appointment_code: str, organization=None, status="PERMANENT", effective_date=None, authority=None, order_number=None, remarks=None, unit_node_id=None):
    appt = fetch_one("SELECT * FROM appointment_catalog WHERE appointment_code=%s AND is_active=TRUE", (appointment_code,))
    if not appt:
        raise ValueError("Appointment not found")
    eff = effective_date or date.today()
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
    sync_access_from_appointments(personnel_id)


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
    sync_access_from_appointments(row["personnel_id"])


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


def activity_classification(person):
    """Translate last known participation into period-appropriate readiness language."""
    stamp = person.get("activity_last_duty_at") or person.get("activity_last_seen_at") or person.get("updated_at") or person.get("created_at")
    if not stamp:
        return "CURRENT", 0
    if hasattr(stamp, "date"):
        delta = datetime.now(stamp.tzinfo) - stamp if getattr(stamp, "tzinfo", None) else datetime.now() - stamp
        days = max(0, delta.days)
    else:
        days = 0
    if days <= 7:
        return "CURRENT", days
    if days <= 14:
        return "ACTIVITY DECLINING", days
    if days <= 21:
        return "READINESS DEFICIENCY", days
    if days <= 30:
        return "INACTIVE", days
    return "ADMINISTRATIVE REVIEW", days


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


def soldier_readiness(person):
    """Compute a restrained staff readiness classification from existing records."""
    activity, inactive_days = activity_classification(person)
    weapon = fetch_one(
        """SELECT wi.* FROM weapon_issue_history wih
           JOIN weapon_inventory wi ON wi.id=wih.weapon_id
           WHERE wih.personnel_id=%s AND wih.is_current=TRUE LIMIT 1""",
        (person["id"],),
    )
    deficiencies = []
    if activity in {"READINESS DEFICIENCY","INACTIVE","ADMINISTRATIVE REVIEW"}:
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

    duty = str(person.get("duty_status") or "Present for Duty").upper()
    if duty in {"HOSPITAL","WIA","AWOL","INACTIVE"}:
        deficiencies.append(("PERSONNEL", duty))

    if any(x[1] in {"ADMINISTRATIVE REVIEW","UNSERVICEABLE","AWOL"} for x in deficiencies):
        overall = "NOT COMBAT EFFECTIVE"
    elif duty in {"HOSPITAL","WIA"}:
        overall = "LIMITED"
    elif deficiencies:
        overall = "COMBAT EFFECTIVE — DEFICIENCIES NOTED"
    else:
        overall = "COMBAT EFFECTIVE"
    return {"overall": overall, "activity": activity, "inactive_days": inactive_days,
            "weapon": weapon, "deficiencies": deficiencies}


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
    payload = {
        "companies":[{"unit_code":x["unit"]["unit_code"],"display_name":x["unit"]["display_name"],"summary":x["summary"]} for x in company_rows],
        "vacancies":vacancy_report(),
    }
    execute(
        """INSERT INTO morning_report_snapshots
           (report_date,prepared_by,battalion_assigned,battalion_present,battalion_combat_effective,
            battalion_inactive,battalion_wia,battalion_leave,battalion_hospital,battalion_replacements,
            battalion_deros_30,data_json)
           VALUES(CURRENT_DATE,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT(report_date) DO UPDATE SET
             prepared_by=EXCLUDED.prepared_by,battalion_assigned=EXCLUDED.battalion_assigned,
             battalion_present=EXCLUDED.battalion_present,battalion_combat_effective=EXCLUDED.battalion_combat_effective,
             battalion_inactive=EXCLUDED.battalion_inactive,battalion_wia=EXCLUDED.battalion_wia,
             battalion_leave=EXCLUDED.battalion_leave,battalion_hospital=EXCLUDED.battalion_hospital,
             battalion_replacements=EXCLUDED.battalion_replacements,battalion_deros_30=EXCLUDED.battalion_deros_30,
             data_json=EXCLUDED.data_json""",
        (prepared_by,total["assigned"],total["present"],total["combat_effective"],total["inactive"],
         total["wia"],total["leave"],total["hospital"],total["replacements"],len(forecast),Json(payload)),
    )


def weapon_condition_from_rounds_and_time(weapon, person=None):
    """Derive a serviceability state without pretending the site controls in-game reliability."""
    total = int(weapon.get("rounds_fired") or 0)
    since_clean = int(weapon.get("rounds_since_cleaning") or 0)
    pct = int(weapon.get("condition_percent") or 100)
    last_clean = weapon.get("last_cleaned_at")
    days_since_clean = 0
    if last_clean:
        now = datetime.now(last_clean.tzinfo) if getattr(last_clean, "tzinfo", None) else datetime.now()
        days_since_clean = max(0, (now - last_clean).days)

    inactivity = 0
    if person:
        _, inactivity = activity_classification(person)

    score = pct
    score -= min(45, since_clean // 12)
    score -= min(15, days_since_clean // 4)
    if inactivity > 14:
        score -= min(18, (inactivity - 14))
    score = max(0, min(100, score))

    if score <= 15:
        state = "UNSERVICEABLE"
    elif score <= 30:
        state = "MAINTENANCE REQUIRED"
    elif score <= 48:
        state = "CLEANING REQUIRED"
    elif score <= 65:
        state = "FOULED"
    elif score <= 82:
        state = "FIELD WORN"
    else:
        state = "SERVICEABLE"
    return state, score


def refresh_weapon_condition(weapon_id):
    weapon = fetch_one("SELECT * FROM weapon_inventory WHERE id=%s", (weapon_id,))
    if not weapon:
        return None
    issue = fetch_one("SELECT personnel_id FROM weapon_issue_history WHERE weapon_id=%s AND is_current=TRUE", (weapon_id,))
    person = fetch_one("SELECT * FROM personnel WHERE id=%s", (issue["personnel_id"],)) if issue and issue.get("personnel_id") else None
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
        new_pct = min(100,max(int(weapon.get("condition_percent") or 0),92))
        new_state = "SERVICEABLE"
        execute("""UPDATE weapon_inventory SET rounds_since_cleaning=0,last_cleaned_at=NOW(),
                   condition_percent=%s,condition_state=%s,updated_at=NOW() WHERE id=%s""",
                (new_pct,new_state,weapon_id))
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
            (weapon_id,personnel_id,action,before,new_state,int(weapon.get("rounds_fired") or 0),performed_by,remarks))
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
    execute("""UPDATE weapon_inventory SET rounds_fired=COALESCE(rounds_fired,0)+%s,
               rounds_since_cleaning=COALESCE(rounds_since_cleaning,0)+%s,
               last_fired_at=NOW(),updated_at=NOW() WHERE id=%s""",(rounds,rounds,weapon_id))
    refresh_weapon_condition(weapon_id)


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
    row = fetch_one("SELECT COUNT(*)+1 AS n FROM operations WHERE EXTRACT(YEAR FROM COALESCE(start_at,NOW()))=EXTRACT(YEAR FROM CURRENT_DATE)")
    n = int(row["n"] or 1) if row else 1
    return f"OPORD {n:02d}-{str(date.today().year)[-2:]}"


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

    # If an individual M16 is issued, apply ammunition expenditure to that rifle.
    if int(rounds_expended or 0) > 0:
        weapon = fetch_one(
            """SELECT wi.id FROM weapon_issue_history wih
               JOIN weapon_inventory wi ON wi.id=wih.weapon_id
               WHERE wih.personnel_id=%s AND wih.is_current=TRUE LIMIT 1""",
            (personnel_id,),
        )
        if weapon:
            record_weapon_rounds(
                weapon["id"],int(rounds_expended),personnel_id,operation_id,
                "OPERATION RECORD",credited_by,
                f"Round expenditure recorded for {title}.",
            )

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


def personal_operations(personnel_id):
    return fetch_all(
        """SELECT opart.*,o.title,o.operation_number,o.area_of_operations,o.start_at,o.result,o.status
           FROM operation_participation opart
           JOIN operations o ON o.id=opart.operation_id
           WHERE opart.personnel_id=%s
           ORDER BY COALESCE(o.start_at,opart.credited_at) DESC""",
        (personnel_id,),
    )


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
    write_service_entry(personnel_id,"TRAINING","DUTY QUALIFICATION",
                        f"Qualified for HLL: Vietnam duty role: {q['display_name']}.",
                        authority,q["code"],qdate)


def training_deficiencies():
    return fetch_all(
        """SELECT p.id,p.rank_code,p.first_name,p.last_name,p.unit_code,
                  dqt.display_name,dqt.battlefield_unit,pdq.expiration_date,pdq.status
           FROM personnel_duty_qualifications pdq
           JOIN personnel p ON p.id=pdq.personnel_id
           JOIN duty_qualification_types dqt ON dqt.id=pdq.qualification_type_id
           WHERE pdq.status='EXPIRED'
              OR (pdq.expiration_date IS NOT NULL AND pdq.expiration_date <= CURRENT_DATE + INTERVAL '30 days')
           ORDER BY pdq.expiration_date,p.last_name""") if database_ready() else []

def linked_personnel():
    if session.get("personnel_id"):
        return fetch_one("SELECT * FROM personnel WHERE id=%s", (session["personnel_id"],))
    if not session.get("user_id"):
        return None
    return fetch_one(
        """
        SELECT p.* FROM personnel p
        JOIN user_personnel_links upl ON upl.personnel_id=p.id
        WHERE upl.user_id=%s
        """,
        (session["user_id"],),
    )


def soldier_view(personnel: dict | None) -> dict | None:
    """Presentation-only derived tour fields; stored records remain untouched."""
    if not personnel:
        return None
    record = dict(personnel)
    arrival = record.get("rvn_arrival_date")
    deros = record.get("deros_date")
    record["days_in_country"] = max((date.today() - arrival).days, 0) if arrival else None
    record["days_to_deros"] = max((deros - date.today()).days, 0) if deros else None

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
        SELECT dm.guild_id, dm.discord_user_id, dm.username, dm.display_name, dm.last_seen_at
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


@app.get("/")
def home():
    if not database_ready():
        return render_template("setup.html")
    operation = fetch_one("SELECT * FROM operations ORDER BY operation_date NULLS FIRST, created_at DESC LIMIT 1")
    org = fetch_all("SELECT unit_code,display_name,unit_type FROM unit_nodes WHERE is_active=TRUE ORDER BY sort_order")
    personnel = soldier_view(linked_personnel()) if session.get("user_id") else None
    return render_template("home.html", operation=operation, org=org, personnel=personnel)


@app.route("/login", methods=["GET", "POST"])
@app.route("/report-for-duty", methods=["GET", "POST"])
def login():
    if not database_ready():
        return render_template("setup.html")
    if request.method == "POST":
        roster_number = request.form.get("roster_number", "").strip().upper()
        field_code = request.form.get("field_code", "").strip().upper()
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
        if not card or not check_password_hash(card["field_code_hash"], field_code):
            flash("BATTLE ROSTER CREDENTIALS COULD NOT BE VERIFIED.", "danger")
        else:
            user_id = card.get("user_id") or ensure_member_site_user(card["personnel_id"], card["roster_number"])
            session.clear()
            session["user_id"] = str(user_id)
            session["personnel_id"] = str(card["personnel_id"])
            session["username"] = card["roster_number"]
            session["access_role"] = "member"
            execute("UPDATE battle_roster_cards SET last_used_at=NOW() WHERE id=%s", (card["id"],))
            flash(f"DUTY STATUS CONFIRMED — {card['rank_code']} {card['last_name'].upper()}.", "success")
            return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/staff-access", methods=["GET", "POST"])
def staff_access():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = fetch_one("SELECT * FROM site_users WHERE username=%s AND is_active=TRUE AND access_role<>'member'", (username,))
        if not user or not check_password_hash(user["password_hash"], password):
            flash("STAFF CREDENTIALS COULD NOT BE VERIFIED.", "danger")
        else:
            session.clear()
            session["user_id"] = str(user["id"])
            session["username"] = user["username"]
            session["access_role"] = user["access_role"]
            return redirect(url_for("hq") if user["access_role"] in {"battalion_hq","admin"} else url_for("dashboard"))
    return render_template("staff_access.html")


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


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


@app.get("/my-201-file")
@login_required
def my_201_file():
    personnel = soldier_view(linked_personnel())
    qualifications = []
    equipment = []
    awards = []
    activity = []
    if personnel:
        qualifications = fetch_all("SELECT * FROM qualifications WHERE personnel_id=%s ORDER BY qualification_name", (personnel["id"],))
        equipment = fetch_all("SELECT * FROM equipment_issues WHERE personnel_id=%s ORDER BY item_type,nomenclature", (personnel["id"],))
        awards = fetch_all("SELECT * FROM personnel_awards WHERE personnel_id=%s ORDER BY award_date DESC", (personnel["id"],))
        activity = fetch_all(
            "SELECT * FROM personnel_activity_credit WHERE personnel_id=%s ORDER BY activity_date DESC, created_at DESC LIMIT 50",
            (personnel["id"],),
        )
        service_history = fetch_all("SELECT * FROM personnel_service_history WHERE personnel_id=%s ORDER BY entry_date DESC, created_at DESC LIMIT 100", (personnel["id"],))
        assignments = fetch_all("SELECT * FROM assignment_history WHERE personnel_id=%s ORDER BY effective_date DESC, created_at DESC", (personnel["id"],))
        roster_card = battle_roster_for(personnel)
        weapon = current_weapon_for(personnel)
    else:
        service_history, assignments, roster_card, weapon = [], [], None, None
    return render_template(
        "personnel_file.html",
        personnel=personnel,
        qualifications=qualifications,
        equipment=equipment,
        awards=awards,
        activity=activity,
        service_history=service_history,
        assignments=assignments,
        promotions=promotions,
        appointments=appointments,
        roster_card=roster_card,
        weapon=weapon,
        chain_of_command=chain_of_command_for(personnel),
        readiness=soldier_readiness(personnel),
        tour_phase_record=tour_phase(personnel),
    )




@app.get("/battle-roster-card")
@login_required
def battle_roster_card():
    personnel = soldier_view(linked_personnel())
    return render_template("battle_roster_card.html", personnel=personnel, roster_card=battle_roster_for(personnel))

@app.get("/battalion")
@app.get("/organization")
def organization():
    nodes = fetch_all(
        """SELECT id,parent_id,unit_code,display_name,unit_type,sort_order
           FROM unit_nodes WHERE is_active=TRUE ORDER BY sort_order,display_name"""
    ) if database_ready() else []
    roster = fetch_all(
        """SELECT p.id,p.rank_code,p.last_name,p.first_name,p.unit_code,p.platoon,p.squad,
                  p.duty_position,p.field_status,p.unit_node_id
           FROM personnel p
           ORDER BY p.unit_code,p.platoon NULLS FIRST,p.squad NULLS FIRST,p.last_name"""
    ) if database_ready() else []
    appointments = fetch_all(
        """SELECT pa.personnel_id,pa.unit_node_id,pa.appointment_status,
                  ac.appointment_name,ac.appointment_code
           FROM personnel_appointments pa
           JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code
           WHERE pa.is_current=TRUE
           ORDER BY ac.sort_order"""
    ) if database_ready() else []
    appt_map = {}
    for row in appointments:
        appt_map.setdefault(str(row["personnel_id"]), []).append(row)

    # Build a nested tree for the roster board.
    children = {}
    node_map = {str(n["id"]): n for n in nodes}
    for n in nodes:
        children.setdefault(str(n.get("parent_id") or "ROOT"), []).append(n)
    return render_template("organization.html", nodes=nodes, roster=roster,
                           children=children, node_map=node_map, appt_map=appt_map)


@app.get("/company/<unit_code>")
def company(unit_code: str):
    unit = fetch_one("SELECT * FROM unit_nodes WHERE unit_code=%s AND is_active=TRUE", (unit_code,)) if database_ready() else None
    if not unit:
        return render_template("company.html", unit=None, roster=[], platoons=[], leadership={}, appointment_map={}), 404
    ids = unit_descendant_ids(unit["id"])
    roster = fetch_all(
        """SELECT * FROM personnel
           WHERE unit_node_id = ANY(%s)
              OR (unit_node_id IS NULL AND unit_code ILIKE %s)
           ORDER BY platoon NULLS FIRST,squad NULLS FIRST,last_name""",
        (ids, f"{unit_code.split('-')[0]}/%"),
    )
    platoons = fetch_all(
        "SELECT * FROM unit_nodes WHERE parent_id=%s AND unit_type='Platoon' AND is_active=TRUE ORDER BY sort_order",
        (unit["id"],),
    )
    current_appts = fetch_all(
        """SELECT pa.*,ac.appointment_name,ac.appointment_code,p.rank_code,p.last_name,p.first_name
           FROM personnel_appointments pa
           JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code
           JOIN personnel p ON p.id=pa.personnel_id
           WHERE pa.is_current=TRUE AND pa.unit_node_id = ANY(%s)
           ORDER BY ac.sort_order""",
        (ids,),
    )
    appointment_map = {}
    for ap in current_appts:
        appointment_map.setdefault(str(ap["personnel_id"]), []).append(ap)

    leadership = {
        "co": appointment_for_node("CO_CO", unit["id"]),
        "xo": appointment_for_node("CO_XO", unit["id"]),
        "first_sergeant": appointment_for_node("CO_1SG", unit["id"]),
    }
    return render_template("company.html", unit=unit, roster=roster, platoons=platoons,
                           leadership=leadership, appointment_map=appointment_map)


@app.get("/platoon/<unit_code>")
def platoon(unit_code: str):
    unit = fetch_one("SELECT * FROM unit_nodes WHERE unit_code=%s AND unit_type='Platoon' AND is_active=TRUE", (unit_code,))
    if not unit:
        return render_template("platoon.html", unit=None, squads=[], roster=[], leadership={}), 404
    ids = unit_descendant_ids(unit["id"])
    squads = fetch_all("SELECT * FROM unit_nodes WHERE parent_id=%s AND unit_type='Squad' AND is_active=TRUE ORDER BY sort_order", (unit["id"],))
    roster = fetch_all("SELECT * FROM personnel WHERE unit_node_id = ANY(%s) ORDER BY squad NULLS FIRST,last_name", (ids,))
    leadership = {
        "leader": appointment_for_node("PL", unit["id"]),
        "sergeant": appointment_for_node("PSG", unit["id"]),
        "rto": appointment_for_node("PLT_RTO", unit["id"]),
    }
    return render_template("platoon.html", unit=unit, squads=squads, roster=roster, leadership=leadership)


@app.get("/my-soldiers")
@login_required
def my_soldiers():
    personnel = soldier_view(linked_personnel())
    soldiers, scope = scoped_personnel_for(personnel)
    return render_template("my_soldiers.html", personnel=personnel, soldiers=soldiers, scope=scope)




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
            execute(
                """INSERT INTO operations
                   (title,operation_number,operation_type,area_of_operations,commander,h_hour,
                    situation,mission,execution,service_support,command_signal,status,start_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PLANNING',%s)""",
                (request.form.get("title"),opnum,request.form.get("operation_type") or "OFFICIAL OPERATION",
                 request.form.get("area_of_operations"),request.form.get("commander"),
                 request.form.get("h_hour"),request.form.get("situation"),request.form.get("mission"),
                 request.form.get("execution"),request.form.get("service_support"),
                 request.form.get("command_signal"),request.form.get("start_at") or None),
            )
            flash(f"{opnum} FILED IN S-3 OPERATIONS.","success")
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
            execute(
                """INSERT INTO personnel_recommendations
                   (personnel_id,operation_id,recommendation_type,recommended_action,justification,
                    recommending_personnel_id)
                   VALUES(%s,%s,%s,%s,%s,%s)""",
                (request.form.get("personnel_id"),request.form.get("operation_id") or None,
                 request.form.get("recommendation_type") or "PERSONNEL ACTION",
                 request.form.get("recommended_action"),request.form.get("justification"),
                 (linked_personnel() or {}).get("id")),
            )
            flash("PERSONNEL ACTION RECOMMENDATION FORWARDED.","success")
        return redirect(url_for("operations"))

    current=fetch_all(
        """SELECT * FROM operations WHERE status <> 'COMPLETED'
           ORDER BY COALESCE(start_at,NOW()) ASC,created_at DESC"""
    ) if database_ready() else []
    completed=fetch_all(
        """SELECT o.*,aar.ammunition_expended,aar.filed_at
           FROM operations o LEFT JOIN after_action_reports aar ON aar.operation_id=o.id
           WHERE o.status='COMPLETED'
           ORDER BY COALESCE(o.completed_at,o.start_at) DESC LIMIT 100"""
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
    return render_template("operations.html",current=current,completed=completed,
                           personnel_list=personnel_list,units=units,
                           participants=participants,op_units=op_units,aars=aars,
                           recommendations=recommendations)


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
    return render_template("operation_detail.html",op=op,participants=participants,units=units,aar=aar,journal=journal,photos=photos)


@app.route("/training-office", methods=["GET","POST"])
def training_office_phase9():
    if request.method=="POST":
        if not session.get("user_id"): return redirect(url_for("report_for_duty"))
        role=session.get("access_role")
        action=request.form.get("action")
        authority=session.get("display_name") or session.get("username") or "TRAINING OFFICE"
        if action=="award_qualification":
            if role not in {"training","company_hq","battalion_hq","commander","admin"}: abort(403)
            award_duty_qualification(
                request.form.get("personnel_id"),request.form.get("qualification_type_id"),
                request.form.get("instructor_personnel_id") or None,
                request.form.get("qualified_date") or date.today(),
                request.form.get("expiration_date") or None,
                request.form.get("remarks") or None,authority)
            flash("DUTY QUALIFICATION ENTERED IN THE SOLDIER'S TRAINING RECORD.","success")
        elif action=="request_training":
            p=linked_personnel()
            if not p: abort(403)
            execute("""INSERT INTO training_requests(personnel_id,qualification_type_id,request_type,remarks)
                       VALUES(%s,%s,'DUTY QUALIFICATION',%s)""",
                    (p["id"],request.form.get("qualification_type_id"),request.form.get("remarks")))
            flash("REQUEST FOR TRAINING FORWARDED TO THE TRAINING OFFICE.","success")
        return redirect(url_for("training_office_phase9"))
    catalog=duty_qualification_catalog()
    personnel_list=fetch_all("SELECT * FROM personnel ORDER BY last_name,first_name") if database_ready() else []
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
    return render_template("training_office.html",catalog=catalog,personnel_list=personnel_list,
                           records=records,requests=requests,mine=mine,deficiencies=training_deficiencies())

@app.route("/supply", methods=["GET","POST"])
@login_required
def supply():
    personnel = soldier_view(linked_personnel())
    if request.method == "POST":
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
        weapon = refresh_weapon_condition(weapon["id"])
    issued_equipment=current_equipment_for(personnel["id"]) if personnel else []
    catalog=fetch_all("SELECT * FROM supply_item_catalog WHERE is_active=TRUE ORDER BY category,sort_order")
    personnel_list=fetch_all("SELECT id,rank_code,last_name,first_name,unit_code,platoon,squad FROM personnel ORDER BY last_name")
    weapons=fetch_all("""SELECT wi.*,p.rank_code,p.last_name,p.first_name,wih.personnel_id
                         FROM weapon_inventory wi
                         LEFT JOIN weapon_issue_history wih ON wih.weapon_id=wi.id AND wih.is_current=TRUE
                         LEFT JOIN personnel p ON p.id=wih.personnel_id
                         ORDER BY wi.rack_number NULLS LAST,wi.serial_number""")
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
                                  WHERE eih.is_current=TRUE ORDER BY sic.sort_order,p.last_name""")
    companies=fetch_all("SELECT * FROM unit_nodes WHERE unit_type='Company' AND is_active=TRUE ORDER BY sort_order")
    stock={str(c["id"]):company_supply_readiness(c["id"]) for c in companies}
    requisitions=fetch_all("""SELECT sr.*,sic.item_name,un.display_name AS requesting_unit
                              FROM supply_requisitions sr JOIN supply_item_catalog sic ON sic.item_code=sr.item_code
                              LEFT JOIN unit_nodes un ON un.id=sr.requesting_unit_node_id
                              ORDER BY CASE sr.status WHEN 'SUBMITTED' THEN 0 WHEN 'APPROVED' THEN 1 ELSE 2 END,sr.submitted_at DESC LIMIT 100""")
    maintenance_log=fetch_all("""SELECT wml.*,wi.serial_number,wi.rack_number,p.rank_code,p.last_name
                                 FROM weapon_maintenance_log wml JOIN weapon_inventory wi ON wi.id=wml.weapon_id
                                 LEFT JOIN personnel p ON p.id=wml.personnel_id
                                 ORDER BY wml.performed_at DESC LIMIT 40""")
    return render_template("supply.html",personnel=personnel,weapon=weapon,issued_equipment=issued_equipment,
        personal_ops=personal_ops,
        duty_quals=duty_quals,
                           catalog=catalog,personnel_list=personnel_list,weapons=weapons,
                           equipment_issues=equipment_issues,companies=companies,stock=stock,
                           requisitions=requisitions,maintenance_log=maintenance_log)


@app.get("/arms-room")
@login_required
def arms_room():
    return redirect(url_for("supply"))


@app.route("/morning-report", methods=["GET","POST"])
def morning_report():
    if request.method == "POST":
        if not session.get("user_id"):
            return redirect(url_for("report_for_duty"))
        save_morning_report_snapshot(session.get("display_name") or session.get("username") or "BATTALION CLERK")
        flash("MORNING REPORT FILED IN THE BATTALION ARCHIVE.", "success")
        return redirect(url_for("morning_report"))
    company_rows,total = morning_report_data() if database_ready() else ([],{})
    forecast = deros_forecast(90) if database_ready() else []
    vacancies = vacancy_report() if database_ready() else []
    snapshots = fetch_all("SELECT * FROM morning_report_snapshots ORDER BY report_date DESC LIMIT 30") if database_ready() else []
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
    return render_template("readiness.html",personnel=personnel,individual=individual,
                           scope=scope,scoped=scoped)


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
    execute("UPDATE duty_status_history SET is_current=FALSE,ended_at=NOW() WHERE personnel_id=%s AND is_current=TRUE",(personnel_id,))
    execute("INSERT INTO duty_status_history(personnel_id,duty_status,authority,remarks) VALUES(%s,%s,%s,%s)",
            (personnel_id,status,session.get("display_name") or session.get("username"),request.form.get("remarks")))
    execute("UPDATE personnel SET duty_status=%s,updated_at=NOW() WHERE id=%s",(status,personnel_id))
    write_service_entry(personnel_id,"DUTY STATUS",status,
                        f"Duty status changed to {status}.",session.get("display_name") or session.get("username"),
                        None,date.today())
    flash("DUTY STATUS ENTERED ON PERSONNEL RECORD.", "success")
    return redirect(request.referrer or url_for("morning_report"))


@app.get("/personnel")
def personnel_office():
    personnel = soldier_view(linked_personnel()) if database_ready() else None
    roster = fetch_all("SELECT rank_code,last_name,first_name,unit_code,platoon,squad,duty_position,field_status,readiness_status FROM personnel ORDER BY unit_code,last_name LIMIT 150") if database_ready() else []
    return render_template("personnel.html", personnel=personnel, roster=roster)




@app.get("/training")
def training():
    personnel = soldier_view(linked_personnel()) if database_ready() else None
    qualifications = []
    if personnel:
        qualifications = fetch_all("SELECT * FROM qualifications WHERE personnel_id=%s ORDER BY qualification_name", (personnel["id"],))
    return render_template("training.html", personnel=personnel, qualifications=qualifications)


@app.get("/orders")
def orders():
    ops = fetch_all("SELECT * FROM operations ORDER BY operation_date DESC NULLS LAST, created_at DESC") if database_ready() else []
    return render_template("orders.html", operations=ops)


@app.get("/recruiting")
def recruiting():
    return render_template("recruiting.html")


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
                service_number = allocate_service_number()
                personnel = fetch_one(
                    """
                    INSERT INTO personnel
                    (service_number,first_name,last_name,rank_code,mos_code,duty_position,unit_code,platoon,squad,date_joined,rvn_arrival_date,deros_date,field_status,readiness_status,readiness_percent,duty_status,roster_entered_at)
                    VALUES (%s,%s,%s,'PVT',%s,%s,%s,%s,%s,%s,%s,%s,'Assigned','PROCESSING',25,'PRESENT FOR DUTY',%s)
                    RETURNING *
                    """,
                    (
                        service_number, first_name, last_name,
                        request.form.get("mos_code", "11B").strip() or "11B",
                        request.form.get("duty_position", "Rifleman").strip() or "Rifleman",
                        request.form.get("unit_code", "A/1-5 CAV").strip() or "A/1-5 CAV",
                        request.form.get("platoon", "").strip() or None,
                        request.form.get("squad", "").strip() or None,
                        request.form.get("date_joined") or date.today(),
                        request.form.get("rvn_arrival_date") or date.today(),
                        request.form.get("deros_date") or None,
                        date.today(),
                    ),
                )
                execute(
                    "INSERT INTO assignment_history (personnel_id,unit_code,platoon,squad,duty_position,effective_date) VALUES (%s,%s,%s,%s,%s,%s)",
                    (personnel["id"], personnel["unit_code"], personnel.get("platoon"), personnel.get("squad"), personnel.get("duty_position"), personnel.get("date_joined") or date.today()),
                )
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
            process_assignment_action(
                request.form.get("personnel_id"),
                request.form.get("unit_node_id"),
                request.form.get("duty_position") or None,
                request.form.get("effective_date") or date.today(),
                request.form.get("authority") or None,
                request.form.get("order_number") or None,
                request.form.get("remarks") or None,
            )
            flash("ASSIGNMENT ORDERS FILED. ROSTER, CHAIN OF COMMAND AND 201 FILE UPDATED.", "success")
        elif action == "relieve_appointment":
            relieve_appointment(
                request.form.get("appointment_id"),
                request.form.get("ended_date") or date.today(),
                request.form.get("authority") or None,
                request.form.get("order_number") or None,
                request.form.get("remarks") or None,
            )
            flash("RELIEF FROM APPOINTMENT FILED IN THE SERVICE RECORD.", "success")
    counts = fetch_one("SELECT COUNT(*) total, COUNT(*) FILTER (WHERE readiness_percent>=80) ready FROM personnel")
    recent = fetch_all("SELECT * FROM personnel ORDER BY created_at DESC LIMIT 30")
    cards = fetch_all("SELECT brc.personnel_id,brc.roster_number,brc.issued_at,brc.last_used_at FROM battle_roster_cards brc WHERE brc.is_active=TRUE")
    card_map = {str(c['personnel_id']): c for c in cards}
    weapons = fetch_all("SELECT wih.personnel_id,wi.serial_number,wi.rack_number,wi.status,wi.condition_state FROM weapon_issue_history wih JOIN weapon_inventory wi ON wi.id=wih.weapon_id WHERE wih.is_current=TRUE")
    weapon_map = {str(w['personnel_id']): w for w in weapons}
    communications_roster = fetch_all("SELECT guild_id,discord_user_id,username,display_name FROM discord_members WHERE is_bot=FALSE AND left_at IS NULL ORDER BY COALESCE(display_name,username)")
    ranks = fetch_all("SELECT * FROM rank_catalog WHERE is_active=TRUE ORDER BY precedence")
    appointment_catalog = fetch_all("SELECT * FROM appointment_catalog WHERE is_active=TRUE ORDER BY sort_order,appointment_name")
    organization_nodes = fetch_all("""SELECT id,parent_id,unit_code,display_name,unit_type,sort_order
                                      FROM unit_nodes WHERE is_active=TRUE
                                      ORDER BY CASE unit_type WHEN 'Battalion' THEN 0 WHEN 'Company' THEN 1 WHEN 'Headquarters' THEN 2 WHEN 'Section' THEN 3 WHEN 'Platoon' THEN 4 WHEN 'Squad' THEN 5 ELSE 9 END,sort_order,display_name""")
    current_appointments = fetch_all("""SELECT pa.id,pa.personnel_id,pa.organization,pa.appointment_status,pa.effective_date,ac.appointment_name FROM personnel_appointments pa JOIN appointment_catalog ac ON ac.appointment_code=pa.appointment_code WHERE pa.is_current=TRUE ORDER BY ac.sort_order""")
    appointment_map = {}
    for appt in current_appointments:
        appointment_map.setdefault(str(appt["personnel_id"]), []).append(appt)
    return render_template("s1_personnel.html", counts=counts, recent=recent, card_map=card_map, weapon_map=weapon_map, issued_packet=issued_packet, communications_roster=communications_roster, ranks=ranks, appointment_catalog=appointment_catalog, appointment_map=appointment_map, organization_nodes=organization_nodes)


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
    return render_template("section.html", section="S-3 OPERATIONS", section_code="s3", subtitle="Operations, training, attendance, readiness and after-action workflow.", counts=None, recent=recent)


@app.get("/s4")
@login_required
@role_required("s4")
def s4():
    counts = fetch_one("SELECT COUNT(*) total, COUNT(*) FILTER (WHERE condition_percent>=80) ready FROM equipment_issues")
    return render_template("section.html", section="S-4 SUPPLY", section_code="s4", subtitle="Arms, equipment, inspections, serviceability and supply readiness.", counts=counts, recent=[])


@app.get("/hq")
@login_required
@role_required("battalion_hq")
def hq():
    personnel_count = fetch_one("SELECT COUNT(*) total FROM personnel")
    ready_count = fetch_one("SELECT COUNT(*) total FROM personnel WHERE readiness_status='READY' OR readiness_percent>=80")
    current_ops = fetch_all("SELECT * FROM operations ORDER BY operation_date NULLS LAST, created_at DESC LIMIT 4")
    pending = fetch_one("SELECT COUNT(*) total FROM personnel WHERE readiness_status<>'READY' AND readiness_percent<80")
    weapons_due = fetch_one("SELECT COUNT(*) total FROM weapon_inventory WHERE status='ISSUED' AND (last_inspected_at IS NULL OR last_inspected_at < NOW() - INTERVAL '14 days')")
    return render_template("hq.html", personnel_count=personnel_count, ready_count=ready_count, pending=pending, weapons_due=weapons_due, current_ops=current_ops)




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
    if total < 2700 or already:
        return False, total

    execute("""UPDATE battalion_event_attendance SET credited_at=NOW(),updated_at=NOW()
               WHERE event_id=%s AND personnel_id=%s""", (event["id"], personnel_id))
    execute("""INSERT INTO personnel_activity_credit
               (personnel_id,source,source_reference,activity_type,activity_date,duration_seconds,credited)
               VALUES (%s,'BATTALION DUTY',%s,%s,%s,%s,TRUE)""",
            (personnel_id, str(event["id"]), event["event_type"], event["starts_at"].date(), total))
    write_service_entry(
        personnel_id,
        "FIELD SERVICE" if event["event_type"] == "OPERATION" else "DUTY",
        f'{event["event_type"]} CREDIT',
        f'{event["title"]} — PRESENT FOR DUTY; {total // 60} MINUTES CREDITED.',
        None,
        source_reference or event.get("external_event_id"),
    )
    if event["event_type"] == "OPERATION" and event.get("operation_id"):
        file_operation_participation(
            event["operation_id"], personnel_id,
            attendance_status="PARTICIPATED",
            remarks=f'Attendance verified: {total // 60} minutes present for duty.'
        )
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
            rank = _rank_from_discord_roles(roles)
            if rank and rank != person.get("rank_code"):
                process_rank_action(person["id"], rank, date.today(),
                    "HEADQUARTERS — BATTALION CLERK", None,
                    "Rank synchronized from the battalion communications roster.")
                person = fetch_one("SELECT * FROM personnel WHERE id=%s", (person["id"],))
            return {"created":False,"linked":True,"personnel":person,
                    "roster":battle_roster_for(person),"field_code":None,
                    "weapon":current_weapon_for(person)}

    rank = _rank_from_discord_roles(roles)
    if rank:
        create_if_missing = True

    candidate = _existing_personnel_candidate(username, display_name)
    if candidate:
        execute("""INSERT INTO website_member_links(guild_id,discord_user_id,personnel_id)
                   VALUES(%s,%s,%s)
                   ON CONFLICT(guild_id,discord_user_id)
                   DO UPDATE SET personnel_id=EXCLUDED.personnel_id,linked_at=NOW()""",
                (guild_id, discord_user_id, str(candidate["id"])))
        card, field_code = issue_battle_roster_card(candidate["id"])
        weapon = issue_m16(candidate["id"])
        if rank and rank != candidate.get("rank_code"):
            process_rank_action(candidate["id"], rank, date.today(),
                "HEADQUARTERS — BATTALION CLERK", None,
                "Rank synchronized from the battalion communications roster.")
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
           VALUES(%s,%s,%s,%s,'11B','Replacement','1-5 CAV',
                  CURRENT_DATE,CURRENT_DATE,'Replacement','PROCESSING',10,'REPLACEMENT — UNASSIGNED',CURRENT_DATE)
           RETURNING *""",
        (allocate_service_number(), first_name, last_name, rank or ""))
    execute("""INSERT INTO assignment_history(personnel_id,unit_code,duty_position,effective_date)
               VALUES(%s,'1-5 CAV','Replacement',CURRENT_DATE)""",(person["id"],))
    execute("""INSERT INTO website_member_links(guild_id,discord_user_id,personnel_id)
               VALUES(%s,%s,%s)
               ON CONFLICT(guild_id,discord_user_id)
               DO UPDATE SET personnel_id=EXCLUDED.personnel_id,linked_at=NOW()""",
            (guild_id,discord_user_id,str(person["id"])))
    card,field_code=issue_battle_roster_card(person["id"])
    weapon=issue_m16(person["id"])
    write_service_entry(person["id"],"ARRIVAL","PERSONNEL RECORD OPENED",
        "Entered on the battalion personnel roster by Headquarters. Status: Replacement — Unassigned.",
        "HEADQUARTERS — BATTALION CLERK")
    write_service_entry(person["id"],"ADMIN","BATTLE ROSTER CARD ISSUED",
        f"Battle Roster Card {card['roster_number']} issued for battalion identification and record access.",
        "HEADQUARTERS — BATTALION CLERK")
    if weapon:
        write_service_entry(person["id"],"EQUIPMENT","INDIVIDUAL WEAPON ISSUED",
            f"U.S. Rifle, 5.56-MM, M16, Serial No. {weapon['serial_number']}, Rack No. {weapon['rack_number']}.",
            "S-4 SUPPLY")
    return {"created":True,"linked":True,"personnel":person,"roster":card,
            "field_code":field_code,"weapon":weapon,"reason":reason}

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
    return {"ok":True,"created":bool(result.get("created")),"linked":bool(result.get("linked")),
            "attached_existing":bool(result.get("attached_existing")),
            "personnel_id":str(person.get("id")) if person else None,
            "rank_code":person.get("rank_code") if person else None,
            "roster_number":card.get("roster_number") if card else None,
            "field_code":result.get("field_code"),
            "weapon_serial":weapon.get("serial_number") if weapon else None}

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
    if external:
        event = fetch_one("""INSERT INTO battalion_events
            (external_event_id,event_type,title,starts_at,ends_at,channel_name,channel_id,operation_id,status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'SCHEDULED')
            ON CONFLICT(external_event_id) DO UPDATE SET event_type=EXCLUDED.event_type,title=EXCLUDED.title,
              starts_at=EXCLUDED.starts_at,ends_at=EXCLUDED.ends_at,channel_name=EXCLUDED.channel_name,
              channel_id=EXCLUDED.channel_id,operation_id=EXCLUDED.operation_id,status='SCHEDULED'
            RETURNING *""", (external,event_type,data.get("title") or event_type,starts_at,ends_at,
                               channel_name,data.get("channel_id"),operation_id))
    else:
        event = fetch_one("""INSERT INTO battalion_events
            (event_type,title,starts_at,ends_at,channel_name,channel_id,operation_id,status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'SCHEDULED') RETURNING *""",
            (event_type,data.get("title") or event_type,starts_at,ends_at,channel_name,data.get("channel_id"),operation_id))
    return {"ok": True, "event_id": str(event["id"]), "credit_threshold_minutes": 45}




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
        return {"ok": False, "error": "authorization required"}, 401
    event = fetch_one("SELECT * FROM battalion_events WHERE id=%s", (event_id,))
    if not event:
        return {"ok": False, "error": "event not found"}, 404
    execute("UPDATE battalion_events SET status='CLOSED',ends_at=LEAST(ends_at,NOW()) WHERE id=%s", (event_id,))
    summary = fetch_one("""SELECT COUNT(*) AS tracked,
                         COALESCE(SUM(CASE WHEN credited_at IS NOT NULL THEN 1 ELSE 0 END),0) AS credited
                         FROM battalion_event_attendance WHERE event_id=%s""", (event_id,))
    return {"ok": True, "event_id": event_id, "summary": summary}


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
        provisioned=_ensure_clerk_personnel(
            guild_id,member_id,str(data.get("username") or ""),
            str(data.get("display_name") or data.get("username") or ""),
            data.get("roles") or [],create_if_missing=True,reason="official_duty_presence")
        if not provisioned.get("linked"):
            return {"ok":True,"credited":False,"reason":"personnel record could not be opened"}
        link={"personnel_id":str(provisioned["personnel"]["id"])}
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

@app.get("/health")
def health():
    return {"ok": True, "site": "5th Cavalry Regiment", "database": database_ready()}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=CONFIG.port, debug=True)
