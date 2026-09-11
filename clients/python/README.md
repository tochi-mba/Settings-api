# settings-client

The client every service in this family uses to read one person's settings from
settings-api. Four lines at the call site; everything else -- caching, revalidation,
single-flight, and what to do when settings-api is down -- is handled here so that six
services do not each get it slightly wrong.

See `docs/integration.md` in the settings-api repository for the per-service wiring.
