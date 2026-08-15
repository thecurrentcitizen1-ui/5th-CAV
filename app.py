from __future__ import annotations

import logging
from datetime import date

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
    user = fetch_one("SELECT id FROM site_users WHERE username=%s", (CONFIG.admin_username,))
    if not user:
        execute(
            "INSERT INTO site_users (username,password_hash,access_role) VALUES (%s,%s,'admin')",
            (CONFIG.admin_username, generate_password_hash(CONFIG.admin_password)),
        )
        log.info("Initial site administrator created: %s", CONFIG.admin_username)


try:
    bootstrap()
except Exception:
    log.exception("Website bootstrap failed. Check DATABASE_URL and PostgreSQL connectivity.")


@app.context_processor
def inject_globals():
    return {
        "site_name": "5th Cavalry Regiment",
        "unit_name": "1st Battalion, 5th Cavalry Regiment",
        "today": date.today(),
    }


@app.get("/")
def home():
    if not database_ready():
        return render_template("setup.html")
    operation = fetch_one("SELECT * FROM operations ORDER BY operation_date NULLS FIRST, created_at DESC LIMIT 1")
    org = fetch_all("SELECT unit_code,display_name,unit_type FROM unit_nodes WHERE is_active=TRUE ORDER BY sort_order")
    return render_template("home.html", operation=operation, org=org)


@app.route("/login", methods=["GET", "POST"])
def login():
    if not database_ready():
        return render_template("setup.html")
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = fetch_one("SELECT * FROM site_users WHERE username=%s AND is_active=TRUE", (username,))
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Invalid credentials.", "danger")
        else:
            session.clear()
            session["user_id"] = str(user["id"])
            session["username"] = user["username"]
            session["access_role"] = user["access_role"]
            return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.get("/dashboard")
@login_required
def dashboard():
    personnel = fetch_one(
        """
        SELECT p.* FROM personnel p
        JOIN user_personnel_links upl ON upl.personnel_id=p.id
        WHERE upl.user_id=%s
        """,
        (session["user_id"],),
    )
    discord = None
    voice = None
    if personnel:
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
    upcoming = fetch_all("SELECT * FROM operations ORDER BY operation_date NULLS LAST, created_at DESC LIMIT 5")
    return render_template("dashboard.html", personnel=personnel, discord=discord, voice=voice, upcoming=upcoming)


@app.get("/my-201-file")
@login_required
def my_201_file():
    personnel = fetch_one(
        """SELECT p.* FROM personnel p JOIN user_personnel_links upl ON upl.personnel_id=p.id WHERE upl.user_id=%s""",
        (session["user_id"],),
    )
    qualifications = []
    equipment = []
    awards = []
    if personnel:
        qualifications = fetch_all("SELECT * FROM qualifications WHERE personnel_id=%s ORDER BY qualification_name", (personnel["id"],))
        equipment = fetch_all("SELECT * FROM equipment_issues WHERE personnel_id=%s ORDER BY item_type,nomenclature", (personnel["id"],))
        awards = fetch_all("SELECT * FROM personnel_awards WHERE personnel_id=%s ORDER BY award_date DESC", (personnel["id"],))
    return render_template("personnel_file.html", personnel=personnel, qualifications=qualifications, equipment=equipment, awards=awards)


@app.get("/organization")
def organization():
    nodes = fetch_all("SELECT id,parent_id,unit_code,display_name,unit_type,sort_order FROM unit_nodes WHERE is_active=TRUE ORDER BY sort_order") if database_ready() else []
    return render_template("organization.html", nodes=nodes)


@app.get("/operations")
def operations():
    ops = fetch_all("SELECT * FROM operations ORDER BY operation_date DESC NULLS LAST, created_at DESC") if database_ready() else []
    return render_template("operations.html", operations=ops)


@app.get("/recruiting")
def recruiting():
    return render_template("recruiting.html")


@app.get("/s1")
@login_required
@role_required("s1")
def s1():
    counts = fetch_one("SELECT COUNT(*) total, COUNT(*) FILTER (WHERE readiness_percent>=80) ready FROM personnel")
    recent = fetch_all("SELECT * FROM personnel ORDER BY created_at DESC LIMIT 10")
    return render_template("section.html", section="S-1 PERSONNEL", subtitle="Strength, assignments, personnel actions, records and tour administration.", counts=counts, recent=recent)


@app.get("/s2")
@login_required
@role_required("s2")
def s2():
    return render_template("section.html", section="S-2 INTELLIGENCE", subtitle="Maps, intelligence summaries, threat reporting and operational intelligence.", counts=None, recent=[])


@app.get("/s3")
@login_required
@role_required("s3")
def s3():
    recent = fetch_all("SELECT * FROM operations ORDER BY created_at DESC LIMIT 10")
    return render_template("section.html", section="S-3 OPERATIONS", subtitle="Operations, training, attendance, readiness and after-action workflow.", counts=None, recent=recent)


@app.get("/s4")
@login_required
@role_required("s4")
def s4():
    counts = fetch_one("SELECT COUNT(*) total, COUNT(*) FILTER (WHERE condition_percent>=80) ready FROM equipment_issues")
    return render_template("section.html", section="S-4 LOGISTICS", subtitle="Arms, equipment, inspections, serviceability and supply readiness.", counts=counts, recent=[])


@app.get("/hq")
@login_required
@role_required("battalion_hq")
def hq():
    personnel_count = fetch_one("SELECT COUNT(*) total FROM personnel")
    discord_count = fetch_one("SELECT COUNT(*) total FROM discord_members WHERE is_bot=FALSE AND left_at IS NULL")
    voice_today = fetch_one("SELECT COALESCE(SUM(duration_seconds),0) total_seconds FROM voice_sessions WHERE started_at>=CURRENT_DATE")
    return render_template("hq.html", personnel_count=personnel_count, discord_count=discord_count, voice_today=voice_today)


@app.get("/health")
def health():
    return {"ok": True, "site": "5th Cavalry Regiment", "database": database_ready()}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=CONFIG.port, debug=True)
