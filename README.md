# 5th Cavalry Regiment — Phase I

A fresh Hell Let Loose: Vietnam website foundation for **1st Battalion, 5th Cavalry Regiment**. This is not a reskin of the War of Rights site. The older codebase was used only as a reference for workflow ideas.

## Phase I includes

- Completely new Vietnam-era visual system and responsive layout.
- Public Headquarters, Organization, Operations and Recruiting shells.
- Member login and role-aware staff navigation.
- Member dashboard and 201 File shell.
- S-1, S-2, S-3, S-4 and Battalion HQ access architecture.
- New PostgreSQL website schema for personnel, unit organization, operations, qualifications, equipment, awards and activity credit.
- Direct read integration with the existing **Battalion Clerk** PostgreSQL tables (`discord_members`, `voice_sessions`, `website_member_links`).
- Automatic initial administrator creation using Railway environment variables.

## Railway deployment

Create a **new website service/repository** for this project. Do not overwrite the Battalion Clerk service.

Use the same PostgreSQL service that Battalion Clerk already uses.

### Required variables

```text
DATABASE_URL=${{Postgres.DATABASE_URL}}
SECRET_KEY=<long random secret>
ADMIN_USERNAME=<your command username>
ADMIN_PASSWORD=<strong password>
```

Railway should detect the `Procfile` automatically. If it asks for a start command:

```text
gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 60
```

On first successful startup, the app creates the Phase I schema and creates the initial administrator if one does not already exist.

## Battalion Clerk integration

The website intentionally does **not** control the bot. Both applications share PostgreSQL.

```text
Discord -> Battalion Clerk -> PostgreSQL <- 5th Cavalry Regiment Website
```

The website can read Discord member identity and voice-session history. A future S-1 workflow will populate `website_member_links` so a Discord member can be tied to a website Soldier/201 File.

## Security / architecture notes

- Never commit `.env` or Discord tokens to GitHub.
- The website has its own login identities in `site_users`.
- Personnel records are separate from website accounts (`user_personnel_links`).
- Staff access is role-based and can later be made more granular without changing URLs or page structure.
- Battalion Clerk remains a collector only.

## Phase II recommendation

Build the actual S-1 personnel system and account/Discord linking first, then company/platoon/squad assignment and NCO dashboards. Once people exist as real personnel records, the readiness, DEROS, equipment and operations systems have something stable to attach to.
