A tree whose whole purpose is appearance (§22).

Every rung of the style ladder is exercised by something here, so that the pair
of goldens over it — `styled-l1-plain` and `styled-l1-themed` — pin down both
what a style does and what a theme does *without* one:

* `rtr-core` declares a full `spec.style`, so it is what the element rung looks
  like when it wins outright;
* `rtr-edge` declares one field, which is what pins down that a style is merged
  field by field rather than adopted whole;
* `sw-hq-a` and `sw-hq-b` declare nothing and carry `role` labels, so the theme
  is the only thing that can be drawing them differently from each other;
* the two sites are separate namespaces, so a `namespace:` selector has
  something to distinguish;
* `cbl-core-hq` declares a style on a *cable*, which is the one kind that has a
  colour and no shape;
* `pc-desk` sets `opacity`, the one field that reaches the output as an alpha
  pair rather than as an attribute of its own.

`theme.yaml` is beside them rather than inside the tree, because a theme is not
an inventory document — see `docs/schema.md` §22.3.
