"""A hash of an inventory state, so a plan can refuse to be applied to another.

``netgraph apply`` executes a changeset somebody has read and approved. What
they approved was a diff *from a particular state*, and if the tree has moved on
since — a colleague pushed, a script ran, the branch changed — the entries are
no longer a description of what will happen. The plan therefore records what its
source state hashed to, and apply refuses to run against a tree that hashes
differently.

The hash is over **meaning, not bytes**: every element, addressed, with its
fields as the loader resolved them. Reformatting the tree, re-wrapping a
comment, moving a document to another file in the same folder — none of those
change what the plan would do, and none of them invalidate it. Adding an
element, changing a field or moving a document to another *namespace* all do,
and all of them show up here.
"""

from __future__ import annotations

import hashlib
import json
from typing import Final

from netgraph.loader.inventory import Inventory
from netgraph.plan.diff import elements_by_address
from netgraph.plan.document import document_of

__all__ = ["DIGEST_PREFIX", "state_digest"]

#: Written into the plan so the algorithm is legible in the file, and so a
#: future revision can add a second one without ambiguity.
DIGEST_PREFIX: Final = "sha256:"


def state_digest(inventory: Inventory) -> str:
    """``sha256:...`` over every addressable document of ``inventory``.

    Deterministic across runs, platforms and Python versions: the payload is
    canonical JSON with sorted keys, and the documents are listed in address
    order rather than in load order.
    """
    payload = {
        str(address): document_of(element)
        for address, element in sorted(
            elements_by_address(inventory).items(), key=lambda item: str(item[0])
        )
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return DIGEST_PREFIX + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
