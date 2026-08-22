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


-- ---------------------------------------------------------------------------
-- SHARED DISCORD / BATTALION CLERK INTAKE TABLES
-- Website and Battalion Clerk both depend on these records. Defining them here
-- removes deployment-order dependency between the two Railway services.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS discord_members (
    guild_id BIGINT NOT NULL,
    discord_user_id BIGINT NOT NULL,
    username TEXT,
    display_name TEXT,
    is_bot BOOLEAN NOT NULL DEFAULT FALSE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    joined_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, discord_user_id)
);
ALTER TABLE discord_members ADD COLUMN IF NOT EXISTS username TEXT;
ALTER TABLE discord_members ADD COLUMN IF NOT EXISTS display_name TEXT;
ALTER TABLE discord_members ADD COLUMN IF NOT EXISTS is_bot BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE discord_members ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE discord_members ADD COLUMN IF NOT EXISTS joined_at TIMESTAMPTZ;
ALTER TABLE discord_members ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
CREATE INDEX IF NOT EXISTS discord_members_active_idx
ON discord_members(active,is_bot,updated_at DESC);

CREATE TABLE IF NOT EXISTS voice_sessions (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    discord_user_id BIGINT NOT NULL,
    username TEXT,
    display_name TEXT,
    channel_id TEXT,
    channel_name TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ NOT NULL,
    duration_seconds INTEGER NOT NULL DEFAULT 0,
    close_reason TEXT,
    recovered_after_restart BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS voice_sessions_member_idx
ON voice_sessions(guild_id,discord_user_id,ended_at DESC);

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


-- ---------------------------------------------------------------------------
-- PHASE 3 — SOLDIER CAREER / BATTLE ROSTER / INDIVIDUAL WEAPON FOUNDATION
-- Additive only: existing Phase 1/2 identifiers remain intact.
-- ---------------------------------------------------------------------------

ALTER TABLE personnel ADD COLUMN IF NOT EXISTS duty_status TEXT NOT NULL DEFAULT 'PRESENT FOR DUTY';
ALTER TABLE personnel ADD COLUMN IF NOT EXISTS tour_number INTEGER NOT NULL DEFAULT 1;
ALTER TABLE personnel ADD COLUMN IF NOT EXISTS roster_entered_at DATE;

ALTER TABLE personnel ADD COLUMN IF NOT EXISTS loa_start_date DATE;
ALTER TABLE personnel ADD COLUMN IF NOT EXISTS loa_expected_return_date DATE;
ALTER TABLE personnel ADD COLUMN IF NOT EXISTS loa_actual_return_date DATE;
ALTER TABLE personnel ADD COLUMN IF NOT EXISTS loa_remarks TEXT;

CREATE TABLE IF NOT EXISTS battle_roster_cards (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL UNIQUE REFERENCES personnel(id) ON DELETE CASCADE,
    roster_number TEXT NOT NULL UNIQUE,
    field_code_hash TEXT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    replaced_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS personnel_service_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    entry_date DATE NOT NULL DEFAULT CURRENT_DATE,
    entry_type TEXT NOT NULL,
    title TEXT NOT NULL,
    narrative TEXT,
    authority TEXT,
    reference_number TEXT,
    visibility TEXT NOT NULL DEFAULT 'MEMBER',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS assignment_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    unit_code TEXT NOT NULL,
    platoon TEXT,
    squad TEXT,
    duty_position TEXT,
    effective_date DATE NOT NULL DEFAULT CURRENT_DATE,
    ended_date DATE,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS weapon_inventory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    weapon_type TEXT NOT NULL DEFAULT 'U.S. RIFLE, 5.56-MM, M16',
    serial_number TEXT NOT NULL UNIQUE,
    rack_number TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'AVAILABLE FOR ISSUE',
    condition_state TEXT NOT NULL DEFAULT 'SERVICEABLE',
    condition_percent INTEGER NOT NULL DEFAULT 100 CHECK (condition_percent BETWEEN 0 AND 100),
    total_rounds INTEGER NOT NULL DEFAULT 0,
    rounds_since_cleaning INTEGER NOT NULL DEFAULT 0,
    last_fired_at TIMESTAMPTZ,
    last_cleaned_at TIMESTAMPTZ,
    last_inspected_at TIMESTAMPTZ,
    maintenance_notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS weapon_issue_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    weapon_id UUID NOT NULL REFERENCES weapon_inventory(id) ON DELETE CASCADE,
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    issued_at DATE NOT NULL DEFAULT CURRENT_DATE,
    turned_in_at DATE,
    condition_at_issue TEXT NOT NULL DEFAULT 'SERVICEABLE',
    condition_at_turn_in TEXT,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    remarks TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS one_current_weapon_per_soldier
ON weapon_issue_history(personnel_id) WHERE is_current=TRUE;
CREATE UNIQUE INDEX IF NOT EXISTS one_current_holder_per_weapon
ON weapon_issue_history(weapon_id) WHERE is_current=TRUE;
CREATE INDEX IF NOT EXISTS personnel_service_history_personnel_date_idx
ON personnel_service_history(personnel_id, entry_date DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS assignment_history_personnel_idx
ON assignment_history(personnel_id, effective_date DESC);


-- ---------------------------------------------------------------------------
-- PHASE 4 — RANKS, APPOINTMENTS, PERSONNEL ACTIONS
-- Historically scoped to the 1965–1967 presentation. CSM is intentionally
-- excluded from the 1965 catalog; the Command Sergeant Major program followed
-- later in the Vietnam era.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS rank_catalog (
    rank_code TEXT PRIMARY KEY,
    rank_name TEXT NOT NULL,
    pay_grade TEXT NOT NULL,
    rank_class TEXT NOT NULL,
    precedence INTEGER NOT NULL UNIQUE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

INSERT INTO rank_catalog (rank_code,rank_name,pay_grade,rank_class,precedence) VALUES
('PVT','Private','E-1','ENLISTED',10),
('PV2','Private','E-2','ENLISTED',20),
('PFC','Private First Class','E-3','ENLISTED',30),
('SP4','Specialist Four','E-4','SPECIALIST',40),
('CPL','Corporal','E-4','NCO',41),
('SP5','Specialist Five','E-5','SPECIALIST',50),
('SGT','Sergeant','E-5','NCO',51),
('SP6','Specialist Six','E-6','SPECIALIST',60),
('SSG','Staff Sergeant','E-6','NCO',61),
('SP7','Specialist Seven','E-7','SPECIALIST',70),
('SFC','Sergeant First Class','E-7','NCO',71),
('MSG','Master Sergeant','E-8','SENIOR NCO',80),
('1SG','First Sergeant','E-8','SENIOR NCO',81),
('SGM','Sergeant Major','E-9','SENIOR NCO',90),
('2LT','Second Lieutenant','O-1','OFFICER',100),
('1LT','First Lieutenant','O-2','OFFICER',110),
('CPT','Captain','O-3','OFFICER',120),
('MAJ','Major','O-4','OFFICER',130),
('LTC','Lieutenant Colonel','O-5','OFFICER',140),
('COL','Colonel','O-6','OFFICER',150)
ON CONFLICT (rank_code) DO UPDATE SET
rank_name=EXCLUDED.rank_name,pay_grade=EXCLUDED.pay_grade,
rank_class=EXCLUDED.rank_class,precedence=EXCLUDED.precedence,is_active=TRUE;

CREATE TABLE IF NOT EXISTS appointment_catalog (
    appointment_code TEXT PRIMARY KEY,
    appointment_name TEXT NOT NULL,
    echelon TEXT NOT NULL,
    suggested_rank TEXT,
    access_role TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

INSERT INTO appointment_catalog
(appointment_code,appointment_name,echelon,suggested_rank,access_role,sort_order) VALUES
('BN_CO','Battalion Commander','BATTALION','LTC','battalion_hq',10),
('BN_XO','Battalion Executive Officer','BATTALION','MAJ','battalion_hq',20),
('BN_SGM','Battalion Sergeant Major','BATTALION','SGM','battalion_hq',30),
('S1','Adjutant / S-1 Personnel Officer','BATTALION','CPT / 1LT','s1',40),
('S2','Intelligence Officer / S-2','BATTALION','CPT / 1LT','s2',50),
('S3','Operations Officer / S-3','BATTALION','MAJ / CPT','s3',60),
('S4','Logistics Officer / S-4','BATTALION','CPT / 1LT','s4',70),
('ASST_S3','Assistant Operations Officer','BATTALION','CPT / 1LT','s3',80),
('COMM_O','Battalion Communications Officer','BATTALION','1LT / 2LT','battalion_hq',90),
('BN_SURG','Battalion Surgeon','BATTALION','CPT','battalion_hq',100),
('BN_CLERK','Battalion Clerk','BATTALION','SP5 / SP4','s1',110),
('CO_CO','Company Commander','COMPANY','CPT','company_hq',200),
('CO_XO','Company Executive Officer','COMPANY','1LT','company_hq',210),
('CO_1SG','Company First Sergeant','COMPANY','1SG','company_hq',220),
('CO_CLERK','Company Clerk','COMPANY','SP5 / SP4','company_hq',230),
('SUP_SGT','Supply Sergeant','COMPANY','SSG','s4',240),
('COMM_SGT','Communications Sergeant','COMPANY','SGT','company_hq',250),
('ARMORER','Armorer','COMPANY','SP4','s4',260),
('PL','Platoon Leader','PLATOON','1LT / 2LT','company_hq',300),
('PSG','Platoon Sergeant','PLATOON','SFC','nco',310),
('PLT_RTO','Platoon RTO','PLATOON','PFC / SP4','member',320),
('SL','Squad Leader','SQUAD','SSG','nco',400),
('FTL','Team Leader','SQUAD','SGT / CPL','nco',410),
('ASST_SL','Assistant Squad Leader','SQUAD','CPL / SGT','nco',420),
('TRNG_NCO','Training NCO','SPECIAL DUTY','SSG / SGT','s3',500)
ON CONFLICT (appointment_code) DO UPDATE SET
appointment_name=EXCLUDED.appointment_name,echelon=EXCLUDED.echelon,
suggested_rank=EXCLUDED.suggested_rank,access_role=EXCLUDED.access_role,
sort_order=EXCLUDED.sort_order,is_active=TRUE;

CREATE TABLE IF NOT EXISTS promotion_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    old_rank_code TEXT,
    new_rank_code TEXT NOT NULL REFERENCES rank_catalog(rank_code),
    effective_date DATE NOT NULL DEFAULT CURRENT_DATE,
    authority TEXT,
    order_number TEXT,
    remarks TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS personnel_appointments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    appointment_code TEXT NOT NULL REFERENCES appointment_catalog(appointment_code),
    organization TEXT,
    appointment_status TEXT NOT NULL DEFAULT 'PERMANENT',
    effective_date DATE NOT NULL DEFAULT CURRENT_DATE,
    ended_date DATE,
    authority TEXT,
    order_number TEXT,
    remarks TEXT,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS one_current_specific_appointment_per_soldier
ON personnel_appointments(personnel_id, appointment_code) WHERE is_current=TRUE;
CREATE INDEX IF NOT EXISTS promotion_history_personnel_idx
ON promotion_history(personnel_id,effective_date DESC,created_at DESC);
CREATE INDEX IF NOT EXISTS personnel_appointments_personnel_idx
ON personnel_appointments(personnel_id,effective_date DESC,created_at DESC);


-- ---------------------------------------------------------------------------
-- PHASE 5 — BATTALION ORGANIZATION / CHAIN OF COMMAND
-- Additive organization layer. Existing unit_code/platoon/squad fields remain
-- for compatibility; unit_node_id becomes the structured source for hierarchy.
-- ---------------------------------------------------------------------------

ALTER TABLE personnel ADD COLUMN IF NOT EXISTS unit_node_id UUID REFERENCES unit_nodes(id);
ALTER TABLE assignment_history ADD COLUMN IF NOT EXISTS unit_node_id UUID REFERENCES unit_nodes(id);
ALTER TABLE personnel_appointments ADD COLUMN IF NOT EXISTS unit_node_id UUID REFERENCES unit_nodes(id);

-- The early airmobile battalion presentation uses HHC, A/B/C rifle companies,
-- and a Combat Support Company. Keep any legacy D Company row for database
-- compatibility but remove it from the active organizational roster.
UPDATE unit_nodes SET is_active=FALSE WHERE unit_code='D-1-5';

INSERT INTO unit_nodes (parent_id,unit_code,display_name,unit_type,sort_order,is_active)
SELECT id,'CS-1-5','Combat Support Company','Company',50,TRUE
FROM unit_nodes WHERE unit_code='1-5-CAV'
ON CONFLICT (unit_code) DO UPDATE SET display_name=EXCLUDED.display_name,unit_type=EXCLUDED.unit_type,sort_order=EXCLUDED.sort_order,is_active=TRUE;

-- Company headquarters / rifle platoons / rifle squads.
DO $$
DECLARE
    company_rec RECORD;
    company_letter TEXT;
    company_order INTEGER;
    company_id UUID;
    platoon_id UUID;
    p INTEGER;
    s INTEGER;
BEGIN
    FOR company_rec IN
        SELECT id,unit_code,display_name,sort_order
        FROM unit_nodes
        WHERE unit_code IN ('A-1-5','B-1-5','C-1-5')
    LOOP
        company_id := company_rec.id;
        company_letter := split_part(company_rec.unit_code,'-',1);
        company_order := company_rec.sort_order;

        INSERT INTO unit_nodes(parent_id,unit_code,display_name,unit_type,sort_order,is_active)
        VALUES(company_id, company_letter||'-HQ-1-5', 'Company Headquarters', 'Headquarters', 1, TRUE)
        ON CONFLICT(unit_code) DO UPDATE SET parent_id=EXCLUDED.parent_id,display_name=EXCLUDED.display_name,unit_type=EXCLUDED.unit_type,sort_order=EXCLUDED.sort_order,is_active=TRUE;

        FOR p IN 1..3 LOOP
            INSERT INTO unit_nodes(parent_id,unit_code,display_name,unit_type,sort_order,is_active)
            VALUES(company_id, company_letter||'-P'||p||'-1-5',
                   CASE p WHEN 1 THEN '1st Platoon' WHEN 2 THEN '2d Platoon' ELSE '3d Platoon' END,
                   'Platoon', 10+p, TRUE)
            ON CONFLICT(unit_code) DO UPDATE SET parent_id=EXCLUDED.parent_id,display_name=EXCLUDED.display_name,unit_type=EXCLUDED.unit_type,sort_order=EXCLUDED.sort_order,is_active=TRUE;

            SELECT id INTO platoon_id FROM unit_nodes WHERE unit_code=company_letter||'-P'||p||'-1-5';

            FOR s IN 1..3 LOOP
                INSERT INTO unit_nodes(parent_id,unit_code,display_name,unit_type,sort_order,is_active)
                VALUES(platoon_id, company_letter||'-P'||p||'-S'||s||'-1-5',
                       CASE s WHEN 1 THEN '1st Squad' WHEN 2 THEN '2d Squad' ELSE '3d Squad' END,
                       'Squad', 20+s, TRUE)
                ON CONFLICT(unit_code) DO UPDATE SET parent_id=EXCLUDED.parent_id,display_name=EXCLUDED.display_name,unit_type=EXCLUDED.unit_type,sort_order=EXCLUDED.sort_order,is_active=TRUE;
            END LOOP;

            INSERT INTO unit_nodes(parent_id,unit_code,display_name,unit_type,sort_order,is_active)
            VALUES(platoon_id, company_letter||'-P'||p||'-WPN-1-5','Weapons Squad','Squad',29,TRUE)
            ON CONFLICT(unit_code) DO UPDATE SET parent_id=EXCLUDED.parent_id,display_name=EXCLUDED.display_name,unit_type=EXCLUDED.unit_type,sort_order=EXCLUDED.sort_order,is_active=TRUE;
        END LOOP;
    END LOOP;
END $$;

-- HHC staff sections.
INSERT INTO unit_nodes(parent_id,unit_code,display_name,unit_type,sort_order,is_active)
SELECT id,'HHC-CMD-1-5','Battalion Command Group','Section',10,TRUE FROM unit_nodes WHERE unit_code='HHC-1-5'
ON CONFLICT(unit_code) DO UPDATE SET parent_id=EXCLUDED.parent_id,is_active=TRUE;
INSERT INTO unit_nodes(parent_id,unit_code,display_name,unit_type,sort_order,is_active)
SELECT id,'HHC-S1-1-5','S-1 Personnel Section','Section',20,TRUE FROM unit_nodes WHERE unit_code='HHC-1-5'
ON CONFLICT(unit_code) DO UPDATE SET parent_id=EXCLUDED.parent_id,is_active=TRUE;
INSERT INTO unit_nodes(parent_id,unit_code,display_name,unit_type,sort_order,is_active)
SELECT id,'HHC-S2-1-5','S-2 Intelligence Section','Section',30,TRUE FROM unit_nodes WHERE unit_code='HHC-1-5'
ON CONFLICT(unit_code) DO UPDATE SET parent_id=EXCLUDED.parent_id,is_active=TRUE;
INSERT INTO unit_nodes(parent_id,unit_code,display_name,unit_type,sort_order,is_active)
SELECT id,'HHC-S3-1-5','S-3 Operations Section','Section',40,TRUE FROM unit_nodes WHERE unit_code='HHC-1-5'
ON CONFLICT(unit_code) DO UPDATE SET parent_id=EXCLUDED.parent_id,is_active=TRUE;
INSERT INTO unit_nodes(parent_id,unit_code,display_name,unit_type,sort_order,is_active)
SELECT id,'HHC-S4-1-5','S-4 Supply Section','Section',50,TRUE FROM unit_nodes WHERE unit_code='HHC-1-5'
ON CONFLICT(unit_code) DO UPDATE SET parent_id=EXCLUDED.parent_id,is_active=TRUE;

CREATE INDEX IF NOT EXISTS personnel_unit_node_idx ON personnel(unit_node_id);
CREATE INDEX IF NOT EXISTS assignment_history_unit_node_idx ON assignment_history(unit_node_id);
CREATE INDEX IF NOT EXISTS appointments_unit_node_idx ON personnel_appointments(unit_node_id);

-- Backfill legacy A/B/C personnel to a company node where no structured
-- assignment exists. Platoon/squad-specific records are resolved by the app
-- when future assignment actions are filed.
UPDATE personnel p
SET unit_node_id = n.id
FROM unit_nodes n
WHERE p.unit_node_id IS NULL
  AND n.unit_code = CASE
      WHEN p.unit_code ILIKE 'A/%' THEN 'A-1-5'
      WHEN p.unit_code ILIKE 'B/%' THEN 'B-1-5'
      WHEN p.unit_code ILIKE 'C/%' THEN 'C-1-5'
      WHEN p.unit_code ILIKE 'HHC%' THEN 'HHC-1-5'
      ELSE NULL
  END;


-- ---------------------------------------------------------------------------
-- PHASE 6 — MORNING REPORT / BATTALION READINESS
-- ---------------------------------------------------------------------------
ALTER TABLE personnel ADD COLUMN IF NOT EXISTS activity_last_seen_at TIMESTAMPTZ;
ALTER TABLE personnel ADD COLUMN IF NOT EXISTS activity_last_duty_at TIMESTAMPTZ;
ALTER TABLE personnel ADD COLUMN IF NOT EXISTS activity_state TEXT NOT NULL DEFAULT 'CURRENT';
ALTER TABLE personnel ADD COLUMN IF NOT EXISTS administrative_review BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS duty_status_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    duty_status TEXT NOT NULL,
    effective_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    authority TEXT,
    remarks TEXT,
    is_current BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS duty_status_history_person_idx ON duty_status_history(personnel_id,effective_at DESC);

CREATE TABLE IF NOT EXISTS morning_report_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    report_date DATE NOT NULL UNIQUE,
    as_of_time TEXT NOT NULL DEFAULT '0600',
    prepared_by TEXT,
    battalion_assigned INTEGER NOT NULL DEFAULT 0,
    battalion_present INTEGER NOT NULL DEFAULT 0,
    battalion_combat_effective INTEGER NOT NULL DEFAULT 0,
    battalion_inactive INTEGER NOT NULL DEFAULT 0,
    battalion_wia INTEGER NOT NULL DEFAULT 0,
    battalion_leave INTEGER NOT NULL DEFAULT 0,
    battalion_hospital INTEGER NOT NULL DEFAULT 0,
    battalion_replacements INTEGER NOT NULL DEFAULT 0,
    battalion_deros_30 INTEGER NOT NULL DEFAULT 0,
    data_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS morning_report_snapshots_date_idx ON morning_report_snapshots(report_date DESC);

CREATE TABLE IF NOT EXISTS readiness_deficiencies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID REFERENCES personnel(id) ON DELETE CASCADE,
    unit_node_id UUID REFERENCES unit_nodes(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'NOTICE',
    title TEXT NOT NULL,
    detail TEXT,
    opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    source_key TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS readiness_deficiencies_active_idx ON readiness_deficiencies(is_active,category);


-- ---------------------------------------------------------------------------
-- PHASE 7 — S-4 SUPPLY / ARMS ROOM / PERSISTENT EQUIPMENT
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS supply_item_catalog (
    item_code TEXT PRIMARY KEY,
    item_name TEXT NOT NULL,
    category TEXT NOT NULL,
    stock_number TEXT,
    is_serialized BOOLEAN NOT NULL DEFAULT FALSE,
    default_condition TEXT NOT NULL DEFAULT 'SERVICEABLE',
    default_unit TEXT NOT NULL DEFAULT 'EA',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order INTEGER NOT NULL DEFAULT 0
);

INSERT INTO supply_item_catalog
(item_code,item_name,category,stock_number,is_serialized,default_condition,default_unit,sort_order) VALUES
('M16','Rifle, 5.56-MM, M16','ARMS','1005-856-6885',TRUE,'SERVICEABLE','EA',10),
('M60','Machine Gun, 7.62-MM, M60','ARMS','1005-605-7710',TRUE,'SERVICEABLE','EA',20),
('M1911A1','Pistol, Caliber .45, M1911A1','ARMS','1005-726-5655',TRUE,'SERVICEABLE','EA',30),
('M79','Grenade Launcher, 40-MM, M79','ARMS','1010-690-0301',TRUE,'SERVICEABLE','EA',40),
('M72','Launcher, Rocket, 66-MM, M72','ARMS','1340-087-7897',FALSE,'SERVICEABLE','EA',50),
('PRC25','Radio Set, AN/PRC-25','COMMUNICATIONS','5820-889-9723',TRUE,'SERVICEABLE','EA',60),
('M1HELMET','Helmet, Steel, M1','FIELD GEAR',NULL,FALSE,'SERVICEABLE','EA',70),
('AG44','Army Green Service Uniform','CLOTHING',NULL,FALSE,'SERVICEABLE','SET',75),
('WEBGEAR','Load-Carrying Equipment, M1956','FIELD GEAR',NULL,FALSE,'SERVICEABLE','SET',80),
('CANTEEN','Canteen, Water, 1-Quart','FIELD GEAR',NULL,FALSE,'SERVICEABLE','EA',90),
('AMMOPOUCH','Ammunition Pouch, Small Arms','FIELD GEAR',NULL,FALSE,'SERVICEABLE','EA',100),
('ETOOL','Entrenching Tool','FIELD GEAR',NULL,FALSE,'SERVICEABLE','EA',110),
('RUCK','Rucksack / Field Pack','FIELD GEAR',NULL,FALSE,'SERVICEABLE','EA',120),
('BINOCULARS','Binoculars','SPECIALIST EQUIPMENT',NULL,TRUE,'SERVICEABLE','EA',130),
('MAPCASE','Map Case','SPECIALIST EQUIPMENT',NULL,FALSE,'SERVICEABLE','EA',140),
('MEDICAL','Medical Aid Bag / Individual Medical Equipment','MEDICAL',NULL,FALSE,'SERVICEABLE','SET',150)
ON CONFLICT(item_code) DO UPDATE SET
item_name=EXCLUDED.item_name,category=EXCLUDED.category,stock_number=EXCLUDED.stock_number,
is_serialized=EXCLUDED.is_serialized,default_condition=EXCLUDED.default_condition,
default_unit=EXCLUDED.default_unit,sort_order=EXCLUDED.sort_order,is_active=TRUE;

CREATE TABLE IF NOT EXISTS equipment_inventory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    item_code TEXT NOT NULL REFERENCES supply_item_catalog(item_code),
    serial_number TEXT,
    rack_number TEXT,
    owning_unit_node_id UUID REFERENCES unit_nodes(id),
    status TEXT NOT NULL DEFAULT 'AVAILABLE',
    condition_state TEXT NOT NULL DEFAULT 'SERVICEABLE',
    condition_percent INTEGER NOT NULL DEFAULT 100 CHECK(condition_percent BETWEEN 0 AND 100),
    total_usage_count INTEGER NOT NULL DEFAULT 0,
    last_inspected_at TIMESTAMPTZ,
    last_maintained_at TIMESTAMPTZ,
    remarks TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS equipment_inventory_serial_unique
ON equipment_inventory(item_code,serial_number)
WHERE serial_number IS NOT NULL;

CREATE TABLE IF NOT EXISTS equipment_issue_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    equipment_id UUID NOT NULL REFERENCES equipment_inventory(id) ON DELETE CASCADE,
    personnel_id UUID REFERENCES personnel(id) ON DELETE SET NULL,
    unit_node_id UUID REFERENCES unit_nodes(id) ON DELETE SET NULL,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    returned_at TIMESTAMPTZ,
    condition_at_issue TEXT NOT NULL DEFAULT 'SERVICEABLE',
    condition_at_return TEXT,
    issue_authority TEXT,
    turn_in_authority TEXT,
    remarks TEXT,
    is_current BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS equipment_issue_current_idx
ON equipment_issue_history(personnel_id,is_current);

CREATE TABLE IF NOT EXISTS weapon_maintenance_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    weapon_id UUID NOT NULL REFERENCES weapon_inventory(id) ON DELETE CASCADE,
    personnel_id UUID REFERENCES personnel(id) ON DELETE SET NULL,
    action_type TEXT NOT NULL,
    condition_before TEXT,
    condition_after TEXT,
    rounds_at_action INTEGER,
    performed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    performed_by TEXT,
    remarks TEXT
);
CREATE INDEX IF NOT EXISTS weapon_maintenance_weapon_idx
ON weapon_maintenance_log(weapon_id,performed_at DESC);

-- Backward-compatible weapon maintenance migrations. Earlier Railway databases
-- may already have this table, and CREATE TABLE IF NOT EXISTS does not add
-- columns introduced by later website releases. Keep member/S-4 cleaning safe
-- across cumulative upgrades.
ALTER TABLE weapon_maintenance_log ADD COLUMN IF NOT EXISTS personnel_id UUID REFERENCES personnel(id) ON DELETE SET NULL;
ALTER TABLE weapon_maintenance_log ADD COLUMN IF NOT EXISTS action_type TEXT;
ALTER TABLE weapon_maintenance_log ADD COLUMN IF NOT EXISTS condition_before TEXT;
ALTER TABLE weapon_maintenance_log ADD COLUMN IF NOT EXISTS condition_after TEXT;
ALTER TABLE weapon_maintenance_log ADD COLUMN IF NOT EXISTS rounds_at_action INTEGER;
ALTER TABLE weapon_maintenance_log ADD COLUMN IF NOT EXISTS performed_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE weapon_maintenance_log ADD COLUMN IF NOT EXISTS performed_by TEXT;
ALTER TABLE weapon_maintenance_log ADD COLUMN IF NOT EXISTS remarks TEXT;

CREATE TABLE IF NOT EXISTS weapon_round_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    weapon_id UUID NOT NULL REFERENCES weapon_inventory(id) ON DELETE CASCADE,
    personnel_id UUID REFERENCES personnel(id) ON DELETE SET NULL,
    operation_id UUID REFERENCES operations(id) ON DELETE SET NULL,
    rounds_fired INTEGER NOT NULL CHECK(rounds_fired >= 0),
    source_type TEXT NOT NULL DEFAULT 'MANUAL ENTRY',
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    recorded_by TEXT,
    remarks TEXT
);
CREATE INDEX IF NOT EXISTS weapon_round_events_weapon_idx
ON weapon_round_events(weapon_id,recorded_at DESC);

CREATE TABLE IF NOT EXISTS company_supply_stock (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    unit_node_id UUID NOT NULL REFERENCES unit_nodes(id) ON DELETE CASCADE,
    item_code TEXT NOT NULL REFERENCES supply_item_catalog(item_code),
    quantity_on_hand INTEGER NOT NULL DEFAULT 0,
    reorder_level INTEGER NOT NULL DEFAULT 0,
    readiness_state TEXT NOT NULL DEFAULT 'ADEQUATE',
    last_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(unit_node_id,item_code)
);

CREATE TABLE IF NOT EXISTS supply_requisitions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_number TEXT NOT NULL UNIQUE,
    requesting_unit_node_id UUID REFERENCES unit_nodes(id),
    requested_by_personnel_id UUID REFERENCES personnel(id),
    item_code TEXT NOT NULL REFERENCES supply_item_catalog(item_code),
    quantity_requested INTEGER NOT NULL CHECK(quantity_requested > 0),
    priority TEXT NOT NULL DEFAULT 'ROUTINE',
    status TEXT NOT NULL DEFAULT 'SUBMITTED',
    reason TEXT,
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_at TIMESTAMPTZ,
    approved_by TEXT,
    filled_at TIMESTAMPTZ,
    remarks TEXT
);
CREATE INDEX IF NOT EXISTS supply_requisitions_status_idx ON supply_requisitions(status,submitted_at DESC);

CREATE TABLE IF NOT EXISTS supply_inspections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID REFERENCES personnel(id) ON DELETE CASCADE,
    unit_node_id UUID REFERENCES unit_nodes(id) ON DELETE CASCADE,
    inspection_type TEXT NOT NULL,
    result TEXT NOT NULL,
    inspected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    inspector TEXT,
    remarks TEXT
);

-- Seed a small arms-room pool if none exists.
DO $$
DECLARE
  i INTEGER;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM weapon_inventory) THEN
    FOR i IN 1..24 LOOP
      INSERT INTO weapon_inventory
      (weapon_type,serial_number,rack_number,condition_state,condition_percent,status)
      VALUES(
        'M16',
        (1847000 + i)::TEXT,
        'A-' || LPAD(i::TEXT,2,'0'),
        'SERVICEABLE',
        100,
        'AVAILABLE'
      );
    END LOOP;
  END IF;
END $$;


-- ---------------------------------------------------------------------------
-- PHASE 8 — S-3 OPERATIONS CENTER / COMBAT HISTORY
-- ---------------------------------------------------------------------------

ALTER TABLE operations ADD COLUMN IF NOT EXISTS operation_number TEXT;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS operation_type TEXT NOT NULL DEFAULT 'OFFICIAL OPERATION';
ALTER TABLE operations ADD COLUMN IF NOT EXISTS area_of_operations TEXT;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS commander TEXT;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS h_hour TEXT;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS situation TEXT;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS mission TEXT;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS execution TEXT;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS service_support TEXT;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS command_signal TEXT;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS result TEXT;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS commander_remarks TEXT;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'PLANNING';
ALTER TABLE operations ADD COLUMN IF NOT EXISTS start_at TIMESTAMPTZ;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS operations_operation_number_unique
ON operations(operation_number) WHERE operation_number IS NOT NULL;

CREATE TABLE IF NOT EXISTS operation_units (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operation_id UUID NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    unit_node_id UUID NOT NULL REFERENCES unit_nodes(id) ON DELETE CASCADE,
    task TEXT,
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE(operation_id,unit_node_id)
);

CREATE TABLE IF NOT EXISTS operation_participation (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operation_id UUID NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    unit_node_id UUID REFERENCES unit_nodes(id),
    duty_role TEXT,
    attendance_status TEXT NOT NULL DEFAULT 'PARTICIPATED',
    rounds_expended INTEGER NOT NULL DEFAULT 0,
    casualty_status TEXT,
    remarks TEXT,
    credited_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    credited_by TEXT,
    UNIQUE(operation_id,personnel_id)
);
CREATE INDEX IF NOT EXISTS operation_participation_person_idx
ON operation_participation(personnel_id,credited_at DESC);

CREATE TABLE IF NOT EXISTS after_action_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operation_id UUID NOT NULL UNIQUE REFERENCES operations(id) ON DELETE CASCADE,
    objective TEXT,
    result TEXT,
    significant_actions TEXT,
    casualties TEXT,
    ammunition_expended INTEGER NOT NULL DEFAULT 0,
    commander_remarks TEXT,
    prepared_by TEXT,
    filed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS operation_photographs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operation_id UUID NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    image_path TEXT NOT NULL,
    caption TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS casualty_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    operation_id UUID REFERENCES operations(id) ON DELETE SET NULL,
    casualty_type TEXT NOT NULL,
    effective_date DATE NOT NULL DEFAULT CURRENT_DATE,
    returned_to_duty_date DATE,
    award_recommended BOOLEAN NOT NULL DEFAULT FALSE,
    remarks TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS casualty_records_person_idx
ON casualty_records(personnel_id,effective_date DESC);

CREATE TABLE IF NOT EXISTS personnel_recommendations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    operation_id UUID REFERENCES operations(id) ON DELETE SET NULL,
    recommendation_type TEXT NOT NULL,
    recommended_action TEXT NOT NULL,
    justification TEXT NOT NULL,
    recommending_personnel_id UUID REFERENCES personnel(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    reviewed_by TEXT,
    reviewed_at TIMESTAMPTZ,
    remarks TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS personnel_recommendations_status_idx
ON personnel_recommendations(status,created_at DESC);
ALTER TABLE personnel_recommendations ADD COLUMN IF NOT EXISTS s1_award_name TEXT;
ALTER TABLE personnel_recommendations ADD COLUMN IF NOT EXISTS s1_justification TEXT;
ALTER TABLE personnel_recommendations ADD COLUMN IF NOT EXISTS s1_reviewed_by TEXT;
ALTER TABLE personnel_recommendations ADD COLUMN IF NOT EXISTS s1_reviewed_at TIMESTAMPTZ;
ALTER TABLE personnel_recommendations ADD COLUMN IF NOT EXISTS command_reviewed_by TEXT;
ALTER TABLE personnel_recommendations ADD COLUMN IF NOT EXISTS command_reviewed_at TIMESTAMPTZ;
ALTER TABLE personnel_recommendations ADD COLUMN IF NOT EXISTS command_decision TEXT;
ALTER TABLE personnel_recommendations ADD COLUMN IF NOT EXISTS award_order_number TEXT;
ALTER TABLE personnel_recommendations ADD COLUMN IF NOT EXISTS award_effective_date DATE;

CREATE TABLE IF NOT EXISTS operation_journal_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operation_id UUID NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    entry_date DATE NOT NULL DEFAULT CURRENT_DATE,
    entry_type TEXT NOT NULL DEFAULT 'OPERATIONS JOURNAL',
    title TEXT NOT NULL,
    body TEXT,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);



-- ---------------------------------------------------------------------------
-- PHASE 9 — TRAINING OFFICE / HLL: VIETNAM DUTY QUALIFICATIONS
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS duty_qualification_types (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    battlefield_unit TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS personnel_duty_qualifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    qualification_type_id UUID NOT NULL REFERENCES duty_qualification_types(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'QUALIFIED',
    qualified_date DATE,
    expiration_date DATE,
    instructor_personnel_id UUID REFERENCES personnel(id) ON DELETE SET NULL,
    remarks TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(personnel_id,qualification_type_id)
);

CREATE TABLE IF NOT EXISTS training_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    qualification_type_id UUID REFERENCES duty_qualification_types(id) ON DELETE SET NULL,
    request_type TEXT NOT NULL DEFAULT 'QUALIFICATION',
    status TEXT NOT NULL DEFAULT 'PENDING',
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    scheduled_for TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    remarks TEXT
);

INSERT INTO duty_qualification_types(code,display_name,battlefield_unit,sort_order) VALUES
('COMMANDER','Commander','COMMAND',10),
('SQUAD_LEADER','Squad Leader','INFANTRY',20),
('RIFLEMAN','Rifleman','INFANTRY',30),
('GRENADIER','Grenadier','INFANTRY',40),
('ENGINEER','Engineer','INFANTRY',50),
('MEDIC','Medic','INFANTRY',60),
('SPECIALIST','Specialist','INFANTRY',70),
('MACHINE_GUNNER','Machine Gunner','INFANTRY',80),
('SPOTTER','Spotter','RECON',90),
('SNIPER','Sniper','RECON',100),
('TANK_COMMANDER','Tank Commander','ARMOUR',110),
('CREWMAN','Crewman','ARMOUR',120),
('LOGISTICS_OFFICER','Logistics Officer','HELICOPTER',130),
('PILOT','Pilot','HELICOPTER',140),
('MORTAR_OBSERVER','Observer','MORTAR SQUAD',150),
('MORTAR_SUPPORT','Support','MORTAR SQUAD',160),
('MORTAR_GUNNER','Gunner','MORTAR SQUAD',170)
ON CONFLICT(code) DO UPDATE SET
 display_name=EXCLUDED.display_name,battlefield_unit=EXCLUDED.battlefield_unit,
 sort_order=EXCLUDED.sort_order,is_active=TRUE;


-- ---------------------------------------------------------------------------
-- Battalion scheduled duty / attendance bridge.
-- Battalion Clerk files event windows and voice-presence intervals here. The
-- website awards one activity credit after 45 minutes (2700 seconds) of
-- qualifying presence in TRAINING, OPERATION, or MEETING duty.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS battalion_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_event_id TEXT UNIQUE,
    event_type TEXT NOT NULL CHECK (event_type IN ('TRAINING','OPERATION','MEETING')),
    title TEXT NOT NULL,
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ NOT NULL,
    channel_name TEXT NOT NULL,
    channel_id BIGINT,
    operation_id UUID REFERENCES operations(id) ON DELETE SET NULL,
    rounds_per_soldier INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'SCHEDULED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (ends_at > starts_at)
);

