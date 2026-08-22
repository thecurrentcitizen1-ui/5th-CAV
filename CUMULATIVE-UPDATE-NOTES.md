# 1/5 Cavalry cumulative update
- Replacement recruiting card image swapped to supplied soldier image.
- Public Home "What You Receive" replaced with live scheduled battalion events from `/schedule`.
- Headquarters dispatch cards enlarged slightly.
- S-1 Personnel Roster Control with search, 201 access, quick corrections, credential reissue, and Command-only erroneous-record deletion link.
- S-1 in-processing made actionable with per-Soldier requirement visibility and direct S-1 certification.
- Command navigation reorganized for Personnel, Recruiting, Operations, Training, Supply, Billets, Actions.
- Logged-in smart Back button added.
- Replacement/Special Orders document-number overlays shifted right only.
- MOS, duty qualification, and training-program completions now generate permanent personnel orders.
- M16 model separates fouling/cleanliness/serviceability/inspection/neglect.
- Member Clean Weapon remains operator-controlled and files weapon maintenance history.
- Uniform view zoom increased.
- S-1 overflow/responsive containment improved.


## 2026-08-17 Recruiting Application Hotfix
- Public recruiting page now uses `static/art/recruiting-card-soldier.webp` in the application sidebar instead of `arrival-company.webp`.
- Replaced the developer-facing OAuth warning text with a public-safe temporary-unavailable message.
- Discord verification still requires `DISCORD_CLIENT_ID` and `DISCORD_CLIENT_SECRET` in the WEBSITE Railway service. `DISCORD_OAUTH_REDIRECT_URI` should be `https://www.5thcavgaming.com/recruiting/discord/callback`.


## Leadership Appointment Sync — 2026-08-17
- Added/confirmed website appointments: Platoon Sergeant, Squad Leader, Assistant Squad Leader, Team Leader.
- Team Leader retains the stable internal `FTL` code so existing database records remain compatible.
- Personnel sync now exposes current field-leadership appointments to Battalion Clerk for Discord role mirroring.

## S-1 Personnel Layout Repair — 2026-08-17
- Fixed lower S-1 administrative panels incorrectly auto-flowing into the narrow right column.
- Personnel Assignment Orders now spans the full workroom width.
- Initial Soldier Processing / Promotion Recommendation / Promotion Hold now use a stable responsive desk layout.
- Promotion Eligibility Board now spans full width.
- Rank Action / Appointment Action now occupy a balanced two-column desktop row and stack on smaller screens.
- Incomplete Initial Soldier Processing records no longer auto-expand every card on page load; each Soldier summary can be opened as needed.
- Added overflow/min-width safeguards to processing requirements and action controls.


## Uniform Replacement
- Replaced `static/art/army-green-service-uniform.webp` with the user-supplied Army green service uniform image.
- No templates or display logic changed; all existing uniform references now use the new image.

## 2026-08-17 — Automatic Headquarters Order Numbering
- Removed manual order-number entry from S-1 assignment, rank, appointment, and HQ award workflows.
- Official order numbers are allocated transactionally by the website using battalion_document_sequences.
- Special Orders use SO-YY-####.
- General Orders use GO-YY-####.
- Appointment Orders use AO-YY-####.
- Promotion, assignment, appointment, relief-from-appointment, and award permanent-record entries now use the same automatically generated number as the official document.
- Each order class/year has its own sequence to avoid duplicate numbers under concurrent filing.

## Automatic Ribbon Progress + Recruiting Referral Update — 18 AUG 2026
- Added automatic ribbon catalog/progress records and earned-ribbon filing.
- Battalion Clerk facts drive Instructor, NCO Leadership, Recruiting, Combat Infantry, Good Conduct, Tour of Duty, and Military Service eligibility.
- Added hourly server-side ribbon eligibility recheck endpoint; no per-ribbon Discord tracking command is required.
- Added public recruiting application referral question with active-personnel dropdown.
- Referral credit is not counted until the referred applicant is converted to ENLISTED personnel.
- Added member-facing Ribbon Progress and Earned Ribbons panels.
- Added training-event instructor filing support for Battalion Clerk.
- Campaign Ribbon is cataloged and shown as pending until a Headquarters campaign designation workflow is added.

## Ribbon Rack / Wear Toggle Update — 18 AUG 2026
- Added the first uniform ribbon asset: Military Service Ribbon (`static/art/ribbons/military-service-ribbon.png`).
- Extended ribbon data with optional `image_filename` and per-member `is_worn` state.
- Added member wear / take off controls for earned ribbons.
- Added live ribbon rack rendering to the member dashboard uniform preview and 201 File uniform panel.

