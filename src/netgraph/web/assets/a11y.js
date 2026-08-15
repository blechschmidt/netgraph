/* What makes a Graphviz drawing usable without a pointer or without eyes.
 *
 * A rendered SVG is inert. Graphviz emits `<g class="node"><title>x</title>
 * <ellipse/><text/></g>`: no roles, no labels, nothing focusable, and a
 * `<title>` that assistive technology reads as a tooltip rather than as the
 * element's name. A screen reader lands on it and says "graphic". That is the
 * whole diagram, gone.
 *
 * Four things happen here, and all four come off the *same records the info box
 * uses* -- netgraphDetail's -- because a second description of an element is a
 * second description to keep in step:
 *
 *   1. **Semantics on the picture.** Every `g.node` and `g.edge` gets a role and
 *      an `aria-label` built from its record. The canvas is the
 *      `role="application"` that owns them, and says which one is current with
 *      `aria-activedescendant`.
 *   2. **An outline.** The same records, in reading order, as a list -- 'sw-home,
 *      switch, 8 interfaces, linked to rtr-edge on eth0'. Off screen until it is
 *      focused, at which point it becomes a real panel: a keyboard user gets the
 *      diagram as text, and does not have to know it was there.
 *   3. **Focus.** Arrow keys walk the drawing, preferring the elements the
 *      focused one is *linked to*, so a path can be followed rather than a grid
 *      swept. The ring this paints is deliberately not the selection ring; see
 *      app.css.
 *   4. **Announcements.** One polite live region for what happened, one
 *      assertive one for what was refused. Everything the editor applies,
 *      refuses or reverts goes through here, once.
 *
 * Dependency-free, like the rest of this page.
 */

