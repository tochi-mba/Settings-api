# Architecture decision records

One file per decision that future-us would otherwise re-litigate. Each says what was
decided, what it cost, and what would make us change our minds. The format, and the habit,
are keyring's.

A record describes the moment it was written. Its counts -- forty-two settings, seven
namespaces, six consuming services -- are that moment's, and a record whose premise later
moved says so in a note or an amendment rather than being rewritten.
[catalogue.md](../catalogue.md) is the catalogue as it is now, and the family's own
decisions, such as [ADR-0011](https://github.com/tochi-mba/LUCY-assistant/blob/main/docs/adr/0011-private-services-are-extensions.md) on private services, live in the
[hub](https://github.com/tochi-mba/LUCY-assistant/tree/main/docs/adr).

| ADR | Decision |
| --- | --- |
| [0001](0001-settings-are-their-own-service.md) | Settings are their own service |
| [0002](0002-settings-are-per-account-not-per-profile.md) | Settings scopes are exclusive: account or profile, declared per entry |
| [0003](0003-the-catalogue-is-code-not-data-in-the-database.md) | The catalogue is code, not data in the database |
| [0004](0004-services-may-write-within-their-own-namespace.md) | Services may write within their own namespace |
| [0005](0005-sparse-storage-so-defaults-can-move.md) | Sparse storage, so defaults can move |
| [0006](0006-on-unavailable-is-declared-per-setting.md) | `on_unavailable` is declared per setting, and has no default |
| [0007](0007-namespaces-are-scopes.md) | Namespaces are scopes |
| [0008](0008-no-administrative-surface.md) | No administrative surface |