CREATE INDEX IF NOT EXISTS battalion_events_window_idx
ON battalion_events(starts_at,ends_at,event_type);

CREATE TABLE IF NOT EXISTS battalion_event_attendance (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id UUID NOT NULL REFERENCES battalion_events(id) ON DELETE CASCADE,
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    qualifying_seconds INTEGER NOT NULL DEFAULT 0,
    first_seen_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ,
    credited_at TIMESTAMPTZ,
    source_reference TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(event_id,personnel_id)
);

-- Battalion Clerk persistent duty-channel assignments.
CREATE TABLE IF NOT EXISTS clerk_duty_channels (
    guild_id BIGINT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('TRAINING','OPERATION','MEETING')),
    channel_id BIGINT NOT NULL,
    channel_name TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id,event_type)
);

-- Idempotency ledger for voice-presence chunks sent by Battalion Clerk.
CREATE TABLE IF NOT EXISTS battalion_attendance_segments (
    segment_id TEXT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    discord_user_id BIGINT NOT NULL,
    event_id UUID REFERENCES battalion_events(id) ON DELETE CASCADE,
    personnel_id UUID REFERENCES personnel(id) ON DELETE CASCADE,
    qualifying_seconds INTEGER NOT NULL DEFAULT 0,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS battalion_attendance_segments_event_idx
ON battalion_attendance_segments(event_id,personnel_id);

-- ---------------------------------------------------------------------------
-- REPLACEMENT TRAINING / PROMOTION PROGRESSION
-- Vietnam-era battalion onboarding and rank eligibility system.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS personnel_progress_control (
    personnel_id UUID PRIMARY KEY REFERENCES personnel(id) ON DELETE CASCADE,
    s1_onboarded_at TIMESTAMPTZ,
    s1_onboarded_by TEXT,
    rules_acknowledged_at TIMESTAMPTZ,
    rules_acknowledged_by TEXT,
    promotion_hold BOOLEAN NOT NULL DEFAULT FALSE,
    promotion_hold_reason TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS training_program_catalog (
    program_code TEXT PRIMARY KEY,
    program_name TEXT NOT NULL,
    description TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS personnel_training_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    program_code TEXT NOT NULL REFERENCES training_program_catalog(program_code),
    status TEXT NOT NULL DEFAULT 'IN PROGRESS',
    started_at DATE NOT NULL DEFAULT CURRENT_DATE,
    completed_at DATE,
    certified_by TEXT,
    remarks TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(personnel_id, program_code)
);

INSERT INTO training_program_catalog(program_code,program_name,description,sort_order) VALUES
('INITIAL_INPROCESSING','Initial Battalion In-Processing','Administrative S-1 in-processing for Soldiers who enter the battalion above Private; no retroactive PVT progression is created.',5),
('REPLACEMENT','Replacement Training','Initial battalion replacement processing for newly assigned Privates.',10),
('COMBAT_ORIENTATION','Battalion Combat Orientation','Battalion field procedures, combat SOPs, communications, movement, LZ procedures, and individual readiness.',20),
('SQUAD_LEADERSHIP','Squad Leadership Course','Small-unit leadership preparation for junior NCOs.',30),
('PLATOON_LEADERSHIP','Platoon Leadership Course','Advanced platoon-level leadership preparation for senior NCOs.',40),
('OFFICER_ORIENTATION','Officer Orientation','Battalion orientation and command procedures for newly assigned officers.',50),
('COMPANY_LEADERSHIP','Company Leadership Course','Company-level leadership and command preparation for officers.',60)
ON CONFLICT (program_code) DO UPDATE SET
program_name=EXCLUDED.program_name,description=EXCLUDED.description,
sort_order=EXCLUDED.sort_order,is_active=TRUE;

CREATE INDEX IF NOT EXISTS personnel_training_records_person_idx
ON personnel_training_records(personnel_id,completed_at DESC,created_at DESC);

-- S-3 now owns both operations and training. Migrate legacy Training Office access.
UPDATE site_users SET access_role='s3', updated_at=NOW() WHERE access_role='training';
UPDATE appointment_catalog SET access_role='s3' WHERE appointment_code='TRNG_NCO';

CREATE TABLE IF NOT EXISTS personnel_documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    document_type TEXT NOT NULL,
    document_number TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    effective_date DATE NOT NULL DEFAULT CURRENT_DATE,
    authority TEXT,
    body_text TEXT NOT NULL,
    details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_key TEXT UNIQUE,
    source_guild_id BIGINT,
    discord_posted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS personnel_documents_person_idx ON personnel_documents(personnel_id,effective_date DESC,created_at DESC);
CREATE INDEX IF NOT EXISTS personnel_documents_pending_idx ON personnel_documents(source_guild_id,discord_posted_at,created_at);

-- ---------------------------------------------------------------------------
-- INTEGRATED BATTALION ACTION / DOCUMENT / MOS CONTROL
-- Central workflow ledger shared by S-1, S-3, S-4 and Battalion Headquarters.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS personnel_actions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID REFERENCES personnel(id) ON DELETE CASCADE,
    action_type TEXT NOT NULL,
    subject TEXT NOT NULL,
    owning_section TEXT NOT NULL DEFAULT 'S-1',
    status TEXT NOT NULL DEFAULT 'OPEN',
    priority TEXT NOT NULL DEFAULT 'ROUTINE',
    initiated_by TEXT,
    assigned_to TEXT,
    due_date DATE,
    details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_key TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS personnel_actions_section_status_idx ON personnel_actions(owning_section,status,created_at DESC);
CREATE INDEX IF NOT EXISTS personnel_actions_person_idx ON personnel_actions(personnel_id,created_at DESC);

CREATE TABLE IF NOT EXISTS personnel_action_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action_id UUID NOT NULL REFERENCES personnel_actions(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    actor TEXT,
    remarks TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS personnel_action_events_action_idx ON personnel_action_events(action_id,created_at);

CREATE TABLE IF NOT EXISTS personnel_document_amendments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES personnel_documents(id) ON DELETE CASCADE,
    amendment_number TEXT NOT NULL UNIQUE,
    amendment_date DATE NOT NULL DEFAULT CURRENT_DATE,
    authority TEXT,
    reason TEXT NOT NULL,
    corrected_text TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS personnel_document_amendments_doc_idx ON personnel_document_amendments(document_id,amendment_date DESC);

CREATE TABLE IF NOT EXISTS personnel_mos_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    mos_code TEXT NOT NULL,
    mos_title TEXT NOT NULL,
    mos_kind TEXT NOT NULL DEFAULT 'SECONDARY',
    status TEXT NOT NULL DEFAULT 'CURRENT',
    effective_date DATE NOT NULL DEFAULT CURRENT_DATE,
    qualified_by TEXT,
    remarks TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(personnel_id,mos_code,mos_kind)
);
CREATE INDEX IF NOT EXISTS personnel_mos_records_person_idx ON personnel_mos_records(personnel_id,effective_date DESC);

-- Preserve every existing Soldier's currently stored MOS as his primary battlefield MOS.
INSERT INTO personnel_mos_records(personnel_id,mos_code,mos_title,mos_kind,effective_date,remarks)
SELECT id,mos_code,COALESCE(duty_position,mos_code),'PRIMARY',COALESCE(date_joined,CURRENT_DATE),'Migrated from current personnel record.'
FROM personnel
WHERE mos_code IS NOT NULL AND BTRIM(mos_code)<>''
ON CONFLICT (personnel_id,mos_code,mos_kind) DO NOTHING;

-- ---------------------------------------------------------------------------
-- FULL FLOW / BATTALION LIFECYCLE CONTROL
-- Persistent lifecycle, staff audit, notifications, inspection cycles,
-- historical archive, document sequence control and system recovery.
-- ---------------------------------------------------------------------------
ALTER TABLE personnel ADD COLUMN IF NOT EXISTS lifecycle_state TEXT NOT NULL DEFAULT 'REPLACEMENT';
ALTER TABLE personnel ADD COLUMN IF NOT EXISTS lifecycle_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE personnel ADD COLUMN IF NOT EXISTS separated_at DATE;
ALTER TABLE personnel ADD COLUMN IF NOT EXISTS separation_reason TEXT;
ALTER TABLE personnel ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE personnel ADD COLUMN IF NOT EXISTS deros_extension_date DATE;

CREATE TABLE IF NOT EXISTS battalion_document_sequences (
    document_class TEXT NOT NULL,
    document_year INTEGER NOT NULL,
    next_number INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(document_class,document_year)
);

CREATE TABLE IF NOT EXISTS staff_duty_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    section TEXT NOT NULL,
    actor TEXT,
    personnel_id UUID REFERENCES personnel(id) ON DELETE SET NULL,
    action_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    reference_number TEXT,
    details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS staff_duty_log_created_idx ON staff_duty_log(created_at DESC);
CREATE INDEX IF NOT EXISTS staff_duty_log_person_idx ON staff_duty_log(personnel_id,created_at DESC);

CREATE TABLE IF NOT EXISTS soldier_notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    section TEXT NOT NULL DEFAULT 'HEADQUARTERS',
    notification_type TEXT NOT NULL DEFAULT 'NOTICE',
    title TEXT NOT NULL,
    message TEXT,
    target_endpoint TEXT,
    target_anchor TEXT,
    source_key TEXT UNIQUE,
    priority TEXT NOT NULL DEFAULT 'ROUTINE',
    acknowledged_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS soldier_notifications_person_idx ON soldier_notifications(personnel_id,acknowledged_at,created_at DESC);

CREATE TABLE IF NOT EXISTS weapon_inspections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    weapon_id UUID NOT NULL REFERENCES weapon_inventory(id) ON DELETE CASCADE,
    personnel_id UUID REFERENCES personnel(id) ON DELETE SET NULL,
    inspection_date DATE NOT NULL DEFAULT CURRENT_DATE,
    next_due_date DATE NOT NULL DEFAULT (CURRENT_DATE + 14),
    condition_state TEXT NOT NULL DEFAULT 'SERVICEABLE',
    inspected_by TEXT,
    remarks TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS weapon_inspections_due_idx ON weapon_inspections(next_due_date);

CREATE TABLE IF NOT EXISTS battalion_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    history_date DATE NOT NULL DEFAULT CURRENT_DATE,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    narrative TEXT,
    personnel_id UUID REFERENCES personnel(id) ON DELETE SET NULL,
    operation_id UUID REFERENCES operations(id) ON DELETE SET NULL,
    reference_number TEXT,
    visibility TEXT NOT NULL DEFAULT 'STAFF',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS battalion_history_date_idx ON battalion_history(history_date DESC,created_at DESC);

CREATE TABLE IF NOT EXISTS personnel_tour_extensions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    previous_deros DATE,
    new_deros DATE NOT NULL,
    extension_days INTEGER,
    authority TEXT,
    remarks TEXT,
    document_id UUID REFERENCES personnel_documents(id) ON DELETE SET NULL,
    effective_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE operations ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS aar_filed_at TIMESTAMPTZ;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS lifecycle_status TEXT NOT NULL DEFAULT 'PLANNING';

-- Existing active personnel are normalized into lifecycle control without deleting history.
UPDATE personnel
SET lifecycle_state = CASE
    WHEN archived OR separated_at IS NOT NULL THEN 'ARCHIVED'
    WHEN UPPER(COALESCE(duty_status,''))='LEAVE' THEN 'AUTHORIZED LEAVE'
    WHEN UPPER(COALESCE(duty_status,''))='HOSPITAL' THEN 'HOSPITAL'
    WHEN UPPER(COALESCE(duty_status,''))='WIA' THEN 'WIA'
    WHEN UPPER(COALESCE(duty_status,''))='TEMPORARY DUTY' THEN 'TEMPORARY DUTY'
    WHEN UPPER(COALESCE(duty_status,'')) LIKE 'REPLACEMENT%' OR COALESCE(platoon,'')='' OR COALESCE(squad,'')='' THEN 'REPLACEMENT'
    ELSE lifecycle_state
END
WHERE lifecycle_state IS NULL OR lifecycle_state='REPLACEMENT';

-- ---------------------------------------------------------------------------
-- DEEP BATTALION IMMERSION / FLOW PACK
-- Billets, MOS proficiency, instructor status, leadership records, acting
-- appointments, operation duty assignments, recognition, Tour Book, command
-- changes, document workflow stamps, attendance grades and authoritative sync.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS battalion_mos_catalog (
    mos_code TEXT PRIMARY KEY,
    mos_title TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'INFANTRY',
    recruiting_priority INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order INTEGER NOT NULL DEFAULT 0
);

INSERT INTO battalion_mos_catalog(mos_code,mos_title,category,sort_order) VALUES
('00R','Replacement — MOS Pending','ADMIN',5),('00C','Battalion Commander','COMMAND',10),('11L','Infantry Squad Leader','INFANTRY',20),
('11R','Rifleman','INFANTRY',30),('11G','Grenadier','INFANTRY',40),('11M','Machine Gunner','INFANTRY',50),
('91M','Combat Medic','MEDICAL',60),('12E','Combat Engineer','ENGINEER',70),('76S','Supply & Support Specialist','SUPPORT',80),
('11S','Reconnaissance Team Leader','RECON',90),('11N','Sniper','RECON',100),('19C','Armor Commander','ARMOR',110),
('19K','Armor Crewman','ARMOR',120),('67L','Aviation Logistics','AVIATION',130),('67P','Rotary-Wing Pilot','AVIATION',140),
('67C','Helicopter Crew Chief','AVIATION',150),('67G','Aerial Door Gunner','AVIATION',160),('11O','Mortar Observer','MORTAR',170),
('11A','Mortar Ammunition Bearer','MORTAR',180),('11T','Mortar Gunner','MORTAR',190)
ON CONFLICT(mos_code) DO UPDATE SET mos_title=EXCLUDED.mos_title,category=EXCLUDED.category,sort_order=EXCLUDED.sort_order;

CREATE TABLE IF NOT EXISTS unit_billets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    unit_node_id UUID REFERENCES unit_nodes(id) ON DELETE CASCADE,
    unit_code TEXT NOT NULL,
    billet_code TEXT NOT NULL,
    billet_title TEXT NOT NULL,
    preferred_mos_code TEXT REFERENCES battalion_mos_catalog(mos_code),
    authorized_strength INTEGER NOT NULL DEFAULT 1,
    min_rank_code TEXT,
    is_leadership BOOLEAN NOT NULL DEFAULT FALSE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE(unit_code,billet_code)
);

CREATE TABLE IF NOT EXISTS personnel_mos_proficiency (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    mos_code TEXT NOT NULL REFERENCES battalion_mos_catalog(mos_code),
    proficiency_level TEXT NOT NULL DEFAULT 'QUALIFIED',
    proficiency_order INTEGER NOT NULL DEFAULT 1,
    effective_date DATE NOT NULL DEFAULT CURRENT_DATE,
    certified_by TEXT,
    remarks TEXT,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE(personnel_id,mos_code)
);

CREATE TABLE IF NOT EXISTS instructor_qualifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    qualification_area TEXT NOT NULL,
    effective_date DATE NOT NULL DEFAULT CURRENT_DATE,
    expires_at DATE,
    certified_by TEXT,
    status TEXT NOT NULL DEFAULT 'CURRENT',
    remarks TEXT,
    UNIQUE(personnel_id,qualification_area)
);

