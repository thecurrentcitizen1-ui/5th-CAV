from __future__ import annotations

from functools import wraps
from flask import flash, redirect, session, url_for

ROLE_WEIGHT = {
    "member": 10,
    "nco": 20,
    "s1": 40,
    "s2": 40,
    "s3": 40,
    "s4": 40,
    "company_hq": 50,
    "battalion_hq": 60,
    "commander": 90,
    "admin": 100,
}

COMMAND_ROLES = {"battalion_hq", "commander", "admin"}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def role_required(required_role: str):
    """Section-scoped authorization. Command may enter every staff section;
    staff sections do not inherit access to one another merely because their
    historical role weights are similar.
    """
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            current = session.get("access_role", "member")
            if current not in COMMAND_ROLES and current != required_role:
                flash("Your current duty access does not authorize that section.", "warning")
                landing = {
                    "s1": "s1", "s2": "s2", "s3": "s3", "s4": "s4",
                    "nco": "my_soldier_record",
                }.get(current, "my_soldier_record" if current == "member" else "staff_access")
                return redirect(url_for(landing))
            return view(*args, **kwargs)
        return wrapped
    return decorator
