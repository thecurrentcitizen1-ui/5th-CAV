# V66 — Publication Routing Regression Repair

- Restores selective public Discord publication for awards, promotions, and Headquarters/personnel orders.
- Configured per-type routes remain authoritative.
- Missing AWARD/PROMOTION routes fall back to `honors-and-promotions` (1537242629131993159).
- Missing Headquarters/battalion order routes fall back to `orders-from-headquarters` (1534357136858157157).
- Added `/repair-publication-routing` for an explicit persistent-route repair.
- The persistent Discord routing pause remains respected; this update never silently bypasses it.
- Pending website personnel documents remain unmarked until a Discord post succeeds, so a missing channel does not discard the order.
