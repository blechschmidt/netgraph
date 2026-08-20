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
 * An annotation is not in here, and that is not an oversight. This set holds
 * *element addresses*, and an address is what /api/ops takes — so a bulk edit is
 * the selection posted verbatim. A note is not addressable that way: it is named
 * by a kind *and* a name, because a note called `core` may sit beside a switch
 * called `core` (§21). One in this set would be an address `element.set`,
 * `element.move` and every alignment would send to the server as an element and
 * be refused. The commentary keeps its own one-at-a-time selection in notes.js,
 * which is the same shape links.js has for the link it is routing.
 *
 * Focus is not selection. The focus ring says where the keyboard is; the
 * selection says what a command will act on. They coincide most of the time,
 * which is why every bulk command falls back to the focused element when
 * nothing is selected — but a diagram where Shift-clicking moved the keyboard,
 * or where arrowing changed what Delete would remove, would be neither.
 *
 * Dependency-free, like the rest of this page.
 */
window.netvizSelect = (function () {
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
  /** address -> the SVG ids drawing it, in graph order.
   *
   * A list and not a single id, because one address may be drawn as several
   * shapes: layer 3 gives a machine one node per network namespace (§23.1), so
   * a container host is its own box plus one per container, and all of them are
   * *that machine*. A click on any of them selects the host, the halo goes
   * round all of them, and a drag moves them together -- which is the only
   * reading that keeps the selection something /api/ops can be posted. */
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
    var ids = drawn[String(address || "")] || [];
    return ids.length ? records[ids[0]] || null : null;
  }

  /** Every SVG id an address is drawn as; empty when it is not drawn. */
  function shapesOf(address) { return drawn[String(address || "")] || []; }

  /** The one shape an address is focused and scrolled to by: the first drawn.
   *
   * The first is the element's own node, because a stack node is always minted
   * after the machine it is inside -- so focus lands on the machine and not on
   * whichever container happened to be declared first. */
  function shapeOf(address) { return shapesOf(address)[0] || null; }

  /** The address a detail record belongs to.
   *
   * `id` for everything that is its own thing, and the machine for a node that
   * is one network stack of one: `netns:hosts/srv-01:blue` is an identity the
   * graph mints and not one any document has, so selecting it would offer a
   * rename that `netviz edit` would refuse. See Node.address in
   * netviz/render/graph.py, which is where the server decides this. */
  function addressOf(record) {
    if (!record) { return ""; }
    return String(record.address || record.id || "");
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
    var here = window.netvizA11y.focused();
    var address = here ? addressOf(here.record) : "";
    return address ? [address] : [];
  }

  /** What was selected, in a form a status line or a live region can say. */
  function summary() {
    if (!picked.length) { return "nothing selected"; }
    if (picked.length === 1) { return picked[0] + " selected"; }
    return picked.length + " selected";
  }

  /* There used to be a `dangling()` here: the cables that would be left with an
   * end missing if the selection went, read off the records the drawing came
   * with. It was right about cables and blind to everything else a delete takes
   * — a tunnel over one of them, a note anchored to one, a group listing one,
   * the layout entries that placed all of it — because none of that is drawn.
   * `GET /api/cascade` answers with netviz.edit's own closure instead, so the
   * confirmation cannot disagree with what the write then does. See
   * session.js's `cascading`. */

  /** Note that the set moved: repaint it, mirror it, and tell app.js. */
  function settled(options) {
    var opts = options || {};
    paint();
    window.netvizA11y.mark(marked(), picked.length);
    if (!opts.quiet) {
      window.netvizA11y.announce(summary(), false);
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
      var address = addressOf(records[id]);
      if (!address) { return; }
      if (!drawn[address]) { drawn[address] = []; }
      drawn[address].push(id);
    });
    var kept = picked.filter(function (address) { return shapesOf(address).length > 0; });
    if (kept.length !== picked.length) {
      picked = kept;
      index = {};
      picked.forEach(function (address) { index[address] = true; });
      if (host && host.changed) { host.changed(picked.slice()); }
    }
    paint();
    window.netvizA11y.mark(marked(), picked.length);
  }

  /** Every SVG id the selection is drawn as, for the screen reader's outline. */
  function marked() {
    var ids = [];
    picked.forEach(function (one) { ids = ids.concat(shapesOf(one)); });
    return ids;
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
      Array.prototype.forEach.call(root.querySelectorAll("g.nv-selection"), function (stale) {
        stale.remove();
      });
    }
    if (!root || !picked.length) { return; }
    var window_ = window.netvizCull.viewportBox();
    var group = document.createElementNS(SVG_NS, "g");
    group.setAttribute("class", "nv-selection");
    group.setAttribute("aria-hidden", "true");
    var count = 0;
    for (var i = 0; i < picked.length && count < MAX_HALOS; i++) {
      // One ring per *shape*, not per address: a selected container host is
      // drawn as several boxes at layer 3 and a ring round one of them would
      // say the others were not selected.
      var shapes = shapesOf(picked[i]);
      for (var s = 0; s < shapes.length && count < MAX_HALOS; s++) {
        var box = window.netvizCull.boxOf(shapes[s]);
        if (!box || (window_ && !overlaps(box, window_))) { continue; }
        group.appendChild(halo(box));
        count += 1;
      }
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
    rect.setAttribute("class", "nv-halo");
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
    var here = window.netvizA11y.focused();
    if (!here) {
      var first = window.netvizA11y.first({ quiet: false });
      if (first) { fromFocus(); }
      return first;
    }
    if (!picked.length) { fromFocus(); }
    var next = window.netvizA11y.neighbour(direction);
    if (!next) {
      window.netvizA11y.announce("nothing to the " + direction, false);
      return false;
    }
    window.netvizA11y.focus(next, { quiet: true });
    var record = records[next];
    add([addressOf(record)]);
    return true;
  }

  /** Put the focused element into the selection, so a gesture has a seed. */
  function fromFocus() {
    var here = window.netvizA11y.focused();
    if (!here) { return false; }
    add([addressOf(here.record)], { quiet: true });
    return true;
  }

  /* ------------------------------------------------------- the rubber band */

  /* A drag on the paper is a band; a drag on a shape is a move where the shape
   * can be moved (see containers.js and notes.js) and a pan where it cannot —
   * a canvas where dragging a node did nothing at all would be worse than one
   * where it pans.
   *
   * None of it is reached while the hand tool is up: app.js decides that before
   * asking anything here, because "the pan tool pans" has to be true of every
   * press and not only of the ones nothing else wanted. */

  /** Is this press on the paper rather than on anything drawn on it?
   *
   * Exported because the pan tool needs the same answer for a different
   * reason: a press that goes nowhere is a click, and a click on the paper
   * clears the selection whichever tool made it. One hit test, so the two
   * tools cannot come to disagree about where the paper is.
   */
  function onPaper(event) {
    var target = event.target;
    return !!el && !(
      target && target.closest && target.closest("g.node, g.edge, .nv-handle, .nv-link-hit")
    );
  }

  /** Does this press start a band? Called before the pan arms itself. */
  function grab(event) {
    if (!el || event.button !== 0 || !onPaper(event)) { return false; }
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
    var matrix = window.netvizCull.matrix();
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
      var hit = shapesOf(address).some(function (id) {
        var found = window.netvizCull.boxOf(id);
        return !!found && found.x <= region.right && found.x + found.w >= region.left &&
          found.y <= region.bottom && found.y + found.h >= region.top;
      });
      if (hit) { caught.push(address); }
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
    var K = window.netvizKeys;

    K.define("select.all", {
      run: function () {
        var count = all();
        if (!count) { host.refuse("nothing is drawn to select"); }
      }
    });
    K.define("select.none", {
      run: function () {
        if (!clear()) { window.netvizA11y.announce("nothing was selected", false); }
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
     * out of this file to prove that every binding netviz declares has
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
          detail: record ? window.netvizA11y.label(record) : "selected",
          group: "selected",
          run: function () {
            var id = shapeOf(address);
            if (!id) { return; }
            el.canvas.focus();
            window.netvizA11y.focus(id, { quiet: false });
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
    window.netvizSession.arrange(command, chosen);
  }

  return {
    attach: attach,
    defineCommands: defineCommands,
    addressOf: addressOf,
    shapesOf: shapesOf,
    shapeOf: shapeOf,
    annotate: annotate,
    paint: paint,
    size: size,
    has: has,
    addresses: addresses,
    nodes: nodes,
    links: links,
    targets: targets,
    summary: summary,
    set: set,
    add: add,
    toggle: toggle,
    remove: remove,
    clear: clear,
    all: all,
    extend: extend,
    fromFocus: fromFocus,
    onPaper: onPaper,
    grab: grab,
    move: move,
    release: release,
    dragging: dragging
  };
})();
