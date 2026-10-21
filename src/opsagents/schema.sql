-- Three schemas, three owners:
--   pas  stands in for the policy administration system (the agents read and post through integrations/pas.py)
--   ops  is the agents' own state: orders, the hash-chained event log, exceptions, documents, cursors
--   sim  belongs to the vendor and mailbox simulator, which the agents only reach over HTTP

CREATE SCHEMA IF NOT EXISTS pas;
CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS sim;

-- pas ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS pas.class_codes (
    code        text PRIMARY KEY,
    description text NOT NULL,
    hazard      text NOT NULL CHECK (hazard IN ('standard', 'high'))
);

-- Rate per $100 of payroll, by line of business.
CREATE TABLE IF NOT EXISTS pas.class_rates (
    line       text NOT NULL CHECK (line IN ('WC', 'GL')),
    class_code text NOT NULL REFERENCES pas.class_codes (code),
    rate       numeric(8, 4) NOT NULL,
    PRIMARY KEY (line, class_code)
);

CREATE TABLE IF NOT EXISTS pas.policies (
    policy_number        text PRIMARY KEY,
    line                 text NOT NULL CHECK (line IN ('WC', 'GL')),
    insured_name         text NOT NULL,
    contact_name         text NOT NULL,
    insured_email        text NOT NULL,
    insured_phone        text NOT NULL,
    city                 text NOT NULL,
    effective_date       date NOT NULL,
    expiration_date      date NOT NULL,
    status               text NOT NULL CHECK (status IN ('in_force', 'cancelled')),
    cancellation_date    date,
    new_business         boolean NOT NULL,
    last_inspection_date date,
    deposit_premium      numeric(12, 2) NOT NULL
);

CREATE TABLE IF NOT EXISTS pas.policy_exposures (
    policy_number     text NOT NULL REFERENCES pas.policies,
    class_code        text NOT NULL REFERENCES pas.class_codes (code),
    estimated_payroll numeric(14, 2) NOT NULL,
    rate              numeric(8, 4) NOT NULL,
    PRIMARY KEY (policy_number, class_code)
);

-- Audits completed by hand before the agents went live.
CREATE TABLE IF NOT EXISTS pas.audit_history (
    policy_number text NOT NULL REFERENCES pas.policies,
    period_end    date NOT NULL,
    completed_on  date NOT NULL,
    PRIMARY KEY (policy_number, period_end)
);

CREATE TABLE IF NOT EXISTS pas.activity (
    id            bigserial PRIMARY KEY,
    policy_number text NOT NULL REFERENCES pas.policies,
    logged_on     date NOT NULL,
    author        text NOT NULL,
    note          text NOT NULL
);

CREATE TABLE IF NOT EXISTS pas.inspections (
    order_id             bigint PRIMARY KEY,
    policy_number        text NOT NULL REFERENCES pas.policies,
    survey_date          date NOT NULL,
    risk_rating          text NOT NULL,
    recommendation_count int NOT NULL,
    critical_count       int NOT NULL,
    posted_on            date NOT NULL
);

CREATE TABLE IF NOT EXISTS pas.audit_endorsements (
    order_id        bigint PRIMARY KEY,
    policy_number   text NOT NULL REFERENCES pas.policies,
    period_start    date NOT NULL,
    period_end      date NOT NULL,
    basis           text NOT NULL CHECK (basis IN ('audited', 'estimated')),
    audited_premium numeric(12, 2) NOT NULL,
    deposit_premium numeric(12, 2) NOT NULL,
    adjustment      numeric(12, 2) NOT NULL,
    posted_on       date NOT NULL,
    posted_by       text NOT NULL
);

-- ops ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ops.clock (
    id    int PRIMARY KEY CHECK (id = 1),
    today date NOT NULL
);

