CREATE INDEX idx_settings_by_setting ON settings (namespace, key);
CREATE INDEX idx_settings_events_account ON settings_events (account_id, sequence DESC);
CREATE TABLE accounts (
    account_id TEXT    NOT NULL PRIMARY KEY,
    -- Monotonic per account, bumped in the same transaction as every write that changes
    -- something. It is the ETag, the cache key and the optimistic-concurrency token all
    -- at once: `"<account_id>.<revision>"`. Starts at 0, so a first write reaches 1 and
    -- "revision 0" means "this account has never changed anything".
    revision   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL,

    CHECK (revision >= 0)
) STRICT;
CREATE TABLE schema_version (
    version    INTEGER NOT NULL PRIMARY KEY,
    applied_at TEXT    NOT NULL
) STRICT
;
CREATE TABLE settings (
    account_id TEXT NOT NULL REFERENCES accounts (account_id) ON DELETE CASCADE,
    namespace  TEXT NOT NULL,
    key        TEXT NOT NULL,
    -- The value, as JSON text. NOT NULL even for a nullable setting, because a nullable
    -- setting stores the four bytes `null` -- which is a value somebody chose, and is a
    -- different thing from having no row at all.
    value_json TEXT NOT NULL,
    set_at     TEXT NOT NULL,
    -- The verified audience of the token that set it, or `service:<name>` when a service
    -- wrote it while holding this person's token. Provenance the server derived rather
    -- than provenance the writer claimed.
    set_by     TEXT NOT NULL,

    PRIMARY KEY (account_id, namespace, key)
) STRICT, WITHOUT ROWID;
CREATE TABLE settings_events (
    sequence   INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    account_id TEXT    NOT NULL,
    at         TEXT    NOT NULL,
    action     TEXT    NOT NULL,
    -- Present when the event is about one namespace or one setting; absent for the
    -- account-wide ones.
    namespace  TEXT,
    key        TEXT,
    -- The account's revision as of this event. A write that changes nothing bumps
    -- nothing and logs nothing, so consecutive events normally carry increasing
    -- revisions. The one exception is the retired-key sweep, which records that rows were
    -- destroyed without bumping: those rows were already ignored on every read, so no
    -- resolved value changed and invalidating every cached copy would be a lie.
    revision   INTEGER NOT NULL,
    actor      TEXT    NOT NULL,
    service    TEXT,
    -- JSON, and NEVER a setting value. It holds which keys were touched and how many --
    -- metadata about the change, not the change. `forget_settings` deletes these rows
    -- outright rather than blanking this column, because "delete everything you know
    -- about me" has one honest meaning.
    detail     TEXT
) STRICT;
CREATE TABLE sqlite_sequence(name,seq);