CREATE TABLE IF NOT EXISTS leadership_performance_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    record_date DATE NOT NULL DEFAULT CURRENT_DATE,
    leadership_type TEXT NOT NULL,
    title TEXT NOT NULL,
    narrative TEXT NOT NULL,
    operation_id UUID REFERENCES operations(id) ON DELETE SET NULL,
    recorded_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS acting_appointments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    billet_title TEXT NOT NULL,
    unit_code TEXT,
    effective_date DATE NOT NULL DEFAULT CURRENT_DATE,
    ended_date DATE,
    authority TEXT,
    remarks TEXT,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS operation_duty_assignments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operation_id UUID NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    duty_role TEXT NOT NULL,
    mos_code TEXT,
    element TEXT,
    assigned_by TEXT,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    discord_published_at TIMESTAMPTZ,
    remarks TEXT,
    UNIQUE(operation_id,personnel_id)
);
CREATE INDEX IF NOT EXISTS operation_duty_assignments_publish_idx ON operation_duty_assignments(operation_id,discord_published_at);

CREATE TABLE IF NOT EXISTS operation_readiness_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operation_id UUID NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    assigned_personnel INTEGER NOT NULL DEFAULT 0,
    present_personnel INTEGER NOT NULL DEFAULT 0,
    serviceable_weapons INTEGER NOT NULL DEFAULT 0,
    qualified_roles INTEGER NOT NULL DEFAULT 0,
    total_roles INTEGER NOT NULL DEFAULT 0,
    readiness_percent INTEGER NOT NULL DEFAULT 0,
    remarks TEXT
);

