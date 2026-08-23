UPLOAD TARGET: Website GitHub repository
Replace templates/member_record.html with this file.
Fix: Jinja dict key collision on duty_desk.items caused Wall Locker runtime TypeError.
Changed to explicit duty_desk['items'] access.
