PHASE 9 — AUTOMATIC PERSONNEL IDENTITY / UNRANKED ROSTER — FINAL

UPLOAD TO: 5th-Cavalry-Website GitHub repository

Replace:
- app.py
- templates/personnel_file.html
- templates/personnel.html
- templates/organization.html
- templates/dashboard.html

FINAL BEHAVIOR
1. A Soldier can be entered on the Battalion Roster before receiving any rank.
2. Until a recognized Discord rank role is assigned, the roster displays the Soldier's name with no rank prefix.
3. Rank fields display an administrative dash / NO RANK ASSIGNED where a field must exist.
4. The system DOES NOT automatically assign PVT just because a Soldier joins Discord, enters The LZ, or receives a Battle Roster Card.
5. A recognized Discord rank role is the authority for assigning the Soldier's actual rank.
6. Once a rank role is assigned, Battalion Clerk updates the 201 File and rank history.
7. Battle Roster Number, 201 File, internal Discord identity, and M16 issue automation remain enabled.

No new Railway variables are required.
