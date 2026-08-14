"""Users, groups and membership: the model, the resolution, and the view.

The other modules cover these where they touch what already existed — one
invalid fixture per rule in ``tests/fixtures/invalid``, the JSON Schema, the
generated reference, the completion lists. What is new and has no home elsewhere
is here:

* the two ``spec`` shapes, and what each refuses. A private key pasted into
  ``ssh_keys`` is the one that matters most, because the mistake is silent
  everywhere else;
* **membership resolution**, which four consumers share
  (:mod:`netgraph.identity`) precisely so that they cannot disagree — the
  validator, the identity view, the two listings and any future export. The
  tests below assert the plan and the view agree rather than asserting each
  separately;
* expansion through nested groups, including the cycle-safety a listing needs
  even though a cycle is an error;
* what the identity view draws, and what it deliberately does not: no cable, no
  device, no hardware of any kind.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from netgraph.errors import SchemaError
from netgraph.identity import identity_plan
from netgraph.loader import Inventory, load_tree
from netgraph.models import (
    API_VERSION,
    Group,
    User,
    UserStatus,
    UserType,
    parse_document,
)
from netgraph.render.graph import EdgeKind, Layer, build_graph
from netgraph.validate import validate

#: A well-formed OpenSSH public key, shortened. Only the shape is checked.
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyForTheTestsOnlyAAAAAAAAAAA ana@laptop"


def document(kind: str, name: str, spec: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "apiVersion": API_VERSION,
        "kind": kind,
        "metadata": {"name": name},
        **({"spec": spec} if spec is not None else {}),
    }


def parse_user(spec: dict[str, object] | None = None, name: str = "ana") -> User:
    element = parse_document(document("user", name, spec))
    assert isinstance(element, User)
    return element


def parse_group(spec: dict[str, object] | None = None, name: str = "admins") -> Group:
    element = parse_document(document("group", name, spec))
    assert isinstance(element, Group)
    return element


def refusal(kind: str, name: str, spec: dict[str, object]) -> SchemaError:
    with pytest.raises(SchemaError) as excinfo:
        parse_document(document(kind, name, spec))
    return excinfo.value


def tree(tmp_path: Path, *documents: str) -> Inventory:
    """An inventory of one file holding ``documents``."""
    (tmp_path / "identity.yaml").write_text("---\n".join(documents), encoding="utf-8")
    return load_tree(tmp_path)


USERS = """
apiVersion: netgraph.dev/v1alpha1
kind: user
metadata: {name: ana}
spec: {full_name: Ana Brandt, uid: 1000}
"""

# --------------------------------------------------------------------------- #
# The user document
# --------------------------------------------------------------------------- #


def test_a_user_needs_no_spec_at_all() -> None:
    """A name is an identity. Everything else is detail somebody may not have."""
    user = parse_user()
    assert user.login == "ana"
    assert user.spec.type is UserType.PERSON
    assert user.spec.status is UserStatus.ACTIVE
    assert user.is_person and not user.has_departed


def test_the_login_defaults_to_the_document_name_and_is_materialised() -> None:
    """Absent means "the same"; the property applies the default once, for everyone."""
    assert parse_user().login == "ana"
    assert parse_user({"login": "a.brandt"}).login == "a.brandt"


def test_a_login_may_be_a_user_principal_name() -> None:
    """A domain-joined estate's login *is* an address; refusing it teaches nothing."""
    assert parse_user({"login": "ana@example.invalid"}).login == "ana@example.invalid"


@pytest.mark.parametrize("login", ["-ana", "ana brandt", "ana/brandt", "ana!", "a" * 65])
def test_a_login_outside_the_account_alphabet_is_refused(login: str) -> None:
    error = refusal("user", "ana", {"login": login})
    assert any(issue.rule == "NG-S001" for issue in error.issues), error.issues


@pytest.mark.parametrize("email", ["ana", "ana@example", "ana@ex ample.invalid", "@invalid.test"])
def test_an_address_that_is_not_one_is_refused(email: str) -> None:
    error = refusal("user", "ana", {"email": email})
    assert any(issue.rule == "NG-S001" for issue in error.issues), error.issues


@pytest.mark.parametrize("uid", [-1, 2**32 - 1, 2**32])
def test_a_uid_outside_the_assignable_range_is_refused(uid: int) -> None:
    error = refusal("user", "ana", {"uid": uid})
    assert any(issue.rule == "NG-S001" for issue in error.issues), error.issues


