"""``search`` -- which model answers, which backend looks, and who never sees the query.

``disabled_providers`` is the canonical ``REFUSE`` case in this catalogue and the clearest
illustration of why that field has no default. Its own default is ``[]``, meaning "every
provider is allowed", because that is the only value that lets the service work out of the
box. So a fallback to the default is not a neutral act: for a person who has written down
"never send my queries to provider X", falling back to ``[]`` sends their next query to
exactly the provider they refused, silently, because a different service was down.
Refusing the search is the lesser failure and the only one that is not a broken promise.

Two notes on the owning service, both of which the generated documentation repeats. Its
``search_backend`` is a *preference with failover* rather than a hard selection -- the
router tries the named backend first and then works through the rest -- so a value here
changes ordering, not sources. And ``safe_search`` exists today only as a per-request
boolean on its request schema, with no server-side setting at all, so the three-way choice
below needs a change there before it means anything.

The last six shape a search a request leaves unsaid: its language and region, the
sites a person never wants to see, how many pages a summary is written from, how long
the summary is, and the standing guidance every summary follows. ``research_notes`` is
prompt text that reaches a model on every summary, so no assistant may write it: one
injected page could otherwise plant standing instructions for every search after it.
"""

from __future__ import annotations

from settings_api.domain.types import (
    AgentAccess,
    OnUnavailable,
    Origin,
    SettingDef,
    SettingScope,
    SettingType,
)

NAMESPACE = "search"

