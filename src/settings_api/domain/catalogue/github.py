"""``github`` -- where new repositories go, and who can see them.

Read by the lucy hub for its `repos` capability, which is served by Github-api. A model
asked to "make a repo for this" names a repository and nothing else; these two settings are
what the person would have said if asked, so the repository lands under the right account
and is not public by accident.

Both are profile-scoped on purpose: a `work` profile usually creates under an organisation
and a `personal` one under the person's own login, and one value for both would be wrong
for one of them.
"""

from __future__ import annotations

from settings_api.domain.types import OnUnavailable, Origin, SettingDef, SettingScope, SettingType

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
)