def test_the_largest_assignable_uid_is_accepted() -> None:
    """4294967295 is ``(uid_t) -1``; one below it is a real account."""
    assert parse_user({"uid": 2**32 - 2}).spec.uid == 2**32 - 2


def test_a_pasted_ssh_key_is_folded_onto_single_spaces() -> None:
    """Two documents recording one key must compare equal, however it was pasted."""
    wrapped = KEY.replace(" ", "\n  ", 1)
    assert parse_user({"ssh_keys": [wrapped]}).spec.ssh_keys == [KEY]


def test_a_private_key_is_refused_with_the_reason() -> None:
    """The mistake this check exists for: the wrong half of the pair, committed."""
    error = refusal("user", "ana", {"ssh_keys": ["-----BEGIN OPENSSH PRIVATE KEY-----\nb3Bl"]})
    (issue,) = error.issues
    assert issue.rule == "NG-S002"
    assert "private key" in issue.message


def test_a_key_that_is_not_a_key_is_refused() -> None:
    error = refusal("user", "ana", {"ssh_keys": ["hunter2"]})
    assert any(issue.rule == "NG-S002" for issue in error.issues), error.issues


def test_one_key_twice_is_refused_even_with_a_different_comment() -> None:
    """A comment is not what makes two keys different."""
    other = f"{KEY.rsplit(' ', 1)[0]} ana@desktop"
    error = refusal("user", "ana", {"ssh_keys": [KEY, other]})
    (issue,) = error.issues
    assert issue.rule == "NG-S002"
    assert issue.path == ("spec", "ssh_keys", 1)


def test_a_user_owns_no_interfaces() -> None:
    """What keeps an identity out of the topology and out of every port rule."""
    assert User.has_interfaces is False
    assert Group.has_interfaces is False


def test_the_details_of_a_user_leave_out_what_the_title_already_says() -> None:
    user = parse_user({"full_name": "Ana Brandt", "uid": 1000, "status": "suspended"})
    assert user.details() == ("Ana Brandt", "uid 1000", "suspended")
    assert user.describe() == "Ana Brandt, uid 1000, suspended"
    assert "ana" not in user.describe()


# --------------------------------------------------------------------------- #
# The group document
# --------------------------------------------------------------------------- #


def test_a_group_may_be_empty_and_says_so() -> None:
    """Empty is a state, not a syntax error; ``W139`` is where it is reported."""
    group = parse_group()
    assert group.members == () and group.is_empty


def test_one_member_twice_is_refused_at_the_position_of_the_second() -> None:
    error = refusal("group", "admins", {"members": ["ana", "kit", "ana"]})
    (issue,) = error.issues
    assert issue.rule == "NG-S003"
    assert issue.path == ("spec", "members", 2)


def test_a_group_that_names_itself_is_refused_without_an_inventory() -> None:
    """The one loop a single document can see; the longer ones are ``NG-S012``."""
    error = refusal("group", "admins", {"members": ["ana", "admins"]})
    (issue,) = error.issues
    assert issue.rule == "NG-S003"
    assert issue.path == ("spec", "members", 1)


def test_the_details_of_a_group_count_what_it_names() -> None:
    group = parse_group({"members": ["ana"], "gid": 100})
    assert group.details() == ("1 member", "gid 100")
    assert parse_group({"members": ["ana", "kit"]}).details() == ("2 members",)


# --------------------------------------------------------------------------- #
# Membership resolution
# --------------------------------------------------------------------------- #

NESTED = """
apiVersion: netgraph.dev/v1alpha1
kind: user
metadata: {name: ana}
---
apiVersion: netgraph.dev/v1alpha1
kind: user
metadata: {name: kit}
---
apiVersion: netgraph.dev/v1alpha1
kind: group
metadata: {name: admins}
spec: {members: [ana]}
---
apiVersion: netgraph.dev/v1alpha1
kind: group
metadata: {name: household}
spec: {members: [admins, kit]}
"""


def test_a_tree_with_no_group_costs_nothing(tmp_path: Path) -> None:
    """The short-circuit: no groups, no walk, no indexes."""
    plan = identity_plan(tree(tmp_path, USERS))
    assert len(plan) == 0
    assert plan.direct == {} and plan.holders == {}