SETTINGS: tuple[SettingDef, ...] = (
    SettingDef(
        namespace=NAMESPACE,
        key="default_model",
        scope=SettingScope.PROFILE,
        value_type=SettingType.STR,
        default=None,
        nullable=True,
        max_chars=128,
        pattern=r"^[a-z0-9][a-z0-9_-]*:[A-Za-z0-9][A-Za-z0-9._:-]*$",
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(None,),
        origin=Origin.EXISTING,
        origin_note=(
            "web-search-api's `WSA_DEFAULT_MODEL`, a namespaced `provider:model` identifier."
        ),
        summary="Which model answers when a request does not name one, as `provider:model`.",
        description=(
            "Namespaced, because web-search-api reaches around sixty providers and a bare "
            "model name is ambiguous across them. The pattern enforces the shape and nothing "
            "more: whether the deployment actually has that provider is something only "
            "web-search-api knows, and it says so at the moment of use rather than here.\n\n"
            "Null means 'whatever this deployment's default model is', which is the "
            "conservative value for an unusual reason: a null names no provider, so it cannot "
            "name one this person would not have chosen. An outage lands on the deployment's "
            "own default rather than on a stale preference."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="search_backend",
        scope=SettingScope.PROFILE,
        value_type=SettingType.ENUM,
        default="google",
        choices=("google", "searxng"),
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("google",),
        origin=Origin.EXISTING,
        origin_note=(
            "web-search-api's `WSA_SEARCH_BACKEND`, default 'google'. A third backend, "
            "serper, exists in that codebase but is unreachable, so it is not offered here."
        ),
        summary="Which search backend to try first: Google, or a self-hosted SearXNG.",
        description=(
            "A preference, not a restriction, and the difference matters. web-search-api "
            "tries the named backend first and fails over through the others, so choosing "
            "`searxng` means 'prefer the self-hosted one' rather than 'never scrape "
            "Google'.\n\n"
            "If somebody wants the stronger statement, it is not expressible today in the "
            "owning service, and it should not be faked here: a setting that reads as a "
            "guarantee and behaves as a hint is worse than no setting. Falling back to "
            "`google` restores the shipped order, which is why this is safe to land on -- it "
            "cannot send a query anywhere the failover would not already have sent it."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="disabled_providers",
        value_type=SettingType.STR_LIST,
        default=[],
        max_items=60,
        max_item_chars=64,
        on_unavailable=OnUnavailable.REFUSE,
        origin=Origin.EXISTING,
        origin_note="web-search-api's `WSA_DISABLED_PROVIDERS`, honoured at provider build.",
        summary="Model providers this person's queries must never be sent to.",
        description=(
            "Provider keys, one per entry. An empty list means every provider the deployment "
            "has is allowed, which is what web-search-api does today.\n\n"
            "**This refuses rather than falling back, and it is the reason that field has no "
            "default.** The permissive value has to be the default for the service to work at "
            "all, so falling back to it during an outage would override the one thing this "
            "setting is for. 'Never send my queries to provider X' is meaningless if a brief "
            "outage turns X back on -- and nobody would find out, because the query would "
            "succeed.\n\n"
            "Note the change web-search-api needs: it builds its provider list once at "
            "startup from a process-wide list, so per-person filtering has to happen when a "
            "caller's provider catalogue is assembled rather than when the process starts."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="max_content_chars",
        value_type=SettingType.INT,
        default=40_000,
        minimum=1_000,
        maximum=200_000,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(40_000,),
        origin=Origin.EXISTING,
        origin_note="web-search-api's `WSA_MAX_CONTENT_CHARS`, default 40000.",
        summary="How much scraped page text may be handed to a model in one request.",
        description=(
            "A ceiling on cost and on latency as much as on context: the effective limit for "
            "any one request is the smaller of this and what the chosen model can hold. "
            "Raising it buys more of a long article at the price of a larger prompt.\n\n"
            "The default is web-search-api's own, so falling back to it reproduces today's "
            "behaviour exactly. A person who lowered it to save money gets a more expensive "
            "request during an outage, which is a bill rather than a broken promise."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="safe_search",
        scope=SettingScope.PROFILE,
        value_type=SettingType.ENUM,
        default="moderate",
        choices=("off", "moderate", "strict"),
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("moderate", "strict"),
        origin=Origin.EXISTING,
        origin_note=(
            "web-search-api applies it to a search that does not say, and holds a request "
            "that does to at least this level. Google has one filter, so `moderate` and "
            "`strict` both turn it on; SearxNG is sent all three."
        ),
        summary="How aggressively to filter explicit results out of searches.",
        description=(
            "Three states rather than the on/off the owning service has, because the middle "
            "one is what most people mean: filter the obvious, do not filter a medical "
            "question into uselessness.\n\n"
            "Both `moderate` and `strict` are conservative, and `off` is not -- so a future "
            "change of default has to stay inside the filtering two. An outage cannot turn "
            "filtering off, which is the property worth having on a setting a household might "
            "share. For the same reason a request body can ask for more filtering than this "
            "and never for less."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="store_query_history",
        value_type=SettingType.BOOL,
        default=False,
        owner_writable_only=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(False,),
        origin=Origin.PROPOSED,
        origin_note=(
            "New here, and there is nothing to configure yet: web-search-api persists no "
            "queries, results or summaries anywhere, and has no database at all."
        ),
        summary="Whether this person's search queries may be kept after the answer is given.",
        description=(
            "Off, a query exists for the length of the request and is gone. On, it is kept, "
            "and a search history is among the most revealing records a person can have: what "
            "somebody searched for and when says more about them than most of what they would "
            "willingly write down.\n\n"
            "**Owner-writable only.** A service holding this person's token may write within "
            "its own namespace, and this is one of the two exceptions, because the service "
            "that would benefit from switching it on is the service that would be doing the "
            "keeping. Turning it on has to be the person's own act, with a token they minted "
            "for settings itself.\n\n"
            "Off is conservative and is what an outage lands on: nothing is kept."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="default_result_count",
        scope=SettingScope.PROFILE,
        value_type=SettingType.INT,
        default=8,
        minimum=1,
        maximum=20,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(8,),
        origin=Origin.EXISTING,
        origin_note=(
            "Read by the lucy hub, which passes it, held to 1-20, as the result count when "
            "a research call names none. web-search-api takes a count per request and "
            "does not read it."
        ),
        summary="How many results a search returns when the request does not ask for a number.",
        description=(
            "More results are more of the web in the prompt and a larger bill. Fewer are "
            "more round trips. Twenty is the owning service's own ceiling, expressed here "
            "so a person may lower it and never raise it past what the service will serve.\n\n"
            "Eight is a typical default and the fallback, so an outage neither floods the "
            "prompt nor starves a lookup that expected a handful of hits."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="recency_days",
        scope=SettingScope.PROFILE,
        value_type=SettingType.INT,
        default=None,
        nullable=True,
        minimum=1,
        maximum=365,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(None,),
        origin=Origin.EXISTING,
        origin_note=(
            "web-search-api applies it to a search that names no recency of its own: a day "
            "count to Google, and to SearxNG the smallest of day, week, month or year that "
            "covers it."
        ),
        summary="How recent a result must be, in days, when the request does not say.",
        description=(
            "Null means no recency filter, which is what the service does today and is "
            "therefore the conservative fallback: an outage does not hide a year-old page "
            "somebody needed. A number of 1 means 'today', 7 a week, 365 a year.\n\n"
            "This is a search preference, not a prompt line. Which backend is in force in "
            "the live block is Lucy's `feeds_research_backend`."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="language",
        scope=SettingScope.PROFILE,
        value_type=SettingType.STR,
        default="en",
        max_chars=8,
        pattern=r"^[a-z]{2,3}(?:-[a-z0-9]{2,4})?$",
        agent_writable=AgentAccess.FREELY,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("en",),
        origin=Origin.EXISTING,
        origin_note=(
            "Read by web-search-api as the language a query is run in when the request names "
            "none (before, always `en`)."
        ),
        summary="The language search results come back in, when a search does not say.",
        description=(
            "A language code such as `fr` or `pt-br`. A search that names a language uses "
            "that one instead. English is what an outage lands on, and what the service always "
            "used."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="region",
        scope=SettingScope.PROFILE,
        value_type=SettingType.STR,
        default="us",
        max_chars=8,
        pattern=r"^[a-z]{2}$",
        agent_writable=AgentAccess.FREELY,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("us",),
        origin=Origin.EXISTING,
        origin_note=(
            "Read by web-search-api as the country a query is ranked for when the request names "
            "none (before, always `us`)."
        ),
        summary="The country search results are ranked for, when a search does not say.",
        description=(
            "A two-letter country code such as `gb` or `ng`, so a search like 'plumber near "
            "me' finds local results. A search that names a region uses that one instead."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="blocked_domains",
        scope=SettingScope.ACCOUNT,
        value_type=SettingType.STR_LIST,
        default=[],
        max_items=100,
        max_item_chars=253,
        agent_writable=AgentAccess.WITH_APPROVAL,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=([],),
        origin=Origin.EXISTING,
        origin_note=(
            "Read by web-search-api, which drops results whose host is one of these or a "
            "subdomain of one before any page is read or summarised. A query restricted to "
            "one of these sites, and a page named to be read directly, are not blocked."
        ),
        summary="Sites whose search results you never see.",
        description=(
            "Results from these sites, and from their subdomains, are removed from every "
            "search before anything is read or summarised. A search you limit to one of these "
            "sites still searches it, because you asked for that site.\n\n"
            "An assistant may add one only with your approval: a domain added quietly is a "
            "source quietly hidden from your research. An outage falls back to none blocked."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="read_top_pages",
        scope=SettingScope.PROFILE,
        value_type=SettingType.INT,
        default=0,
        minimum=0,
        maximum=10,
        operator_clampable=True,
        agent_writable=AgentAccess.FREELY,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(0,),
        origin=Origin.EXISTING,
        origin_note=(
            "Read by web-search-api when a summarised search does not say whether to read "
            "pages, or asks for pages without a count. An explicit request wins, and a search "
            "that is not summarised reads no page it did not ask for."
        ),
        summary="How many top result pages each search summary is written from.",
        description=(
            "Zero, the default, writes a summary from titles and snippets alone, which is "
            "fast. Higher reads the top pages themselves first: slower, and a summary written "
            "from what the pages say rather than what their snippets suggest."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="summary_length",
        scope=SettingScope.PROFILE,
        value_type=SettingType.ENUM,
        default="standard",
        choices=("brief", "standard", "detailed"),
        agent_writable=AgentAccess.FREELY,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("standard", "brief", "detailed"),
        origin=Origin.EXISTING,
        origin_note=(
            "Read by web-search-api for search, scrape and summarize alike. `standard` is the "
            "summary prompt as it always was; the other two change its length rule only."
        ),
        summary="How long research summaries are.",
        description=(
            "`brief` is one or two sentences and at most three key points. `standard` is two "
            "to five sentences. `detailed` is five to ten sentences and at most ten key points."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="research_notes",
        scope=SettingScope.PROFILE,
        value_type=SettingType.STR,
        default="",
        max_chars=2_000,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("",),
        origin=Origin.EXISTING,
        origin_note=(
            "Read by web-search-api and added after a request's own notes in the summary "
            "prompt, framed as what to focus on rather than as a source of facts, within the "
            "service's 4,000-character notes limit."
        ),
        summary="Standing guidance every research summary follows.",
        description=(
            "Such as 'prefer primary sources and flag sponsored content'. It steers what a "
            "summary pays attention to, never what counts as fact, and it follows any notes a "
            "single search gives.\n\n"
            "Only you can change it. It is text a model reads on every summary, so an "
            "assistant that could write it could leave instructions for every search after."
        ),
    ),
)