## Ribbon Asset Update — Unit Citation Ribbon
- Added the Unit Citation Ribbon asset (`static/art/ribbons/unit-citation-ribbon.png`) and linked it to `UNIT_CITATION` in the ribbon catalog.

## Ribbon Asset Update — Meritorious Service Ribbon
- Added the Meritorious Service Ribbon asset (`static/art/ribbons/meritorious-service-ribbon.png`) and linked it to `MERITORIOUS_SERVICE` in the ribbon catalog.

## Ribbon Asset Update — NCO Leadership Ribbon
- Added the NCO Leadership Ribbon asset (`static/art/ribbons/nco-leadership-ribbon.png`) and linked it to `NCO_LEADERSHIP` in the ribbon catalog.

## Ribbon Asset Update — Instructor Ribbon
- Added the Instructor Ribbon asset (`static/art/ribbons/instructor-ribbon.png`) and linked it to `INSTRUCTOR` in the ribbon catalog.


## Ribbon Asset Update — Tour of Duty Ribbon
- Added the corrected Tour of Duty Ribbon asset (`static/art/ribbons/tour-of-duty-ribbon.png`) and linked it to `TOUR_OF_DUTY` in the ribbon catalog.

## Ribbon Asset Update — Good Conduct Ribbon
- Added the Good Conduct Ribbon asset (`static/art/ribbons/good-conduct-ribbon.png`) and linked it to `GOOD_CONDUCT` in the ribbon catalog.

## Ribbon Asset Update — Recruiting Ribbon
- Added the Recruiting Ribbon asset (`static/art/ribbons/recruiting-ribbon.png`) and linked it to `RECRUITING` in the ribbon catalog.

## Public Awards & Decorations Page — 18 AUG 2026
- Added public `/1-5-awards-and-decorations` page with separate Ribbons and Medals categories.
- Ribbon entries display their image, award mode, precedence, and published earning requirements.
- Added public navigation link `1/5 AWARDS & DECORATIONS`.
- Added pending ribbon assets for Campaign, Tour of Duty, Good Conduct, Recruiting, and Combat Infantry.

## Automatic Wear on Award — 18 AUG 2026
- Newly earned automatic ribbons now default to WORN immediately when awarded.
- Website/Headquarters-approved awards whose names match a ribbon in the ribbon catalog are automatically authorized in `personnel_ribbons` and set to WORN.
- Members can still use TAKE OFF afterward; taking a ribbon off does not remove it from the permanent earned record.


## S-1 Direct Award Issue + Awards Card Layout Repair — 2026-08-18
- Fixed public Awards & Decorations cards so the ribbon image frame no longer stretches vertically to match long award descriptions.
- Added S-1 Direct Award Issue desk with Soldier dropdown, official award dropdown, award date, citation, optional remarks, and automatic order numbering.
- Directly issued ribbons are entered in personnel_awards, personnel_ribbons, service history, and official personnel documents.
- Directly issued ribbons default to NOT WORN so the Soldier can choose whether to display them on the uniform.
- Soldier receives an award notification after filing.

## 2026-08-18 — 201 File Ribbon Wear Controls
- Added WEAR / TAKE OFF controls directly to the 201 File Service Recognition block.
- Awards & Decorations now shows WORN / NOT WORN for awards that correspond to earned ribbons.
- Added wear-toggle control beside ribbon-bearing awards in the Awards & Decorations section.
- Wear state continues to drive which ribbons appear on the issued service uniform.


## Uniform Ribbon Rack Placement Repair
- Moved the uniform ribbon rack to the upper left breast area above the pocket, matching the marked placement reference.
- Rack now fills exactly three ribbons per row from the top row downward.
- Additional ribbons create new rows beneath the first row while remaining centered and evenly spaced.


## 2026-08-18 — Member Dashboard Ribbon Rack Placement
- Adjusted only the Issued Property / member dashboard service-uniform ribbon rack position.
- Full 201 File / Open Uniform Record rack position intentionally unchanged.
- Existing three-across rack sizing and row logic preserved.

