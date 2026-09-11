# Exposing settings-api as assistant tools

This service is designed to be fronted by an MCP server, so an assistant can read and
change a person's settings as tools. Nothing here is required to run the service; it is
the contract a bridge depends on, and the rules for writing one.

## The operation ids are the tool names

There are sixteen, they are snake_case `verb_noun`, and they are **stable for ever**.
Renaming one breaks every client with a tool bound to it, which is why a contract test pins
the exact set.

| Tool | What it does |
| --- | --- |
| `describe_settings` | Every setting, its bounds, its current value, and whether it is pinned. |
| `get_settings` | Every namespace this token grants, resolved. |
| `get_namespace` | One namespace. |
| `get_setting` | One value, plus where it came from. |
| `update_settings` | Apply a whole document, all of it or none of it. |
| `set_setting` | Set one. |
| `reset_setting` | Put one back to its default. |
| `reset_namespace` | Put a whole namespace back. |
| `forget_settings` | Destroy everything. |
| `export_settings` | A portable copy of every choice. |
| `import_settings` | Apply one back. |
| `read_settings_events` | The person's history of their own changes. |
| `resolve_settings` | **Service-facing.** Never expose this. |
| `set_setting_for_user` | **Service-facing.** Never expose this. |
| `check_liveness` | Is the process up. |
| `check_readiness` | Are its dependencies usable. |

## What to expose, and what not to

**Expose the twelve under `/v1/settings`.** They take a person's own token, and every one of
them is something a person might reasonably ask an assistant to do.

**Never expose the two under `/v1/internal`.** They take a service token, which is a static
string in a deployment's configuration rather than something a person mints. An assistant
holding one could act for any person whose token it could obtain, within that service's
namespaces. The service token belongs in a service, not in a tool.

**Think about `forget_settings` before exposing it.** It is irreversible, has no grace
period and no confirmation step, and it is one tool call away from a model that misread
"reset my search settings". If the bridge has a confirmation mechanism, this is what it is
for. If it does not, consider leaving the tool out and letting people do it themselves.

## The descriptions are written for a model, not for a browser

Every route carries a `summary` and a real `description`, and two paragraphs in them are
load-bearing rather than decorative. A bridge should surface them verbatim.

The first is on every write:

> Do not change these on your own initiative. They are the person's decision about their
> own data; ask, then set what they asked for.

A settings service an assistant tidies up unprompted is worse than no settings service. The
failure is quiet and compounding: a model that "helpfully" sets `safe_search` to `strict`
has made a decision on somebody's behalf that they will discover weeks later and will not
be able to explain.

The second is on anything touching erasure:

> Changes are never retroactive. Switching `user.erasure_mode` to `immediate` does not
> destroy what is already waiting out a grace period, and switching away from `tombstone`
> does not schedule what is already tombstoned.

Without it, a model reasoning about what a change will do has to guess, and the plausible
guess is wrong in the direction that destroys data.

## Call `describe_settings` first

The single most useful thing a bridge can do is make `describe_settings` cheap and
obvious, because it is the call that prevents the two most common failures:

- **Guessing a key name.** A document with an unknown key is a 400 that names every
  unknown key at once and points back at `describe_settings`. The keys are never silently
  dropped, because a model that believes it wrote a setting it did not is worse off than
  one that was refused.
- **Guessing a value.** Types are exact -- a boolean setting refuses the string `"true"`,
  and a whole-number setting refuses `"5"` -- and `describe_settings` returns this
  deployment's **narrowed** bounds, so a value inside them is a value a write will accept.

It also returns `pinned`, so a model can see a refusal coming rather than discovering it,
and `origin`, so it can tell a person that a setting they are about to change does not do
anything yet in the owning service.

## Errors a tool will meet

Every failure is RFC 9457 `application/problem+json` with the same six fields, so a bridge
has one error shape to render. The `detail` names the rule that failed and **never echoes
the value** -- which matters here, because a 422 body is logged by the caller and often
handed straight back to a model.

| Status | Means | What a model should do |
| --- | --- | --- |
| 400 | The document names settings that do not exist. | Read `describe_settings`, fix the names, retry. |
| 401 | The token was not accepted. | Stop. Do not retry; the person needs a new token. |
| 403 | This token does not grant that namespace, or only the person may change that setting. | Tell them which, and stop. |
| 404 | No such namespace or setting in this build. | Read `describe_settings`. |
| 409 | The operator has pinned this value. | Tell them it is fixed by their deployment. Do not retry. |
| 412 | Something changed since you read it. | Re-read and reapply, or ask. |
| 422 | The value is the wrong type, out of bounds, or looks like a credential. | Fix it. If it is a credential, it belongs in keyring. |
| 503 | keyring could not be reached, so the token could not be checked. | Retry after the `Retry-After`. The token is probably fine. |

## Two things a bridge should not do

**Do not cache across people.** Every response is one person's settings, and the ETag is
`"<account_id>.<revision>"`. A bridge that keyed a cache on the namespace alone would serve
one person's decisions to another.

**Do not paper over 409 or 422 by retrying with a different value.** Both mean a decision
was refused, and picking a different value that happens to be accepted is making the
decision on the person's behalf -- which is the thing the write descriptions ask a model
not to do.
