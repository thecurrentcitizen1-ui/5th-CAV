# Battalion Clerk cumulative update
- Approved voice-channel activity system added.
- `/activity-channel-add`, `/activity-channel-remove`, `/activity-channel-status`.
- Only 10+ minute sessions in explicitly approved voice channels update Soldier activity.
- Voice activity remains an activity/neglect signal only; it does not grant operation or training credit.

## Battalion Help Desk Expansion
- Added persistent interactive Help Desk panel with Personnel, Training, Supply, Leadership, and Technical routing.
- Added private ticket channels under `BATTALION HELP DESK` with requester + proper staff-section visibility only.
- Linked Soldiers automatically receive a website `personnel_actions` record when a ticket opens.
- Tickets include Claim and Close Ticket buttons.
- Closing a ticket marks the linked personnel action CLOSED, exports up to 500 messages as a transcript, archives it to the configured archive channel, then removes the private ticket channel.
- Added `/helpdesk-panel`, `/helpdesk-archive`, `/helpdesk-status`, and `/ticket` commands.
- Existing `/request-channel` routes are also notified when a private Help Desk ticket opens.


## Leadership Appointment Sync — 2026-08-17
- Added Team Leader to the Battalion Clerk appointment-role blueprint.
- Platoon Sergeant, Squad Leader, Assistant Squad Leader, and Team Leader are managed as appointments, not ranks.
- Battalion Clerk now mirrors these four leadership appointment roles from the authoritative website personnel record during canonical personnel synchronization.

## Automatic Ribbon Progress + Instructor Credit — 18 AUG 2026
- Added hourly automatic ribbon eligibility recheck against the website personnel system.
- Existing /schedule command now accepts optional instructor and assistant_instructor members for TRAINING events.
- Instructor selections are filed with the website and receive instructional-period credit when the training duty period is closed.
- No member or staff command is required to start individual ribbon trackers.
