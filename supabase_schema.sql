-- ==============================================================================
-- CONFLUENCE TICKETING & ATTENDANCE PLATFORM
-- Supabase PostgreSQL Schema Script
-- ==============================================================================
-- Instructions:
-- 1. Open your Supabase Project Dashboard: https://supabase.com/dashboard
-- 2. Go to "SQL Editor" -> "New query"
-- 3. Paste this entire script and click "Run"
-- ==============================================================================

-- 1. Alembic version tracking
CREATE TABLE IF NOT EXISTS public.alembic_version (
    version_num VARCHAR(32) NOT NULL PRIMARY KEY
);

-- 2. Events table
CREATE TABLE IF NOT EXISTS public.events (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    slug VARCHAR(120) NOT NULL UNIQUE,
    timezone VARCHAR(64) DEFAULT 'Asia/Kolkata' NOT NULL,
    status VARCHAR(20) DEFAULT 'active' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL
);

-- 3. Staff users table
CREATE TABLE IF NOT EXISTS public.staff_users (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    email VARCHAR(320) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(32) DEFAULT 'CHECKIN_STAFF' NOT NULL,
    active BOOLEAN DEFAULT true NOT NULL,
    session_version INTEGER DEFAULT 1 NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL,
    CONSTRAINT ck_staff_role CHECK (role IN ('OWNER', 'ADMIN', 'CHECKIN_STAFF', 'VIEWER'))
);

-- 4. Attendees table
CREATE TABLE IF NOT EXISTS public.attendees (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    event_id VARCHAR(36) NOT NULL REFERENCES public.events(id) ON DELETE CASCADE,
    full_name VARCHAR(200) NOT NULL,
    year VARCHAR(80),
    email VARCHAR(320),
    normalized_email VARCHAR(320),
    phone VARCHAR(40),
    import_fingerprint VARCHAR(64) NOT NULL,
    source_row INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL,
    CONSTRAINT uq_attendee_event_email UNIQUE (event_id, normalized_email),
    CONSTRAINT uq_attendee_event_fingerprint UNIQUE (event_id, import_fingerprint)
);
CREATE INDEX IF NOT EXISTS ix_attendees_event_id ON public.attendees USING btree (event_id);

-- 5. Tickets table
CREATE TABLE IF NOT EXISTS public.tickets (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    event_id VARCHAR(36) NOT NULL REFERENCES public.events(id) ON DELETE CASCADE,
    attendee_id VARCHAR(36) NOT NULL REFERENCES public.attendees(id) ON DELETE CASCADE,
    ticket_number VARCHAR(40) NOT NULL UNIQUE,
    status VARCHAR(20) DEFAULT 'ISSUED' NOT NULL,
    qr_token_hash VARCHAR(64) NOT NULL UNIQUE,
    qr_token_encrypted TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL,
    revoked_at TIMESTAMP WITH TIME ZONE,
    revoked_by VARCHAR(36) REFERENCES public.staff_users(id),
    CONSTRAINT ck_ticket_status CHECK (status IN ('ISSUED', 'REVOKED')),
    CONSTRAINT uq_ticket_one_per_attendee_event UNIQUE (event_id, attendee_id)
);
CREATE INDEX IF NOT EXISTS ix_tickets_event_id ON public.tickets USING btree (event_id);

-- 6. Check-ins table (attendance source of truth)
CREATE TABLE IF NOT EXISTS public.check_ins (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    event_id VARCHAR(36) NOT NULL REFERENCES public.events(id) ON DELETE CASCADE,
    ticket_id VARCHAR(36) NOT NULL UNIQUE REFERENCES public.tickets(id) ON DELETE CASCADE,
    status VARCHAR(20) DEFAULT 'CHECKED_IN' NOT NULL,
    checked_in_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL,
    scanner_user_id VARCHAR(36) NOT NULL REFERENCES public.staff_users(id),
    reversed_at TIMESTAMP WITH TIME ZONE,
    reversed_by VARCHAR(36) REFERENCES public.staff_users(id),
    reversal_reason VARCHAR(300),
    CONSTRAINT ck_checkin_status CHECK (status IN ('CHECKED_IN', 'REVERSED'))
);
CREATE INDEX IF NOT EXISTS ix_check_ins_event_id ON public.check_ins USING btree (event_id);

-- 7. Scan attempts table (request idempotency & auditing)
CREATE TABLE IF NOT EXISTS public.scan_attempts (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    event_id VARCHAR(36) NOT NULL REFERENCES public.events(id) ON DELETE CASCADE,
    scanner_user_id VARCHAR(36) NOT NULL REFERENCES public.staff_users(id),
    request_id VARCHAR(36) NOT NULL,
    request_fingerprint VARCHAR(64) NOT NULL,
    ticket_id VARCHAR(36) REFERENCES public.tickets(id) ON DELETE SET NULL,
    result_code VARCHAR(40) NOT NULL,
    response_json JSON NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL,
    CONSTRAINT uq_scan_idempotency_per_user UNIQUE (scanner_user_id, request_id)
);
CREATE INDEX IF NOT EXISTS ix_scan_attempts_event_id ON public.scan_attempts USING btree (event_id);

-- 8. Audit logs table
CREATE TABLE IF NOT EXISTS public.audit_logs (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    event_id VARCHAR(36) NOT NULL REFERENCES public.events(id) ON DELETE CASCADE,
    actor_user_id VARCHAR(36) REFERENCES public.staff_users(id) ON DELETE SET NULL,
    action VARCHAR(80) NOT NULL,
    entity_type VARCHAR(60) NOT NULL,
    entity_id VARCHAR(36),
    details JSON NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_logs_event_id ON public.audit_logs USING btree (event_id);

-- 9. Import previews table
CREATE TABLE IF NOT EXISTS public.import_previews (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    event_id VARCHAR(36) NOT NULL REFERENCES public.events(id) ON DELETE CASCADE,
    created_by VARCHAR(36) NOT NULL REFERENCES public.staff_users(id),
    filename VARCHAR(255) NOT NULL,
    payload JSON NOT NULL,
    summary JSON NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    committed_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_import_previews_event_id ON public.import_previews USING btree (event_id);

-- 10. Ticket templates table
CREATE TABLE IF NOT EXISTS public.ticket_templates (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    event_id VARCHAR(36) NOT NULL UNIQUE REFERENCES public.events(id) ON DELETE CASCADE,
    image_path VARCHAR(500) NOT NULL,
    image_width INTEGER NOT NULL,
    image_height INTEGER NOT NULL,
    placements JSON NOT NULL,
    updated_by VARCHAR(36) NOT NULL REFERENCES public.staff_users(id),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL
);

-- Seed initial Alembic revision
INSERT INTO public.alembic_version (version_num) 
VALUES ('0001_initial') 
ON CONFLICT (version_num) DO NOTHING;

-- Seed default Confluence event
INSERT INTO public.events (id, name, slug, timezone, status)
VALUES ('55e083b2-ccc1-40af-9c4b-d6be0cafd922', 'Confluence', 'confluence', 'Asia/Kolkata', 'active')
ON CONFLICT (slug) DO NOTHING;
