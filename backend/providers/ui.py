"""Vendor-neutral view models for the plugin's "Configure preset" dialog.

The dialog renders a header (workflow actions), one tab per side of the
pipeline and one card per service account. Providers and core fill these
models; the plugin renders them without knowing any provider by name. They
never carry secrets: a credential field reports only whether it is set.

See ``backend/services/preset_view.py`` for how a preset is turned into a
:class:`PresetView` and ``plugin/src/preset-dialog-view.js`` for the renderer.
"""

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

#: Tab indicator: green, yellow, red, grey (no check available) and hollow (switched off).
SideState = Literal["ready", "starting", "down", "unknown", "off"]

#: Where the plugin keeps a field's value.
#: ``none``: display only. ``client_pref``: a plain plugin preference (``key`` is its name).
#: ``client_secret``: a personal key in the plugin's preferences, sent as the ``header`` on
#: every request (``key`` is the key name). ``shared``: an admin-set server value, saved
#: through POST /api/config/remote-fields. ``session``: a one-time credential kept in
#: memory until Zotero closes and sent in the ``keys`` of provisioning requests.
#: ``login``: an input of an account login form, sent once in ``body.values`` of the
#: card action that has ``sends_login`` and then cleared (never stored).
FieldStorage = Literal["none", "client_pref", "client_secret", "shared", "session", "login"]


class UiField(BaseModel):
    """One input (or read-only value) in a tab's Configuration section or an account card."""

    id: str
    label: str
    type: Literal["text", "secret", "number", "select", "readonly", "toggle"] = "text"
    value: Optional[Any] = None
    default: Optional[Any] = None
    placeholder: str = ""
    help: str = ""
    pattern: Optional[str] = None
    options: list[str] = Field(default_factory=list)
    min: Optional[float] = None
    max: Optional[float] = None
    storage: FieldStorage = "none"
    #: Preference name, key name or env name, depending on ``storage``.
    key: Optional[str] = None
    #: Request header that carries a ``client_secret``.
    header: Optional[str] = None
    #: For credentials: whether a value is available (never the value itself).
    is_set: Optional[bool] = None
    optional: bool = False
    #: False when the caller may see but not change the value (e.g. a non-admin and a shared key).
    editable: bool = True
    docs_url: Optional[str] = None
    #: Server-side settings that read this field (e.g. "indexing"); lets the plugin re-sync copies.
    required_for: list[str] = Field(default_factory=list)
    #: Show the field only while another field of the same list has one of these values
    #: (``{"<field id>": ["<value>", ...]}``); None shows it always.
    show_if: Optional[dict[str, list[str]]] = None


class UiMessage(BaseModel):
    """A status or feedback line."""

    level: Literal["ok", "info", "warn", "error"] = "info"
    text: str
    link: Optional[str] = None


class UiFact(BaseModel):
    """A short label/value pair in a tab's Status section."""

    label: str
    value: str


class UiRequest(BaseModel):
    """The backend call an action makes. ``body.sides`` lists are merged when several
    actions with the same path run together (the header's Start all / Pause all)."""

    method: Literal["POST"] = "POST"
    path: str
    body: dict = Field(default_factory=dict)


class UiAction(BaseModel):
    """A button in a tab's Actions section, a card, or the dialog header."""

    id: str
    label: str
    kind: Literal["primary", "normal", "danger"] = "normal"
    enabled: bool = True
    #: Why the action is disabled; empty when enabled.
    reason: str = ""
    #: Part of the header's Start all / Pause all.
    workflow: Optional[Literal["start", "pause"]] = None
    request: Optional[UiRequest] = None
    #: Account card whose ``session`` credentials go into ``request.body.keys``.
    account: Optional[str] = None
    #: A card action that submits the card's ``login`` fields as ``request.body.values``.
    sends_login: bool = False


class UiQuota(BaseModel):
    """How much of a provider's quota is used, combined into one value by the provider
    (``Provider.quota_summary``), with the meters behind it for a detailed display."""

    used_pct: int
    level: Literal["ok", "warn", "error"] = "ok"
    text: str
    #: ``backend.providers.types.Meter`` dumps (the plugin's rate-limit widget draws them).
    meters: list[dict] = Field(default_factory=list)


class SideProvider(BaseModel):
    id: str
    label: str


class SideView(BaseModel):
    """One tab of the dialog: Configuration, Status (with an optional log) and Actions."""

    side: str
    title: str
    optional: bool = False
    enabled: bool = True
    provider: Optional[SideProvider] = None
    #: The account card that holds this side's credentials, if any.
    account: Optional[str] = None
    state: SideState = "unknown"
    summary: str = ""
    fields: list[UiField] = Field(default_factory=list)
    messages: list[UiMessage] = Field(default_factory=list)
    facts: list[UiFact] = Field(default_factory=list)
    log: list[str] = Field(default_factory=list)
    actions: list[UiAction] = Field(default_factory=list)
    quota: Optional[UiQuota] = None


class AccountCard(BaseModel):
    """One card in the dialog's footer: the credentials of one service provider.

    Saving a card's fields starts the sides it ``serves`` (``auto_start``). A provider
    with an account login instead has a login form (``login`` fields) and ``actions``
    (log in, log out); its login request starts the sides itself.
    """

    id: str
    label: str
    serves: list[str] = Field(default_factory=list)
    state: Literal["ready", "needs_input"] = "ready"
    scope_note: str = ""
    fields: list[UiField] = Field(default_factory=list)
    messages: list[UiMessage] = Field(default_factory=list)
    actions: list[UiAction] = Field(default_factory=list)
    auto_start: bool = True
    #: The most used quota of the sides this account serves.
    quota: Optional[UiQuota] = None
    #: How the user is billed for this paid service and the prices of their contract
    #: (``client_pref`` fields, saved when changed). Empty for a free service.
    billing: list[UiField] = Field(default_factory=list)


class PresetHeader(BaseModel):
    name: str
    description: str = ""
    is_admin: bool = False
    #: Options for the admin's server-default chooser; empty for everyone else.
    default_options: list[str] = Field(default_factory=list)
    default_preset: str = ""


class PresetView(BaseModel):
    """GET /api/config/preset-view."""

    preset: PresetHeader
    sides: list[SideView]
    accounts: list[AccountCard]
    #: The job of the caller's provisioning slot is running (the plugin keeps polling).
    busy: bool = False
