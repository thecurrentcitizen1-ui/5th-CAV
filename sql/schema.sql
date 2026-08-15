CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- 5th Cavalry Regiment website foundation.
-- Battalion Clerk tables are intentionally NOT recreated here. The site reads
-- its existing discord_members, voice_sessions and website_member_links tables.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS site_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    access_role TEXT NOT NULL DEFAULT 'member',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS personnel (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    service_number TEXT UNIQUE,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    rank_code TEXT NOT NULL DEFAULT 'PVT',
    mos_code TEXT NOT NULL DEFAULT '11B',
    duty_position TEXT,
    unit_code TEXT NOT NULL DEFAULT 'A/1-5 CAV',
    platoon TEXT,
    squad TEXT,
    date_joined DATE NOT NULL DEFAULT CURRENT_DATE,
    rvn_arrival_date DATE,
    deros_date DATE,
    field_status TEXT NOT NULL DEFAULT 'Replacement',
    readiness_status TEXT NOT NULL DEFAULT 'PROCESSING',
    readiness_percent INTEGER NOT NULL DEFAULT 0 CHECK (readiness_percent BETWEEN 0 AND 100),
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_personnel_links (
    user_id UUID PRIMARY KEY REFERENCES site_users(id) ON DELETE CASCADE,
    personnel_id UUID NOT NULL UNIQUE REFERENCES personnel(id) ON DELETE CASCADE,
    linked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS unit_nodes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    parent_id UUID REFERENCES unit_nodes(id) ON DELETE CASCADE,
    unit_code TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    unit_type TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS operations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operation_code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    operation_date DATE,
    status TEXT NOT NULL DEFAULT 'PLANNING',
    location TEXT,
    classification TEXT NOT NULL DEFAULT 'FOR OFFICIAL USE',
    situation TEXT,
    mission TEXT,
    execution TEXT,
    commander_notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS qualifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    qualification_code TEXT NOT NULL,
    qualification_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'CURRENT',
    earned_at DATE,
    expires_at DATE,
    UNIQUE(personnel_id, qualification_code)
);

CREATE TABLE IF NOT EXISTS equipment_issues (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    nomenclature TEXT NOT NULL,
    item_type TEXT NOT NULL,
    serial_number TEXT,
    condition_percent INTEGER NOT NULL DEFAULT 100 CHECK (condition_percent BETWEEN 0 AND 100),
    status TEXT NOT NULL DEFAULT 'SERVICEABLE',
    issued_at DATE NOT NULL DEFAULT CURRENT_DATE,
    rounds_since_service INTEGER NOT NULL DEFAULT 0,
    last_serviced_at DATE,
    UNIQUE(personnel_id, nomenclature, serial_number)
);

CREATE TABLE IF NOT EXISTS personnel_awards (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    award_name TEXT NOT NULL,
    award_date DATE NOT NULL DEFAULT CURRENT_DATE,
    citation TEXT,
    order_number TEXT
);

CREATE TABLE IF NOT EXISTS personnel_activity_credit (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    source_reference TEXT,
    activity_type TEXT NOT NULL,
    activity_date DATE NOT NULL DEFAULT CURRENT_DATE,
    duration_seconds INTEGER NOT NULL DEFAULT 0,
    credited BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Ensure the bridge table exists even if the site is deployed before Battalion Clerk.
CREATE TABLE IF NOT EXISTS website_member_links (
    guild_id BIGINT NOT NULL,
    discord_user_id BIGINT NOT NULL,
    personnel_id TEXT NOT NULL,
    linked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, discord_user_id),
    UNIQUE (personnel_id)
);

-- Organizational framework. Exact 1965 billet counts can be refined later
-- without changing the application architecture.
INSERT INTO unit_nodes (unit_code, display_name, unit_type, sort_order)
VALUES ('1-5-CAV', '1st Battalion, 5th Cavalry Regiment', 'Battalion', 10)
ON CONFLICT (unit_code) DO NOTHING;

INSERT INTO unit_nodes (parent_id, unit_code, display_name, unit_type, sort_order)
SELECT id, 'HHC-1-5', 'Headquarters & Headquarters Company', 'Company', 10 FROM unit_nodes WHERE unit_code='1-5-CAV'
ON CONFLICT (unit_code) DO NOTHING;
INSERT INTO unit_nodes (parent_id, unit_code, display_name, unit_type, sort_order)
SELECT id, 'A-1-5', 'A Company', 'Company', 20 FROM unit_nodes WHERE unit_code='1-5-CAV'
ON CONFLICT (unit_code) DO NOTHING;
INSERT INTO unit_nodes (parent_id, unit_code, display_name, unit_type, sort_order)
SELECT id, 'B-1-5', 'B Company', 'Company', 30 FROM unit_nodes WHERE unit_code='1-5-CAV'
ON CONFLICT (unit_code) DO NOTHING;
INSERT INTO unit_nodes (parent_id, unit_code, display_name, unit_type, sort_order)
SELECT id, 'C-1-5', 'C Company', 'Company', 40 FROM unit_nodes WHERE unit_code='1-5-CAV'
ON CONFLICT (unit_code) DO NOTHING;
INSERT INTO unit_nodes (parent_id, unit_code, display_name, unit_type, sort_order)
SELECT id, 'D-1-5', 'D Company', 'Company', 50 FROM unit_nodes WHERE unit_code='1-5-CAV'
ON CONFLICT (unit_code) DO NOTHING;

INSERT INTO operations (operation_code, title, status, classification, situation, mission)
VALUES (
    'OP-001',
    'FIELD EXERCISE — INITIAL READINESS',
    'PLANNING',
    'FOR OFFICIAL USE',
    'Battalion elements prepare for initial field operations.',
    'Establish unit SOPs, communications discipline, movement standards, and reporting procedures.'
)
ON CONFLICT (operation_code) DO NOTHING;