var netgraphA11y = (function () {
  "use strict";

  /** How many links an element's label spells out before it counts the rest.
   *  A 48-port switch must not read its whole patch panel out loud. */
  var MAX_LINKS_SPOKEN = 4;

  /** How far off a direction a candidate may be and still count as "that way",
   *  in radians. 50 degrees: wide enough that a diagonal neighbour is reachable,
   *  narrow enough that "right" never means "back the way you came". */
  var DIRECTION_ARC = 0.87;

  var el = null;
  var host = null;
  /** element id -> record, for whatever is drawn now. */
  var records = {};
  /** The drawn order: element ids of nodes, then of edges. */
  var order = [];
  /** The SVG id of the focused group. */
  var current = null;
  /** Which element's links are being cycled, and how far through them we are.
   *  Held across the moves, because the cycle belongs to the *node* it started
   *  from: deriving it afresh from each link would hand the cycle to whichever
   *  end of that link happened to be listed first. */
  var linkAnchor = null;
  var linkIndex = -1;
  /** True only inside cycleLink, so focus() knows not to reset the two above. */
  var cycling = false;
  /** What this tab has selected, which is not the same as what it has focused. */
  var selected = null;
  /** The multi-selection, as SVG ids: select.js owns it, this file speaks it.
   *  A screen reader hears the count on the outline's summary and hears which
   *  entries are in it from their own pressed state. */
  var marked = {};
  var markedCount = 0;
  /** What the outline is currently listing, so its summary can be re-said when
   *  the selection moves without the drawing having changed. */
  var outlined = [];
  var outlineMeta = null;

  /* --------------------------------------------------------------- attach */

  /** Take over the diagram's semantics.
   *
   * `bridge` is app.js's side: the elements, and the two things this file
   * cannot decide for itself -- what to do when an element is activated, and
   * what the current view is called.
   */
  function attach(bridge) {
    host = bridge;
    el = bridge.el;
    el.canvas.addEventListener("focus", onCanvasFocus);
    // A pointer user clicking a shape should leave the keyboard where the
    // pointer went, so the two ways of driving the canvas agree about "here".
    el.canvas.addEventListener("mousedown", function (event) {
      var group = event.target.closest ? event.target.closest("g.node, g.edge") : null;
      if (group) { focus(group.id, { quiet: true, scroll: false }); }
    });
    return true;
  }

  function onCanvasFocus() {
    if (!current || !group(current)) { first({ quiet: false }); }
    else { paint(); announce(labelOf(records[current]), false); }
  }

  /* ------------------------------------------------------------ the label */

  /** One record as the single line a screen reader should hear.
   *
   * The order is identity, then what it is, then what it is joined to -- which
   * is the order the info box puts them in, and the order somebody asking "what
   * is this" wants them in.
   */
  function labelOf(record) {
    if (!record) { return ""; }
    return (record.type === "edge" ? edgeLabel(record) : nodeLabel(record)).join(", ");
  }

  function nodeLabel(record) {
    var parts = [String(record.name || record.id || "element")];
    parts.push(subtitle(record));
    var ports = (record.interfaces || []).length;
    if (ports) { parts.push(ports === 1 ? "1 interface" : ports + " interfaces"); }
    var links = record.links || [];
    links.slice(0, MAX_LINKS_SPOKEN).forEach(function (link) {
      parts.push("linked to " + (link.peer || "?") +
        (link.interface ? " on " + link.interface : ""));
    });
    if (links.length > MAX_LINKS_SPOKEN) {
      var rest = links.length - MAX_LINKS_SPOKEN;
      parts.push("and " + rest + " more link" + (rest === 1 ? "" : "s"));
    }
    if (!links.length) { parts.push("not linked"); }
    return parts;
  }

  function edgeLabel(record) {
    var ends = record.endpoints || [];
    var parts = [String(record.kind || "link")];
    if (record.label) { parts.push(String(record.label)); }
    if (ends.length === 2) {
      parts.push("from " + endpointText(ends[0]) + " to " + endpointText(ends[1]));
    }
    if (record.medium) { parts.push(String(record.medium)); }
    if (record.speedText) { parts.push(String(record.speedText)); }
    var vlans = record.vlans || [];
    if (vlans.length) { parts.push("vlan " + vlans.join(" ")); }
    return parts;
  }

  function endpointText(end) {
    if (!end) { return "?"; }
    return String(end.node || "?") + (end.interface ? " " + end.interface : "");
  }

  /** The kind, as the picture itself spells it. Mirrors details._subtitle. */
  function subtitle(record) {
    if (record.subnet) { return (record.subnet.family || "ip") + " subnet"; }
    if (record.tunnel) { return (record.tunnel.type || "tunnel") + " tunnel"; }
    if (record.aggregate) { return "namespace"; }
    return String(record.kind || "element");
  }

  /* ---------------------------------------------------------- annotating */

  /** Give the SVG that is on screen its semantics, and rebuild the outline.
   *
   * Called after every redraw, including the ones that reuse a cached SVG: the
   * records may be the same but the *labels* are computed from them here, and a
   * view switched back to must be as legible as one drawn fresh.
   */
  function annotate(details, meta) {
    records = details || {};
    order = [];
    var svg = el.viewport.firstElementChild;
    if (!svg) {
      current = null;
      outline([], meta);
      describeCanvas(meta, 0);
      return;
    }
    // The drawing itself is one image with a name, not a tree of anonymous
    // shapes: without this a screen reader walks a few hundred <path>s.
    svg.setAttribute("role", "presentation");
    svg.setAttribute("focusable", "false");

    var nodes = [];
    var edges = [];
    svg.querySelectorAll("g.node, g.edge").forEach(function (group) {
      var record = records[group.id];
      if (!record) {
        // Furniture: a cluster's frame, a legend. Nothing to say about it, so
        // say nothing rather than let it be walked into.
        group.setAttribute("aria-hidden", "true");
        return;
      }
      group.setAttribute("role", "img");
      group.setAttribute("aria-label", labelOf(record));
      // The <title> Graphviz writes is the tooltip text, and a duplicate name
      // is read twice. The label above is the name; this is not.
      group.querySelectorAll(":scope > title").forEach(function (title) {
        title.setAttribute("aria-hidden", "true");
      });
      (record.type === "edge" ? edges : nodes).push(group.id);
    });
    order = nodes.concat(edges);
    if (current && order.indexOf(current) === -1) { current = null; }
    outline(order, meta);
    describeCanvas(meta, order.length);
    paint();
  }

  /** What the canvas itself is, for somebody who lands on it with one Tab. */
  function describeCanvas(meta, count) {
    var view = (meta && meta.view) || "diagram";
    el.canvas.setAttribute("role", "application");
    el.canvas.setAttribute("aria-roledescription", "network diagram");
    el.canvas.setAttribute(
      "aria-label",
      view + " view, " + count + " element" + (count === 1 ? "" : "s") +
      ". Arrow keys move between elements, Enter opens the inspector, " +
      "question mark lists every shortcut."
    );
    if (!el.canvas.hasAttribute("tabindex")) { el.canvas.setAttribute("tabindex", "0"); }
  }

  /* ------------------------------------------------------------- outline */

  /** The diagram as a list, in the order the drawing declares its elements.
   *
   * This is the fallback that always works: no roles to interpret, no SVG to
   * traverse, no pointer. It is also the fastest way for a sighted keyboard
   * user to find something, which is why focusing it brings it on screen
   * instead of leaving it at -10000px.
   */
  function outline(ids, meta) {
    var list = el.outlineList;
    if (!list) { return; }
    list.replaceChildren();
    outlined = ids.slice();
    outlineMeta = meta || null;
    sayOutline();
    ids.forEach(function (id) {
      var record = records[id];
      var item = document.createElement("li");
      var button = document.createElement("button");
      button.type = "button";
      button.className = "outline-entry" + (marked[id] ? " picked" : "");
      button.dataset.element = id;
      // A multi-selection is a set of toggles, and `aria-pressed` is what a
      // screen reader reads as "selected" on a control that is one. Set on
      // every entry rather than only on the selected ones, so the state is
      // *announced* as off rather than merely absent.
      button.setAttribute("aria-pressed", marked[id] ? "true" : "false");
      button.textContent = labelOf(record);
      button.addEventListener("click", function (event) {
        // The same modifiers the canvas takes, so the outline is a way of
        // building a selection and not only of jumping to one thing.
        if ((event.shiftKey || event.ctrlKey || event.metaKey) && window.netgraphSelect) {
          window.netgraphSelect.toggle([String(record.id || "")]);
          return;
        }
        focus(id, { quiet: false });
        el.canvas.focus();
      });
      item.appendChild(button);
      list.appendChild(item);
    });
  }

  /** What the outline says it is: the view, its size, and what is selected. */
  function sayOutline() {
    if (!el.outlineSummary) { return; }
    var ids = outlined;
    if (!ids.length) {
      el.outlineSummary.textContent = "nothing is drawn";
      return;
    }
    var nodes = ids.filter(function (id) { return records[id].type !== "edge"; }).length;
    var text = ((outlineMeta && outlineMeta.view) || "diagram") + " view: " + nodes +
      " element" + (nodes === 1 ? "" : "s") + ", " + (ids.length - nodes) +
      " link" + (ids.length - nodes === 1 ? "" : "s");
    if (markedCount) { text += ", " + markedCount + " selected"; }
    el.outlineSummary.textContent = text;
  }

  /** Adopt select.js's set. Nothing here decides what is selected; see that
   *  file. What this does is make it *audible*: the count on the summary and a
   *  pressed state on each entry, which is the whole of what a screen reader
   *  needs to answer "what am I about to delete".
   *
   *  `count` is passed rather than derived from `ids` because an address that
   *  is selected and off this layer is still selected, and the summary should
   *  say so rather than quietly under-count. */
  function mark(ids, count) {
    marked = {};
    (ids || []).forEach(function (id) { marked[id] = true; });
    markedCount = count === undefined ? (ids || []).length : count;
    // Deliberately *not* `aria-multiselectable` on the canvas: it is a
    // `role="application"`, which does not take it, and axe is right to say so.
    // The set is announced on the outline's summary and carried on each entry's
    // pressed state, which are both roles that mean it.
    if (el.outlineList) {
      el.outlineList.querySelectorAll(".outline-entry").forEach(function (entry) {
        var on = !!marked[entry.dataset.element];
        entry.classList.toggle("picked", on);
        entry.setAttribute("aria-pressed", on ? "true" : "false");
      });
    }
    sayOutline();
  }

  /* --------------------------------------------------------------- focus */

  /** The drawn group for an element id, drawn back in if it had been culled.
   *
   * cull.js empties the group of anything off screen but never removes it, so
   * the lookup itself always succeeds; what would be missing is the shape the
   * ring goes round. Asking for the group is asking to do something with it, so
   * this is where the contents come back — which is what makes "select the
   * thing find-in-diagram just landed on" work when it is half a screen away.
   */
  function group(id) {
    var svg = el.viewport.firstElementChild;
    var found = svg ? svg.querySelector('[id="' + cssEscape(id) + '"]') : null;
    if (found && window.netgraphCull) { window.netgraphCull.materialise(id); }
    return found;
  }

  function cssEscape(value) {
    return window.CSS && window.CSS.escape ? window.CSS.escape(value) : value;
  }

  /** Put the focus ring on one element and say what it is. */
  function focus(id, options) {
    var opts = options || {};
    if (!records[id]) { return false; }
    current = id;
    if (!cycling) {
      // Arriving anywhere by any other means starts the link cycle over, from
      // the node that was landed on -- or from the near end of the link.
      linkIndex = -1;
      linkAnchor = records[id].type === "edge" ? nodeOf(records[id]) : id;
    }
    paint();
    if (opts.scroll !== false) { reveal(id); }
    if (!opts.quiet) { announce(labelOf(records[id]), false); }
    if (host && host.onFocus) { host.onFocus(records[id]); }
    return true;
  }

  function focused() {
    return current && records[current] ? { element: current, record: records[current] } : null;
  }

  /** Draw the ring, and tell the application which descendant is current. */
  function paint() {
    var svg = el.viewport.firstElementChild;
    if (!svg) { return; }
    svg.querySelectorAll("g.focused, g.selected").forEach(function (group_) {
      group_.classList.remove("focused", "selected");
    });
    if (selected) {
      var chosen = group(selected);
      if (chosen) { chosen.classList.add("selected"); }
    }
    var target = current ? group(current) : null;
    if (!target) { el.canvas.removeAttribute("aria-activedescendant"); return; }
    target.classList.add("focused");
    el.canvas.setAttribute("aria-activedescendant", target.id);
  }

  /** Note what this tab has *selected*, which the focus ring must not be
   *  mistaken for: focus is where the keyboard is, selection is what the user
   *  said they were working on. */
  function select(id) {
    selected = id || null;
    paint();
  }

  /** Bring the focused shape into the visible part of the canvas.
   *
   * The canvas pans by transforming a wrapper, so there is nothing to scroll:
   * the translation is adjusted instead, which is the same arithmetic the wheel
   * handler does and is delegated to app.js so there is one copy of it.
   */
  function reveal(id) {
    var target = group(id);
    if (!target || !host || !host.bringIntoView) { return; }
    var box = target.getBoundingClientRect();
    if (!box.width && !box.height) { return; }
    host.bringIntoView(box);
  }

  /* ---------------------------------------------------------- navigation */

  function first(options) {
    return order.length ? focus(order[0], options) : false;
  }

  function last(options) {
    return order.length ? focus(order[order.length - 1], options) : false;
  }

  /** Step to the nearest element in `direction`, links first.
   *
   * "Links first" is what makes this navigation rather than a grid sweep: a
   * network is read by following what is joined to what, so a linked neighbour
   * in roughly the right direction always beats an unlinked one that happens to
   * be nearer. Only when nothing linked lies that way does it fall back to
   * geometry, which is what keeps a diagram of islands walkable.
   */
  function move(direction) {
    if (!current) { return first({ quiet: false }); }
    var best = neighbour(direction);
    if (!best) { announce("nothing to the " + direction, false); return false; }
    return focus(best, { quiet: false });
  }

  /** The id `move` would step to, without stepping to it.
   *
   * Separated so that Shift-arrow can *extend a selection* along exactly the
   * same search — a second, subtly different notion of "the thing to the right"
   * would be a diagram that walks one way with the arrow keys and another way
   * with Shift held.
   */
  function neighbour(direction) {
    if (!current) { return null; }
    var here = centre(current);
    if (!here) { return null; }
    var wanted = { right: 0, down: Math.PI / 2, left: Math.PI, up: -Math.PI / 2 }[direction];
    if (wanted === undefined) { return null; }
    var neighbours = linkedTo(current);
    var best = null;
    order.forEach(function (id) {
      if (id === current || records[id].type === "edge") { return; }
      var there = centre(id);
      if (!there) { return; }
      var dx = there.x - here.x;
      var dy = there.y - here.y;
      var distance = Math.sqrt(dx * dx + dy * dy);
      if (!distance) { return; }
      var off = Math.abs(angle(Math.atan2(dy, dx) - wanted));
      if (off > DIRECTION_ARC) { return; }
      var candidate = {
        id: id,
        linked: neighbours[id] ? 0 : 1,
        // Off-axis distance costs more than along-axis distance, so "right"
        // prefers the thing beside you over the thing diagonally past it.
        cost: distance * (1 + off)
      };
      if (!best || candidate.linked < best.linked ||
          (candidate.linked === best.linked && candidate.cost < best.cost)) {
        best = candidate;
      }
    });
    return best ? best.id : null;
  }

  /** Normalise an angle into (-pi, pi]. */
  function angle(value) {
    while (value <= -Math.PI) { value += 2 * Math.PI; }
    while (value > Math.PI) { value -= 2 * Math.PI; }
    return value;
  }

  /** The middle of an element, in the diagram's own coordinates.
   *
   * From cull.js's index rather than from `getBBox`, for two reasons. It is the
   * only answer that exists for an element whose contents have been culled out
   * of the render tree — and navigation has to work on those, or arrowing
   * across a large diagram would stop at the edge of the screen. And it is
   * arithmetic on numbers already in hand rather than a forced layout, which
   * matters because `move` asks for the centre of every candidate on every
   * keypress: on a two-thousand-element diagram that was two thousand layout
   * flushes per arrow key.
   */
  function centre(id) {
    var known = window.netgraphCull ? window.netgraphCull.centreOf(id) : null;
    if (known) { return known; }
    var target = group(id);
    if (!target) { return null; }
    var box = target.getBBox ? target.getBBox() : null;
    if (!box || (!box.width && !box.height)) { return null; }
    return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  }

  /** The elements the one at `id` is joined to, as a set of SVG ids. */
  function linkedTo(id) {
    var found = {};
    var record = records[id];
    if (!record) { return found; }
    (record.links || []).forEach(function (link) {
      if (link.peerElement) { found[link.peerElement] = true; }
    });
    (record.endpoints || []).forEach(function (end) {
      if (end.element) { found[end.element] = true; }
    });
    return found;
  }

  /** Focus each link of the current element in turn.
   *
   * A cable is a first-class element -- it has a document, it can be deleted --
   * so there has to be a way to put the focus on one. Arrows are spent on nodes,
   * which is the common case, so the links of a node are a cycle of their own.
   */
  function cycleLink(delta) {
    var here = focused();
    if (!here) { return first({ quiet: false }); }
    var record = records[linkAnchor];
    var links = record ? (record.links || []) : [];
    var reachable = links.map(function (link) { return link.element; })
      .filter(function (element) { return !!records[element]; });
    if (!reachable.length) {
      announce("this element has no links", false);
      return false;
    }
    if (linkIndex < 0 && here.record.type === "edge") {
      linkIndex = reachable.indexOf(here.element);
    }
    // Arriving from the node rather than from another of its links: start at
    // whichever end the direction implies, so the first press never skips one.
    linkIndex = linkIndex < 0
      ? (delta > 0 ? 0 : reachable.length - 1)
      : (linkIndex + delta + reachable.length) % reachable.length;
    cycling = true;
    var ok = focus(reachable[linkIndex], { quiet: false });
    cycling = false;
    return ok;
  }

  /** Which node a link hangs off, for the purpose of cycling its siblings. */
  function nodeOf(record) {
    var ends = record.endpoints || [];
    for (var i = 0; i < ends.length; i++) {
      if (ends[i].element && records[ends[i].element]) { return ends[i].element; }
    }
    return null;
  }

  /* --------------------------------------------------- live announcements */

  /** Say something to a screen reader, once.
   *
   * `assertive` interrupts, and is for a refusal: "that was not applied" is not
   * something to hear after the next three things you did. Everything else is
   * polite. The text is re-set even when it has not changed, because two
   * identical announcements in a row is exactly what "undo, undo" produces and
   * both of them are worth hearing -- hence the clear-then-set.
   */
  function announce(text, assertive) {
    var region = assertive ? el.alert : el.announcer;
    if (!region || !text) { return; }
    region.textContent = "";
    // A live region only fires on a mutation the AT observes, and setting the
    // same string twice in one frame is not one.
    window.setTimeout(function () { region.textContent = String(text); }, 30);
  }

  return {
    attach: attach,
    annotate: annotate,
    label: labelOf,
    focus: focus,
    focused: focused,
    select: select,
    mark: mark,
    first: first,
    last: last,
    move: move,
    neighbour: neighbour,
    cycleLink: cycleLink,
    announce: announce,
    /** Every drawn element, for the palette's "go to" entries. */
    elements: function () {
      return order.map(function (id) { return { element: id, record: records[id] }; });
    }
  };
})();
