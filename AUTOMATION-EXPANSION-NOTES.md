# Battalion Clerk Automation Expansion

Added:
- `/request` routed member requests to Personnel/S-1, Training/S-3, Supply/S-4, Leadership/HQ, Technical/HQ.
- `/request-channel` and `/request-channel-status` for staff notification routing.
- Inactivity escalation: member DM -> S-1 flag/action -> Command escalation/action.
- `/inactivity-report-channel` and `/inactivity-thresholds`.
- Promotion eligibility watcher: privately DMs the best matching NCO/leader and optionally posts a staff summary.
- `/promotion-report-channel`.
- Post-operation processing report channel.
- `/post-operation-report-channel`.
- `/operation-rounds-default` and optional `rounds_per_soldier` on `/schedule`.
- Existing `/activity-channel-add`, `/activity-channel-remove`, `/activity-channel-status` remain authoritative for community activity voice credit.
