-- Exclusive per-setting scope (amended ADR-0002). Account-scoped rows live under the
-- sentinel profile `*`; profile-scoped rows live under a keyring profile name. Existing
-- rows were all account-level, so they move to `*`.
--
-- SQLite cannot add a column to a WITHOUT ROWID primary key in place, so the table is
-- rebuilt. The sentinel cannot collide with a keyring profile: keyring's name pattern
-- refuses `*`.

DROP INDEX IF EXISTS idx_settings_by_setting;

CREATE TABLE settings_next (
    account_id TEXT NOT NULL REFERENCES accounts (account_id) ON DELETE CASCADE,
    profile    TEXT NOT NULL,
    namespace  TEXT NOT NULL,
    key        TEXT NOT NULL,
    value_json TEXT NOT NULL,
    set_at     TEXT NOT NULL,
    set_by     TEXT NOT NULL,

    PRIMARY KEY (account_id, profile, namespace, key),
    CHECK (length(profile) BETWEEN 1 AND 64)
) STRICT, WITHOUT ROWID;

INSERT INTO settings_next (
    account_id, profile, namespace, key, value_json, set_at, set_by
)
SELECT account_id, '*', namespace, key, value_json, set_at, set_by
FROM settings;

DROP TABLE settings;

ALTER TABLE settings_next RENAME TO settings;

CREATE INDEX idx_settings_by_setting ON settings (namespace, key);
