# Copyright (c) netviz contributors
# MIT (see https://github.com/blechschmidt/netviz)
"""The one place these plugins reach into netviz, and the one place they fail.

Every plugin in this collection is a translation: Ansible's option system on one
side, :mod:`netviz.ansible` on the other, and nothing in between that decides an
answer. That is deliberate. A rule that lives in a plugin can only be tested
with Ansible installed and a play running; the same rule in netviz is tested by
``pytest`` in a second, and this collection stays small enough to read.

Two things do have to live here. The **import**, because netviz is a control-node
dependency that Ansible knows nothing about and the failure when it is missing
should say what to install rather than raise ``ModuleNotFoundError`` out of a
template. And the **error translation**, because every netviz failure is a
:class:`netviz.errors.NetvizError` carrying a diagnostic that is already written
for a person to read — a query error underlines the character it is about — and
the job here is to let that text through unmangled.
"""

from __future__ import annotations

from ansible.errors import AnsibleError

#: What to say when the control node has Ansible but not netviz. The plugins run
#: on the controller, not on the target, so this is about the machine typing
#: ``ansible-playbook``.
MISSING = (
    "the netviz package is not importable by the Ansible control node "
    "({reason}). These plugins run on the controller: install it into the same "
    "Python that runs ansible-playbook, e.g. 'pip install netviz', and check "
    "with 'python -c \"import netviz\"'."
)


def api():
    """The :mod:`netviz.ansible` module.

    Raises:
        AnsibleError: netviz is not installed on the control node.
    """
    try:
        import netviz.ansible as module
    except ImportError as error:  # pragma: no cover - exercised without netviz installed
        raise AnsibleError(MISSING.format(reason=error)) from error
    return module


def errors():
    """The netviz exception base class, for an ``except`` clause.

    Raises:
        AnsibleError: netviz is not installed on the control node.
    """
    try:
        from netviz.errors import NetvizError
    except ImportError as error:  # pragma: no cover - as above
        raise AnsibleError(MISSING.format(reason=error)) from error
    return NetvizError


def resolve_root(root, config_path=None, variables=None):
    """Where the inventory tree is, as an absolute path, or ``None``.

    A ``root`` written in an inventory configuration file is relative to *that
    file* rather than to whatever directory ``ansible-playbook`` was run from —
    the file and the tree are checked in together, and the working directory is
    not. Anywhere else it is relative to the working directory, which is what a
    command-line argument means everywhere.
    """
    import os

    found = root
    if not found and variables:
        found = variables.get("netviz_root")
    if not found:
        found = os.environ.get("NETVIZ_ROOT")
    if not found:
        return None
    found = os.path.expanduser(str(found))
    if not os.path.isabs(found) and config_path:
        found = os.path.join(os.path.dirname(os.path.abspath(config_path)), found)
    return os.path.abspath(found)


def translate(error):
    """A netviz failure as the exception Ansible reports.

    The message is passed through whole: a query diagnostic is three lines with
    a caret under the offending token, and a plugin that flattened it to its
    first sentence would be hiding the half that says where.
    """
    return AnsibleError(str(error))