def test_the_plan_indexes_membership_in_both_directions(tmp_path: Path) -> None:
    plan = identity_plan(tree(tmp_path, NESTED))
    assert plan.members_of("household") == ("admins", "kit")
    assert plan.groups_of("ana") == ("admins",)
    assert plan.groups_of("admins") == ("household",)


def test_expansion_walks_the_nesting_and_names_the_groups_it_crossed(tmp_path: Path) -> None:
    """A reader auditing a group needs to see what it holds as well as who."""
    plan = identity_plan(tree(tmp_path, NESTED))
    assert plan.expand("household") == ("admins", "kit", "ana")
    assert plan.users_in("household") == ("kit", "ana")
    assert plan.users_in("admins") == ("ana",)


def test_expansion_terminates_on_an_inventory_with_a_cycle(tmp_path: Path) -> None:
    """A cycle is ``NG-S012``, an error — and a listing still has to run on it."""
    inventory = tree(
        tmp_path,
        """
apiVersion: netgraph.dev/v1alpha1
kind: group
metadata: {name: a}
spec: {members: [b]}
""",
        """
apiVersion: netgraph.dev/v1alpha1
kind: group
metadata: {name: b}
spec: {members: [a]}
""",
    )
    plan = identity_plan(inventory)
    assert plan.expand("a") == ("b",)
    assert plan.cycles() == [["a", "b"]]


def test_a_member_that_resolves_to_a_device_is_not_indexed(tmp_path: Path) -> None:
    """``NG-S011`` reports it; the indexes must not carry it into a diagram."""
    inventory = tree(
        tmp_path,
        """
apiVersion: netgraph.dev/v1alpha1
kind: pdu
metadata: {name: pdu-1}
spec: {outlets: 8}
""",
        """
apiVersion: netgraph.dev/v1alpha1
kind: group
metadata: {name: admins}
spec: {members: [pdu-1]}
""",
    )
    plan = identity_plan(inventory)
    (entry,) = plan.memberships
    assert entry.resolved and not entry.is_identity and entry.kind == "pdu"
    assert plan.members_of("admins") == ()
    assert [finding.rule for finding in validate(inventory)] == ["E044"]


def test_a_member_is_resolved_outwards_from_the_groups_own_namespace(tmp_path: Path) -> None:
    """A membership is an ordinary reference, resolved the ordinary way (§4.1)."""
    (tmp_path / "people").mkdir()
    (tmp_path / "people" / "ana.yaml").write_text(USERS, encoding="utf-8")
    (tmp_path / "groups.yaml").write_text(
        """
apiVersion: netgraph.dev/v1alpha1
kind: group
metadata: {name: admins}
spec: {members: [ana]}
""",
        encoding="utf-8",
    )
    plan = identity_plan(load_tree(tmp_path))
    assert plan.members_of("admins") == ("people/ana",)


# --------------------------------------------------------------------------- #
# The identity view
# --------------------------------------------------------------------------- #


def test_the_identity_view_draws_the_identities_and_nothing_else(tmp_path: Path) -> None:
    """A cable says nothing about who may log in, so no cable is drawn."""
    inventory = tree(
        tmp_path,
        NESTED,
        """
apiVersion: netgraph.dev/v1alpha1
kind: switch
metadata: {name: sw-1}
spec: {interfaces: [{name: eth0, type: ethernet}]}
""",
    )
    graph = build_graph(inventory, layer=Layer.IDENTITY)
    assert sorted(graph.nodes) == ["admins", "ana", "household", "kit"]
    assert all(node.identity is not None for node in graph.nodes.values())
    assert all(edge.kind is EdgeKind.MEMBERSHIP for edge in graph.edges)


def test_a_membership_edge_runs_from_the_group_to_the_member(tmp_path: Path) -> None:
    """The direction the fact is written in, and the one a reader follows."""
    graph = build_graph(tree(tmp_path, NESTED), layer=Layer.IDENTITY)
    assert [(edge.source, edge.target) for edge in graph.edges] == [
        ("admins", "ana"),
        ("household", "admins"),
        ("household", "kit"),
    ]