## 2026-08-18 — Battalion Inactivity & Property Accountability
- Added S-1 Inactivity Control Board with CURRENT / WATCH / DEFICIENT / INACTIVE / COMMAND REVIEW / EXCUSED ABSENCE stages.
- Added contact history, excused absence, return-to-active, and command referral controls.
- M16 inactivity now progresses at 7 / 14 / 21 / 30 days and pauses during authorized leave.
- 201 File now shows last qualifying activity, days inactive, current status, and next threshold.
- Website login no longer counts as qualifying activity; meaningful activity comes from approved voice/duty credits and deliberate equipment maintenance actions.

## 2026-08-18 — Personnel / Training / Replacement Expansion
- 201 File now tracks total leadership service days by Team Leader, Assistant Squad Leader, Squad Leader, Platoon Sergeant, and higher qualifying leadership appointments.
- Added experience-based MOS proficiency. 11R uses Rifleman III → Rifleman II → Rifleman I → Senior Rifleman; other MOSs use equivalent MOS proficiency labels. Proficiency derives from credited operations, completed training programs, and current duty qualifications and does not change rank.
- Qualification currency now expires automatically. Duty qualifications default to a 90-day currency period unless S-3 enters another expiration date. Expired credentials stop counting toward training readiness until renewed.
- Added recurring weapons, communications, medical, leadership, and specialty qualification types to S-3.
- Expanded S-3 Training Deficiency Board to show missing required schools plus qualifications expiring within 30 days or already expired.
- Recruiting approval now places the applicant in REPLACEMENT_DEPOT status. Battalion Clerk holds the member there until rank, MOS, company, platoon, and squad assignment is valid.
- On successful conversion, Headquarters automatically files Movement Orders releasing the Soldier from Replacement Depot to the assigned organization and records the order in the 201 File.
- Added S-1 Personnel Action Suspense Board. Existing Personnel Actions due dates are surfaced and overdue status is visible.
- Added internal Clerk suspense feed for due-soon and overdue personnel actions.

## 2026-08-18 — Combat Leadership / Cohesion / Unit Experience
- Added advisory Combat Leadership Score (0–100) from leadership days, operations led, completed instructor periods, assigned-Soldier readiness, and completed leadership assignments.
- Score is shown on the 201 File and promotion worksheet but never grants rank automatically.
- Added dynamic Squad/Platoon Cohesion from activity, recent attendance, training, leadership stability/30-day roster churn, qualification currency, and staffing.
- Added Unit Experience classifications: NEWLY FORMED, FIELD EXPERIENCED, COMBAT TESTED, VETERAN, based on qualifying completed operations performed together by the current formation.
- Added scoped Unit Cohesion & Experience board to Readiness; members see their formation and leaders see their command scope.
- No Battalion Clerk schema/command update is required; existing attendance/activity/instructor records are consumed by the website as the source of truth.


## 2026-08-18 — Member Career Experience Expansion
- Added member career progression, qualification cards, leadership and assignment records, M16 service history, service statistics, combat experience, tour phases, weekly report, buddy history, squad/platoon identity pages, and Soldier of the Month/Quarter recognition.


## 2026-08-18 — Member Career Progression II
- Added Where You Stand summary, Personal Formation Legacy, approved squad/platoon nicknames and call signs, member-selected Service Goals, and detailed Promotion Readiness category percentages.


## 2026-08-18 — Verified Automation & Operation Credit
- Unified website M16/readiness inactivity thresholds with Battalion Clerk.
- Enforced 45-minute official duty/operation credit.
- Added member-facing Official Operation Credit Ledger.

## 2026-08-18 — Recruiting Approval / Discord Recovery Fix
- Command can now approve a Recruiting Case even when Discord OAuth was not attached.
- Unverified approved cases enter `APPROVED_AWAITING_DISCORD` instead of being blocked by a disabled button.
- Applicant status page provides a Discord verification recovery button.
- Successful OAuth attachment automatically moves a Command-approved case into `REPLACEMENT_DEPOT`.
- Duplicate active Discord identity protection remains in place.
- Verified applicants still move directly from Command approval to Replacement Depot.

## 2026-08-18 — Final Integrity / Clean Upload Pass
- Corrected member-career UUID foreign-key types.
- Corrected Personal Formation Legacy operation SQL.
- Configured Operation rounds now apply to the issued M16 when 45-minute official credit is earned; close-duty keeps delta-only duplicate protection.
- Added layout containment/mobile wrapping safeguards.
- Revalidated Python, Jinja, Flask routes, forms/buttons, recruiting approval, and Battalion Clerk automation commands.


