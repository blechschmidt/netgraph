"""The ``template`` document kind (§6.6 of ``docs/schema.md``).

A template is a *partial* device ``spec`` given a name, so that the fifty
switches wired into the same access layer can be declared once and referenced
fifty times::

    apiVersion: netviz.dev/v1alpha1
    kind: template
    metadata:
      name: c9200l-48p
    spec:
      vendor: Cisco
      model: C9200L-48P
      interfaces:
        - range: GigabitEthernet1/0/[1-48]
          type: ethernet

A template is **not an element**. It is never indexed in an
:class:`~netviz.loader.Inventory`, never drawn, never listed, and never
validated on its own; the only place it surfaces is as the source location of a
field it contributed to a device that named it in ``spec.from``.

Which is why the model here is deliberately shallow: it validates the envelope
(``apiVersion``, ``kind``, ``metadata``) and asserts that ``spec`` is a mapping
whose keys are device-spec keys, and stops. Everything deeper is checked on the
*merged* device, where the value is finally in a context that says what it must
satisfy — a ``vlan`` block is legal on a switch and illegal on a hub, and the
template does not know which it will be merged into. The merge keeps the
provenance of every inherited field, so a value the template got wrong is still
reported against the template's own file and line.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import Field, model_validator

from netviz.errors import echo_value
from netviz.models.base import NetvizModel
from netviz.models.device import DeviceSpec
from netviz.models.diagnostics import field_error
from netviz.models.element import TEMPLATE_KIND
from netviz.models.metadata import Metadata
from netviz.models.scalars import ApiVersion

__all__ = ["INHERIT_KEY", "TEMPLATE_SPEC_KEYS", "Template"]

#: The key a device (or another template) uses to name the template it extends.
#: It lives in ``spec`` rather than in ``metadata`` because what it contributes
#: is a ``spec``; the loader strips it before the models ever see the document.
INHERIT_KEY: Final = "from"

#: Everything a template's ``spec`` may declare: every key of a device ``spec``,
#: plus :data:`INHERIT_KEY` so templates can be layered. Derived from the model
#: so a new device-spec field is inheritable the day it is added.
TEMPLATE_SPEC_KEYS: Final[frozenset[str]] = frozenset(DeviceSpec.model_fields) | {INHERIT_KEY}


class Template(NetvizModel):
    """A ``kind: template`` document: a named, partial device ``spec``."""

    api_version: ApiVersion = Field(alias="apiVersion", serialization_alias="apiVersion")
    kind: Literal["template"] = "template"
    metadata: Metadata
    #: The partial spec, left as the mapping it was written as. Validated on the
    #: devices that merge it, not here; see the module docstring.
    spec: dict[str, Any]

    @model_validator(mode="before")
    @classmethod
    def _reject_non_mapping_spec(cls, value: Any) -> Any:
        """``NG-M005`` — say "spec must be a mapping", not "not a valid dict"."""
        if isinstance(value, dict) and "spec" in value and not isinstance(value["spec"], dict):
            raise field_error(
                "a template 'spec' must be a mapping of device-spec keys, got "
                f"{type(value['spec']).__name__}",
                rule="NG-M005",
                path=("spec",),
            )
        return value

    @model_validator(mode="after")
    def _check_spec_keys(self) -> Template:
        """``NG-M005`` — a template may only carry keys a device ``spec`` has."""
        for key in self.spec:
            if key not in TEMPLATE_SPEC_KEYS:
                permitted = ", ".join(sorted(TEMPLATE_SPEC_KEYS))
                raise field_error(
                    f"unknown key {echo_value(key)} in a template spec; "
                    f"expected one of {permitted}",
                    rule="NG-M005",
                    path=("spec", key),
                )
        return self

    @property
    def name(self) -> str:
        """Shortcut for ``metadata.name``."""
        return self.metadata.name

    def __str__(self) -> str:
        return f"{TEMPLATE_KIND}/{self.metadata.name}"