CREATE TABLE IF NOT EXISTS unit_readiness_streaks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    unit_code TEXT NOT NULL UNIQUE,
    current_streak INTEGER NOT NULL DEFAULT 0,
    best_streak INTEGER NOT NULL DEFAULT 0,
    last_qualified_operation_id UUID REFERENCES operations(id) ON DELETE SET NULL,
    last_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    threshold_percent INTEGER NOT NULL DEFAULT 85
);

CREATE TABLE IF NOT EXISTS soldier_recognitions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    recognition_type TEXT NOT NULL DEFAULT 'BATTALION HONOR SOLDIER',
    period_label TEXT NOT NULL,
    effective_date DATE NOT NULL DEFAULT CURRENT_DATE,
    narrative TEXT,
    authority TEXT,
    document_id UUID REFERENCES personnel_documents(id) ON DELETE SET NULL,
    UNIQUE(recognition_type,period_label)
);

CREATE TABLE IF NOT EXISTS soldier_tour_book (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    entry_date DATE NOT NULL DEFAULT CURRENT_DATE,
    entry_type TEXT NOT NULL,
    title TEXT NOT NULL,
    narrative TEXT,
    operation_id UUID REFERENCES operations(id) ON DELETE SET NULL,
    document_id UUID REFERENCES personnel_documents(id) ON DELETE SET NULL,
    image_path TEXT,
    source_key TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS soldier_tour_book_person_idx ON soldier_tour_book(personnel_id,entry_date DESC,created_at DESC);

CREATE TABLE IF NOT EXISTS command_change_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    unit_code TEXT NOT NULL,
    billet_title TEXT NOT NULL,
    outgoing_personnel_id UUID REFERENCES personnel(id) ON DELETE SET NULL,
    incoming_personnel_id UUID REFERENCES personnel(id) ON DELETE SET NULL,
    effective_date DATE NOT NULL DEFAULT CURRENT_DATE,
    authority TEXT,
    document_id UUID REFERENCES personnel_documents(id) ON DELETE SET NULL,
    remarks TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE personnel_documents ADD COLUMN IF NOT EXISTS workflow_status TEXT NOT NULL DEFAULT 'FILED';
ALTER TABLE personnel_documents ADD COLUMN IF NOT EXISTS by_order_of TEXT;
ALTER TABLE personnel_documents ADD COLUMN IF NOT EXISTS signature_block TEXT;
ALTER TABLE personnel_documents ADD COLUMN IF NOT EXISTS supersedes_document_id UUID REFERENCES personnel_documents(id) ON DELETE SET NULL;
ALTER TABLE personnel_recommendations ADD COLUMN IF NOT EXISTS promotion_narrative TEXT;
ALTER TABLE battalion_event_attendance ADD COLUMN IF NOT EXISTS attendance_grade TEXT NOT NULL DEFAULT 'NO CREDIT';
ALTER TABLE battalion_event_attendance ADD COLUMN IF NOT EXISTS attendance_percent INTEGER NOT NULL DEFAULT 0;
ALTER TABLE personnel ADD COLUMN IF NOT EXISTS canonical_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- Canonical ownership: personnel is the active state; history/documents are evidence.
CREATE OR REPLACE FUNCTION touch_personnel_canonical() RETURNS trigger AS $$
BEGIN
  IF (OLD.rank_code IS DISTINCT FROM NEW.rank_code) OR
     (OLD.mos_code IS DISTINCT FROM NEW.mos_code) OR
     (OLD.unit_code IS DISTINCT FROM NEW.unit_code) OR
     (OLD.platoon IS DISTINCT FROM NEW.platoon) OR
     (OLD.squad IS DISTINCT FROM NEW.squad) OR
     (OLD.duty_position IS DISTINCT FROM NEW.duty_position) OR
     (OLD.duty_status IS DISTINCT FROM NEW.duty_status) THEN
    NEW.canonical_updated_at = NOW();
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS personnel_canonical_touch ON personnel;
CREATE TRIGGER personnel_canonical_touch BEFORE UPDATE ON personnel FOR EACH ROW EXECUTE FUNCTION touch_personnel_canonical();

-- Starter authorized-strength model; Command may expand these later without code changes.
INSERT INTO unit_billets(unit_code,billet_code,billet_title,preferred_mos_code,authorized_strength,is_leadership,sort_order) VALUES
('A/1-5 CAV','CO-HQ','Company Leadership','11L',2,TRUE,10),('A/1-5 CAV','RFL','Rifleman','11R',12,FALSE,20),('A/1-5 CAV','GRN','Grenadier','11G',3,FALSE,30),('A/1-5 CAV','MG','Machine Gunner','11M',3,FALSE,40),('A/1-5 CAV','MED','Combat Medic','91M',2,FALSE,50),
('B/1-5 CAV','CO-HQ','Company Leadership','11L',2,TRUE,10),('B/1-5 CAV','RFL','Rifleman','11R',12,FALSE,20),('B/1-5 CAV','GRN','Grenadier','11G',3,FALSE,30),('B/1-5 CAV','MG','Machine Gunner','11M',3,FALSE,40),('B/1-5 CAV','MED','Combat Medic','91M',2,FALSE,50),
('C/1-5 CAV','CO-HQ','Company Leadership','11L',2,TRUE,10),('C/1-5 CAV','RFL','Rifleman','11R',12,FALSE,20),('C/1-5 CAV','GRN','Grenadier','11G',3,FALSE,30),('C/1-5 CAV','MG','Machine Gunner','11M',3,FALSE,40),('C/1-5 CAV','MED','Combat Medic','91M',2,FALSE,50),
('HHC/1-5 CAV','SUP','Supply & Support Specialist','76S',4,FALSE,10),('HHC/1-5 CAV','REC','Reconnaissance Team Leader','11S',2,TRUE,20),('HHC/1-5 CAV','SNP','Sniper','11N',2,FALSE,30),('HHC/1-5 CAV','PIL','Rotary-Wing Pilot','67P',4,FALSE,40),('HHC/1-5 CAV','CC','Helicopter Crew Chief','67C',4,FALSE,50),('HHC/1-5 CAV','DG','Aerial Door Gunner','67G',4,FALSE,60),('HHC/1-5 CAV','MORT','Mortar Gunner','11T',4,FALSE,70)
ON CONFLICT(unit_code,billet_code) DO NOTHING;


-- Recruiting pipeline: website application -> Discord verification -> Command approval -> personnel conversion.
CREATE TABLE IF NOT EXISTS recruiting_cases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_number TEXT NOT NULL UNIQUE,
    public_token TEXT NOT NULL UNIQUE,
    verification_code TEXT NOT NULL UNIQUE,
    verification_expires_at TIMESTAMPTZ NOT NULL,
    verification_used_at TIMESTAMPTZ,
    discord_username_input TEXT NOT NULL,
    discord_user_id BIGINT,
    discord_verified_username TEXT,
    guild_id BIGINT,
    age INTEGER,
    timezone_name TEXT NOT NULL,
    hll_experience TEXT NOT NULL,
    role_interest TEXT NOT NULL,
    looking_for TEXT NOT NULL,
    play_style TEXT NOT NULL,
    follows_chain BOOLEAN NOT NULL,
    participation TEXT NOT NULL,
    applicant_notes TEXT,
    status TEXT NOT NULL DEFAULT 'SUBMITTED',
    command_request TEXT,
    applicant_response TEXT,
    command_notes TEXT,
    reviewed_by TEXT,
    reviewed_at TIMESTAMPTZ,
    approved_at TIMESTAMPTZ,
    denied_at TIMESTAMPTZ,
    discord_notified_at TIMESTAMPTZ,
    personnel_id UUID REFERENCES personnel(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS recruiting_cases_status_idx ON recruiting_cases(status,created_at DESC);
CREATE INDEX IF NOT EXISTS recruiting_cases_discord_idx ON recruiting_cases(discord_user_id,status);

-- OAuth identity migration: minimal Discord `identify` scope replaces one-time verification codes.
ALTER TABLE recruiting_cases ALTER COLUMN verification_code DROP NOT NULL;
ALTER TABLE recruiting_cases ALTER COLUMN verification_expires_at DROP NOT NULL;
ALTER TABLE recruiting_cases ALTER COLUMN discord_username_input DROP NOT NULL;
ALTER TABLE recruiting_cases ADD COLUMN IF NOT EXISTS discord_avatar_hash TEXT;
ALTER TABLE recruiting_cases ADD COLUMN IF NOT EXISTS discord_oauth_linked_at TIMESTAMPTZ;
ALTER TABLE recruiting_cases ADD COLUMN IF NOT EXISTS discord_last_notified_status TEXT;
ALTER TABLE recruiting_cases ADD COLUMN IF NOT EXISTS discord_last_notified_at TIMESTAMPTZ;



-- ---------------------------------------------------------------------------
-- RIBBON PROGRESS / AUTOMATIC AWARDS
-- Website remains authoritative for ribbon eligibility; Battalion Clerk files
-- attendance/instructor facts that feed these rules.
-- ---------------------------------------------------------------------------
ALTER TABLE recruiting_cases ADD COLUMN IF NOT EXISTS recruited_by_personnel_id UUID REFERENCES personnel(id) ON DELETE SET NULL;
ALTER TABLE recruiting_cases ADD COLUMN IF NOT EXISTS recruiter_credit_filed_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS ribbon_catalog (
    ribbon_code TEXT PRIMARY KEY,
    ribbon_name TEXT NOT NULL UNIQUE,
    automation_mode TEXT NOT NULL DEFAULT 'AUTOMATIC',
    requirement_text TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

ALTER TABLE ribbon_catalog ADD COLUMN IF NOT EXISTS image_filename TEXT;

INSERT INTO ribbon_catalog(ribbon_code,ribbon_name,automation_mode,requirement_text,sort_order) VALUES
('INSTRUCTOR','Instructor Ribbon','AUTOMATIC','5 completed official training periods as a filed instructor or assistant instructor.',10),
('NCO_LEADERSHIP','NCO Leadership Ribbon','AUTOMATIC','30 qualifying days in Team Leader, Assistant Squad Leader, Squad Leader, or Platoon Sergeant billet and 3 qualifying official events while serving in that billet.',20),
('RECRUITING','Recruiting Ribbon','AUTOMATIC','3 referred applicants who complete the recruiting pipeline and are converted to active personnel.',30),
('COMBAT_INFANTRY','Combat Infantry Ribbon','AUTOMATIC','10 credited official combat operations.',40),
('CAMPAIGN','Campaign Ribbon','AUTOMATIC','Qualify under a designated battalion campaign.',50),
('GOOD_CONDUCT','Good Conduct Ribbon','AUTOMATIC','90 qualifying active-service days in good standing.',60),
('TOUR_OF_DUTY','Tour of Duty Ribbon','AUTOMATIC','180 qualifying service days and 20 credited official operations.',70),
('MILITARY_SERVICE','Military Service Ribbon','VERIFICATION','Command verification of current or prior real-world military service.',80),
('UNIT_CITATION','Unit Citation Ribbon','RECOMMENDATION','Filed by Headquarters for qualifying collective performance.',90),
('COMBAT_ACTION','Combat Action Ribbon','RECOMMENDATION','Command-approved distinguished action during official combat operations.',100),
('MERITORIOUS_SERVICE','Meritorious Service Ribbon','RECOMMENDATION','Command-approved significant service beyond normal duties.',110)
ON CONFLICT(ribbon_code) DO UPDATE SET ribbon_name=EXCLUDED.ribbon_name,automation_mode=EXCLUDED.automation_mode,requirement_text=EXCLUDED.requirement_text,sort_order=EXCLUDED.sort_order,is_active=TRUE;

CREATE TABLE IF NOT EXISTS personnel_ribbons (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    ribbon_code TEXT NOT NULL REFERENCES ribbon_catalog(ribbon_code),
    earned_at DATE NOT NULL DEFAULT CURRENT_DATE,
    source_type TEXT NOT NULL DEFAULT 'AUTOMATIC',
    source_reference TEXT,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(personnel_id,ribbon_code)
);

ALTER TABLE personnel_ribbons ADD COLUMN IF NOT EXISTS is_worn BOOLEAN NOT NULL DEFAULT TRUE;
CREATE INDEX IF NOT EXISTS personnel_ribbons_person_idx ON personnel_ribbons(personnel_id,earned_at DESC);

CREATE TABLE IF NOT EXISTS battalion_event_instructors (
    event_id UUID NOT NULL REFERENCES battalion_events(id) ON DELETE CASCADE,
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    instructor_role TEXT NOT NULL DEFAULT 'INSTRUCTOR',
    filed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(event_id,personnel_id)
);

CREATE TABLE IF NOT EXISTS personnel_military_service_verification (
    personnel_id UUID PRIMARY KEY REFERENCES personnel(id) ON DELETE CASCADE,
    service_category TEXT NOT NULL DEFAULT 'MILITARY SERVICE',
    verified BOOLEAN NOT NULL DEFAULT FALSE,
    verified_by TEXT,
    verified_at TIMESTAMPTZ,
    remarks TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

UPDATE ribbon_catalog SET image_filename='military-service-ribbon.png' WHERE ribbon_code='MILITARY_SERVICE';

UPDATE ribbon_catalog SET image_filename='unit-citation-ribbon.png' WHERE ribbon_code='UNIT_CITATION';

UPDATE ribbon_catalog SET image_filename='meritorious-service-ribbon.png' WHERE ribbon_code='MERITORIOUS_SERVICE';

UPDATE ribbon_catalog SET image_filename='nco-leadership-ribbon.png' WHERE ribbon_code='NCO_LEADERSHIP';

UPDATE ribbon_catalog SET image_filename='instructor-ribbon.png' WHERE ribbon_code='INSTRUCTOR';


UPDATE ribbon_catalog SET image_filename='tour-of-duty-ribbon.png' WHERE ribbon_code='TOUR_OF_DUTY';

UPDATE ribbon_catalog SET image_filename='good-conduct-ribbon.png' WHERE ribbon_code='GOOD_CONDUCT';

UPDATE ribbon_catalog SET image_filename='recruiting-ribbon.png' WHERE ribbon_code='RECRUITING';

UPDATE ribbon_catalog SET image_filename='campaign-ribbon.png' WHERE ribbon_code='CAMPAIGN';

UPDATE ribbon_catalog SET image_filename='combat-infantry-ribbon.png' WHERE ribbon_code='COMBAT_INFANTRY';

UPDATE ribbon_catalog SET image_filename='combat-action-ribbon.png' WHERE ribbon_code='COMBAT_ACTION';

ALTER TABLE personnel_ribbons ALTER COLUMN is_worn SET DEFAULT TRUE;


-- Battalion inactivity / personnel accountability control
CREATE TABLE IF NOT EXISTS inactivity_contact_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    contact_type TEXT NOT NULL DEFAULT 'CONTACT',
    contact_method TEXT,
    notes TEXT,
    contacted_by TEXT,
    contacted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_inactivity_contact_personnel ON inactivity_contact_log(personnel_id,contacted_at DESC);
ALTER TABLE personnel ADD COLUMN IF NOT EXISTS inactivity_disposition TEXT;
ALTER TABLE personnel ADD COLUMN IF NOT EXISTS inactivity_disposition_at TIMESTAMPTZ;


-- ---------------------------------------------------------------------------
-- PERSONNEL / TRAINING EXPANSION — leadership time, MOS proficiency,
-- qualification currency, Replacement Depot movement, and action suspense.
-- ---------------------------------------------------------------------------
ALTER TABLE personnel_actions ADD COLUMN IF NOT EXISTS suspense_last_notified_at TIMESTAMPTZ;
ALTER TABLE personnel_actions ADD COLUMN IF NOT EXISTS overdue_last_notified_at TIMESTAMPTZ;

ALTER TABLE recruiting_cases ADD COLUMN IF NOT EXISTS replacement_depot_entered_at TIMESTAMPTZ;
ALTER TABLE recruiting_cases ADD COLUMN IF NOT EXISTS movement_order_number TEXT;
ALTER TABLE recruiting_cases ADD COLUMN IF NOT EXISTS movement_order_filed_at TIMESTAMPTZ;
ALTER TABLE recruiting_cases ADD COLUMN IF NOT EXISTS movement_unit_code TEXT;

CREATE INDEX IF NOT EXISTS personnel_actions_due_active_idx
ON personnel_actions(due_date,status,owning_section)
WHERE due_date IS NOT NULL AND status NOT IN ('COMPLETE','CLOSED','DENIED');

CREATE INDEX IF NOT EXISTS personnel_mos_proficiency_person_idx
ON personnel_mos_proficiency(personnel_id,is_current,effective_date DESC);

-- Recurring battalion certifications. S-3 may renew these before expiration.
INSERT INTO duty_qualification_types(code,display_name,battlefield_unit,sort_order) VALUES
('M16_RIFLE','M16 Rifle Qualification','WEAPONS',200),
('M60_MACHINE_GUN','M60 Machine Gun Qualification','WEAPONS',210),
('M79_GRENADE_LAUNCHER','M79 Grenade Launcher Qualification','WEAPONS',220),
('PRC25_RADIO','AN/PRC-25 Radio Operator','COMMUNICATIONS',230),
('COMBAT_MEDICAL','Combat Medical Qualification','MEDICAL',240),
('TEAM_LEADER_CERT','Team Leader Certification','LEADERSHIP',250),
('ASL_CERT','Assistant Squad Leader Certification','LEADERSHIP',260),
('SQUAD_LEADER_CERT','Squad Leader Certification','LEADERSHIP',270),
('PLATOON_SERGEANT_CERT','Platoon Sergeant Certification','LEADERSHIP',280),
('HELICOPTER_CREW_CERT','Helicopter Crew Qualification','SPECIALTY',290),
('MORTAR_CREW_CERT','Mortar Crew Qualification','SPECIALTY',300)
ON CONFLICT(code) DO UPDATE SET display_name=EXCLUDED.display_name,battlefield_unit=EXCLUDED.battlefield_unit,sort_order=EXCLUDED.sort_order,is_active=TRUE;


-- ---------------------------------------------------------------------------
-- MEMBER CAREER EXPERIENCE — qualification-card metadata
-- ---------------------------------------------------------------------------
ALTER TABLE qualifications ADD COLUMN IF NOT EXISTS score_text TEXT;
ALTER TABLE qualifications ADD COLUMN IF NOT EXISTS approving_instructor TEXT;
ALTER TABLE personnel_duty_qualifications ADD COLUMN IF NOT EXISTS score_text TEXT;


-- ---------------------------------------------------------------------------
-- MEMBER CAREER PROGRESSION II
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS unit_identity_settings(
  unit_node_id UUID PRIMARY KEY REFERENCES unit_nodes(id) ON DELETE CASCADE,
  nickname TEXT,
  call_sign TEXT,
  approved_by TEXT,
  approved_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS member_service_goals(
  id BIGSERIAL PRIMARY KEY,
  personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
  goal_code TEXT NOT NULL,
  goal_label TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  completed_at TIMESTAMPTZ,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  UNIQUE(personnel_id,goal_code)
);

-- ---------------------------------------------------------------------------
-- SHARED BATTALION CLERK INACTIVITY THRESHOLDS
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clerk_guild_settings (
    guild_id TEXT PRIMARY KEY,
    orders_channel_id TEXT,
    operation_duty_channel_id TEXT,
    welcome_channel_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE clerk_guild_settings ADD COLUMN IF NOT EXISTS inactivity_warning_days INTEGER DEFAULT 7;
ALTER TABLE clerk_guild_settings ADD COLUMN IF NOT EXISTS inactivity_s1_days INTEGER DEFAULT 14;
ALTER TABLE clerk_guild_settings ADD COLUMN IF NOT EXISTS inactivity_property_days INTEGER DEFAULT 21;
ALTER TABLE clerk_guild_settings ADD COLUMN IF NOT EXISTS inactivity_command_days INTEGER DEFAULT 30;
ALTER TABLE clerk_guild_settings ADD COLUMN IF NOT EXISTS operation_rounds_default INTEGER DEFAULT 180;

-- Member-record / operation-credit performance indexes
CREATE INDEX IF NOT EXISTS weapon_round_events_operation_person_idx
ON weapon_round_events(operation_id,personnel_id,weapon_id);
CREATE INDEX IF NOT EXISTS battalion_event_attendance_event_person_idx
ON battalion_event_attendance(event_id,personnel_id);
CREATE INDEX IF NOT EXISTS assignment_history_person_effective_idx
ON assignment_history(personnel_id,effective_date DESC);
CREATE INDEX IF NOT EXISTS personnel_service_history_person_entry_idx
ON personnel_service_history(personnel_id,entry_date DESC);
CREATE INDEX IF NOT EXISTS personnel_documents_person_effective_idx
ON personnel_documents(personnel_id,effective_date DESC);



-- Remove the original development/demo OP-001. The exact code/title pair was shipped as sample data,
-- never as a real battalion operation. Related demo rows cascade from the operation FK where applicable.
DELETE FROM operations
WHERE operation_code='OP-001'
  AND title='FIELD EXERCISE — INITIAL READINESS';

-- ---------------------------------------------------------------------------
-- S-3 OPERATIONS CENTER — WEBSITE-FIRST OPERATION CONTROL
-- ---------------------------------------------------------------------------
ALTER TABLE operations ADD COLUMN IF NOT EXISTS duration_minutes INTEGER NOT NULL DEFAULT 90;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS credit_threshold_minutes INTEGER NOT NULL DEFAULT 45;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS rounds_per_soldier INTEGER NOT NULL DEFAULT 180;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS credit_channel_id BIGINT;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS credit_channel_name TEXT;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS reminder_minutes TEXT NOT NULL DEFAULT '1440,120,30';
ALTER TABLE operations ADD COLUMN IF NOT EXISTS formation_scope TEXT NOT NULL DEFAULT 'BATTALION';
ALTER TABLE operations ADD COLUMN IF NOT EXISTS formation_unit_node_id UUID REFERENCES unit_nodes(id) ON DELETE SET NULL;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS clerk_event_id UUID;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS publish_status TEXT NOT NULL DEFAULT 'DRAFT';
ALTER TABLE operations ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;

ALTER TABLE battalion_events ADD COLUMN IF NOT EXISTS credit_threshold_minutes INTEGER NOT NULL DEFAULT 45;
ALTER TABLE battalion_events ADD COLUMN IF NOT EXISTS reminder_minutes TEXT NOT NULL DEFAULT '1440,120,30';

CREATE TABLE IF NOT EXISTS clerk_runtime_health (
    guild_id BIGINT PRIMARY KEY,
    bot_user TEXT,
    status TEXT NOT NULL DEFAULT 'ONLINE',
    voice_collector_running BOOLEAN NOT NULL DEFAULT FALSE,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    details_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS discord_channel_directory (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    channel_name TEXT NOT NULL,
    channel_type TEXT NOT NULL DEFAULT 'VOICE',
    category_name TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(guild_id,channel_id)
);
CREATE INDEX IF NOT EXISTS discord_channel_directory_voice_idx ON discord_channel_directory(channel_type,active,channel_name);

-- ---------------------------------------------------------------------------
-- PERSISTENT BATTALION STATE / SOLDIER EXPERIENCE EXPANSION — 2026-08-19
-- Additive only. Existing personnel, operations, weapons, awards, and documents
-- remain authoritative; these tables provide event/audit, narrative, and workflow layers.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS battalion_state_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type TEXT NOT NULL,
    personnel_id UUID REFERENCES personnel(id) ON DELETE CASCADE,
    operation_id UUID REFERENCES operations(id) ON DELETE SET NULL,
    weapon_id UUID REFERENCES weapon_inventory(id) ON DELETE SET NULL,
    unit_node_id UUID REFERENCES unit_nodes(id) ON DELETE SET NULL,
    effective_date DATE NOT NULL DEFAULT CURRENT_DATE,
    title TEXT NOT NULL,
    narrative TEXT,
    reference_number TEXT,
    source_key TEXT UNIQUE,
    details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS battalion_state_events_person_idx ON battalion_state_events(personnel_id,effective_date DESC,created_at DESC);
CREATE INDEX IF NOT EXISTS battalion_state_events_type_idx ON battalion_state_events(event_type,effective_date DESC);
CREATE INDEX IF NOT EXISTS battalion_state_events_operation_idx ON battalion_state_events(operation_id,created_at DESC);

CREATE TABLE IF NOT EXISTS personnel_field_citations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    operation_id UUID REFERENCES operations(id) ON DELETE SET NULL,
    citation_date DATE NOT NULL DEFAULT CURRENT_DATE,
    citation_type TEXT NOT NULL DEFAULT 'FIELD CITATION',
    citation_text TEXT NOT NULL,
    cited_by TEXT,
    used_for_award_id UUID REFERENCES personnel_awards(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS personnel_field_citations_person_idx ON personnel_field_citations(personnel_id,citation_date DESC);

CREATE TABLE IF NOT EXISTS command_watchlist (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    watch_type TEXT NOT NULL,
    note TEXT,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    resolved_by TEXT,
    UNIQUE(personnel_id,watch_type)
);
CREATE INDEX IF NOT EXISTS command_watchlist_open_idx ON command_watchlist(resolved_at,watch_type,created_at DESC);

CREATE TABLE IF NOT EXISTS soldier_journal_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    operation_id UUID REFERENCES operations(id) ON DELETE SET NULL,
    entry_date DATE NOT NULL DEFAULT CURRENT_DATE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'PRIVATE',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS soldier_journal_entries_person_idx ON soldier_journal_entries(personnel_id,entry_date DESC,created_at DESC);

CREATE TABLE IF NOT EXISTS discord_role_sync_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    personnel_id UUID NOT NULL REFERENCES personnel(id) ON DELETE CASCADE,
    guild_id BIGINT,
    discord_user_id BIGINT,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ,
    error_text TEXT
);
CREATE INDEX IF NOT EXISTS discord_role_sync_queue_pending_idx ON discord_role_sync_queue(status,requested_at);

CREATE TABLE IF NOT EXISTS operation_role_requirements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operation_id UUID NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    duty_role TEXT NOT NULL,
    required_count INTEGER NOT NULL DEFAULT 1,
    preferred_mos_code TEXT,
    minimum_rank_code TEXT,
    qualification_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(operation_id,duty_role)
);

ALTER TABLE morning_report_snapshots ADD COLUMN IF NOT EXISTS readiness_percent INTEGER NOT NULL DEFAULT 0;
ALTER TABLE morning_report_snapshots ADD COLUMN IF NOT EXISTS weapon_readiness_percent INTEGER NOT NULL DEFAULT 0;
ALTER TABLE morning_report_snapshots ADD COLUMN IF NOT EXISTS training_current_percent INTEGER NOT NULL DEFAULT 0;
ALTER TABLE morning_report_snapshots ADD COLUMN IF NOT EXISTS operation_attendance_percent INTEGER NOT NULL DEFAULT 0;
ALTER TABLE morning_report_snapshots ADD COLUMN IF NOT EXISTS command_attention_count INTEGER NOT NULL DEFAULT 0;

-- One open watch type per Soldier. Resolved records can be reactivated in place.
-- One pending role-sync per Soldier is enough; repeated changes update the queue time/reason.
CREATE UNIQUE INDEX IF NOT EXISTS discord_role_sync_one_pending_per_person
ON discord_role_sync_queue(personnel_id) WHERE status='PENDING';

-- Seed the new state/event layer from existing permanent records without changing them.
INSERT INTO battalion_state_events(event_type,personnel_id,effective_date,title,narrative,reference_number,source_key,details_json)
SELECT CASE UPPER(COALESCE(entry_type,''))
         WHEN 'ARRIVAL' THEN 'PERSONNEL_REPORTED'
         WHEN 'ASSIGNMENT' THEN 'PERSONNEL_ASSIGNED'
         WHEN 'RANK' THEN 'PROMOTED'
         WHEN 'APPOINTMENT' THEN 'APPOINTMENT_STARTED'
         WHEN 'AWARD' THEN 'AWARD_GRANTED'
         WHEN 'TRAINING' THEN 'QUALIFICATION_COMPLETED'
         WHEN 'OPERATIONS' THEN 'OPERATION_CREDITED'
         WHEN 'CASUALTY' THEN 'WIA_RECORDED'
         WHEN 'MOS' THEN 'MOS_CHANGED'
         ELSE 'SERVICE_RECORD_ENTRY' END,
       personnel_id,entry_date,title,narrative,reference_number,
       'SERVICEHIST:'||id::text,
       jsonb_build_object('service_history_id',id::text,'entry_type',entry_type,'authority',authority)
FROM personnel_service_history
ON CONFLICT(source_key) DO NOTHING;

INSERT INTO battalion_state_events(event_type,personnel_id,operation_id,weapon_id,effective_date,title,narrative,source_key,details_json)
SELECT 'WEAPON_ROUNDS_FIRED',wre.personnel_id,wre.operation_id,wre.weapon_id,wre.recorded_at::date,
       'M16 AMMUNITION EXPENDITURE',
       CONCAT(wre.rounds_fired,' rounds recorded',CASE WHEN o.operation_number IS NOT NULL THEN ' for '||o.operation_number ELSE '' END,'.'),
       'WEAPONROUND:'||wre.id::text,
       jsonb_build_object('rounds',wre.rounds_fired,'source_type',wre.source_type,'recorded_by',wre.recorded_by)
FROM weapon_round_events wre
LEFT JOIN operations o ON o.id=wre.operation_id
WHERE wre.personnel_id IS NOT NULL
ON CONFLICT(source_key) DO NOTHING;

ALTER TABLE morning_report_snapshots ADD COLUMN IF NOT EXISTS report_number TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS morning_report_snapshots_report_no_idx ON morning_report_snapshots(report_number) WHERE report_number IS NOT NULL;
ALTER TABLE qualifications ADD COLUMN IF NOT EXISTS qualification_number TEXT;
ALTER TABLE personnel_duty_qualifications ADD COLUMN IF NOT EXISTS qualification_number TEXT;

-- Recruiting OAuth auto-join + deterministic credential delivery (2026-08-21)
ALTER TABLE recruiting_cases ADD COLUMN IF NOT EXISTS discord_oauth_access_token_enc TEXT;
ALTER TABLE recruiting_cases ADD COLUMN IF NOT EXISTS discord_oauth_refresh_token_enc TEXT;
ALTER TABLE recruiting_cases ADD COLUMN IF NOT EXISTS discord_oauth_expires_at TIMESTAMPTZ;
ALTER TABLE recruiting_cases ADD COLUMN IF NOT EXISTS discord_oauth_scope TEXT;
ALTER TABLE recruiting_cases ADD COLUMN IF NOT EXISTS discord_joined_at TIMESTAMPTZ;
ALTER TABLE recruiting_cases ADD COLUMN IF NOT EXISTS discord_join_error TEXT;
ALTER TABLE recruiting_cases ADD COLUMN IF NOT EXISTS discord_join_last_attempt_at TIMESTAMPTZ;
ALTER TABLE recruiting_cases ADD COLUMN IF NOT EXISTS credentials_sent_at TIMESTAMPTZ;
ALTER TABLE recruiting_cases ADD COLUMN IF NOT EXISTS credentials_delivery_error TEXT;
ALTER TABLE recruiting_cases ADD COLUMN IF NOT EXISTS credentials_last_attempt_at TIMESTAMPTZ;
ALTER TABLE recruiting_cases ADD COLUMN IF NOT EXISTS credentials_pending_field_code_enc TEXT;
