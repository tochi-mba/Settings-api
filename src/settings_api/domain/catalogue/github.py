"""``github`` -- where new repositories go, who can see them, and how work lands.

Read by the lucy hub for its `repos` capability, which is served by Github-api. A model
asked to "make a repo for this" names a repository and nothing else; these two settings are
what the person would have said if asked, so the repository lands under the right account
and is not public by accident.

Every one is profile-scoped on purpose: a `work` profile usually creates under an
organisation and merges by its rules, and a `personal` one under the person's own login
and habits, and one value for both would be wrong for one of them.

``merge_method`` refuses rather than falls back. Squashing on a repository whose owner
merges or rebases rewrites how their history lands, and that cannot be cleanly undone, so
an outage makes a merge that names no method a question rather than a guess.
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

NAMESPACE = "github"

SETTINGS: tuple[SettingDef, ...] = (
    SettingDef(
        namespace=NAMESPACE,
        key="default_owner",
        scope=SettingScope.PROFILE,
        value_type=SettingType.STR,
        default=None,
        nullable=True,
        max_chars=39,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$",
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(None,),
        origin=Origin.EXISTING,
        origin_note=(
            "Read by the lucy hub, which uses it as the owner of a new repository, and as the "
            "owner to search under, when the request names none."
        ),
        summary="Which account or organisation new repositories are created under.",
        description=(
            "A GitHub login or organisation name, as it appears in a repository's address. "
            "Null means the connected account itself, which is what GitHub does when no "
            "owner is given and is therefore the conservative fallback: an outage never "
            "puts a repository in an organisation the person did not choose.\n\n"
            "This does not grant anything. Creating under an organisation still needs the "
            "connected account to be allowed to, and Lucy still asks before creating."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="default_visibility",
        scope=SettingScope.PROFILE,
        value_type=SettingType.ENUM,
        default="private",
        choices=("private", "public", "internal"),
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("private",),
        origin=Origin.EXISTING,
        origin_note=(
            "Read by the lucy hub, which uses it as the visibility of a new repository when "
            "the request does not say."
        ),
        summary="Whether a new repository is private, public or internal unless asked.",
        description=(
            "`private` by default and on any outage, because a repository made public by "
            "mistake has been published, and making it private again does not unpublish "
            "it. `internal` exists only for organisations on GitHub Enterprise; GitHub "
            "refuses it anywhere else, and the refusal reaches the person as such."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="merge_method",
        scope=SettingScope.PROFILE,
        value_type=SettingType.ENUM,
        default="squash",
        choices=("merge", "squash", "rebase"),
        agent_writable=AgentAccess.WITH_APPROVAL,
        on_unavailable=OnUnavailable.REFUSE,
        origin=Origin.EXISTING,
        origin_note=(
            "Read by the lucy hub as the method of `repos.merge` when the request names none "
            "(before, a hard-coded squash). If it cannot be read, a merge that names no method "
            "is refused and the model is told to ask."
        ),
        summary="How Lucy merges a pull request when you did not say: merge, squash or rebase.",
        description=(
            "`squash`, the default, is what Lucy always did. `merge` keeps every commit and "
            "adds a merge commit; `rebase` replays the commits onto the base branch without "
            "one. A request that names a method always wins, and GitHub still refuses a method "
            "the repository does not allow.\n\n"
            "This refuses rather than falls back. A merge is shared history and cannot be "
            "cleanly undone, so when this cannot be read Lucy does not guess squash on a "
            "repository whose owner merges or rebases: it asks which."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="draft_pull_requests",
        scope=SettingScope.PROFILE,
        value_type=SettingType.BOOL,
        default=False,
        agent_writable=AgentAccess.WITH_APPROVAL,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(False, True),
        origin=Origin.EXISTING,
        origin_note=(
            "Read by the lucy hub as `draft` on `repos.openPull` when the request does not say "
            "(before, not sent, so GitHub's default of ready applied)."
        ),
        summary="Whether pull requests Lucy opens start as drafts unless you say otherwise.",
        description=(
            "Off by default: a pull request opens ready for review, as it always did, and "
            "reviewers are notified. On opens it as a draft, so nobody is asked to review "
            "until you mark it ready. A request that says draft or ready always wins.\n\n"
            "Both are safe to land on: opening a pull request is already something you "
            "approve, and its draft state can be changed afterwards."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="delete_branch_after_merge",
        scope=SettingScope.PROFILE,
        value_type=SettingType.BOOL,
        default=False,
        agent_writable=AgentAccess.WITH_APPROVAL,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(False,),
        origin=Origin.EXISTING,
        origin_note=(
            "Read by the lucy hub as `delete_branch` on `repos.merge` when the request does "
            "not say (before, always kept). When this setting is what deleted a branch, the "
            "merge step says so."
        ),
        summary="Whether Lucy deletes a pull request's branch after merging it, unless you say.",
        description=(
            "Off by default: the branch is kept after a merge, as it always was. On deletes "
            "the head branch once the pull request has merged, so merged branches stop piling "
            "up; GitHub can restore it from the pull request page. A request that says keep or "
            "delete always wins.\n\n"
            "An outage falls back to off, because keeping a branch is the side that needs no "
            "undoing."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="watch_default_hours",
        scope=SettingScope.PROFILE,
        value_type=SettingType.INT,
        default=1,
        minimum=1,
        maximum=168,
        operator_clampable=True,
        agent_writable=AgentAccess.WITH_APPROVAL,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(1,),
        origin=Origin.EXISTING,
        origin_note=(
            "Read by the lucy hub as the lifetime of `repos.watch` when the request names no "
            "`for_seconds` (before, an hour). The hub holds it between 1 and 168."
        ),
        summary="How many hours Lucy keeps watching a repository when you did not say.",
        description=(
            "One hour by default, which is what Lucy always did. Reviews and slow CI often "
            "take longer, so a longer default keeps 'tell me when it is green' from lapsing. "
            "A week at most, and a request that names how long always wins.\n\n"
            "A watch that wakes the conversation holds standing consent for its lifetime plus "
            "fifteen minutes, so a longer watch is a longer consent, revocable at any time. An "
            "outage falls back to an hour: a shorter watch expires, reports, and does nothing "
            "extra."
        ),
    ),
)
