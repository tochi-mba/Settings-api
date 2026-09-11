-- The whole schema, as it stands at the first release. Three tables.
--
-- Two shapes here are the service rather than details of it, and both have an ADR:
--
--   * There is NO PROFILE COLUMN (ADR-0002). One settings set per account, regardless of
--     how many keyring profiles that account has. The primary key below is
--     (account_id, namespace, key), so a per-profile value is not "discouraged" -- it is
--     unrepresentable. A setting whose value names a profile, `common.default_profile`,
--     is still one account-level value, which is the point: the person answers "which
--     profile do you mean when I don't say" once, and every service gets the same answer.
--
--   * STORAGE IS SPARSE (ADR-0005). A row exists only where somebody expressed a
--     preference. "Unset" is therefore distinguishable from "set to a value that happens
--     to equal the default", so changing a catalogue default moves everyone who never
--     chose and nobody who did. user-api shipped the dense version of this and had to fix
--     it: its row-creating write filled in the domain's default grace window instead of
--     the deployment's, and a seven-day deployment silently became a thirty-day one.
--
-- `value_json` is JSON even for a boolean, so one column serves every type and adding a
-- sixth type is not a migration.

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

-- Created in the write path, not behind a separate create endpoint. This is a defect
-- user-api actually shipped and had to fix: the service never created the row before the
-- first write, so every first write failed a foreign key -- and an assistant will never
-- call an endpoint it was not told about.

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

-- The sweeper's index, and the only read that is not account-scoped: a retired key is
-- swept across every account at once, so it needs (namespace, key) rather than the
-- primary key's account-first ordering.
CREATE INDEX idx_settings_by_setting ON settings (namespace, key);

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

-- Deliberately NO foreign key to `accounts`. An event has to be able to outlive the thing
-- it describes -- `reset_setting` removes the row and the event saying it was reset is the
-- whole record that it happened. Erasure is the one operation that takes the events too,
-- and it does so explicitly.
--
-- Ordered and paged by `sequence` rather than by `at`, because the clock is injectable and
-- two events written in one tick carry the same stamp to the microsecond. Ordered by `at`
-- their relative order is whatever SQLite happened to choose, and a page boundary falling
-- between them drops one from the results or hands it back twice -- intermittently, and
-- only under the fixed clock the tests use, which is the worst place to find out.
CREATE INDEX idx_settings_events_account ON settings_events (account_id, sequence DESC);