CREATE TABLE IF NOT EXISTS ops.orders (
    id              bigserial PRIMARY KEY,
    order_key       text NOT NULL UNIQUE,
    kind            text NOT NULL CHECK (kind IN ('inspection', 'audit')),
    policy_number   text NOT NULL,
    order_type      text NOT NULL,
    vendor          text NOT NULL,
    trigger         text NOT NULL,
    rule_version    text NOT NULL,
    rule_hits       jsonb NOT NULL,
    period_start    date NOT NULL,
    period_end      date NOT NULL,
    state           text NOT NULL,
    identified_on   date NOT NULL,
    order_on        date NOT NULL,
    due_on          date NOT NULL,
    next_action_on  date NOT NULL,
    vendor_ref      text,
    ordered_on      date,
    completed_on    date,
    closed_on       date,
    submit_failures int NOT NULL DEFAULT 0,
    reminders_sent  int NOT NULL DEFAULT 0,
    chased_on       date,
    insured_contacted_on date,
    extraction      jsonb,
    extraction_approved boolean NOT NULL DEFAULT false,
    reconciliation  jsonb,
    human_touched   boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS orders_due ON ops.orders (next_action_on) WHERE state NOT IN ('closed', 'closed_manual');
CREATE INDEX IF NOT EXISTS orders_vendor_ref ON ops.orders (vendor, vendor_ref);

-- Append-only, hash-chained per order. The trigger below rejects UPDATE and DELETE.
CREATE TABLE IF NOT EXISTS ops.events (
    id          bigserial PRIMARY KEY,
    order_id    bigint NOT NULL REFERENCES ops.orders,
    seq         int NOT NULL,
    occurred_on date NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    actor       text NOT NULL,
    event_type  text NOT NULL,
    from_state  text,
    to_state    text,
    payload     jsonb NOT NULL,
    prev_hash   text NOT NULL,
    hash        text NOT NULL,
    UNIQUE (order_id, seq)
);

CREATE OR REPLACE FUNCTION ops.reject_event_change() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ops.events is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS events_append_only ON ops.events;
CREATE TRIGGER events_append_only BEFORE UPDATE OR DELETE ON ops.events
    FOR EACH ROW EXECUTE FUNCTION ops.reject_event_change();

CREATE TABLE IF NOT EXISTS ops.exceptions (
    id            bigserial PRIMARY KEY,
    order_id      bigint NOT NULL REFERENCES ops.orders,
    opened_on     date NOT NULL,
    reason        text NOT NULL,
    required_role text NOT NULL,
    detail        text NOT NULL,
    evidence      jsonb NOT NULL,
    resume_state  text NOT NULL,
    status        text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    resolution    text CHECK (resolution IN ('approve', 'reject')),
    resolved_by   text,
    resolved_on   date,
    note          text
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_exception ON ops.exceptions (order_id) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS ops.documents (
    id          bigserial PRIMARY KEY,
    order_id    bigint NOT NULL REFERENCES ops.orders,
    source      text NOT NULL,
    sha256      text NOT NULL,
    pages       int NOT NULL,
    ocr_pages   int NOT NULL,
    text        text NOT NULL,
    received_on date NOT NULL,
    UNIQUE (order_id, sha256)
);

CREATE TABLE IF NOT EXISTS ops.inbound_mail (
    message_id  bigint PRIMARY KEY,
    order_id    bigint REFERENCES ops.orders,
    received_on date NOT NULL,
    sender      text NOT NULL,
    subject     text NOT NULL,
    intent      text NOT NULL,
    quote       text NOT NULL
);

CREATE TABLE IF NOT EXISTS ops.cursors (
    name     text PRIMARY KEY,
    position bigint NOT NULL
);

CREATE TABLE IF NOT EXISTS ops.llm_calls (
    id         bigserial PRIMARY KEY,
    order_id   bigint,
    task       text NOT NULL,
    provider   text NOT NULL,
    ok         boolean NOT NULL,
    latency_ms int NOT NULL,
    called_on  date NOT NULL
);

-- sim ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sim.clock (
    id    int PRIMARY KEY CHECK (id = 1),
    today date NOT NULL
);

CREATE TABLE IF NOT EXISTS sim.vendor_orders (
    vendor_ref      text PRIMARY KEY,
    vendor          text NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    payload         jsonb NOT NULL,
    outcome         jsonb NOT NULL,
    received_on     date NOT NULL
);

CREATE TABLE IF NOT EXISTS sim.submit_attempts (
    idempotency_key text PRIMARY KEY,
    attempts        int NOT NULL
);

-- Future events the simulator has planned; advancing the clock releases the due ones.
CREATE TABLE IF NOT EXISTS sim.plans (
    id       bigserial PRIMARY KEY,
    due_on   date NOT NULL,
    channel  text NOT NULL CHECK (channel IN ('feed', 'mail')),
    body     jsonb NOT NULL,
    released boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS plans_due ON sim.plans (due_on) WHERE NOT released;

CREATE TABLE IF NOT EXISTS sim.feed (
    seq         bigserial PRIMARY KEY,
    vendor      text NOT NULL,
    vendor_ref  text NOT NULL,
    status      text NOT NULL,
    occurred_on date NOT NULL,
    report_id   text
);

CREATE TABLE IF NOT EXISTS sim.mailbox (
    id          bigserial PRIMARY KEY,
    folder      text NOT NULL CHECK (folder IN ('inbox', 'sent')),
    sent_on     date NOT NULL,
    sender      text NOT NULL,
    recipient   text NOT NULL,
    subject     text NOT NULL,
    body        text NOT NULL,
    attachment  text
);

CREATE TABLE IF NOT EXISTS sim.reports (
    report_id text PRIMARY KEY,
    spec      jsonb NOT NULL
);

-- Changes the policy system will make on a future day, such as a mid-term cancellation.
CREATE TABLE IF NOT EXISTS sim.pas_changes (
    policy_number text NOT NULL,
    change_on     date NOT NULL,
    kind          text NOT NULL CHECK (kind IN ('cancel')),
    applied       boolean NOT NULL DEFAULT false,
    PRIMARY KEY (policy_number, kind)
);
