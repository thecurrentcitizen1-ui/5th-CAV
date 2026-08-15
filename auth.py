from __future__ import annotations

from functools import wraps
from flask import flash, redirect, session, url_for

ROLE_WEIGHT = {
    "member": 10,
    "nco": 20,
    "training": 30,
    "s1": 40,
    "s2": 40,
    "s3": 40,
    "s4": 40,
    "company_hq": 50,
    "battalion_hq": 60,
    "admin": 100,
}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=url_for("dashboard")))
        return view(*args, **kwargs)
    return wrapped


def role_required(min_role: str):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            current = session.get("access_role", "member")
            if ROLE_WEIGHT.get(current, 0) < ROLE_WEIGHT.get(min_role, 999):
                flash("Your current duty access does not authorize that section.", "warning")
                return redirect(url_for("dashboard"))
            return view(*args, **kwargs)
        return wrapped
    return decorator