## 2026-08-18 — Recruiting Application Archive
- Added permanent read-only full-application view for approved, denied, closed, and enlisted Recruiting Cases.
- Added VIEW FULL APPLICATION buttons in Replacement Depot and Closed Files.
- Added Command-only RETURN TO COMMAND REVIEW for eligible historical cases without creating a duplicate application or personnel record.
- ENLISTED cases remain permanently viewable but cannot be reopened through Recruiting Control.


## 2026-08-18 — Prospective Replacements Live Intake (Website)
- Every non-bot Discord member is tracked in discord_members on join.
- Website now exposes active unlinked Discord arrivals on a Command-only Prospective Replacements board.
- Prospective arrivals do not receive a 201 File, rank, strength credit, or official personnel status.
- Board links to an existing Recruiting Case when one is linked; otherwise shows NO APPLICATION.
- Battalion Clerk now preserves the true Discord joined_at timestamp.
- Members automatically disappear from the prospective board once a website_member_links personnel link is created or they leave Discord.


## 2026-08-18 — Command Adopt Discord Member
- Added ADOPT TO RECRUITING CASE on the Prospective Replacements board.
- Battalion HQ can now create a Recruiting Case for a Discord-first joiner without forcing a public website application.
- The adopted case begins in DISCORD_VERIFIED status and enters normal Recruiting Control review/approval workflow.
- Duplicate-safe protections prevent creating a second active case or a second personnel record for the same Discord account.
- Adopting a Discord member does not create a 201 File or grant battalion personnel status by itself.


## 2026-08-18 — Direct Discord Intake Visibility Fix
- Prospective Discord-first members now appear directly on Recruiting Control.
- ADOPT TO RECRUITING CASE is displayed directly on the member row when no case exists.
- Members who already have a case display OPEN RECRUITING CASE instead.
- The separate full Prospective Replacements roster remains available, but is no longer required to perform adoption.


## 2026-08-18 — Service Uniform Ribbon Placement Refinement
- Moved the service-uniform ribbon rack higher and toward the visual left/chest center in the member Issued Property view.
- Matched the full 201 File uniform rack position so the two uniform views remain consistent.
- Ribbon sizing, rack order, earned-award logic, and uniform artwork are unchanged.


## 2026-08-18 — Railway Startup Crash Fix
- Fixed fatal NameError caused by an undefined `permission_required` decorator on `/hq/unit-identity`.
- Replaced it with the website's existing `login_required` + `role_required("battalion_hq")` authorization pattern.
- Audited all app.py decorators for undefined decorator roots.
- Revalidated Python compilation, Jinja templates, template endpoints, and ZIP integrity.


## 2026-08-18 — Stability / Performance / M16 / Adopt Repair
- Fixed Discord-first Adopt database insert by supplying required Recruiting Case fields with explicit administrative placeholders.
- Adopted cases now enter PENDING_COMMAND and return to active Recruiting Control.
- Operation ammunition now reconciles against the actual weapon_round_events ledger, eliminating missed-round and double-count conditions.
- Added internal repair endpoint for already-filed operation participation.
- Soldier Record now falls back to a core record instead of a generic fatal 500 if an extended optional panel fails.
- Replaced per-query PostgreSQL connection creation with a reusable per-worker connection pool.
- Added common member-record and operation-credit indexes.


## 2026-08-18 — Soldier Record Hard-Safe Recovery
- Replaced the previous fallback path with a dedicated low-dependency Soldier Record recovery template.
- Full record data/render failures can no longer recursively fail by reusing the full member_record template.
- Personnel-link lookup, full context build, fallback queries, and fallback rendering now have independent failure boundaries.
- Recovery mode preserves core identity/assignment data and attempts to show roster number and issued weapon.
- Every server-side failure produces a short diagnostic reference in Railway logs and on the recovery page.


## 2026-08-18 — Full System Stabilization Review
- Consolidated cumulative recent changes into a reviewed baseline instead of stacking another patch.
- Fixed Discord schema contract mismatches (`left_at` / `last_seen_at`).
- Website now guarantees shared `discord_members` / `voice_sessions` tables and migrations.
- Replaced experimental connection pool dependency with one reusable PostgreSQL connection per HTTP request.
- Cached linked Soldier lookup within each request.
- Removed write-heavy synchronization from normal Soldier Record page loads.
- Added isolated member-panel failures plus app-wide diagnostic references.
- Hardened Command Adopt compatibility with legacy verification fields.
- Preserved Discord-first intake/archive, refined ribbon placement, 45-minute operation credit, M16 ledger reconciliation, and operation reminder systems.


