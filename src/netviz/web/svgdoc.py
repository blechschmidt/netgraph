"""The diagram, as ``netviz web`` needs it: an embeddable, inert fragment.

The rewriting itself is :mod:`netviz.render.fragment`, which two embedders
share — this preview and the self-contained page ``netviz render -f html``
writes. What is decided *here* is the pair of choices that make this embedder
different from that one:

* **No native tooltips.** The info box says strictly more than a browser
  tooltip, immediately, and in a place the page controls; keeping both would
  mean two tooltips a second apart disagreeing about which element the pointer
  is over.
* **No links.** A preview is a live page around a diagram built from text
  someone is typing. Letting that diagram navigate the page is not a capability
  the preview needs, so it is not one it has.

Both are the safer half of the choice, which is why this module keeps its own
name rather than its callers reaching for ``fragment`` with arguments: the
preview's answer is settled, and not a per-call decision.
"""

from __future__ import annotations

from netviz.render.fragment import SVG_NAMESPACE, XLINK_NAMESPACE, fragment

__all__ = ["SVG_NAMESPACE", "XLINK_NAMESPACE", "prepare"]


def prepare(payload: bytes) -> str:
    """Return ``payload`` as an ``<svg>`` fragment safe to embed in the preview.

    Args:
        payload: An SVG rendering, as Graphviz produced it.

    Returns:
        The serialised ``<svg>`` element, without the XML declaration, with
        everything that could execute, navigate or pop a second tooltip
        removed.

    Raises:
        RenderError: The payload is not parseable XML, is not an SVG, or has no
            ``viewBox`` to scale against.
    """
    return fragment(payload, tooltips=False, links=False)
