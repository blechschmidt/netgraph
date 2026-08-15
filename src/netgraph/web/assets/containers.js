/* Namespaces as containers: the boxes you drag things into (§2).
 *
 * A folder is a namespace and a namespace is a folder, so the box drawn around
 * one is not decoration -- it is the boundary of a directory on disk. That is
 * what makes draw.io's defining gesture available here without inventing a
 * concept: dropping a switch inside the rack's rectangle *is*
 * `netgraph edit move`, and the file moves with it.
 *
 * Four things live in this file, and they are all the same object seen from
 * different sides:
 *
 *   the frame     one rectangle per namespace level, drawn as an overlay
 *   the header    its name, how many elements are in it, and a fold triangle
 *   the drop      dragging an element (or a selection) from one frame to another
 *   the resize    four corner handles, written to the layout document's `groups`
 *
 * **Why the frames are drawn here rather than read off the SVG.** Graphviz
 * boxes the namespaces that hold elements *directly* -- it has no reason to draw
 * `sites` around three sites that each have their own box -- and under a fixed
 * arrangement it draws no clusters at all, the rectangles being painted into the
 * graph's `_background` with no id to click. Meanwhile the editing gesture needs
 * *every* level: `sites/south` is a legal place to drop a switch whether or not
 * it holds one already. So the server publishes a `containers` list beside
 * `geometry` -- one entry per level, with the members whose hull each frame
 * follows -- and this file draws from that. Exactly the bargain notes.js makes
 * for an annotation area, for the same reason.
 *
 * **What `boxed` means, and why a resize is refused without it.** A namespace
 * Graphviz boxes is one `netgraph.render.dot.cluster_keys` names, and only such
 * a namespace has anywhere for a resize to go: its rectangle is stored under
 * that key in the layout document's `groups` and drawn back from it. A level in
 * between follows whatever is under it and has no stored box, so it is a drop
 * target and a fold target but not a resize target -- and the page says which,
 * rather than writing a number the next render would ignore.
 *
 * **Folding is a view, not an edit.** The triangle on a header re-renders with
 * `collapse=<namespace>`, which is `netgraph render --collapse` and
 * `netgraph.render.aggregate.collapse_namespaces`. It writes nothing: how much
 * of a diagram somebody wants to look at is not a fact about the network.
 *
 * Coordinates: the overlay is drawn in the SVG's own user space, which is what
 * `netgraphCull.boxOf` answers in, so nothing is converted to draw. A *write*
 * is in netgraph's -- points, `y` upwards -- so the offset between the two is
 * measured off the nodes exactly as links.js and notes.js measure it.
 */