## 2026-08-18 — where_you_stand Template Contract Fix
- Confirmed from Railway logs that member_record.html could render without `where_you_stand` when the career-context helper failed.
- Member Record now always provides a complete `where_you_stand` object with safe defaults.
- member_record.html also defines a defensive local `wys` object, preventing future missing-context regressions from causing a 500.

## 2026-08-18 — Full Uniform Ribbon Position Correction
- Adjusted only the full Open Uniform Record ribbon rack to sit above the wearer's left breast pocket.
- S-4 / Personnel member-panel ribbon placement remains unchanged.


## 2026-08-18 — Staff UX / Action Center Overhaul
- Added role-aware Staff Action Center as the default landing for S-1/S-2/S-3/S-4/Training/HQ.
- Added Morning Staff Brief, Attention Required board, universal personnel search, Soldier Quick Action drawer, recent actions, and active suspense.
- Consolidated top-level staff navigation into Action Center / Personnel / Operations / Logistics / Honors / Reports while preserving specialized offices.
- Added staff breadcrumbs, Return to Action Center control, and persistent personnel search.
- Added read-only Staff Personnel Snapshot for cross-office Soldier lookup.
- Added auditable batch-action routing to multiple Soldiers; it opens actions instead of directly mutating records.
- Added saved roster filters and text filtering on Personnel Files.
- Added five-step visual workflow and recommended-route helper to Personnel Action Control.
- Added consistent attention/status styling and responsive staff layouts.
- Existing Recruiting Control, Operations Center, Arms Room, 201 File, automation, M16 reconciliation, and ribbon positions were preserved.


## 2026-08-18 — Final Cumulative S-3 / Staff / Mobile / Public UX Pass
- Website-first S-3 Operations Center with publish-to-Clerk schedule, per-operation Discord channel, duration, credit threshold, rounds, reminders, formation scope, live attendance, manual overrides, duplicate, closeout and AAR/M16 reconciliation.
- Battalion Clerk heartbeat and dynamic duty-binding reload added; website-scheduled Operations are announced/reminded without requiring routine `/schedule`.
- Active 201 folders receive prominent front name stamps.
- PVT uses the supplied 1/5 CAV PVT dog-tag image wherever rank art is rendered.
- Staff-opened 201 Files now include direct Award filing, Command/Staff Remarks, and action routing.
- Staff button/card contrast standardized for readability.
- Awards & Decorations cards widened/cleaned with compact ribbon image areas.
- Homepage adds Incoming Mail / Orders Pouch (left) and Newest Arrivals / Replacement Board (right).
- Recruiting card slogan changed to “CHARLIE WON’T WAIT. NEITHER SHOULD YOU” on responsive desktop/mobile markup.
- Mobile navigation exposes 1/5 Awards and mobile layouts were rebalanced for public and staff pages.


## 2026-08-19 — Structured Personnel Dropdown Control
- Added a dedicated Manage Soldier page from every staff-visible 201 File.
- Company/platoon/squad assignments are selected from active unit_nodes; no manual unit labels.
- MOS values come from battalion_mos_catalog; duty/billet values come from unit_billets, MOS titles, and appointment catalog.
- Rank changes come from rank_catalog; appointments come from appointment_catalog.
- Filing authority is automatically taken from the authenticated staff account.
- S-1 new-personnel, quick-correction, and Assignment Orders forms now use validated system dropdowns.
- Backend validation rejects values that are not in authoritative catalogs.


## 2026-08-19 — Persistent Battalion State Expansion
- Added standardized Battalion State events and backfilled service/weapon history into a unified audit timeline.
- Added Current Situation, Field Reputation, Record of Active Service, Tour completion summary, weapon personality/previous holders, award evidence, promotion board packet, Most Served With, unit history/cohesion, leadership lineage, Command watchlist, smart search, personnel comparison, personal action center, Soldier journal, richer Morning Reports and 90-day trends.
- Added operation-close cascade for state events, Tour Book, readiness and automatic ribbon-progress evaluation.
- Added system document classes MR and Q while preserving GO/SO/AO/OP numbering.
- Added automatic duty-roster candidate suggestions and retained individual S-3 ammunition overrides.
- Added Discord role-sync queue and member reminder endpoints; website remains authoritative.


## 2026-08-19 — Staff Action Center Suspense Fix
- Fixed fatal `/staff` Action Center error caused by missing `staff_suspense_summary()` helper.
- Added role-aware Due Today / Next 7 Days / Overdue suspense counts.
- Audited direct function calls; no other missing global helper references remain.


