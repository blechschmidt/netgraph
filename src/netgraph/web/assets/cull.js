/* Drawing only the part of a large diagram that is on screen.
 *
 * A thousand-device inventory is a Graphviz SVG of about twelve thousand
 * elements. The browser handles that: it lays it out once and pans it with a
 * CSS transform, so scrolling is cheap. What is not cheap is everything the
 * render tree costs *per frame* once the tab is that heavy — hit-testing a
 * pointer against twelve thousand shapes, repainting text at a scale where it
 * is one pixel tall, and holding the layer's worth of composited tiles.
 *
 * So this file does two things to a drawing that is too big, and nothing at all
 * to one that is not:
 *
 *   culling      Every node and every edge outside the viewport (plus a margin,
 *                so a small pan needs no work) has its *contents* moved into a
 *                detached fragment. The `<g>` itself stays exactly where it
 *                was, empty.
 *   detail       Below a zoom threshold the labels and the icons come off, and
 *                each namespace grows a frame with its name on it, because at
 *                that scale the name of one device is a smudge and the shape of
 *                the site is the only thing legible.
 *
 * Why the `<g>` stays. Everything else on this page addresses an element by the
 * id of its group: the focus ring, the remote-selection marks, the info box,
 * the link overlay, the outline. Removing the group would break all of them for
 * anything off screen, and "select something you cannot see" is a thing the
 * command palette and find-in-diagram do all the time. An empty group costs one
 * DOM node with no box, no paint and no hit test; its twelve children are what
 * cost something, and those are what go.
 *
 * The box index. Culling needs to know where every element is, and asking the
 * DOM (`getBBox`) is exactly what cannot be done for an element whose contents
 * are detached. So every box is measured once, when the drawing arrives, into a
 * plain object of numbers — and that index is then the answer to "where is this
 * element" for everybody, including a11y.js's arrow-key navigation, which used
 * to call `getBBox` once per candidate per keypress. Off-screen elements
 * therefore stay navigable, findable and selectable, which is the property this
 * file is not allowed to break.
 *
 * Coordinates. The index is in the SVG's own user space. The viewport is a
 * client rectangle. `graph0`'s transform maps between them and is read once per
 * drawing, in `frame()`.
 */
