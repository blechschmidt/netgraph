/* What the editor is acting on: a set, not a shape.
 *
 * Everything on this canvas used to happen to one element — the focused one.
 * That is enough for a diagram viewer and nowhere near enough for a diagram
 * *editor*: aligning is a claim about several things, deleting a rack is eleven
 * deletions somebody means as one, and "set spec.site on these twelve switches"
 * is the reason a bulk edit exists at all.
 *
 * So this file holds the selection, and holds it as **addresses**:
 *
 *   ["core/sw-a", "core/sw-b", "core/cbl-uplink"]
 *
 * Not DOM nodes, not SVG ids. The whole page is built on the drawing being
 * replaced wholesale on every render — a save, an undo, somebody else's edit —
 * and a selection of DOM nodes would be a selection of corpses a second later.
 * Addresses survive that, and they survive the thing an address is *for*: they
 * are what /api/ops takes, so a bulk edit is the selection posted verbatim.
 *
 * Three things follow from holding addresses rather than shapes, and they are
 * the three that make this file worth its length:
 *
 *   **It survives a re-render.** `annotate` re-resolves each address against
 *   the records the new drawing came with. What is no longer drawn — deleted,
 *   filtered out, on another layer — drops out; everything else stays selected
 *   with the ring in the same place.
 *
 *   **It survives culling.** cull.js empties the `<g>` of anything off screen,
 *   so a selected element may have no shape to draw a ring around. The halo is
 *   therefore drawn from cull.js's *box index* into an overlay of its own, one
 *   rectangle per element, clipped to what is on screen. Selecting a thousand
 *   devices and panning across them costs a rectangle per visible one.
 *
 *   **It is one thing, said twice.** The halo is for eyes; the outline in
 *   a11y.js is for a screen reader, and hears "3 selected" and which three.
 *   Neither is derived from the other — both are derived from this.
 *
 * Focus is not selection. The focus ring says where the keyboard is; the
 * selection says what a command will act on. They coincide most of the time,
 * which is why every bulk command falls back to the focused element when
 * nothing is selected — but a diagram where Shift-clicking moved the keyboard,
 * or where arrowing changed what Delete would remove, would be neither.
 *
 * Dependency-free, like the rest of this page.
 */
