"""Common pydantic configuration for every netviz model.

All models reject unknown keys (``NG-D005`` in :doc:`docs/schema.md`): silently
ignoring a misspelt key would produce a diagram that disagrees with the file,
which is the exact failure mode this tool exists to prevent.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = ["NetvizModel"]


class NetvizModel(BaseModel):
    """Base class for every schema model."""

    model_config = ConfigDict(
        extra="forbid",
        # Field names are the YAML keys; only ``apiVersion`` carries an alias.
        populate_by_name=True,
        # ``spec.model`` is a legitimate field name here (hardware model).
        protected_namespaces=(),
        validate_default=True,
        # Keep enums as their string values so renderers and JSON exporters can
        # use them directly.
        use_enum_values=False,
        str_strip_whitespace=False,
    )
