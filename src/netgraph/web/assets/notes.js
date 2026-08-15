/* The commentary on the canvas: notes, areas and legends (§21).
 *
 * These three are not elements. They declare no network fact, nothing in the
 * inventory refers to them, and no diagnostic they cause can change what the
 * tool concludes -- which is exactly why they can be dragged around freely and
 * why they need a file of their own rather than a branch inside select.js.
 *
 * Where the boxes are is *not* read off the drawing, and that is the one
 * surprising thing here. A note is a Graphviz node and does carry its
 * `note-<slug>` id, but an area in an arranged drawing is a rectangle painted
 * into the graph's `_background` with no id at all, and a legend in the same
 * drawing is a table Graphviz never wrapped in a cluster. So the server sends
 * `annotations` beside `geometry` -- the same payload `netgraph render -f json`
 * publishes -- and this file hit-tests against *that*: the id, the document
 * behind it, whatever rectangle it pins, and for an area the members whose hull
 * it follows. Anything the payload does not pin is computed here the way
 * netgraph.render.annotations computes it, and never guessed from pixels.
 *
 * What a gesture ends in is one POST to /api/ops, exactly as a link's does:
 *
 *   dragging a note      set-annotation spec.geometry.x, .y
 *   resizing one         .width, .height
 *   dragging an area     the four together
 *   retyping a note      set-annotation spec.text
 *   deleting one         delete-annotation, from the ordinary Delete gesture
 *
 * ...with one rule that is not obvious and is load-bearing. An annotation that
 * has never been placed gets its **whole `spec.geometry` block in one write**,
 * because `x` on its own is a position that places nothing and §21 says so
 * (NG-G005): a leaf-at-a-time sequence onto a missing block would be refused at
 * the first operation. One that is already placed gets a field at a time, which
 * is what a reviewer wants to read in the changes drawer. That is the same rule
 * netgraph/drawio/reconcile.py applies to a diagram coming home from draw.io,
 * and it is written down in netgraph.edit.operations.SetAnnotation.
 *
 * Handles are only offered on a **fixed** arrangement, for the reason links.js
 * offers none on an unarranged one: under an automatic layout Graphviz places a
 * note itself and ignores `spec.geometry`, so a drag would write a number that
 * moved nothing. Selecting, retyping and deleting work either way, because none
 * of the three is about coordinates.
 *
 * Coordinates are links.js's: netgraph's are points with `y` upwards, the SVG's
 * are `y` downwards, and the offset between the two is measured from the nodes
 * rather than assumed. Measured again here rather than borrowed, because a
 * drawing with annotations and no links at all is one links.js bows out of --
 * and a note on it is still a note somebody can drag.
 */
