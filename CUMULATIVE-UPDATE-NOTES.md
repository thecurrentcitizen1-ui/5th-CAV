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
