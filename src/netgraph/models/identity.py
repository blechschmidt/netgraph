"""The ``user`` and ``group`` elements (§19 of ``docs/schema.md``).

Why identity belongs in a network inventory
-------------------------------------------

Every other kind in this schema answers "what is there". These two answer "whose
is it, and who may touch it" — the question an audit asks first and the one an
inventory of boxes and cables cannot answer at all. A wireless network with a
per-user PSK, a jump host with a list of authorised keys, a VLAN that exists
because one department needed it: all three are facts about people, written down
today in a spreadsheet nobody diffs.

So an identity is an element like any other. It has a document, a namespace, a
name that must be unique in it, labels, a description and a source location; it
is planned, applied, formatted, exported and drawn by exactly the machinery every
other kind goes through. Nothing here is a special case, which is the whole
point: the moment identity needs its own loader or its own diff, the single
source of truth has stopped being single.

Why membership lives on the group
---------------------------------

A ``group`` lists its members; a ``user`` does not list its groups. Both
spellings are readable and only one can be authoritative, and writing the fact
twice is how an inventory starts disagreeing with itself — the failure mode this
tool exists to prevent. The group side is the one chosen because it is the side
an access rule is written against ("the ``vpn`` group may dial in"), and because
the count that matters — how many people can do this? — is then a property of one
document rather than a search across all of them.

A member is a plain element reference (§4.1), resolved outwards from the group's
own namespace like every other reference in the schema. It may name a ``user``
**or another ``group``**, which is what makes a hierarchy expressible:
``everyone`` contains ``engineering`` contains ``alice``. The model refuses a
group that names itself; the validator, which can see the whole tree, refuses the
longer cycles (``NG-S012``) and everything else a single document cannot know —
a member that resolves to nothing (``NG-S010``), or to a switch (``NG-S011``).

Why a user owns no interfaces
-----------------------------

Because a person is not a host. :attr:`User.has_interfaces` is false for the same
reason a PDU's is (§17.1): it keeps identities out of the layer-1 topology, out
of every cable-endpoint lookup and out of every rule about ports. An identity is
joined to other identities by membership and to nothing else, which is why it is
drawn in a view of its own (``--layer identity``) rather than sprinkled over a
picture of the cabling.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Any, ClassVar, Final, Literal

from pydantic import BeforeValidator, Field, model_validator

from netgraph.errors import echo_value
from netgraph.models.base import NetgraphModel
from netgraph.models.diagnostics import field_error
from netgraph.models.element import ElementBase
from netgraph.models.scalars import ElementRef

__all__ = [
    "EMAIL_PATTERN",
    "GROUP_KIND",
    "IDENTITY_KINDS",
    "LOGIN_PATTERN",
    "MAX_EMAIL_LENGTH",
    "MAX_GROUP_MEMBERS",
    "MAX_LOGIN_LENGTH",
    "MAX_POSIX_ID",
    "MAX_SSH_KEYS",
    "USER_KIND",
    "EmailAddress",
    "Group",
    "GroupSpec",
    "Login",
    "PosixId",
    "SshPublicKey",
    "User",
    "UserSpec",
    "UserStatus",
    "UserType",
]

#: ``kind`` of a user identity, and of a collection of them. Named once so
#: nothing downstream spells either out.
USER_KIND: Final = "user"
GROUP_KIND: Final = "group"

#: The two kinds that make up the identity graph, in the order §3 lists them.
IDENTITY_KINDS: Final[tuple[str, ...]] = (USER_KIND, GROUP_KIND)

#: ``NG-S001`` — ceiling on ``uid`` and ``gid``. POSIX ids are 32-bit and
#: ``4294967295`` is reserved as "no such id" (``(uid_t) -1``), so the largest
#: assignable one is one below it.
MAX_POSIX_ID: Final = 2**32 - 2

#: ``NG-S001`` — ceiling on a login. 32 is what ``useradd`` enforces on Linux and
#: 64 is what a directory tends to allow, so the larger one is used: netgraph
#: records the account, it does not create it.
MAX_LOGIN_LENGTH: Final = 64

#: ``NG-S001`` — ceiling on an address, from the SMTP path limit of RFC 5321 §4.5.3.1.
MAX_EMAIL_LENGTH: Final = 254

#: ``NG-S002`` — ceiling on the keys one identity may carry. Generous: a person
#: with a laptop, a desktop, a phone and two spares is normal, and a list longer
#: than this is a paste accident rather than a key ring.
MAX_SSH_KEYS: Final = 32

#: ``NG-S003`` — ceiling on the members one group may name directly. A group
#: larger than this is a nested hierarchy that has not been written as one.
MAX_GROUP_MEMBERS: Final = 4096

#: What a login may look like: a letter, a digit or an underscore, then anything
#: from the portable account alphabet. ``@`` is in it because a user principal
#: name (``alice@example.com``) *is* the login on a domain-joined estate, and
#: refusing the spelling an operator reads off the screen would only teach them
#: to put it in the description instead.
_LOGIN_RE: Final = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._@-]*$")

#: Deliberately not RFC 5322. The full grammar accepts things no mail system
#: will deliver to and rejects nothing an operator actually mistypes; what is
#: checked here is the shape a reader means by "an address": something, one
#: ``@``, a dotted domain, no spaces.
_EMAIL_RE: Final = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: One OpenSSH public key: an algorithm, base64 key material, an optional
#: comment. The private half has a header this refuses, which is the point —
#: ``NG-S002`` is the rule that stops a private key being committed to an
#: inventory by somebody who copied the wrong file.
_SSH_KEY_RE: Final = re.compile(
    r"^(?P<algorithm>[A-Za-z0-9@._-]+)"
    r"\s+(?P<material>[A-Za-z0-9+/]+={0,3})"
    r"(?:\s+(?P<comment>\S.*))?$"
)

#: The grammars as bare regular expressions, for the JSON Schema of
#: :mod:`netgraph.schema`. Deriving the published schema from the constants
#: enforced here is what stops the two from drifting apart.
LOGIN_PATTERN: Final = _LOGIN_RE.pattern
EMAIL_PATTERN: Final = _EMAIL_RE.pattern


class UserType(str, Enum):
    """What sort of account an identity is (§19.1).

    The distinction is not cosmetic: three of the rules in the ``S`` group only
    make sense for one of these. A service account belongs to no group in a great
    many estates and reporting that as an oversight (``NG-S016``) would be noise;
    a shared account has no single person to depart, so ``NG-S015`` has nothing
    to say about it.
    """

    #: A human being.
    PERSON = "person"
    #: A daemon, a robot, a CI runner: an account nobody logs in as.
    SERVICE = "service"
    #: One account several people use — a console login, an operator role. Worth
    #: recording as such rather than as a person who does not exist.
    SHARED = "shared"

    def __str__(self) -> str:
        return self.value


class UserStatus(str, Enum):
    """Whether the account should still work (§19.1)."""

    #: In use.
    ACTIVE = "active"
    #: Deliberately disabled, and expected back — leave, an investigation.
    SUSPENDED = "suspended"
    #: The person is gone. The document is kept so that the group memberships
    #: they still hold are *visible* rather than merely deleted, which is what
    #: ``NG-S015`` reports.
    DEPARTED = "departed"

    def __str__(self) -> str:
        return self.value

    @property
    def grants_access(self) -> bool:
        """Should a membership of this identity still let anything happen?"""
        return self is UserStatus.ACTIVE


def _normalise_ssh_key(value: Any) -> Any:
    """Fold an OpenSSH public key onto one space between its parts.

    A key pasted out of a terminal arrives wrapped, indented, or with a trailing
    newline, and none of those are differences in the key. Normalising here means
    two documents that record the same key compare equal — which is what makes a
    ``netgraph plan`` over a re-pasted file empty rather than noisy.
    """
    if not isinstance(value, str):
        return value
    text = " ".join(value.split())
    if not text:
        raise field_error("an ssh key is empty", rule="NG-S002")
    if text.startswith("-----BEGIN"):
        raise field_error(
            "that is a private key, not a public one; an inventory records the public half "
            "(the contents of the '.pub' file) and nothing else",
            rule="NG-S002",
        )
    match = _SSH_KEY_RE.match(text)
    if match is None:
        raise field_error(
            f"{echo_value(text)} is not an ssh public key; expected "
            f"'<algorithm> <base64 key> [comment]', e.g. 'ssh-ed25519 AAAAC3Nz... alice@laptop'",
            rule="NG-S002",
        )
    return text


def _check_login(value: Any) -> Any:
    """``NG-S001`` — reject a login pydantic would only call a pattern mismatch."""
    if not isinstance(value, str):
        return value
    if len(value) > MAX_LOGIN_LENGTH:
        raise field_error(
            f"a login is at most {MAX_LOGIN_LENGTH} characters; this one is {len(value)}",
            rule="NG-S001",
        )
    if _LOGIN_RE.match(value) is None:
        raise field_error(
            f"{echo_value(value)} is not a login; expected letters, digits and "
            f"'. _ - @', starting with a letter, a digit or '_'",
            rule="NG-S001",
        )
    return value


def _check_email(value: Any) -> Any:
    """``NG-S001`` — the shape a reader means by "an address"; see :data:`_EMAIL_RE`."""
    if not isinstance(value, str):
        return value
    if len(value) > MAX_EMAIL_LENGTH:
        raise field_error(
            f"an address is at most {MAX_EMAIL_LENGTH} characters (RFC 5321); this one "
            f"is {len(value)}",
            rule="NG-S001",
        )
    if _EMAIL_RE.match(value) is None:
        raise field_error(
            f"{echo_value(value)} is not an address; expected 'local@domain.tld'",
            rule="NG-S001",
        )
    return value


def _check_posix_id(value: Any) -> Any:
    """``NG-S001`` — a ``uid``/``gid`` inside the 32-bit assignable range."""
    if isinstance(value, bool) or not isinstance(value, int):
        return value
    if not 0 <= value <= MAX_POSIX_ID:
        raise field_error(
            f"{value} is not an assignable POSIX id; expected 0 to {MAX_POSIX_ID} "
            f"({MAX_POSIX_ID + 1} is reserved as 'no such id')",
            rule="NG-S001",
        )
    return value


#: ``spec.login`` and the account half of every diagnostic about one.
Login = Annotated[
    str,
    BeforeValidator(_check_login),
    Field(min_length=1, max_length=MAX_LOGIN_LENGTH, pattern=LOGIN_PATTERN),
]

#: ``spec.email`` on a user, and on a group that is also a distribution list.
EmailAddress = Annotated[
    str,
    BeforeValidator(_check_email),
    Field(min_length=3, max_length=MAX_EMAIL_LENGTH, pattern=EMAIL_PATTERN),
]

#: ``spec.uid`` and ``spec.gid``. See :data:`MAX_POSIX_ID`.
PosixId = Annotated[int, BeforeValidator(_check_posix_id), Field(ge=0, le=MAX_POSIX_ID)]

#: One entry of ``spec.ssh_keys``, normalised to single spaces.
SshPublicKey = Annotated[str, BeforeValidator(_normalise_ssh_key), Field(min_length=1)]


class UserSpec(NetgraphModel):
    """``spec`` of a ``user`` document (§19.1)."""

    #: The account name, when it differs from ``metadata.name``. Absent means
    #: they are the same, which is the common case and is why this is optional
    #: rather than required-and-duplicated; read :attr:`User.login` instead of
    #: this field, which materialises the default.
    login: Login | None = None
    #: The person's name as they write it. Free text: a real name is not a
    #: grammar, and the ones that do not fit an ASCII first/last split are
    #: exactly the ones a schema must not mangle.
    full_name: str | None = Field(default=None, max_length=253)
    #: Where mail reaches them. Also what ties the identity to a directory
    #: without netgraph having to model the directory.
    email: EmailAddress | None = None
    #: POSIX user id, when the estate assigns one. Two users claiming one id is
    #: ``NG-S013``.
    uid: PosixId | None = None
    #: Person, service or shared account. See :class:`UserType`.
    type: UserType = UserType.PERSON
    #: Whether the account should still work. See :class:`UserStatus`.
    status: UserStatus = UserStatus.ACTIVE
    #: The public keys the account authenticates with, normalised to one space
    #: between the algorithm, the material and the comment. Public halves only
    #: (``NG-S002``).
    ssh_keys: list[SshPublicKey] = Field(default_factory=list, max_length=MAX_SSH_KEYS)

    @model_validator(mode="after")
    def _check_shape(self) -> UserSpec:
        seen: dict[str, int] = {}
        for index, key in enumerate(self.ssh_keys):
            material = key.split()[1]
            first = seen.setdefault(material, index)
            if first != index:
                raise field_error(
                    f"key {index + 1} is the key already given as key {first + 1}; "
                    f"a comment is not what makes two keys different",
                    rule="NG-S002",
                    path=("ssh_keys", index),
                )
        return self


class User(ElementBase):
    """One identity: a person, a service account or a shared login (§19.1)."""

    kind: Literal["user"] = "user"
    spec: UserSpec = Field(default_factory=UserSpec)

    default_glyph: ClassVar[str] = USER_KIND
    #: A person is not a host; see the module docstring.
    has_interfaces: ClassVar[bool] = False

    @property
    def login(self) -> str:
        """The account name, defaulting to ``metadata.name``.

        Materialised rather than left absent because everything downstream —
        ``NG-S013``, an export, a diagram label — wants "the account", and
        making each of them re-apply the default is how they come to disagree.
        """
        return self.spec.login or self.metadata.name

    @property
    def display_name(self) -> str:
        """What to put on a diagram: the real name if there is one, else the login."""
        return self.spec.full_name or self.login

    @property
    def status(self) -> UserStatus:
        return self.spec.status

    @property
    def type(self) -> UserType:
        return self.spec.type

    @property
    def is_person(self) -> bool:
        """Is there somebody who could leave?"""
        return self.spec.type is UserType.PERSON

    @property
    def has_departed(self) -> bool:
        """Has the person gone, leaving whatever they were a member of behind?"""
        return self.spec.status is UserStatus.DEPARTED

    def details(self) -> tuple[str, ...]:
        """The account, one clause per fact, for a label or a table cell.

        ``metadata.name`` is deliberately absent: every caller already prints it
        as the title, and repeating it would spend the widest line on the one
        thing the reader can already see.
        """
        parts: list[str] = []
        if self.spec.login and self.spec.login != self.metadata.name:
            parts.append(f"login {self.spec.login}")
        if self.spec.full_name:
            parts.append(self.spec.full_name)
        if self.spec.email:
            parts.append(self.spec.email)
        if self.spec.uid is not None:
            parts.append(f"uid {self.spec.uid}")
        if self.spec.type is not UserType.PERSON:
            parts.append(str(self.spec.type))
        if self.spec.status is not UserStatus.ACTIVE:
            parts.append(str(self.spec.status))
        if self.spec.ssh_keys:
            count = len(self.spec.ssh_keys)
            parts.append(f"{count} ssh key{'' if count == 1 else 's'}")
        return tuple(parts)

    def describe(self) -> str:
        """``Alice Meyer, uid 1001`` — one line for a label."""
        return ", ".join(self.details())


class GroupSpec(NetgraphModel):
    """``spec`` of a ``group`` document (§19.2)."""

    #: The users and groups in this group, as element references resolved
    #: outwards from the group's own namespace (§4.1). A nested group brings
    #: everything it holds; ``NG-S012`` refuses a cycle.
    members: list[ElementRef] = Field(default_factory=list, max_length=MAX_GROUP_MEMBERS)
    #: POSIX group id, when the estate assigns one. Two groups claiming one id
    #: is ``NG-S013``.
    gid: PosixId | None = None
    #: Where mail to the whole group goes, when the group is also a list.
    email: EmailAddress | None = None

    @model_validator(mode="after")
    def _check_members(self) -> GroupSpec:
        seen: dict[str, int] = {}
        for index, member in enumerate(self.members):
            first = seen.setdefault(member, index)
            if first != index:
                raise field_error(
                    f"member {index + 1} names {echo_value(member)}, which member "
                    f"{first + 1} already names",
                    rule="NG-S003",
                    path=("members", index),
                )
        return self


class Group(ElementBase):
    """A named set of identities, which may hold other groups (§19.2)."""

    kind: Literal["group"] = "group"
    spec: GroupSpec = Field(default_factory=GroupSpec)

    default_glyph: ClassVar[str] = GROUP_KIND
    #: A group is not a host either.
    has_interfaces: ClassVar[bool] = False

    @model_validator(mode="after")
    def _check_not_self(self) -> Group:
        """``NG-S003`` — the one cycle a single document can see.

        A group naming its own ``metadata.name`` is refused here rather than by
        the validator, because it needs no inventory to know it is wrong and
        because the error then names the line it is on. Every longer loop needs
        the tree and is ``NG-S012``.
        """
        for index, member in enumerate(self.spec.members):
            if member == self.metadata.name:
                raise field_error(
                    f"group {echo_value(self.metadata.name)} names itself as a member; "
                    f"a group cannot contain itself",
                    rule="NG-S003",
                    path=("spec", "members", index),
                )
        return self

    @property
    def members(self) -> tuple[str, ...]:
        """The members as written, in document order."""
        return tuple(self.spec.members)

    @property
    def gid(self) -> int | None:
        return self.spec.gid

    @property
    def is_empty(self) -> bool:
        """Does the group hold nothing at all (``NG-S014``)?"""
        return not self.spec.members

    def details(self) -> tuple[str, ...]:
        """The group, one clause per fact. See :meth:`User.details`."""
        count = len(self.spec.members)
        parts = [f"{count} member{'' if count == 1 else 's'}"]
        if self.spec.gid is not None:
            parts.append(f"gid {self.spec.gid}")
        if self.spec.email:
            parts.append(self.spec.email)
        return tuple(parts)

    def describe(self) -> str:
        """``3 members, gid 100`` — one line for a label."""
        return ", ".join(self.details())