## 2026-08-19 — S-3 Schedule Visibility / Layout Cleanup
- Public homepage Scheduled Operations now uses published website S-3 operations as its authoritative source and falls back to unlinked Clerk events.
- Removed the original seeded OP-001 FIELD EXERCISE — INITIAL READINESS demo record and stopped it from being re-created.
- Current / Upcoming excludes CLOSED, CANCELLED, COMPLETED and ARCHIVED records.
- Hardened Operation Control, Duty Assignment Board, timeline, roster and operation-card layout against clipped text/panels and large empty gaps.
- Compact date/time formatting prevents raw PostgreSQL timestamps from overrunning cards.


## 2026-08-19 — Website-Authoritative Operations / Live M16 Accrual
- Website S-3 scheduling is now the authoritative normal Operation workflow; it creates/updates the shared Battalion Clerk event automatically.
- Public-home Scheduled Operations continues to read scheduled/published website Operations directly.
- M16 rounds now accrue proportionally during verified Operation voice time on every Clerk attendance flush instead of waiting until closeout.
- Official credit remains controlled by the configured per-operation credit threshold.
- Operation closeout reconciles to verified-time expenditure rather than forcing every credited Soldier to the full default.
- Issued M16 condition now includes a smaller inactivity/neglect fouling component after the configured inactivity warning period, while firing remains the primary fouling source.
- A fresh cleaning or recent issue resets the inactivity-neglect clock.
- Added Clerk endpoint to refresh issued-weapon inactivity condition without requiring a page visit.

## 2026-08-19 — S-1 Replacement Detachment / Rapid Personnel Control
- Added a dedicated Replacement Detachment roster derived from authoritative personnel, assignment, progress, training, property, Discord-link and personnel-action data.
- Added automatic workflow stages: New Arrivals, In-Processing, Training, Ready for Assignment, Assignment Pending and Hold.
- Added a universal staff Soldier Action Drawer. Staff can click a Soldier from Personnel Records, S-1 roster, Staff Action Center, or Replacement Detachment without first navigating through the full 201 File.
- Added one-click S-1 controls for beginning in-processing, completing S-1 onboarding, issuing/checking an M16, reminders, controlled assignment/MOS actions, and opening/claiming staff workflows.
- Added Process Next Replacement, S-1 Priority Work, Assigned to Me action queue, quick Claim/Review/Complete, personnel exception detection, and safe batch replacement processing.
- Replacement Detachment automatically clears Soldiers once the initial-processing pipeline is complete; a premature permanent assignment does not hide an active initial-training record.
- Added S-3 Delete Scheduled Operation controls on the operation control board and Current/Upcoming cards. Real service history (credit/rounds/AAR/completed records) is protected from hard deletion.
- Deleting a planning/scheduled operation also deletes its linked Clerk event so reminders/voice tracking stop and the homepage schedule clears.
- Revalidated the public homepage schedule contract: website S-3 records are authoritative and published SCHEDULED/ACTIVE future operations are read directly by the Scheduled Operations panel, with Clerk-only events as legacy fallback.

## 2026-08-20 — Replacement Detachment / New Soldier Pipeline Authority Fix
- Corrected Replacement Detachment inclusion: battalion/root placeholder assignments no longer count as permanent formation.
- Website approval now immediately provisions an unassigned PVT / 00R MOS Pending 201 File for verified approved applicants.
- Battalion Clerk no longer requires Discord rank/MOS/company/platoon/squad roles before 201 File creation.
- Recruiting Case remains APPROVED_AWAITING_PROCESSING until S-1 releases the Soldier; ENLISTED is now the final release state.
- Replacement Training is separated from PVT promotion gates; 7 days + 1 Operation remain PVT -> PFC consideration requirements.
- Added MOS-only quick action, controlled training certification, and automatic release check after qualifying actions.


## 2026-08-20 — Replacement Assignment Release Fix
- Fixed `valid_mos` NameError in `_replacement_release_requirements()` that could throw a Record Processing Error immediately after S-1 filed a Replacement formation assignment.
- MOS release validation now reads the active `battalion_mos_catalog` inside the release helper.
- Post-assignment release evaluation is fail-soft: the assignment remains filed and S-1 receives a controlled remaining-requirements message if a secondary release check fails.
- Existing Replacement Detachment, intake authority, Clean Weapon, unified Operations, homepage schedule, and M16 logic retained.