window.netgraphCull = (function () {
  "use strict";

  /** Below this many groups, everything is drawn and this file is inert.
   *
   * Culling costs a pass over the index on every pan, and a diagram of forty
   * devices is not slow. The threshold is where the drawing stops fitting on
   * screen at a readable zoom, not where the browser starts to struggle: past
   * this, most of what is in the DOM is off screen whatever the user does. */
  var CULL_ABOVE = 400;

  /** How far outside the viewport an element is still drawn, as a fraction of
   *  the viewport. Half a screen in each direction, so an ordinary pan or a
   *  flick reveals what is already there and nothing has to be materialised
   *  mid-gesture. */
  var MARGIN = 0.5;

  /** Zoom below which labels and icons come off and namespaces get frames.
   *
   * 0.45 is a little under the scale at which Graphviz's 10pt node labels stop
   * being readable, so the detail is dropped just after it stopped carrying
   * any. */
  var COARSE_SCALE = 0.45;

  /** How tall a namespace frame's label is drawn, in *screen* pixels.
   *  Converted into drawing units when the frames are painted, because the
   *  drawing may be at a four-hundredth of life size and a label sized in its
   *  units would be a fifteenth of a pixel. */
  var LABEL_PX = 15;

  /** How long after a pan or a zoom the cull runs, in milliseconds. The gesture
   *  itself must not wait for it: panning is a transform on a wrapper and stays
   *  at whatever frame rate the compositor manages, and the cull catches up
   *  when the hand stops. */
  var SETTLE_MS = 90;

  var el = null;
  var host = null;
  /** element id -> { x, y, w, h } in SVG user space. */
  var boxes = {};
  /** element id -> the group, for everything culling may touch. */
  var groups = {};
  /** element id -> DocumentFragment holding its detached children. */
  var parked = {};
  /** The <g> the namespace frames are drawn into, or null. */
  var frames = null;
  /** The scale they were sized for, so a zoom re-sizes them. */
  var framesScale = 0;
  /** namespace -> { x, y, w, h } in SVG user space, computed once per drawing. */
  var regions = {};
  /** The transform from SVG user space to client pixels, or null. */
  var mapping = null;
  var active = false;
  var coarse = false;
  var timer = null;
  var drawn = 0;

  /* ------------------------------------------------------------- attach */

  function attach(bridge) {
    host = bridge;
    el = bridge.el;
  }

  /* -------------------------------------------------------- the index */

  /** Measure the drawing that has just been put on screen.
   *
   * Called from app.js after every insertion, before anything is culled: the
   * boxes have to be read while every element still has one. One layout flush
   * for the whole diagram, which is the same one the browser was going to do.
   */
  function index(svg, details) {
    reset();
    if (!svg) { return; }
    mapping = frame(svg);
    var found = svg.querySelectorAll("g.node, g.edge");
    found.forEach(function (group) {
      var box = null;
      try { box = group.getBBox(); } catch (error) { box = null; }
      if (!box) { return; }
      boxes[group.id] = { x: box.x, y: box.y, w: box.width, h: box.height };
      groups[group.id] = group;
    });
    drawn = found.length;
    active = found.length > CULL_ABOVE;
    regions = active ? namespaces(details || {}) : {};
    // Scheduled rather than run: a11y.js and links.js walk the drawing straight
    // after this, and both are entitled to find it whole. a11y in particular
    // hides each node's Graphviz <title> so a screen reader does not read the
    // name twice, and a group whose contents were already parked would keep an
    // unhidden one. The first cull lands a fraction of a second later, which
    // nobody sees and everything downstream survives.
    if (active) { schedule(); }
  }

  /** Forget the drawing. Everything parked is dropped with the SVG that owned it. */
  function reset() {
    window.clearTimeout(timer);
    timer = null;
    boxes = {};
    groups = {};
    parked = {};
    regions = {};
    frames = null;
    framesScale = 0;
    mapping = null;
    active = false;
    coarse = false;
    drawn = 0;
  }

  /** Where this element is, in SVG user space, or null if it is not drawn.
   *
   * The one place anything else should ask. Answers for an element whose
   * contents are parked, which `getBBox` cannot.
   */
  function boxOf(id) {
    return boxes[id] || null;
  }

  function centreOf(id) {
    var box = boxes[id];
    return box ? { x: box.x + box.w / 2, y: box.y + box.h / 2 } : null;
  }

  /** Screen pixels per unit of the drawing at a zoom of 1, or 0 if unknown.
   *
   * The SVG is sized to the canvas, so this is a fact about how big the drawing
   * is: near 1 for a handful of devices, a few thousandths for a thousand of
   * them. app.js works its zoom ceiling out from it — see READABLE_SCALE there
   * — and this file its detail threshold.
   */
  function naturalScale(zoom) {
    var svg = el && el.viewport.firstElementChild;
    var ctm = svg ? frame(svg) : null;
    if (!ctm || !ctm.a) { return 0; }
    return Math.abs(ctm.a) / (zoom || 1);
  }

  /** How SVG user space maps to client pixels: `graph0`'s scale and translate.
   *
   * Read off the element rather than assumed, because Graphviz picks both from
   * the drawing's extent and a rendering at another DPI would pick differently.
   */
  function frame(svg) {
    var root = svg.querySelector("g#graph0, g.graph") || svg.firstElementChild;
    if (!root || !root.getScreenCTM) { return null; }
    var ctm = root.getScreenCTM();
    return ctm ? ctm : null;
  }

  /* ------------------------------------------------------------ culling */

  /** Cull and re-detail, once the view has stopped moving. */
  function schedule() {
    if (!active) { return; }
    window.clearTimeout(timer);
    timer = window.setTimeout(update, SETTLE_MS);
  }

  /** Bring the drawing into line with where the viewport now is. */
  function update() {
    window.clearTimeout(timer);
    timer = null;
    if (!active || !el) { return; }
    var svg = el.viewport.firstElementChild;
    if (!svg) { return; }
    var window_ = visible(svg);
    if (!window_) { return; }
    setCoarse(window_.scale < COARSE_SCALE, svg, window_.scale);
    var shown = 0;
    for (var id in boxes) {
      if (!Object.prototype.hasOwnProperty.call(boxes, id)) { continue; }
      var wanted = intersects(boxes[id], window_);
      if (wanted) { shown += 1; }
      if (wanted === !parked[id]) { continue; }
      if (wanted) { materialise(id); } else { park(id); }
    }
    drawn = shown;
    if (host && host.culled) { host.culled(shown, count()); }
  }

  /** The part of SVG user space the canvas is showing, plus the margin.
   *
   * The mapping is read afresh every time rather than cached: panning and
   * zooming are a CSS transform on the wrapper, `getScreenCTM` accounts for it,
   * and a cached matrix would answer for wherever the view was when the drawing
   * arrived.
   */
  function visible(svg) {
    mapping = frame(svg);
    if (!mapping) { return null; }
    var canvas = el.canvas.getBoundingClientRect();
    var inverse = mapping.inverse();
    var corners = [
      point(inverse, canvas.left, canvas.top),
      point(inverse, canvas.right, canvas.top),
      point(inverse, canvas.left, canvas.bottom),
      point(inverse, canvas.right, canvas.bottom)
    ];
    var xs = corners.map(function (one) { return one.x; });
    var ys = corners.map(function (one) { return one.y; });
    var left = Math.min.apply(null, xs);
    var right = Math.max.apply(null, xs);
    var top = Math.min.apply(null, ys);
    var bottom = Math.max.apply(null, ys);
    var padX = (right - left) * MARGIN;
    var padY = (bottom - top) * MARGIN;
    return {
      left: left - padX,
      right: right + padX,
      top: top - padY,
      bottom: bottom + padY,
      // The on-screen size of one user unit, which is what "zoomed out" means.
      scale: Math.abs(mapping.a) || 1
    };
  }

  function point(matrix, x, y) {
    return {
      x: matrix.a * x + matrix.c * y + matrix.e,
      y: matrix.b * x + matrix.d * y + matrix.f
    };
  }

  function intersects(box, window_) {
    return box.x <= window_.right && box.x + box.w >= window_.left &&
      box.y <= window_.bottom && box.y + box.h >= window_.top;
  }

  /** Move one element's contents out of the render tree, leaving the group. */
  function park(id) {
    var group = groups[id];
    if (!group || parked[id]) { return; }
    var fragment = document.createDocumentFragment();
    while (group.firstChild) { fragment.appendChild(group.firstChild); }
    parked[id] = fragment;
  }

  /** Put one element's contents back. Safe to call on one that never left.
   *
   * Public, because focusing, selecting or finding an element must work whether
   * or not it happens to be on screen: a11y.js calls this before it puts a ring
   * on something, and the pan that follows leaves it materialised for real.
   */
  function materialise(id) {
    var fragment = parked[id];
    if (!fragment) { return; }
    delete parked[id];
    var group = groups[id];
    if (group) { group.appendChild(fragment); }
  }

  /* ------------------------------------------------- level of detail */

  function setCoarse(wanted, svg, scale) {
    if (wanted !== coarse) {
      coarse = wanted;
      el.canvas.classList.toggle("coarse", coarse);
      if (!coarse && frames) { frames.remove(); frames = null; }
    }
    if (!coarse) { return; }
    // Repainted whenever the zoom has moved appreciably, because the frames are
    // drawn in the diagram's units and have to come out a constant size on
    // *screen*: a label sized in user units on a drawing at a four-hundredth of
    // life size is a fifteenth of a pixel tall, which is the state this whole
    // level of detail exists to rescue.
    if (frames && framesScale && Math.abs(Math.log(scale / framesScale)) < 0.2) { return; }
    framesScale = scale;
    paintFrames(svg, scale);
  }

  /** The bounding box of each namespace, from the nodes that are in it.
   *
   * A record's `id` is its address, and an address is its namespace and its
   * name; nothing else has to be sent for this. Namespaces with one member are
   * left out — a frame around a single node says nothing the node did not.
   */
  function namespaces(details) {
    var found = {};
    for (var id in boxes) {
      if (!Object.prototype.hasOwnProperty.call(boxes, id)) { continue; }
      var record = details[id];
      if (!record || record.type === "edge" || !record.id) { continue; }
      var cut = String(record.id).lastIndexOf("/");
      if (cut < 1) { continue; }
      var namespace = String(record.id).slice(0, cut);
      var box = boxes[id];
      var region = found[namespace];
      if (!region) {
        found[namespace] = { x: box.x, y: box.y, right: box.x + box.w, bottom: box.y + box.h,
                             members: 1 };
        continue;
      }
      region.x = Math.min(region.x, box.x);
      region.y = Math.min(region.y, box.y);
      region.right = Math.max(region.right, box.x + box.w);
      region.bottom = Math.max(region.bottom, box.y + box.h);
      region.members += 1;
    }
    return disjoint(found);
  }

  /** Keep the namespaces a frame would actually say something about.
   *
   * A frame is a claim: everything in here is that namespace. That is only true
   * when the layout put the namespace's members together — which a stored
   * arrangement usually does and `--group-by-namespace` always does, and which a
   * plain Graphviz hierarchy usually does not. On a drawing where the racks
   * interleave, the bounding boxes lie on top of one another and forty-two
   * overlapping rectangles tell a reader nothing except that there are
   * forty-two of something.
   *
   * So a namespace is framed only when its box touches no other namespace's,
   * and one with a single member is never framed at all — a box round one node
   * says nothing the node did not.
   */
  function disjoint(found) {
    var names = Object.keys(found).filter(function (name) { return found[name].members > 1; });
    var kept = {};
    names.forEach(function (name) {
      var mine = found[name];
      var clear = names.every(function (other) {
        if (other === name) { return true; }
        var theirs = found[other];
        return mine.x > theirs.right || mine.right < theirs.x ||
          mine.y > theirs.bottom || mine.bottom < theirs.y;
      });
      if (clear) { kept[name] = mine; }
    });
    return kept;
  }

  /** Draw one frame per namespace, under everything else.
   *
   * ``scale`` is screen pixels per drawing unit. Everything drawn here is
   * divided by it, so the frame is a hairline and the label is legible text
   * whatever the diagram has been shrunk to — which is the whole point, since
   * this only runs when the diagram has been shrunk a long way.
   */
  function paintFrames(svg, scale) {
    if (frames) { frames.remove(); frames = null; }
    var root = svg.querySelector("g#graph0, g.graph") || svg.firstElementChild;
    if (!root) { return; }
    var names = Object.keys(regions).sort();
    if (!names.length) { return; }
    var unit = 1 / (scale || 1);
    var pad = LABEL_PX * unit;
    var layer = document.createElementNS("http://www.w3.org/2000/svg", "g");
    layer.setAttribute("class", "ng-lod");
    layer.setAttribute("aria-hidden", "true");
    names.forEach(function (name) {
      var region = regions[name];
      var rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      rect.setAttribute("x", region.x - pad);
      rect.setAttribute("y", region.y - pad);
      rect.setAttribute("width", (region.right - region.x) + 2 * pad);
      rect.setAttribute("height", (region.bottom - region.y) + 2 * pad);
      rect.setAttribute("class", "ng-lod-frame");
      rect.setAttribute("stroke-width", 1.5 * unit);
      rect.setAttribute("stroke-dasharray", (6 * unit) + " " + (4 * unit));
      layer.appendChild(rect);
      var label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("x", region.x - pad);
      label.setAttribute("y", region.y - pad - 4 * unit);
      label.setAttribute("class", "ng-lod-label");
      label.setAttribute("font-size", LABEL_PX * unit);
      label.textContent = name + " (" + region.members + ")";
      layer.appendChild(label);
    });
    // First child, so the frames sit behind the diagram rather than over it.
    root.insertBefore(layer, root.firstChild);
    frames = layer;
  }

  /* ----------------------------------------------------------- reporting */

  function count() {
    var total = 0;
    for (var id in boxes) {
      if (Object.prototype.hasOwnProperty.call(boxes, id)) { total += 1; }
    }
    return total;
  }

  /** What is being drawn out of what there is, for the status line and tests. */
  function stats() {
    return { active: active, coarse: coarse, drawn: drawn, total: count() };
  }

  return {
    attach: attach,
    index: index,
    reset: reset,
    update: update,
    schedule: schedule,
    boxOf: boxOf,
    centreOf: centreOf,
    naturalScale: naturalScale,
    materialise: materialise,
    stats: stats,
    CULL_ABOVE: CULL_ABOVE,
    COARSE_SCALE: COARSE_SCALE
  };
})();