window.netgraphSelect = (function () {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";

  /** How far the pointer must travel before a press counts as a band and not a
   *  click on the paper. In client pixels. */
  var SLOP = 4;

  /** How much a halo rectangle is grown beyond the shape it marks, in drawing
   *  units, so the ring sits *outside* the node rather than on its border. */
  var PADDING = 4;

  /** Most halos drawn at once. Everything off screen is skipped first, so this
   *  only bites on a view zoomed out far enough that a thousand selected
   *  devices are all visible — at which point each is a few pixels across and a
   *  ring around it says nothing anyway. The count in the status line is what
   *  carries the meaning there. */
  var MAX_HALOS = 600;

  var host = null;
  var el = null;

  /** The selection, in the order it was made. Addresses, never DOM nodes. */
  var picked = [];
  /** address -> true, so `has` is not a scan. */
  var index = {};
  /** SVG id -> record, for whatever is drawn now. */
  var records = {};
  /** address -> SVG id, the other way round. */
  var drawn = {};
  /** The <g> the halos live in, or null. */
  var layer = null;
  /** The rubber band in flight, or null. */
  var band = null;

  /* --------------------------------------------------------------- attach */

  /** Take over selection. `bridge` is app.js's side:
   *
   *    el          the elements shared with app.js
   *    refuse(why) say no, visibly
   *    changed()   the selection moved; repaint whatever depends on it
   */
  function attach(bridge) {
    host = bridge;
    el = bridge.el;
    return true;
  }

  /* ------------------------------------------------------------- the set */

  function size() { return picked.length; }

  function addresses() { return picked.slice(); }

  function has(address) { return !!index[String(address || "")]; }

  /** Is this address a link rather than an element? */
  function isLink(address) {
    var record = recordFor(address);
    return !!record && record.type === "edge";
  }

  function nodes() { return picked.filter(function (one) { return !isLink(one); }); }

  function links() { return picked.filter(isLink); }

  /** The detail record behind an address, or null when it is not drawn. */
  function recordFor(address) {
    var id = drawn[String(address || "")];
    return id ? records[id] || null : null;
  }

  /** Replace the selection outright. */
  function set(list, options) {
    picked = [];
    index = {};
    add(list, options);
  }

  /** Add these addresses, keeping what was already there. */
  function add(list, options) {
    (list || []).forEach(function (address) {
      var text = String(address || "");
      if (!text || index[text]) { return; }
      index[text] = true;
      picked.push(text);
    });
    settled(options);
  }

  /** Add what is not selected, remove what is. The shift-click gesture. */
  function toggle(list, options) {
    (list || []).forEach(function (address) {
      var text = String(address || "");
      if (!text) { return; }
      if (index[text]) { remove([text], { quiet: true }); return; }
      index[text] = true;
      picked.push(text);
    });
    settled(options);
  }

  function remove(list, options) {
    (list || []).forEach(function (address) {
      var text = String(address || "");
      if (!index[text]) { return; }
      delete index[text];
      picked = picked.filter(function (one) { return one !== text; });
    });
    settled(options);
  }

  function clear(options) {
    if (!picked.length) { return false; }
    picked = [];
    index = {};
    settled(options);
    return true;
  }

  /** Everything the current view draws. `Ctrl-A`. */
  function all(options) {
    set(Object.keys(drawn), options);
    return picked.length;
  }

  /** The selection a bulk command should act on.
   *
   * The selection when there is one, and the focused element when there is not:
   * pressing Delete with a ring on a switch and nothing selected has always
   * deleted that switch, and a multi-select feature that broke it would be a
   * feature that made the editor worse for everybody who does not use it.
   */
  function targets() {
    if (picked.length) { return picked.slice(); }
    var here = window.netgraphA11y.focused();
    var address = here ? String(here.record.id || "") : "";
    return address ? [address] : [];
  }

  /** What was selected, in a form a status line or a live region can say. */
  function summary() {
    if (!picked.length) { return "nothing selected"; }
    if (picked.length === 1) { return picked[0] + " selected"; }
    return picked.length + " selected";
  }

  /** Every cable and tunnel that would dangle if the selection went.
   *
   * A link dies with either of its ends — that is the mutation layer's rule
   * (``netgraph edit delete`` refuses without ``--cascade`` and names them) —
   * so the confirmation has to say so *before* the deletion, not after. Read off
   * the records the drawing came with, which already carry each node's links.
   */
  function dangling() {
    var doomed = {};
    picked.forEach(function (address) { doomed[address] = true; });
    var found = [];
    nodes().forEach(function (address) {
      var record = recordFor(address);
      ((record && record.links) || []).forEach(function (link) {
        var peer = records[link.element];
        var id = peer && String(peer.id || "");
        if (!id || doomed[id] || found.indexOf(id) !== -1) { return; }
        found.push(id);
      });
    });
    return found;
  }

  /** Note that the set moved: repaint it, mirror it, and tell app.js. */
  function settled(options) {
    var opts = options || {};
    paint();
    window.netgraphA11y.mark(picked.map(function (one) { return drawn[one]; }).filter(Boolean),
      picked.length);
    if (!opts.quiet) {
      window.netgraphA11y.announce(summary(), false);
    }
    if (host && host.changed) { host.changed(picked.slice()); }
  }

  /* ------------------------------------------------------- after a render */

  /** Adopt a new drawing, keeping every address it still draws.
   *
   * The one place the selection may shrink without anybody asking: an element
   * that is no longer drawn cannot be acted on, and keeping it selected would
   * mean a Delete that named something the diagram does not show.
   */
  function annotate(details) {
    records = details || {};
    drawn = {};
    layer = null;
    Object.keys(records).forEach(function (id) {
      var address = String(records[id].id || "");
      if (address) { drawn[address] = id; }
    });
    var kept = picked.filter(function (address) { return !!drawn[address]; });
    if (kept.length !== picked.length) {
      picked = kept;
      index = {};
      picked.forEach(function (address) { index[address] = true; });
      if (host && host.changed) { host.changed(picked.slice()); }
    }
    paint();
    window.netgraphA11y.mark(picked.map(function (one) { return drawn[one]; }).filter(Boolean),
      picked.length);
  }

  /* ------------------------------------------------------------ painting */

  /** Draw the halo: one rectangle per selected element that is on screen.
   *
   * Into an overlay rather than onto the shapes themselves, for the reason this
   * whole file exists twice over: a culled element has no shape to put a class
   * on, and it is still selected.
   */
  function paint() {
    var svg = el && el.viewport.firstElementChild;
    var root = svg && (svg.querySelector("g#graph0, g.graph") || svg.firstElementChild);
    layer = null;
    // Every one of them, not merely the one this file is holding: a repaint that
    // reuses the drawing already on screen -- which is what keeps the pan and
    // the zoom when a view is switched back to -- leaves the previous overlay
    // in the SVG, and each repaint would otherwise add another.
    if (root) {
      Array.prototype.forEach.call(root.querySelectorAll("g.ng-selection"), function (stale) {
        stale.remove();
      });
    }
    if (!root || !picked.length) { return; }
    var window_ = window.netgraphCull.viewportBox();
    var group = document.createElementNS(SVG_NS, "g");
    group.setAttribute("class", "ng-selection");
    group.setAttribute("aria-hidden", "true");
    var count = 0;
    for (var i = 0; i < picked.length && count < MAX_HALOS; i++) {
      var box = window.netgraphCull.boxOf(drawn[picked[i]]);
      if (!box || (window_ && !overlaps(box, window_))) { continue; }
      group.appendChild(halo(box));
      count += 1;
    }
    if (!count) { return; }
    root.appendChild(group);
    layer = group;
  }

  function overlaps(box, window_) {
    return box.x <= window_.right && box.x + box.w >= window_.left &&
      box.y <= window_.bottom && box.y + box.h >= window_.top;
  }

  function halo(box) {
    var rect = document.createElementNS(SVG_NS, "rect");
    rect.setAttribute("class", "ng-halo");
    rect.setAttribute("x", String(box.x - PADDING));
    rect.setAttribute("y", String(box.y - PADDING));
    rect.setAttribute("width", String(Math.max(box.w + 2 * PADDING, 1)));
    rect.setAttribute("height", String(Math.max(box.h + 2 * PADDING, 1)));
    return rect;
  }

  /* -------------------------------------------------------- the keyboard */

  /** Extend the selection to the neighbour in `direction`, and follow it.
   *
   * The same neighbour search the arrow keys use — a11y.js's, which prefers an
   * element this one is *linked to* — so Shift-arrow walks a path and collects
   * it, which is how a trunk and everything hanging off it gets selected
   * without a pointer.
   */
  function extend(direction) {
    var here = window.netgraphA11y.focused();
    if (!here) {
      var first = window.netgraphA11y.first({ quiet: false });
      if (first) { fromFocus(); }
      return first;
    }
    if (!picked.length) { fromFocus(); }
    var next = window.netgraphA11y.neighbour(direction);
    if (!next) {
      window.netgraphA11y.announce("nothing to the " + direction, false);
      return false;
    }
    window.netgraphA11y.focus(next, { quiet: true });
    var record = records[next];
    add([String((record && record.id) || "")]);
    return true;
  }

  /** Put the focused element into the selection, so a gesture has a seed. */
  function fromFocus() {
    var here = window.netgraphA11y.focused();
    if (!here) { return false; }
    add([String(here.record.id || "")], { quiet: true });
    return true;
  }

  /* ------------------------------------------------------- the rubber band */

  /* A drag on the paper is a band; a drag on a shape is still a pan. That split
   * is deliberate and it is the one draw.io users expect the other way round —
   * but the shapes on this canvas are not draggable yet (that is the *next*
   * task), and a canvas where dragging a node did nothing at all would be worse
   * than one where it pans. When direct manipulation lands, the shape branch
   * becomes "move it" and this one does not change. */

  /** Does this press start a band? Called before the pan arms itself. */
  function grab(event) {
    if (!el || event.button !== 0) { return false; }
    if (event.target.closest && event.target.closest("g.node, g.edge, .ng-handle, .ng-link-hit")) {
      return false;
    }
    band = {
      from: { x: event.clientX, y: event.clientY },
      to: { x: event.clientX, y: event.clientY },
      // Held down, the band adds to the selection instead of replacing it: the
      // same modifier that makes a click additive.
      additive: !!(event.shiftKey || event.ctrlKey || event.metaKey),
      moved: false,
      node: null
    };
    return true;
  }

  function dragging() { return !!band; }

  function move(event) {
    if (!band) { return; }
    band.to = { x: event.clientX, y: event.clientY };
    if (Math.abs(band.to.x - band.from.x) > SLOP || Math.abs(band.to.y - band.from.y) > SLOP) {
      band.moved = true;
    }
    if (!band.moved) { return; }
    if (!band.node) {
      band.node = document.createElement("div");
      band.node.className = "rubber";
      el.canvas.appendChild(band.node);
    }
    var frame = el.canvas.getBoundingClientRect();
    var box = clientBox(band);
    band.node.style.left = (box.left - frame.left) + "px";
    band.node.style.top = (box.top - frame.top) + "px";
    band.node.style.width = box.width + "px";
    band.node.style.height = box.height + "px";
  }

  /** Finish the band. Returns true when it actually selected something. */
  function release() {
    if (!band) { return false; }
    var finished = band;
    band = null;
    if (finished.node && finished.node.parentNode) {
      finished.node.parentNode.removeChild(finished.node);
    }
    if (!finished.moved) {
      // A click on the paper, not a band. Clearing is the honest reading, and
      // it is what every diagram editor does.
      if (!finished.additive) { clear({ quiet: true }); }
      return false;
    }
    var caught = within(clientBox(finished));
    if (finished.additive) { add(caught); } else { set(caught); }
    return true;
  }

  /** A band's rectangle in client pixels, whichever way it was dragged.
   *
   * Takes the band rather than reading the module's, because `release` has
   * already cleared that by the time it asks -- a drag is over the moment the
   * button comes up, and only then is there a rectangle to resolve.
   */
  function clientBox(one) {
    var left = Math.min(one.from.x, one.to.x);
    var top = Math.min(one.from.y, one.to.y);
    return {
      left: left,
      top: top,
      width: Math.abs(one.to.x - one.from.x),
      height: Math.abs(one.to.y - one.from.y),
      right: left + Math.abs(one.to.x - one.from.x),
      bottom: top + Math.abs(one.to.y - one.from.y)
    };
  }

  /** Every element whose box the band touches.
   *
   * Against cull.js's index rather than against the DOM, which is the only
   * answer that exists for an element whose contents are parked — and the only
   * one that does not force a layout per element per drag.
   */
  function within(box) {
    var matrix = window.netgraphCull.matrix();
    if (!matrix) { return []; }
    var inverse = matrix.inverse();
    var corners = [
      at(inverse, box.left, box.top),
      at(inverse, box.right, box.top),
      at(inverse, box.left, box.bottom),
      at(inverse, box.right, box.bottom)
    ];
    var xs = corners.map(function (one) { return one.x; });
    var ys = corners.map(function (one) { return one.y; });
    var region = {
      left: Math.min.apply(null, xs),
      right: Math.max.apply(null, xs),
      top: Math.min.apply(null, ys),
      bottom: Math.max.apply(null, ys)
    };
    var caught = [];
    Object.keys(drawn).forEach(function (address) {
      var found = window.netgraphCull.boxOf(drawn[address]);
      if (!found) { return; }
      if (found.x <= region.right && found.x + found.w >= region.left &&
          found.y <= region.bottom && found.y + found.h >= region.top) {
        caught.push(address);
      }
    });
    return caught;
  }

  function at(matrix, x, y) {
    return {
      x: matrix.a * x + matrix.c * y + matrix.e,
      y: matrix.b * x + matrix.d * y + matrix.f
    };
  }

  /* ------------------------------------------------------------ commands */

  /** Register the selection's own commands, and the tidying that needs one. */
  function defineCommands() {
    var K = window.netgraphKeys;

    K.define("select.all", {
      run: function () {
        var count = all();
        if (!count) { host.refuse("nothing is drawn to select"); }
      }
    });
    K.define("select.none", {
      run: function () {
        if (!clear()) { window.netgraphA11y.announce("nothing was selected", false); }
      },
      enabled: function () { return picked.length ? true : "nothing is selected"; }
    });
    K.define("select.extend", {
      run: function (context) {
        var direction = {
          "Shift-ArrowRight": "right", "Shift-ArrowLeft": "left",
          "Shift-ArrowUp": "up", "Shift-ArrowDown": "down"
        }[context.chord];
        if (direction) { extend(direction); }
      }
    });

    /* One registration per command rather than a loop over a table: the id has
     * to be a literal here, because tests/test_web.py reads the registrations
     * out of this file to prove that every binding netgraph declares has
     * something behind it, and a loop would hide nine of them from that check. */
    K.define("align.left", { run: function () { arrange("align.left"); }, enabled: several });
    K.define("align.centre", { run: function () { arrange("align.centre"); }, enabled: several });
    K.define("align.right", { run: function () { arrange("align.right"); }, enabled: several });
    K.define("align.top", { run: function () { arrange("align.top"); }, enabled: several });
    K.define("align.middle", { run: function () { arrange("align.middle"); }, enabled: several });
    K.define("align.bottom", { run: function () { arrange("align.bottom"); }, enabled: several });
    K.define("distribute.horizontal", {
      run: function () { arrange("distribute.horizontal"); },
      enabled: function () { return atLeast(3); }
    });
    K.define("distribute.vertical", {
      run: function () { arrange("distribute.vertical"); },
      enabled: function () { return atLeast(3); }
    });
    K.define("geometry.snap", {
      run: function () { arrange("snap"); },
      enabled: function () { return atLeast(1); }
    });

    K.provide("selection", function () {
      return picked.map(function (address) {
        var record = recordFor(address);
        return {
          id: address,
          title: address,
          detail: record ? window.netgraphA11y.label(record) : "selected",
          group: "selected",
          run: function () {
            var id = drawn[address];
            if (!id) { return; }
            el.canvas.focus();
            window.netgraphA11y.focus(id, { quiet: false });
          }
        };
      });
    });
  }

  /** Why an alignment cannot run: it needs two things to agree about. */
  function several() { return atLeast(2); }

  function atLeast(count) {
    var chosen = targets().length;
    if (chosen >= count) { return true; }
    return count === 1
      ? "select an element first"
      : "select at least " + count + " elements first (drag on the canvas, or shift-click)";
  }

  /** Post one tidying to the server, which owns the arithmetic and the files. */
  function arrange(command) {
    var chosen = targets();
    if (!chosen.length) { host.refuse("nothing is selected"); return; }
    window.netgraphSession.arrange(command, chosen);
  }

  return {
    attach: attach,
    defineCommands: defineCommands,
    annotate: annotate,
    paint: paint,
    size: size,
    has: has,
    addresses: addresses,
    nodes: nodes,
    links: links,
    targets: targets,
    dangling: dangling,
    summary: summary,
    set: set,
    add: add,
    toggle: toggle,
    remove: remove,
    clear: clear,
    all: all,
    extend: extend,
    fromFocus: fromFocus,
    grab: grab,
    move: move,
    release: release,
    dragging: dragging
  };
})();