window.netgraphContainers = (function () {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";
  /** Radius of a corner handle, in SVG user units. Matches notes.js. */
  var HANDLE = 5;
  /** How far a press may travel before it is a drag and not a click. */
  var SLOP = 3;
  /** Smallest container a resize may leave, in points. */
  var MIN_SIZE = 40;
  /** How tall a header band is, in SVG user units, before the drawing's scale
   *  is taken into account. The caption sits inside it. */
  var HEADER = 15;
  /** Font size of a header caption, in SVG user units. */
  var CAPTION = 10;
  /** Space between a frame and what is inside it, in SVG user units. */
  var PADDING = 8;
  /** How many levels of nesting a frame leaves room for below itself. Each one
   *  costs a header's height plus the padding, so that a site's caption and the
   *  caption of the rack inside it never land on the same line. */
  var NESTING = 4;

  var ctx = null;
  /** The frames, behind the drawing. */
  var host = null;
  /** The handles and the drop highlight, in front of it. Two groups rather than
   *  one because they belong at opposite ends of the z-order: a frame is the
   *  paper its members sit on, and a grab handle nobody can click because a
   *  cluster's own outline is drawn over it is not a handle. */
  var tools = null;
  var frame = {
    root: null,
    geometry: null,
    byNs: {},
    order: [],
    drawn: {},
    offset: [0, 0],
    grouped: false
  };
  /** Namespaces the reader has folded. A view, so it survives a re-render and
   *  is never written; see the file header. */
  var folded = {};
  /** The namespace whose frame is selected, or null. */
  var picked = null;
  /** The gesture in flight, or null. */
  var drag = null;

  function attach(context) { ctx = context; }

  /* ------------------------------------------------------------- the frame */

  /** Rebuild the container overlay for a drawing just put on screen.
   *
   * `containers` is the payload described at the top of this file, or an empty
   * list when the drawing is not grouped by namespace -- in which case this
   * layer draws nothing and claims no gesture, and the canvas pans from a node
   * as it always did.
   */
  function annotate(root, geometry, containers, details) {
    host = null;
    tools = null;
    drag = null;
    var entries = containers || [];
    frame = {
      root: root || null,
      geometry: geometry || null,
      byNs: {},
      order: [],
      drawn: {},
      offset: [0, 0],
      grouped: entries.length > 0
    };
    var graph = root && root.querySelector("g.graph");
    if (graph) {
      Array.prototype.forEach.call(
        graph.querySelectorAll("g.ng-containers, g.ng-container-tools"),
        function (stale) { stale.remove(); }
      );
    }
    Object.keys(details || {}).forEach(function (id) {
      var address = String(details[id].id || "");
      if (address) { frame.drawn[address] = id; }
    });
    entries.forEach(function (entry) {
      frame.byNs[entry.namespace] = entry;
      frame.order.push(entry.namespace);
    });
    // A namespace that has stopped existing -- its last element moved out --
    // must not stay folded, or unfolding it would be impossible.
    Object.keys(folded).forEach(function (namespace) {
      if (!frame.byNs[namespace]) { delete folded[namespace]; }
    });
    if (!graph || !frame.grouped) { picked = null; return; }
    frame.offset = measureOffset(graph, geometry, details || {});
    host = document.createElementNS(SVG_NS, "g");
    host.setAttribute("class", "ng-containers");
    // Furniture, not content: every name and count the overlay shows is in the
    // element list a screen reader already walks. See a11y.js.
    host.setAttribute("aria-hidden", "true");
    // Behind the nodes rather than over them: a container is the paper its
    // members sit on, and a rectangle painted on top of a switch is a rectangle
    // you cannot click the switch through. Before the first *group* rather than
    // before the first child, though -- Graphviz's own opaque background
    // polygon is that first child, and an overlay behind it is invisible and
    // unclickable, which is a bug you only find by trying to click one.
    var first = graph.querySelector("g");
    if (first) { graph.insertBefore(host, first); } else { graph.appendChild(host); }
    tools = document.createElementNS(SVG_NS, "g");
    tools.setAttribute("class", "ng-container-tools");
    tools.setAttribute("aria-hidden", "true");
    graph.appendChild(tools);
    if (picked && !frame.byNs[picked]) { picked = null; }
    hideClusterLabels();
    paint();
  }

  /** Take Graphviz's own caption off every cluster this file captions.
   *
   * Otherwise a namespace is named twice, a few points apart: once inside the
   * top of the cluster by the renderer and once in the header drawn above it.
   * Hidden rather than suppressed in the DOT, because the same drawing is what
   * `netgraph render` writes to a file — where there is no header and the
   * caption is the only thing naming the box.
   */
  function hideClusterLabels() {
    visible().forEach(function (entry) {
      var shape = entry.element && !entry.collapsed && shapeOf(entry.element);
      if (!shape || shape.id.indexOf("cluster") !== 0) { return; }
      Array.prototype.forEach.call(shape.querySelectorAll("text"), function (caption) {
        caption.setAttribute("display", "none");
      });
    });
  }

  /** Which namespaces are folded, for the render request. app.js asks. */
  function collapsed() {
    return frame.order.filter(function (namespace) { return !!folded[namespace]; });
  }

  /** Graphviz translates a drawing whose bounding box does not start at the
   *  origin. Measured from the nodes rather than assumed; the same measurement
   *  notes.js makes, and made again here because a grouped drawing with no
   *  routable link in it still has containers to write. */
  function measureOffset(graph, geometry, details) {
    var nodes = (geometry && geometry.nodes) || {};
    var found = [0, 0];
    Array.prototype.some.call(graph.querySelectorAll("g.node"), function (group) {
      var record = details[group.id];
      var placed = record && nodes[record.id];
      if (!placed) { return false; }
      var shape = group.querySelector("ellipse, polygon, rect");
      if (!shape) { return false; }
      var box = shape.getBBox();
      found = [box.x + box.width / 2 - placed.x, box.y + box.height / 2 + placed.y];
      return true;
    });
    return found;
  }

  /** An SVG point in the graph's coordinates. */
  function toGraph(x, y) {
    return [x - frame.offset[0], -(y - frame.offset[1])];
  }

  /** Is this drawing arranged, so a rectangle written for it is honoured?
   *
   * The question links.js and notes.js ask before offering a handle, and the
   * answer matters more here: under an automatic layout Graphviz sizes a cluster
   * to fit its members and ignores the stored box entirely, so a resize would
   * write a number nothing reads.
   */
  function arranged() {
    return !!(frame.geometry && frame.geometry.mode === "fixed");
  }

  /* ------------------------------------------------------------- the boxes */

  /** Where a container is drawn, in SVG user space, or null.
   *
   * Three answers in the order they are trusted, mirroring notes.js: the shape
   * the renderer drew for it (a cluster, or the single node a folded one became),
   * then the hull of its members, and nothing at all for a container none of
   * whose members are on the page.
   */
  function boxOf(entry) {
    var shape = entry.element && shapeOf(entry.element);
    if (shape) {
      var box = null;
      try { box = shape.getBBox(); } catch (error) { box = null; }
      if (box && (box.width || box.height)) {
        return { x: box.x, y: box.y, w: box.width, h: box.height };
      }
    }
    return hullOf(entry);
  }

  /** The rectangle a container's drawn members occupy, with room for a header.
   *
   * Read off the box index rather than the DOM, so a container most of whose
   * members are culled off screen still has a frame; see cull.js.
   */
  function hullOf(entry) {
    var left = null, right = null, top = null, bottom = null;
    (entry.members || []).forEach(function (member) {
      var id = frame.drawn[member];
      var box = id && window.netgraphCull.boxOf(id);
      if (!box) { return; }
      left = left === null ? box.x : Math.min(left, box.x);
      top = top === null ? box.y : Math.min(top, box.y);
      right = right === null ? box.x + box.w : Math.max(right, box.x + box.w);
      bottom = bottom === null ? box.y + box.h : Math.max(bottom, box.y + box.h);
    });
    if (left === null) { return null; }
    // Padded by one step per level of nesting, so a site's frame clears the
    // racks inside it instead of tracing them -- and clears them by more than a
    // header is tall, or the two captions would sit on top of each other. The
    // same idea as an annotation area's `spec.padding`, derived rather than
    // declared because a namespace does not carry one.
    var pad = PADDING + Math.max(0, NESTING - (entry.depth || 1)) * (HEADER + PADDING);
    return { x: left - pad, y: top - pad, w: right - left + pad * 2, h: bottom - top + pad * 2 };
  }

  function shapeOf(id) {
    if (!frame.root || !frame.root.querySelector) { return null; }
    return frame.root.querySelector('g[id="' + escapeId(id) + '"]');
  }

  function escapeId(value) {
    return window.CSS && window.CSS.escape ? window.CSS.escape(value) : value;
  }

  /** Where it is right now: the drag's rectangle while one is in flight. */
  function liveBox(entry) {
    return drag && drag.namespace === entry.namespace && drag.box ? drag.box : boxOf(entry);
  }

  /** Every container that is on the page, outermost first.
   *
   * Outermost first so the frames nest visually, and so a hit test walking the
   * list backwards finds the *innermost* container under the pointer, which is
   * the one a drop means.
   */
  function visible() {
    return frame.order
      .filter(function (namespace) {
        var entry = frame.byNs[namespace];
        return entry && !entry.hidden;
      })
      .map(function (namespace) { return frame.byNs[namespace]; })
      .sort(function (a, b) { return a.depth - b.depth; });
  }

  /* -------------------------------------------------------------- painting */

  function paint() {
    if (!host || !tools) { return; }
    while (host.firstChild) { host.removeChild(host.firstChild); }
    while (tools.firstChild) { tools.removeChild(tools.firstChild); }
    visible().forEach(function (entry) {
      var box = liveBox(entry);
      if (!box) { return; }
      host.appendChild(rectangle(entry, box));
      header(entry, box).forEach(function (node) { host.appendChild(node); });
    });
    if (drag && drag.mode === "drop" && drag.over !== null) {
      var target = frame.byNs[drag.over];
      var over = target && liveBox(target);
      if (over) { tools.appendChild(highlight(over)); }
    }
    var chosen = picked && frame.byNs[picked];
    if (!chosen || !ctx || !ctx.writable()) { return; }
    var outline = liveBox(chosen);
    if (!outline || !chosen.boxed || !arranged()) { return; }
    corners(outline).forEach(function (node) { tools.appendChild(node); });
  }

  function rectangle(entry, box) {
    var node = document.createElementNS(SVG_NS, "rect");
    node.setAttribute("class",
      "ng-container" + (entry.namespace === picked ? " ng-container-picked" : ""));
    node.setAttribute("x", String(round(box.x)));
    node.setAttribute("y", String(round(box.y)));
    node.setAttribute("width", String(Math.max(round(box.w), 1)));
    node.setAttribute("height", String(Math.max(round(box.h), 1)));
    node.setAttribute("data-container", entry.namespace);
    node.setAttribute("data-depth", String(entry.depth));
    return node;
  }

  /** The caption band: a fold triangle, the name, and how much is inside.
   *
   * The count is the point of the header. A folded container is a single node
   * with a label; an open one is a rectangle whose contents you can see but not
   * count, and "12 elements" is the fact a reader of a large diagram wants and
   * cannot get any other way.
   */
  function header(entry, box) {
    var band = document.createElementNS(SVG_NS, "rect");
    band.setAttribute("class", "ng-container-header");
    band.setAttribute("x", String(round(box.x)));
    band.setAttribute("y", String(round(box.y - HEADER)));
    band.setAttribute("width", String(Math.max(round(box.w), 1)));
    band.setAttribute("height", String(HEADER));
    band.setAttribute("data-container", entry.namespace);

    var toggle = document.createElementNS(SVG_NS, "text");
    toggle.setAttribute("class", "ng-container-toggle");
    toggle.setAttribute("x", String(round(box.x + 5)));
    toggle.setAttribute("y", String(round(box.y - 4)));
    toggle.setAttribute("font-size", String(CAPTION));
    toggle.setAttribute("data-toggle", entry.namespace);
    // A right-pointing triangle for a folded container and a down-pointing one
    // for an open one, which is the disclosure idiom every file tree uses.
    toggle.textContent = entry.collapsed ? "▸" : "▾";

    var caption = document.createElementNS(SVG_NS, "text");
    caption.setAttribute("class", "ng-container-label");
    caption.setAttribute("x", String(round(box.x + 16)));
    caption.setAttribute("y", String(round(box.y - 4)));
    caption.setAttribute("font-size", String(CAPTION));
    caption.setAttribute("data-container", entry.namespace);
    caption.textContent = entry.label + "  ·  " + counted(entry.count);
    return [band, toggle, caption];
  }

  function counted(count) {
    return count + (count === 1 ? " element" : " elements");
  }

  function highlight(box) {
    var node = document.createElementNS(SVG_NS, "rect");
    node.setAttribute("class", "ng-container-drop");
    node.setAttribute("x", String(round(box.x)));
    node.setAttribute("y", String(round(box.y)));
    node.setAttribute("width", String(Math.max(round(box.w), 1)));
    node.setAttribute("height", String(Math.max(round(box.h), 1)));
    return node;
  }

  function corners(box) {
    return ["nw", "ne", "se", "sw"].map(function (which) {
      var node = document.createElementNS(SVG_NS, "circle");
      node.setAttribute("class", "ng-container-handle");
      node.setAttribute("cx", String(round(
        which === "ne" || which === "se" ? box.x + box.w : box.x)));
      node.setAttribute("cy", String(round(
        which === "se" || which === "sw" ? box.y + box.h : box.y)));
      node.setAttribute("r", String(HANDLE));
      node.setAttribute("data-handle", "container");
      node.setAttribute("data-which", which);
      return node;
    });
  }

  function round(value) { return Math.round(value * 100) / 100; }

  /* ------------------------------------------------------------- hit tests */

  /** The container a client point is inside, innermost first, or null. */
  function containerAt(clientX, clientY) {
    var point = toUser(clientX, clientY);
    if (!point) { return null; }
    var found = null;
    visible().forEach(function (entry) {
      var box = boxOf(entry);
      if (!box) { return; }
      // A hair of tolerance on every side: a press aimed at a corner handle
      // lands *on* the boundary, and "just outside by a hundredth of a point"
      // is not an answer anybody meant.
      if (point[0] < box.x - HANDLE || point[0] > box.x + box.w + HANDLE) { return; }
      if (point[1] < box.y - HEADER || point[1] > box.y + box.h + HANDLE) { return; }
      // Later wins: `visible` is outermost first, so the last match is the
      // deepest namespace the pointer is in, which is the one a drop means.
      found = entry;
    });
    return found;
  }

  /** A client point in the SVG's user space, or null when nothing is drawn. */
  function toUser(clientX, clientY) {
    var owner = host && host.ownerSVGElement;
    var matrix = host && host.getScreenCTM();
    if (!owner || !matrix) { return null; }
    var point = owner.createSVGPoint();
    point.x = clientX;
    point.y = clientY;
    var local = point.matrixTransform(matrix.inverse());
    return [local.x, local.y];
  }

  /** What an event landed on, as a container record, or null. Used by menu.js
   *  so that right-clicking a namespace frame offers the container's own rows. */
  function at(target, event) {
    if (!frame.grouped) { return null; }
    if (target && target.closest) {
      var band = target.closest("[data-container]");
      if (band) { return frame.byNs[band.getAttribute("data-container")] || null; }
    }
    return event ? containerAt(event.clientX, event.clientY) : null;
  }

  /** The selected container, or null. */
  function selection() { return (picked && frame.byNs[picked]) || null; }

  /** Select one by namespace, or clear with null. */
  function select(namespace) {
    var wanted = namespace && frame.byNs[namespace] ? namespace : null;
    if (wanted === picked) { return wanted; }
    picked = wanted;
    paint();
    return wanted;
  }

  /* -------------------------------------------------------------- folding */

  /** Fold or unfold one container. A view gesture: nothing is written.
   *
   * The re-render is app.js's, because `collapse` is a render option like the
   * layer and the VLAN filter and there is one place that asks for a drawing.
   */
  function fold(namespace, wanted) {
    var entry = frame.byNs[namespace];
    if (!entry) { return false; }
    var now = wanted === undefined ? !folded[namespace] : !!wanted;
    if (now) { folded[namespace] = true; } else { delete folded[namespace]; }
    if (ctx) {
      ctx.say((now ? "folded " : "unfolded ") + namespace
        + (now ? " into one node; nothing was written" : ""));
      ctx.rerender();
    }
    return true;
  }

  /** The command behind `f`, the palette entry and the container menu row.
   *
   * Falls back to the namespace of whatever the *keyboard* has focused, because
   * a container frame is not in the diagram's focus order — there is no way to
   * arrow onto one — and a command only a mouse can reach is a command a screen
   * reader user does not have. Focusing a switch and pressing `f` folds the
   * namespace it is in, which is the reading of "fold this" from the keyboard.
   */
  function toggle(address) {
    var entry = selection() || frame.byNs[namespaceOf(String(address || ""))];
    if (!entry) {
      if (ctx) {
        ctx.refuse(frame.grouped
          ? "nothing names a namespace here: click a container frame, or focus an "
            + "element inside one"
          : "the diagram is not grouped by namespace, so it has no containers: "
            + "turn on 'group' (Alt-G) to draw them");
      }
      return false;
    }
    return fold(entry.namespace);
  }

  /* ------------------------------------------------------------- gestures */

  /** Does this press start something this layer owns?
   *
   * Three, in the order they win: a corner handle of the selected container, an
   * element being dragged out of one, and the container's own frame. Everything
   * is refused outright when the drawing is not grouped, which is what leaves
   * the canvas panning from a node exactly as it did before containers existed.
   */
  function grab(event) {
    if (!ctx || !ctx.writable() || !frame.grouped || !host) { return false; }
    var target = event.target;
    if (target && target.classList && target.classList.contains("ng-container-handle")) {
      var chosen = selection();
      if (!chosen) { return false; }
      beginResize(chosen, target.getAttribute("data-which"), event);
      return true;
    }
    var triangle = target && target.closest && target.closest("[data-toggle]");
    if (triangle) {
      // Claimed so the press does not pan, and acted on when the button comes
      // up in the same place -- a fold is a click, not a drag.
      drag = {
        mode: "fold",
        namespace: triangle.getAttribute("data-toggle"),
        moved: false,
        origin: [event.clientX, event.clientY]
      };
      return true;
    }
    var shape = target && target.closest && target.closest("g.node");
    var record = shape && ctx.recordAt(shape);
    if (record && record.type !== "edge" && String(record.id || "").indexOf("#") === -1) {
      beginDrop(record, event);
      return true;
    }
    var entry = at(target, event);
    if (entry && onEdgeOf(entry, event)) {
      select(entry.namespace);
      beginDrop(null, event, entry);
      return true;
    }
    return false;
  }

  /** Is the pointer on the frame itself rather than deep inside it?
   *
   * A press in the middle of a site's rectangle is a press on the paper and has
   * to keep panning the diagram; a press on its border or its header is a press
   * on the container. The same distinction notes.js draws with its hit band.
   */
  function onEdgeOf(entry, event) {
    var point = toUser(event.clientX, event.clientY);
    var box = boxOf(entry);
    if (!point || !box) { return false; }
    if (point[1] < box.y) { return true; }
    var edge = Math.min(
      Math.abs(point[0] - box.x),
      Math.abs(point[0] - (box.x + box.w)),
      Math.abs(point[1] - box.y),
      Math.abs(point[1] - (box.y + box.h))
    );
    return edge <= HANDLE * 2;
  }

  /** Start dragging something towards a container.
   *
   * `record` is the element that was grabbed, or null when a whole container is
   * being dragged. What travels is the *selection* when the grabbed element is
   * part of one, which is what makes a multi-selection drop in one gesture.
   */
  function beginDrop(record, event, entry) {
    var addresses;
    if (entry) {
      addresses = [entry.namespace];
    } else {
      var address = String(record.id || "");
      addresses = window.netgraphSelect.has(address)
        ? window.netgraphSelect.addresses()
        : [address];
    }
    drag = {
      mode: "drop",
      moved: false,
      origin: [event.clientX, event.clientY],
      addresses: addresses,
      from: entry ? entry.namespace : namespaceOf(String(record.id || "")),
      over: null,
      box: null,
      namespace: null
    };
  }

  function namespaceOf(address) {
    var cut = address.lastIndexOf("/");
    return cut === -1 ? "" : address.slice(0, cut);
  }

  function beginResize(entry, which, event) {
    var box = boxOf(entry);
    if (!box) { return; }
    drag = {
      mode: "resize",
      which: which,
      moved: false,
      origin: toUser(event.clientX, event.clientY) || [0, 0],
      namespace: entry.namespace,
      was: box,
      box: box
    };
  }

  function dragging() { return !!drag; }

  function move(event) {
    if (!drag) { return; }
    if (drag.mode === "fold") {
      // A fold is a click. Dragging off the triangle before letting go is how
      // somebody changes their mind, exactly as it is for a button.
      if (Math.abs(event.clientX - drag.origin[0])
        + Math.abs(event.clientY - drag.origin[1]) > SLOP) {
        drag.moved = true;
      }
      return;
    }
    if (drag.mode === "resize") {
      var at_ = toUser(event.clientX, event.clientY) || drag.origin;
      var dx = at_[0] - drag.origin[0];
      var dy = at_[1] - drag.origin[1];
      if (Math.abs(dx) > SLOP || Math.abs(dy) > SLOP) { drag.moved = true; }
      drag.box = resized(drag, dx, dy);
      paint();
      return;
    }
    var travelled = Math.abs(event.clientX - drag.origin[0])
      + Math.abs(event.clientY - drag.origin[1]);
    if (travelled > SLOP) { drag.moved = true; }
    if (!drag.moved) { return; }
    var over = containerAt(event.clientX, event.clientY);
    // A container cannot be dropped into itself or into anything it holds, so
    // those never light up -- and the refusal is never reached.
    var name = over && !swallows(drag.addresses, over.namespace) ? over.namespace : null;
    if (name !== drag.over) { drag.over = name; paint(); }
  }

  /** Would dropping `addresses` on `namespace` put a container inside itself? */
  function swallows(addresses, namespace) {
    return addresses.some(function (address) {
      return namespace === address || namespace.indexOf(address + "/") === 0;
    });
  }

  /** The rectangle a corner drag leaves: that corner follows the pointer, the
   *  opposite one stays. Note the SVG's `y` grows downwards, so "north" is the
   *  smaller `y` -- the opposite of the graph coordinates a write is in. */
  function resized(gesture, dx, dy) {
    var box = gesture.was;
    var east = gesture.which === "ne" || gesture.which === "se";
    var south = gesture.which === "se" || gesture.which === "sw";
    var width = Math.max(MIN_SIZE, box.w + (east ? dx : -dx));
    var height = Math.max(MIN_SIZE, box.h + (south ? dy : -dy));
    return {
      x: east ? box.x : box.x + box.w - width,
      y: south ? box.y : box.y + box.h - height,
      w: width,
      h: height
    };
  }

  function release() {
    if (!drag) { return; }
    var finished = drag;
    drag = null;
    if (finished.mode === "fold") {
      if (!finished.moved) { fold(finished.namespace); }
      return;
    }
    if (finished.mode === "resize") {
      if (finished.moved) { commitResize(finished); } else { paint(); }
      return;
    }
    if (!finished.moved) { paint(); return; }
    commitDrop(finished);
  }

  /* --------------------------------------------------------------- writes */

  /** Post the drop as `netgraph edit move`, or say why it is not one.
   *
   * Nothing is decided here beyond "which namespace": which *file* each document
   * lands in is the placement convention's answer, and it is the server's, which
   * is why this is one POST to /api/reparent rather than a batch of `move`
   * operations assembled in a browser that cannot know what files exist.
   */
  function commitDrop(finished) {
    var over = finished.over;
    var namespace = over === null ? "" : over;
    if (namespace === finished.from) {
      paint();
      ctx.say(finished.from
        ? "dropped back into " + finished.from + "; nothing to move"
        : "dropped in the root namespace, where it already was");
      return;
    }
    var said = "moved " + subject(finished.addresses) + " into "
      + (namespace || "the root namespace");
    var posted = ctx.reparent(finished.addresses, namespace, said);
    if (posted) {
      posted.catch(function () { paint(); });
    }
  }

  function subject(addresses) {
    return addresses.length === 1
      ? addresses[0]
      : addresses.length + " elements";
  }

  /** Write a resized container's rectangle into the layout document's `groups`.
   *
   * One `set-geometry`, so a reviewer reads "this box got bigger" and an undo
   * puts exactly that back. The box goes in whole -- a `GroupGeometry` requires
   * both a position and a size (§18), because nothing else decides how big a
   * cluster is.
   *
   * **Every stored group is sent, not only the one that moved**, and that is not
   * belt and braces. `set-geometry` replaces the section it is given by a keyed
   * merge (netgraph.edit.apply._merge_section): a `groups` holding one entry
   * means "these are the groups", and sending only the resized one would delete
   * every other box in this view. The rest come from the arrangement the server
   * published with the drawing, so what is written back is what was read.
   */
  function commitResize(finished) {
    var entry = frame.byNs[finished.namespace];
    if (!entry) { return; }
    var box = finished.box;
    var centre = toGraph(box.x + box.w / 2, box.y + box.h / 2);
    var stored = (frame.geometry && frame.geometry.groups) || {};
    var groups = {};
    Object.keys(stored).forEach(function (namespace) {
      groups[namespace] = {
        position: { x: round(stored[namespace].x), y: round(stored[namespace].y) },
        size: { width: round(stored[namespace].width), height: round(stored[namespace].height) }
      };
    });
    groups[entry.namespace] = {
      position: { x: round(centre[0]), y: round(centre[1]) },
      size: { width: round(box.w), height: round(box.h) }
    };
    var posted = ctx.write([{
      op: "set-geometry",
      view: ctx.view(),
      groups: groups
    }], "resized the " + entry.namespace + " container");
    if (posted) { posted.catch(function () { paint(); }); }
  }

  /* ------------------------------------------------------------- creating */

  /** Where a new element or a drop would go, given where the pointer was.
   *
   * Exported so the create prompt can open with the namespace already filled
   * in: right-clicking inside the rack and choosing "New switch" should make a
   * switch in the rack, not one at the root that then has to be dragged in.
   */
  function namespaceAt(at_) {
    if (!at_ || !frame.grouped) { return ""; }
    var entry = containerAt(at_.x, at_.y);
    return entry ? entry.namespace : "";
  }

  /** Every namespace this drawing holds, for a prompt's completion list. */
  function namespaces() { return frame.order.slice(); }

  return {
    attach: attach,
    annotate: annotate,
    collapsed: collapsed,
    at: at,
    select: select,
    selection: selection,
    fold: fold,
    toggle: toggle,
    grab: grab,
    move: move,
    release: release,
    dragging: dragging,
    namespaceAt: namespaceAt,
    namespaces: namespaces
  };
})();