window.netgraphNotes = (function () {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";
  /** Radius of a grab handle, in diagram points. Matches links.js. */
  var HANDLE = 5;
  /** How wide the invisible band that catches a click on an area's outline is. */
  var HIT_WIDTH = 12;
  /** How far a mousedown may travel before it counts as a drag and not a click. */
  var SLOP = 2;
  /** Smallest box a resize may leave, in points. Below this a note is a dot. */
  var MIN_SIZE = 24;
  /** What a new note says until somebody types over it. Not empty: §21 gives a
   *  note a minimum length of one, and a placeholder is also the fastest way to
   *  find out that the gesture worked. */
  var PLACEHOLDER = "New note";
  /** How big a new note claims to be. Only draw.io draws a note at the size it
   *  is given -- Graphviz sizes one to its text -- but the number has to be
   *  something, and this is a callout rather than a paragraph. */
  var NEW_SIZE = [200, 60];
  /** How many times the editor is reopened on a note that has not been drawn
   *  yet. Creating one is a round trip, and this is how long we wait for it. */
  var OPEN_TRIES = 12;
  /** The corners a pinned zone offers, named by compass point. */
  var CORNERS = ["nw", "ne", "se", "sw"];

  var ctx = null;
  var host = null;
  var frame = { geometry: null, root: null, byId: {}, order: [], offset: [0, 0] };
  /** The id of the annotation this canvas is acting on, or null. */
  var picked = null;
  /** The gesture in flight, or null. */
  var drag = null;
  /** The open text box, or null. */
  var editing = null;

  /** Wire the layer into the page. See app.js for what it hands over. */
  function attach(context) { ctx = context; }

  /* ------------------------------------------------------------- the frame */

  /** Rebuild the overlay for a drawing that has just been put on screen.
   *
   * Called on every apply, cached SVG included: the bands and handles live in
   * the SVG, and a view switched back to has to be as editable as one drawn
   * fresh. `annotations` is the payload described at the top of this file, or
   * null when the view declares none or they are turned off.
   */
  function annotate(root, geometry, annotations, details) {
    host = null;
    drag = null;
    frame = { geometry: geometry || null, root: root || null, byId: {}, order: [], offset: [0, 0] };
    var graph = root && root.querySelector("g.graph");
    // The SVG is *not* replaced when a repaint reuses the drawing already on
    // screen -- that is what keeps the pan and the zoom -- so a previous overlay
    // is still in it and has to go, or every repaint would leave another.
    if (graph) {
      Array.prototype.forEach.call(graph.querySelectorAll("g.ng-annotations"), function (stale) {
        stale.remove();
      });
    }
    index(annotations);
    // Built for a drawing with nothing written on it too, and that is not a
    // waste: the first note somebody adds is placed where the pointer is, and
    // working out where that is in the graph's own coordinates needs this group
    // to measure against. An inventory with no annotations is exactly the one
    // where the gesture is most likely to be the first thing tried.
    if (!graph) { picked = null; return; }
    frame.offset = measureOffset(graph, geometry, details);
    host = document.createElementNS(SVG_NS, "g");
    host.setAttribute("class", "ng-annotations");
    // Furniture, not content: what the overlay says is said again in the status
    // line and by the commands, and a screen reader walking a field of handles
    // learns nothing from it. See a11y.js.
    host.setAttribute("aria-hidden", "true");
    graph.appendChild(host);
    // One that was selected before the repaint stays selected, so a note
    // dragged, written and re-rendered keeps its handles.
    if (picked && !frame.byId[picked]) { picked = null; }
    paint();
  }

  /** Turn the payload into one record per annotation, keyed by its SVG id.
   *
   * The id is the payload's, which is also the `id` attribute the renderer put
   * on the shape when it drew one -- so the same key answers "what did the
   * pointer land on" and "what does the server call this".
   */
  function index(annotations) {
    if (!annotations) { return; }
    [
      ["note", annotations.notes],
      ["area", annotations.areas],
      ["legend", annotations.legends]
    ].forEach(function (pair) {
      (pair[1] || []).forEach(function (entry) {
        var record = describe(pair[0], entry);
        frame.byId[record.id] = record;
        frame.order.push(record.id);
      });
    });
  }

  /** One payload entry as this file wants it: the document behind it, split. */
  function describe(kind, entry) {
    var fqn = String(entry.fqn || "");
    var cut = fqn.lastIndexOf("/");
    return {
      kind: kind,
      id: entry.id,
      fqn: fqn,
      namespace: cut === -1 ? "" : fqn.slice(0, cut),
      name: cut === -1 ? fqn : fqn.slice(cut + 1),
      text: entry.text || "",
      members: entry.members || [],
      padding: typeof entry.padding === "number" ? entry.padding : 0,
      layout: entry.layout || null
    };
  }

  /** Is this drawing arranged, so that a coordinate written here is honoured?
   *
   * The same question links.js asks before it offers a bend handle, and for the
   * same reason: under an automatic layout Graphviz places a note itself and
   * `spec.geometry` changes nothing, so a drag would be a write nobody could
   * see the effect of.
   */
  function arranged() {
    return !!(frame.geometry && frame.geometry.mode === "fixed");
  }

  /* Graphviz translates a drawing whose bounding box does not start at the
   * origin. Measured from the nodes rather than assumed; see links.js, which
   * does the same thing for the same reason and cannot be reused here because
   * it bows out of a drawing with no routable link in it. */
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

  /** A graph point in the SVG's coordinates. */
  function toSvg(point) {
    return [point[0] + frame.offset[0], -point[1] + frame.offset[1]];
  }

  /** An SVG point in the graph's coordinates. */
  function toGraph(x, y) {
    return [x - frame.offset[0], -(y - frame.offset[1])];
  }

  /** Where a mouse event is, in graph coordinates. */
  function pointerAt(event) {
    var owner = host && host.ownerSVGElement;
    var matrix = host && host.getScreenCTM();
    if (!owner || !matrix) { return [0, 0]; }
    var point = owner.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    var local = point.matrixTransform(matrix.inverse());
    return toGraph(local.x, local.y);
  }

  /* ------------------------------------------------------------- the boxes */

  /** Where one annotation is drawn, in graph coordinates, or null.
   *
   * Three answers, in the order they are trusted: what the document pins, what
   * the drawing shows, and -- for an area following its members -- the hull the
   * renderer would compute. Nothing here reads a pixel it does not have to.
   */
  function boxOf(entry) {
    if (entry.layout && entry.layout.position && entry.layout.size) {
      return {
        x: entry.layout.position.x,
        y: entry.layout.position.y,
        width: entry.layout.size.width,
        height: entry.layout.size.height
      };
    }
    var drawn = drawnBox(entry);
    if (drawn) { return drawn; }
    return entry.kind === "area" ? hullOf(entry) : null;
  }

  /** The box the *drawing* gives an annotation, when it drew one with an id. */
  function drawnBox(entry) {
    var shape = shapeOf(entry);
    if (!shape) { return null; }
    var box;
    try { box = shape.getBBox(); } catch (error) { return null; }
    if (!box || (!box.width && !box.height)) { return null; }
    var centre = toGraph(box.x + box.width / 2, box.y + box.height / 2);
    return { x: centre[0], y: centre[1], width: box.width, height: box.height };
  }

  /** The SVG group the renderer drew this annotation as, or null.
   *
   * A note is always one; an area is one only under an automatic layout, and a
   * legend likewise -- an arranged drawing paints both without a group to hang
   * an id on. That asymmetry is why the payload exists.
   */
  function shapeOf(entry) {
    if (!frame.root || !frame.root.querySelector) { return null; }
    return frame.root.querySelector('g[id="' + escapeId(entry.id) + '"]');
  }

  function escapeId(value) {
    return window.CSS && window.CSS.escape ? window.CSS.escape(value) : value;
  }

  /** The rectangle an area's members occupy, grown by its padding.
   *
   * The browser's copy of netgraph.render.annotations.member_hull, over the
   * stored arrangement rather than over the drawing: an area that follows its
   * members has no box of its own to read, and the reader still has to be able
   * to point at it and be told why it cannot be dragged.
   */
  function hullOf(entry) {
    var nodes = (frame.geometry && frame.geometry.nodes) || {};
    var left = null, right = null, top = null, bottom = null;
    entry.members.forEach(function (member) {
      var placed = nodes[member];
      if (!placed) { return; }
      var halfW = placed.width ? placed.width / 2 : 0;
      var halfH = placed.height ? placed.height / 2 : 0;
      var minX = placed.x - halfW, maxX = placed.x + halfW;
      var minY = placed.y - halfH, maxY = placed.y + halfH;
      left = left === null ? minX : Math.min(left, minX);
      right = right === null ? maxX : Math.max(right, maxX);
      top = top === null ? minY : Math.min(top, minY);
      bottom = bottom === null ? maxY : Math.max(bottom, maxY);
    });
    if (left === null) { return null; }
    var pad = entry.padding || 0;
    return {
      x: (left + right) / 2,
      y: (top + bottom) / 2,
      width: right - left + pad * 2,
      height: bottom - top + pad * 2
    };
  }

  /** Does this annotation pin a rectangle of its own, rather than follow? */
  function isPlaced(entry) {
    return !!(entry.layout && entry.layout.position);
  }

  /** Where it is *right now*: the drag's rectangle while one is in flight, and
   *  the settled one otherwise. The same split links.js makes between the line
   *  it is drawing and the line the server last sent. */
  function liveBox(entry) {
    return drag && drag.id === entry.id ? drag.box : boxOf(entry);
  }

  /* ------------------------------------------------------------- painting */

  function paint() {
    if (!host) { return; }
    while (host.firstChild) { host.removeChild(host.firstChild); }
    if (!arranged()) { return; }
    // An area in an arranged drawing is a rectangle in the background with no
    // shape to click, so the band *is* its hit area -- including for the one it
    // refuses, which is how "this zone is drawn round its members" gets said at
    // the moment somebody tries to move it.
    frame.order.forEach(function (id) {
      var entry = frame.byId[id];
      if (entry.kind !== "area") { return; }
      var box = liveBox(entry);
      if (box) { host.appendChild(band(entry, box)); }
    });
    var chosen = picked && frame.byId[picked];
    if (!chosen) { return; }
    var outline = liveBox(chosen);
    if (!outline) { return; }
    host.appendChild(rectangle(outline, "ng-anno-picked"));
    if (!ctx || !ctx.writable()) { return; }
    handlesFor(chosen, outline).forEach(function (node) { host.appendChild(node); });
  }

  /** The grab handles one selected annotation offers, in drawing order.
   *
   * A note is moved by dragging the note itself, so it needs one handle and not
   * five: the corner that resizes it. An area pinned to a rectangle is moved by
   * its band and resized by its corners. An area that follows its members and a
   * legend that sits in a corner of the paper have no geometry a handle could
   * write, so they get none -- see `refuseMove`.
   */
  function handlesFor(entry, box) {
    if (entry.kind === "note") {
      return [handle([box.x + box.width / 2, box.y - box.height / 2], "size", "se")];
    }
    if (entry.kind !== "area" || !isPlaced(entry)) { return []; }
    return CORNERS.map(function (corner) {
      return handle(cornerPoint(box, corner), "corner", corner);
    });
  }

  function cornerPoint(box, corner) {
    return [
      box.x + (corner === "ne" || corner === "se" ? box.width / 2 : -box.width / 2),
      box.y + (corner === "nw" || corner === "ne" ? box.height / 2 : -box.height / 2)
    ];
  }

  function band(entry, box) {
    var node = rectangle(box, "ng-anno-band");
    node.setAttribute("stroke-width", String(HIT_WIDTH));
    node.setAttribute("data-annotation", entry.id);
    if (!isPlaced(entry)) { node.setAttribute("data-follows", "members"); }
    return node;
  }

  function rectangle(box, className) {
    var at = toSvg([box.x - box.width / 2, box.y + box.height / 2]);
    var node = document.createElementNS(SVG_NS, "rect");
    node.setAttribute("class", className);
    node.setAttribute("x", String(round(at[0])));
    node.setAttribute("y", String(round(at[1])));
    node.setAttribute("width", String(Math.max(round(box.width), 1)));
    node.setAttribute("height", String(Math.max(round(box.height), 1)));
    return node;
  }

  function handle(point, kind, which) {
    var at = toSvg(point);
    var node = document.createElementNS(SVG_NS, "circle");
    // Deliberately *not* also `ng-handle`, however alike the two look: links.js
    // claims every element carrying that class the moment it is pressed, and a
    // handle claimed by the wrong layer is a gesture that silently does nothing.
    // The style is shared in app.css instead, which is where it belongs.
    node.setAttribute("class", "ng-anno-handle ng-anno-handle-" + kind);
    node.setAttribute("cx", String(round(at[0])));
    node.setAttribute("cy", String(round(at[1])));
    node.setAttribute("r", String(HANDLE));
    node.setAttribute("data-handle", kind);
    node.setAttribute("data-which", which);
    return node;
  }

  function round(value) { return Math.round(value * 100) / 100; }

  /* ----------------------------------------------------------- selection */

  /** What an event landed on: the overlay's own band, or a shape with an id. */
  function at(target) {
    if (!target || !target.closest) { return null; }
    var band_ = target.closest("[data-annotation]");
    if (band_) { return frame.byId[band_.getAttribute("data-annotation")] || null; }
    var group = target.closest("g");
    while (group) {
      if (group.id && frame.byId[group.id]) { return frame.byId[group.id]; }
      group = group.parentNode && group.parentNode.closest
        ? group.parentNode.closest("g")
        : null;
    }
    return null;
  }

  /** Select one by its id, or clear the selection with null. */
  function select(id) {
    var wanted = id && frame.byId[id] ? id : null;
    if (wanted === picked) { return wanted; }
    picked = wanted;
    paint();
    return wanted;
  }

  /** The selected annotation's record, or null. */
  function selection() { return (picked && frame.byId[picked]) || null; }

  /** The selected annotation as the Delete gesture spells one: `note/why-orange`.
   *
   * The kind belongs in the address because a note and a switch may share a
   * name -- that is the whole reason §21 has its own operations -- so the field
   * that names what to delete has to be able to say which of the two it means.
   */
  function token() {
    var entry = selection();
    return entry ? entry.kind + "/" + entry.fqn : "";
  }

  /** Every annotation on this drawing, spelled the same way, for a completion
   *  list. A legend has no shape to right-click in an arranged drawing, so this
   *  is the only way to reach one. */
  function tokens() {
    return frame.order.map(function (id) {
      var entry = frame.byId[id];
      return entry.kind + "/" + entry.fqn;
    });
  }

  /** Read one back, or null when it does not name an annotation this drawing
   *  holds -- which is what keeps an element in a namespace called `note` from
   *  being mistaken for one. */
  function parse(text) {
    var wanted = String(text || "");
    var found = null;
    frame.order.forEach(function (id) {
      var entry = frame.byId[id];
      if (entry.kind + "/" + entry.fqn === wanted) { found = entry; }
    });
    return found;
  }

  /* ------------------------------------------------------------ gestures */

  /** Does this press start something this layer owns? */
  function grab(event) {
    if (!ctx || !ctx.writable() || !host || editing) { return false; }
    var target = event.target;
    if (target && target.classList && target.classList.contains("ng-anno-handle")) {
      var chosen = selection();
      if (!chosen) { return false; }
      begin(chosen, target.getAttribute("data-handle"), target.getAttribute("data-which"), event);
      return true;
    }
    var entry = at(target);
    if (!entry) { return false; }
    select(entry.id);
    if (!arranged()) {
      // Selected, but not draggable: the diagram is still Graphviz's to lay
      // out, so let the press go on to pan the canvas as it always did.
      return false;
    }
    if (entry.kind === "legend") {
      return refuseMove("a legend sits in a corner of the paper, not at a coordinate; "
        + "move it with spec.corner");
    }
    if (entry.kind === "area" && !isPlaced(entry)) {
      return refuseMove("this area is drawn round its members; move them, or give it a "
        + "geometry to pin it to the paper");
    }
    begin(entry, "move", "", event);
    return true;
  }

  function begin(entry, mode, which, event) {
    var box = boxOf(entry);
    if (!box) { return; }
    drag = {
      id: entry.id,
      mode: mode,
      which: which,
      moved: false,
      origin: pointerAt(event),
      was: box,
      box: box
    };
  }

  function dragging() { return !!drag; }

  function move(event) {
    if (!drag) { return; }
    var entry = frame.byId[drag.id];
    if (!entry) { return; }
    var at_ = pointerAt(event);
    var dx = at_[0] - drag.origin[0];
    var dy = at_[1] - drag.origin[1];
    if (Math.abs(dx) > SLOP || Math.abs(dy) > SLOP) { drag.moved = true; }
    drag.box = drag.mode === "move" ? shifted(drag.was, dx, dy) : resized(drag, dx, dy);
    preview(entry);
    paint();
  }

  function shifted(box, dx, dy) {
    return { x: box.x + dx, y: box.y + dy, width: box.width, height: box.height };
  }

  /** The rectangle a corner drag leaves: that corner follows the pointer and
   *  the opposite one stays where it is, which is what a resize means. */
  function resized(gesture, dx, dy) {
    var box = gesture.was;
    var east = gesture.which === "ne" || gesture.which === "se" || gesture.mode === "size";
    var north = gesture.which === "nw" || gesture.which === "ne";
    var width = Math.max(MIN_SIZE, box.width + (east ? dx : -dx));
    var height = Math.max(MIN_SIZE, box.height + (north ? dy : -dy));
    var anchorX = box.x + (east ? -box.width / 2 : box.width / 2);
    var anchorY = box.y + (north ? -box.height / 2 : box.height / 2);
    return {
      x: anchorX + (east ? width / 2 : -width / 2),
      y: anchorY + (north ? height / 2 : -height / 2),
      width: width,
      height: height
    };
  }

  /** Show the drag on the drawing itself while it is in flight.
   *
   * A note is a real shape, so it is translated where it is -- one attribute per
   * frame, and the next render replaces the whole SVG anyway. An area has no
   * shape in an arranged drawing, so its overlay rectangle is the preview.
   */
  function preview(entry) {
    var shape = entry.kind === "note" ? shapeOf(entry) : null;
    if (!shape || !drag) { return; }
    var from = toSvg([drag.was.x, drag.was.y]);
    var to = toSvg([drag.box.x, drag.box.y]);
    shape.setAttribute("transform",
      "translate(" + round(to[0] - from[0]) + " " + round(to[1] - from[1]) + ")");
  }

  function release() {
    if (!drag) { return; }
    var finished = drag;
    drag = null;
    var entry = frame.byId[finished.id];
    if (!entry) { return; }
    if (!finished.moved) {
      // A press and a release in the same place is how somebody decides *not*
      // to move something. Put the preview back and write nothing.
      settle(entry);
      return;
    }
    commit(entry, finished);
  }

  /** Take the drag's preview off, leaving what the server last said. */
  function settle(entry) {
    var shape = shapeOf(entry);
    if (shape) { shape.removeAttribute("transform"); }
    paint();
  }

  /** Post what the canvas now says about one annotation's geometry. */
  function commit(entry, finished) {
    var box = finished.box;
    var updates = finished.mode === "move"
      ? { x: round(box.x), y: round(box.y) }
      : { width: round(box.width), height: round(box.height), x: round(box.x), y: round(box.y) };
    var said = finished.mode === "move"
      ? "moved " + entry.kind + " " + entry.fqn
      : "resized " + entry.kind + " " + entry.fqn
        + (entry.kind === "note"
          ? " (the drawing sizes a note to its text; the box is what draw.io exports)"
          : "");
    var posted = write(geometryOps(entry, updates), said);
    if (posted) {
      // A refused write does not repaint the canvas, so the preview would be
      // left showing a move that did not happen. Only on the refusal: putting it
      // back on the way through would flicker every successful drag.
      posted.catch(function () { settle(entry); });
    }
  }

  /** The fewest geometry writes that are each *individually* valid.
   *
   * One operation per field is what a reviewer wants to read in the changes
   * drawer -- `spec.geometry.x` says what happened -- but every write is checked
   * against §21 as it lands, and an `x` with no `y` is a position that places
   * nothing. So an annotation that has never been placed gets its whole block in
   * one write and one that is already placed gets a field at a time. The same
   * rule, for the same reason, as netgraph/drawio/reconcile.py's `_write_geometry`.
   *
   * An area is a further case: a zone with a position and no size is ignored by
   * the renderer, which draws it round its members again -- so a dragged one
   * that did not carry its extent would silently spring back.
   */
  function geometryOps(entry, updates) {
    var wanted = {};
    Object.keys(updates).forEach(function (key) { wanted[key] = updates[key]; });
    if (entry.kind === "area" && !(entry.layout && entry.layout.size)) {
      var box = boxOf(entry) || { width: NEW_SIZE[0], height: NEW_SIZE[1] };
      if (wanted.width === undefined) { wanted.width = round(box.width); }
      if (wanted.height === undefined) { wanted.height = round(box.height); }
    }
    if (isPlaced(entry)) {
      return Object.keys(wanted).map(function (key) {
        return setOp(entry, "spec.geometry." + key, wanted[key]);
      });
    }
    var merged = {};
    if (entry.layout && entry.layout.size) {
      merged.width = entry.layout.size.width;
      merged.height = entry.layout.size.height;
    }
    Object.keys(wanted).forEach(function (key) { merged[key] = wanted[key]; });
    return [setOp(entry, "spec.geometry", merged)];
  }

  function setOp(entry, path, value) {
    return {
      op: "set-annotation",
      kind: entry.kind,
      name: entry.name,
      namespace: entry.namespace,
      path: path,
      value: value
    };
  }

  /** One gesture, one batch, one entry in the undo stack. */
  function write(operations, said) {
    if (!operations.length || !ctx) { return null; }
    return ctx.write(operations, said);
  }

  function refuseMove(why) {
    if (ctx) { ctx.refuse(why); }
    // Swallowed rather than passed on: the press was aimed at this thing, and
    // panning the canvas away from it would be an odd answer to "why not".
    return true;
  }

  /* ------------------------------------------------------------- creating */

  /** Add a note, and open it for typing.
   *
   * `context.at` is where the pointer was when the menu was opened and
   * `context.on` is what it was over; the keyboard supplies neither, and a note
   * asked for from the keyboard goes in the middle of the view. Anchoring wins
   * over placing when there is something to anchor to: a note that follows the
   * switch it is about survives the diagram being laid out again, and a note
   * pinned at x: 400 does not.
   */
  function create(context) {
    if (!ctx || !ctx.writable()) { return false; }
    var on = context && context.on;
    var anchor = anchorFor(on);
    var name = freeName();
    var spec = { text: PLACEHOLDER };
    if (anchor) {
      spec.anchor = anchor;
    } else {
      var where = placeFor(context && context.at);
      spec.geometry = { x: round(where[0]), y: round(where[1]),
        width: NEW_SIZE[0], height: NEW_SIZE[1] };
    }
    var said = "added note " + name + (anchor ? " on " + (anchor.element || anchor.link) : "");
    var posted = write([{
      op: "create-annotation",
      kind: "note",
      name: name,
      namespace: "",
      spec: spec
    }], said);
    if (posted) {
      posted.then(function () { openLater(name, OPEN_TRIES); }, function () {});
    }
    return true;
  }

  /** What a note right-clicked onto something should be anchored to.
   *
   * Only to something the inventory declares. A derived edge -- an adapter's
   * upstream, a subnet membership -- is drawn but is not a document, so a note
   * anchored to one would name a reference nothing resolves; such a note is
   * pinned to the paper instead, which is the honest fallback.
   */
  function anchorFor(record) {
    var address = record ? String(record.id || "") : "";
    if (!address || address.indexOf("#") !== -1) { return null; }
    return record.type === "edge" ? { link: address } : { element: address };
  }

  /** Where a new note goes, in graph coordinates: under the pointer, or in the
   *  middle of what is on screen when there was no pointer. */
  function placeFor(at_) {
    if (at_ && host) { return pointerAt({ clientX: at_.x, clientY: at_.y }); }
    var canvas = ctx && ctx.canvas && ctx.canvas();
    if (!canvas || !host) { return [0, 0]; }
    return pointerAt({
      clientX: canvas.left + canvas.width / 2,
      clientY: canvas.top + canvas.height / 2
    });
  }

  /** The first `note-N` this drawing does not already hold.
   *
   * Named rather than asked for, because the gesture is "put a note here" and a
   * form in front of it would make it two gestures. The name is `metadata.name`
   * and is renameable like any other, from the palette or in the YAML pane.
   */
  function freeName() {
    var taken = {};
    frame.order.forEach(function (id) {
      var entry = frame.byId[id];
      if (entry.kind === "note") { taken[entry.name] = true; }
    });
    var index_ = 1;
    while (taken["note-" + index_]) { index_ += 1; }
    return "note-" + index_;
  }

  /** Open the editor on a note the server has only just been told about.
   *
   * The render that draws it is a round trip, so this retries rather than
   * guessing how long one takes -- the same bargain session.js makes when it
   * puts the focus ring back on a newly created element.
   */
  function openLater(name, tries) {
    if (tries <= 0) { return; }
    window.setTimeout(function () {
      var found = null;
      frame.order.forEach(function (id) {
        var entry = frame.byId[id];
        if (entry.kind === "note" && entry.fqn === name) { found = entry; }
      });
      if (!found) { openLater(name, tries - 1); return; }
      select(found.id);
      openEditor(found);
    }, 200);
  }

  /* -------------------------------------------------------- editing text */

  /** Is there something whose text this page can edit right now? */
  function editable() {
    var entry = selection();
    if (!entry) { return "select a note first: click one on the diagram"; }
    if (entry.kind !== "note") {
      return "only a note carries text; an area's caption is spec.label";
    }
    return true;
  }

  /** Edit the selected note's text. The command behind Shift-E. */
  function edit() {
    var verdict = editable();
    if (verdict !== true) {
      if (ctx) { ctx.refuse(verdict); }
      return false;
    }
    openEditor(selection());
    return true;
  }

  /** Double-clicking a note edits it, which is the gesture nothing has to teach. */
  function editAt(event) {
    if (!ctx || !ctx.writable()) { return false; }
    var entry = at(event.target);
    if (!entry || entry.kind !== "note") { return false; }
    select(entry.id);
    openEditor(entry);
    return true;
  }

  /** Put a text box over the note and give it the keyboard.
   *
   * An overlay in keys.js's stack, like the palette and the context menu, and
   * for the reason that stack exists: while it is up the page's own chords must
   * not fire, or Ctrl-Enter would re-render the diagram half way through a
   * sentence and `b` would bend a cable. Escape closes it and writes nothing.
   */
  function openEditor(entry) {
    closeEditor();
    var box = document.createElement("div");
    box.className = "note-edit";
    var field = document.createElement("textarea");
    field.className = "note-edit-text";
    field.value = entry.text;
    field.spellcheck = false;
    field.setAttribute("aria-label", "Text of note " + entry.fqn);
    var hint = document.createElement("p");
    hint.className = "note-edit-hint";
    hint.textContent = "Ctrl-Enter or click away to write it · Escape to leave it alone";
    box.appendChild(field);
    box.appendChild(hint);

    editing = { entry: entry, field: field, closing: false, entryHandle: null };
    editing.entryHandle = window.netgraphKeys.overlay(box, {
      focus: function () { field.focus(); field.select(); },
      close: function () { closeEditor(); },
      onKey: onEditorKey
    });
    place(box, entry);
    field.addEventListener("blur", function () {
      // Clicking away is the other way to say "that will do". Guarded, because
      // closing the overlay moves the focus back and would land here again.
      if (editing && !editing.closing) { commitText(); }
    });
  }

  /** Put the box over the note it is editing, and inside the window. */
  function place(box, entry) {
    var shape = shapeOf(entry);
    var found = shape ? shape.getBoundingClientRect() : null;
    var canvas = (ctx && ctx.canvas && ctx.canvas()) || { left: 0, top: 0, width: 0, height: 0 };
    var at_ = found && (found.width || found.height)
      ? { x: found.left, y: found.top }
      : { x: canvas.left + canvas.width / 2 - 110, y: canvas.top + canvas.height / 3 };
    var size = box.getBoundingClientRect();
    box.style.left = Math.max(8, Math.min(at_.x, window.innerWidth - size.width - 8)) + "px";
    box.style.top = Math.max(8, Math.min(at_.y, window.innerHeight - size.height - 8)) + "px";
  }

  function onEditorKey(chord, event) {
    if (chord === "Escape") {
      event.preventDefault();
      closeEditor();
      if (ctx) { ctx.say("left the note as it was"); }
      return true;
    }
    if (chord === "Ctrl-Enter") {
      event.preventDefault();
      commitText();
      return true;
    }
    // Everything else belongs to the text box: Enter is a newline here, not a
    // command, and a note is the markdown subset of §21 rather than one line.
    return true;
  }

  /** Write what was typed, if it is different and if it is anything at all. */
  function commitText() {
    if (!editing) { return; }
    var entry = editing.entry;
    var text = editing.field.value;
    closeEditor();
    if (text === entry.text) { return; }
    if (!text.trim()) {
      if (ctx) { ctx.refuse("a note says something; delete it instead of emptying it"); }
      return;
    }
    write([setOp(entry, "spec.text", text)], "retyped note " + entry.fqn);
  }

  function closeEditor() {
    if (!editing || editing.closing) { return; }
    editing.closing = true;
    var going = editing;
    editing = null;
    window.netgraphKeys.closeOverlay(going.entryHandle);
  }

  return {
    attach: attach,
    annotate: annotate,
    at: at,
    select: select,
    selection: selection,
    token: token,
    tokens: tokens,
    parse: parse,
    grab: grab,
    move: move,
    release: release,
    dragging: dragging,
    create: create,
    edit: edit,
    editAt: editAt,
    editable: editable
  };
})();