def test_the_view_and_the_plan_cannot_disagree(tmp_path: Path) -> None:
    """The whole reason resolution lives in one module: one edge per indexed member."""
    inventory = tree(tmp_path, NESTED)
    plan = identity_plan(inventory)
    graph = build_graph(inventory, layer=Layer.IDENTITY)
    drawn = {(edge.source, edge.target) for edge in graph.edges}
    indexed = {(group, member) for group, members in plan.direct.items() for member in members}
    assert drawn == indexed


def test_an_unresolved_member_is_drawn_as_nothing(tmp_path: Path) -> None:
    """``--force`` must still produce a picture; ``NG-S010`` is what says so."""
    inventory = tree(
        tmp_path,
        """
apiVersion: netgraph.dev/v1alpha1
kind: group
metadata: {name: admins}
spec: {members: [ghost]}
""",
    )
    graph = build_graph(inventory, layer=Layer.IDENTITY)
    assert sorted(graph.nodes) == ["admins"]
    assert graph.edges == ()


def test_an_identity_is_not_a_node_of_any_network_layer(tmp_path: Path) -> None:
    """It owns no interfaces, so it belongs to no topology."""
    inventory = tree(tmp_path, NESTED)
    for layer in (Layer.L1, Layer.L2, Layer.L3, Layer.POWER, Layer.RACK):
        assert build_graph(inventory, layer=layer).nodes == {}


# --------------------------------------------------------------------------- #
# Rules the fixtures cannot express on their own
# --------------------------------------------------------------------------- #


def test_a_login_collision_is_reported_even_when_neither_document_writes_one(
    tmp_path: Path,
) -> None:
    """``login`` defaults to ``metadata.name``, and the account namespace is the estate."""
    (tmp_path / "north").mkdir()
    (tmp_path / "south").mkdir()
    body = """
apiVersion: netgraph.dev/v1alpha1
kind: user
metadata: {name: ana}
---
apiVersion: netgraph.dev/v1alpha1
kind: group
metadata: {name: staff}
spec: {members: [ana]}
"""
    (tmp_path / "north" / "people.yaml").write_text(body, encoding="utf-8")
    (tmp_path / "south" / "people.yaml").write_text(body, encoding="utf-8")
    findings = [finding for finding in validate(load_tree(tmp_path)) if finding.rule == "E046"]
    (finding,) = findings
    assert "login 'ana'" in finding.message
    assert set(finding.elements) == {"north/ana", "south/ana"}


def test_an_ambiguous_member_names_every_candidate(tmp_path: Path) -> None:
    (tmp_path / "north").mkdir()
    (tmp_path / "south").mkdir()
    user = """
apiVersion: netgraph.dev/v1alpha1
kind: user
metadata: {name: ana}
"""
    (tmp_path / "north" / "people.yaml").write_text(user, encoding="utf-8")
    (tmp_path / "south" / "people.yaml").write_text(user, encoding="utf-8")
    (tmp_path / "groups.yaml").write_text(
        """
apiVersion: netgraph.dev/v1alpha1
kind: group
metadata: {name: staff}
spec: {members: [ana]}
""",
        encoding="utf-8",
    )
    findings = [finding for finding in validate(load_tree(tmp_path)) if finding.rule == "E043"]
    (finding,) = findings
    assert "ambiguous" in finding.message
    assert set(finding.elements) == {"staff", "north/ana", "south/ana"}


def test_a_departed_service_account_is_not_reported(tmp_path: Path) -> None:
    """``W140`` is about a person leaving; a robot has nobody to be."""
    inventory = tree(
        tmp_path,
        """
apiVersion: netgraph.dev/v1alpha1
kind: user
metadata: {name: backup}
spec: {type: service, status: departed}
""",
        """
apiVersion: netgraph.dev/v1alpha1
kind: group
metadata: {name: robots}
spec: {members: [backup]}
""",
    )
    assert validate(inventory) == []


def test_a_three_group_cycle_is_reported_once(tmp_path: Path) -> None:
    """One loop per tangle, not one per rotation of it."""
    inventory = tree(
        tmp_path,
        *(
            f"""
apiVersion: netgraph.dev/v1alpha1
kind: group
metadata: {{name: {name}}}
spec: {{members: [{nxt}]}}
"""
            for name, nxt in (("a", "b"), ("b", "c"), ("c", "a"))
        ),
    )
    findings = [finding for finding in validate(inventory) if finding.rule == "E045"]
    (finding,) = findings
    assert "'a' -> 'b' -> 'c' -> 'a'" in finding.message
