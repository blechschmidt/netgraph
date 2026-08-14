/* Routing a cable on the canvas: bends, routing style and label position.
 *
 * The diagram is a Graphviz SVG the page does not rewrite -- everything else
 * about it (hover, focus, remote selection) works by looking element ids up in
 * the detail records, and re-laying it out in the browser would throw away the
 * one property that makes a stored arrangement worth having: what you see here
 * is byte-for-byte what `netgraph render` draws.
 *
 * So this file adds an *overlay* rather than replacing anything. A `<g>` is
 * appended inside Graphviz's own `graph0` group, which puts it in the diagram's
 * coordinate system for free, and into it go the grab handles: one per bend,
 * one at the midpoint of every routable link, one on a nudged label. Dragging
 * one redraws a preview line and nothing else; letting go posts a
 * `set-link-geometry` operation, the server rewrites the `kind: layout`
 * document through the same comment-preserving path `netgraph layout` uses, and
 * the canvas repaints from the render that follows. There is no browser-side
 * model of the arrangement that could drift from the file.
 *
 * The one thing that *is* duplicated is the routing itself, in `routeOf` below,
 * because a line that only updated when the server answered would lag a
 * cursor. It is a faithful port of `netgraph/layout/routing.py` -- same
 * constants, same rules, same fixed iteration counts -- and
 * `tests/test_browser.py` runs the two against each other on a table of cases,
 * so the copy cannot drift silently. Change one, change both.
 *
 * Coordinates. netgraph's are points with `y` upwards (Graphviz's own); the
 * SVG's are `y` downwards. A graph point (x, y) is at (x + dx, -y + dy) inside
 * `graph0`, and the offset is measured from the nodes rather than assumed,
 * because a hand-edited arrangement whose bounding box no longer starts at the
 * origin is one Graphviz quietly translates.
 */
window.netgraphLinks = (function () {
  "use strict";

  /* ---------------------------------------------------- routing, mirrored */

  /** @see netgraph.layout.routing.DEFAULT_NODE_SIZE */
  var DEFAULT_NODE_SIZE = [54, 36];
  /** @see netgraph.layout.routing.CLEARANCE */
  var CLEARANCE = 1;
  /** @see netgraph.layout.routing.LOOP_REACH */
  var LOOP_REACH = 40;
  /** @see netgraph.layout.routing.FAN_GAP */
  var FAN_GAP = 14;
  /** @see netgraph.layout.routing.EPSILON */
  var EPSILON = 1e-9;
  /** @see netgraph.layout.routing.TOUCH */
  var TOUCH = 1e-3;

  /** The line a link is drawn as: the polyline it follows.
   *
   * The Python side also produces the Bézier control points, because Graphviz
   * needs them; an SVG path can be drawn from the corners plus one smoothing
   * pass, so only the corners are computed here.
   */
  function routeOf(source, target, waypoints, style, fan) {
    var spine;
    if (sameNode(source, target)) {
      spine = loopSpine(source, waypoints, fan);
    } else {
      spine = [centre(source)].concat(waypoints.map(pair), [centre(target)]);
      if (!waypoints.length && fan) { spine = fanned(spine, fan); }
    }
    var shaped = dedupe(style === "orthogonal" ? orthogonal(dedupe(spine)) : spine);
    var clipped = clip(shaped, source, target);
    return clipped.length < 2 ? [] : clipped;
  }

  function centre(anchor) { return [anchor.x, anchor.y]; }
  function pair(point) { return [point.x, point.y]; }

  function sameNode(a, b) {
    return Math.abs(a.x - b.x) < EPSILON && Math.abs(a.y - b.y) < EPSILON;
  }

  function loopSpine(node, waypoints, fan) {
    if (waypoints.length) {
      return [centre(node)].concat(waypoints.map(pair), [centre(node)]);
    }
    var reach = node.height / 2 + LOOP_REACH + Math.abs(fan || 0);
    var spread = Math.max(node.width / 4, FAN_GAP) + Math.abs(fan || 0) / 2;
    return [
      centre(node),
      [node.x - spread, node.y + reach],
      [node.x + spread, node.y + reach],
      centre(node)
    ];
  }

  function fanned(spine, offset) {
    var a = spine[0], b = spine[spine.length - 1];
    var dx = b[0] - a[0], dy = b[1] - a[1];
    var length = Math.sqrt(dx * dx + dy * dy);
    if (length < EPSILON) { return spine.slice(); }
    var nx = -dy / length, ny = dx / length;
    return [a, [(a[0] + b[0]) / 2 + nx * offset, (a[1] + b[1]) / 2 + ny * offset], b];
  }

  function orthogonal(points) {
    if (points.length < 2) { return points.slice(); }
    if (points.length === 2) {
      var p = points[0], q = points[1];
      if (Math.abs(q[0] - p[0]) >= Math.abs(q[1] - p[1])) {
        var mx = (p[0] + q[0]) / 2;
        return [p, [mx, p[1]], [mx, q[1]], q];
      }
      var my = (p[1] + q[1]) / 2;
      return [p, [p[0], my], [q[0], my], q];
    }
    var out = [points[0]];
    for (var i = 0; i < points.length - 1; i += 1) {
      var s = points[i], e = points[i + 1];
      out.push(Math.abs(e[0] - s[0]) >= Math.abs(e[1] - s[1]) ? [e[0], s[1]] : [s[0], e[1]]);
      out.push(e);
    }
    return out;
  }

  function dedupe(points) {
    var kept = [];
    points.forEach(function (point) {
      var last = kept[kept.length - 1];
      if (last && Math.abs(point[0] - last[0]) < EPSILON &&
          Math.abs(point[1] - last[1]) < EPSILON) { return; }
      kept.push(point);
    });
    return kept;
  }

  function contains(anchor, point) {
    return Math.abs(point[0] - anchor.x) <= anchor.width / 2 + CLEARANCE &&
      Math.abs(point[1] - anchor.y) <= anchor.height / 2 + CLEARANCE;
  }

  function clip(points, source, target) {
    var forward = clipEnd(points, source);
    var backward = clipEnd(forward.slice().reverse(), target);
    var out = backward.slice().reverse();
    return out.length < 2 && sameNode(source, target) ? points.slice() : out;
  }

  function clipEnd(points, anchor) {
    var index = 0;
    while (index < points.length && contains(anchor, points[index])) { index += 1; }
    if (index >= points.length || index === 0) { return points.slice(); }
    return [crossing(points[index - 1], points[index], anchor)].concat(points.slice(index));
  }

  function crossing(inside, outside, anchor) {
    var dx = outside[0] - inside[0], dy = outside[1] - inside[1];
    var halfW = anchor.width / 2 + CLEARANCE, halfH = anchor.height / 2 + CLEARANCE;
    var best = 1;
    [
      [dx, inside[0], halfW, anchor.x, dy, inside[1], halfH, anchor.y],
      [dy, inside[1], halfH, anchor.y, dx, inside[0], halfW, anchor.x]
    ].forEach(function (axis) {
      if (Math.abs(axis[0]) < EPSILON) { return; }
      [axis[3] - axis[2], axis[3] + axis[2]].forEach(function (edge) {
        var t = (edge - axis[1]) / axis[0];
        if (t < 0 || t > best) { return; }
        var across = axis[5] + axis[4] * t;
        if (Math.abs(across - axis[7]) <= axis[6] + TOUCH) { best = t; }
      });
    });
    return [inside[0] + dx * best, inside[1] + dy * best];
  }

  /** The point `at` of the way along a polyline, by length. */
  function along(points, at) {
    if (!points.length) { return [0, 0]; }
    if (points.length === 1) { return points[0]; }
    var lengths = [], total = 0, i;
    for (i = 0; i < points.length - 1; i += 1) {
      var dx = points[i + 1][0] - points[i][0], dy = points[i + 1][1] - points[i][1];
      lengths.push(Math.sqrt(dx * dx + dy * dy));
      total += lengths[i];
    }
    if (total < EPSILON) { return points[0]; }
    var wanted = Math.max(0, Math.min(1, at)) * total, travelled = 0;
    for (i = 0; i < lengths.length; i += 1) {
      if (travelled + lengths[i] >= wanted) {
        var t = lengths[i] < EPSILON ? 0 : (wanted - travelled) / lengths[i];
        return [
          points[i][0] + (points[i + 1][0] - points[i][0]) * t,
          points[i][1] + (points[i + 1][1] - points[i][1]) * t
        ];
      }
      travelled += lengths[i];
    }
    return points[points.length - 1];
  }

  /** Where along a polyline a point falls, and how far off it is. */
  function nearest(points, x, y) {
    var best = { at: 0.5, index: 0, distance: Infinity };
    var lengths = [], total = 0, i;
    for (i = 0; i < points.length - 1; i += 1) {
      var ax = points[i + 1][0] - points[i][0], ay = points[i + 1][1] - points[i][1];
      lengths.push(Math.sqrt(ax * ax + ay * ay));
      total += lengths[i];
    }
    if (total < EPSILON) { return best; }
    var walked = 0;
    for (i = 0; i < lengths.length; i += 1) {
      var sx = points[i][0], sy = points[i][1];
      var dx = points[i + 1][0] - sx, dy = points[i + 1][1] - sy;
      var t = lengths[i] < EPSILON ? 0
        : Math.max(0, Math.min(1, ((x - sx) * dx + (y - sy) * dy) / (lengths[i] * lengths[i])));
      var px = sx + dx * t, py = sy + dy * t;
      var distance = Math.sqrt((x - px) * (x - px) + (y - py) * (y - py));
      if (distance < best.distance) {
        best = { at: (walked + lengths[i] * t) / total, index: i, distance: distance };
      }
      walked += lengths[i];
    }
    return best;
  }

  /* ------------------------------------------------------------- the layer */

  var SVG_NS = "http://www.w3.org/2000/svg";
  /** Radius of a grab handle, in diagram points. */
  var HANDLE = 5;
  /** How wide the invisible band that catches a click on a link is. */
  var HIT_WIDTH = 12;
  /** How far a mousedown may travel before it counts as a drag and not a click. */
  var SLOP = 2;

  var ctx = null;
  var host = null;
  var frame = { geometry: null, details: {}, offset: [0, 0], links: {} };
  var picked = null;
  var drag = null;

  /** Wire the layer into the page. See app.js for what it hands over. */
  function attach(context) { ctx = context; }

  /** Rebuild the overlay for a drawing that has just been put on screen.
   *
   * Called on every apply, cached SVG included: the handles live in the SVG,
   * and a view switched back to has to be as editable as one drawn fresh.
   */
  function annotate(root, geometry, details) {
    host = null;
    frame = { geometry: geometry || null, details: details || {}, offset: [0, 0], links: {} };
    drag = null;
    var graph = root && root.querySelector("g.graph");
    // The SVG is *not* replaced when a repaint reuses the drawing already on
    // screen -- that is what keeps the pan and the zoom -- so a previous
    // overlay is still in it and has to go, or every repaint would leave
    // another set of handles behind.
    if (graph) {
      Array.prototype.forEach.call(graph.querySelectorAll("g.ng-links"), function (stale) {
        stale.remove();
      });
    }
    if (!graph || !geometry || !geometry.links) { picked = null; return; }
    frame.offset = measureOffset(graph, geometry, details);
    frame.links = geometry.links;
    host = document.createElementNS(SVG_NS, "g");
    host.setAttribute("class", "ng-links");
    graph.appendChild(host);
    // A link that was selected before the repaint stays selected, so a bend
    // dropped, written and re-rendered leaves the handles where they were.
    if (picked && !frame.links[picked]) { picked = null; }
    paint();
  }

  /** Which link the canvas is routing, or null. */
  function selection() { return picked; }

  /** Select a link by its address, or clear the selection with null. */
  function select(id) {
    var wanted = id && frame.links[id] ? id : null;
    if (wanted === picked) { return; }
    picked = wanted;
    paint();
  }

  /* Graphviz translates a drawing whose bounding box does not start at the
   * origin, which a hand-edited arrangement's does not until it has been
   * re-seeded. So the offset between the stored coordinates and the drawn ones
   * is measured from the nodes rather than assumed to be zero. Every node gives
   * the same answer -- the translation is uniform -- so the first one that can
   * be matched settles it, and a drawing with none is left at zero, which is
   * what it was before this file existed. */
  function measureOffset(graph, geometry, details) {
    var nodes = geometry.nodes || {};
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
    if (!host) { return [0, 0]; }
    var owner = host.ownerSVGElement;
    var matrix = host.getScreenCTM();
    if (!owner || !matrix) { return [0, 0]; }
    var point = owner.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    var local = point.matrixTransform(matrix.inverse());
    return toGraph(local.x, local.y);
  }

  /* ------------------------------------------------------------ painting */

  function paint() {
    if (!host) { return; }
    while (host.firstChild) { host.removeChild(host.firstChild); }
    Object.keys(frame.links).forEach(function (id) { host.appendChild(hitBand(id)); });
    if (!picked) { return; }
    var link = frame.links[picked];
    if (!link) { return; }
    var points = live(picked);
    host.appendChild(path(points, "ng-link-route"));
    if (!ctx || !ctx.writable()) { return; }
    (link.waypoints || []).forEach(function (point, index) {
      host.appendChild(handle([point.x, point.y], "bend", index));
    });
    var middle = along(points, 0.5);
    if (!(link.waypoints || []).length || true) {
      host.appendChild(handle(middle, "add", -1));
    }
    if (link.label) {
      var at = along(points, link.label.at);
      host.appendChild(handle([at[0] + link.label.offset.x, at[1] + link.label.offset.y],
        "label", -1));
    }
  }

  /** The polyline for a link right now: the drag's, or the server's. */
  function live(id) {
    if (drag && drag.link === id && drag.points) { return drag.points; }
    var link = frame.links[id];
    return (link && link.route || []).map(pair);
  }

  /** A wide, invisible band along a link, so a click on it lands somewhere. */
  function hitBand(id) {
    var band = path(live(id), "ng-link-hit");
    band.setAttribute("data-link", id);
    return band;
  }

  function path(points, className) {
    var element = document.createElementNS(SVG_NS, "path");
    element.setAttribute("class", className);
    element.setAttribute("d", pathData(points));
    element.setAttribute("fill", "none");
    if (className === "ng-link-hit") { element.setAttribute("stroke-width", String(HIT_WIDTH)); }
    return element;
  }

  function pathData(points) {
    if (!points.length) { return ""; }
    return points.map(function (point, index) {
      var at = toSvg(point);
      return (index ? "L" : "M") + round(at[0]) + " " + round(at[1]);
    }).join(" ");
  }

  function round(value) { return Math.round(value * 100) / 100; }

  function handle(point, kind, index) {
    var at = toSvg(point);
    var element = document.createElementNS(SVG_NS, "circle");
    element.setAttribute("class", "ng-handle ng-handle-" + kind);
    element.setAttribute("cx", String(round(at[0])));
    element.setAttribute("cy", String(round(at[1])));
    element.setAttribute("r", String(kind === "add" ? HANDLE - 1 : HANDLE));
    element.setAttribute("data-kind", kind);
    element.setAttribute("data-index", String(index));
    return element;
  }

  /* ------------------------------------------------------------ gestures */

  /** Does this event start something this layer owns? */
  function grab(event) {
    if (!ctx || !ctx.writable() || !host) { return false; }
    var target = event.target;
    if (target && target.classList && target.classList.contains("ng-handle")) {
      begin(target, event);
      return true;
    }
    return false;
  }

  function begin(target, event) {
    var kind = target.getAttribute("data-kind");
    var index = Number(target.getAttribute("data-index"));
    var link = frame.links[picked];
    if (!link) { return; }
    var at = pointerAt(event);
    if (kind === "add") {
      // Dragging the midpoint handle *is* dropping a bend: the new waypoint is
      // inserted where the line is grabbed and then follows the cursor, which
      // is the gesture every diagram editor has.
      var where = nearest((link.route || []).map(pair), at[0], at[1]);
      index = insertionFor(link, where);
      kind = "bend";
      link.waypoints = (link.waypoints || []).slice();
      link.waypoints.splice(index, 0, { x: at[0], y: at[1] });
    }
    drag = {
      link: picked,
      kind: kind,
      index: index,
      moved: false,
      origin: at,
      points: null,
      // What the link said before the press. A press that turns out not to be a
      // drag has to leave no trace, and the midpoint handle has already
      // inserted a bend by this point.
      was: { waypoints: (link.waypoints || []).slice(), label: link.label }
    };
    move(event);
  }

  /** Which slot a bend dropped on segment `index` belongs in.
   *
   * The route has more corners than the link has waypoints -- an orthogonal
   * one has an elbow per leg and every route is clipped at both ends -- so the
   * segment a click landed on cannot index the waypoint list directly. What it
   * can do is say how far along the line the click was, and the waypoints are
   * in that order too, so the slot is the count of bends that lie before it.
   */
  function insertionFor(link, where) {
    var points = (link.route || []).map(pair);
    var before = 0;
    (link.waypoints || []).forEach(function (point) {
      var found = nearest(points, point.x, point.y);
      if (found.at < where.at) { before += 1; }
    });
    return before;
  }

  function move(event) {
    if (!drag) { return; }
    var link = frame.links[drag.link];
    if (!link) { return; }
    var at = pointerAt(event);
    if (Math.abs(at[0] - drag.origin[0]) > SLOP || Math.abs(at[1] - drag.origin[1]) > SLOP) {
      drag.moved = true;
    }
    if (drag.kind === "bend") {
      link.waypoints[drag.index] = { x: round(at[0]), y: round(at[1]) };
      drag.points = recompute(drag.link);
    } else if (drag.kind === "label") {
      var points = (link.route || []).map(pair);
      var where = nearest(points, at[0], at[1]);
      var on = along(points, where.at);
      link.label = {
        at: Math.round(where.at * 1000) / 1000,
        offset: { x: round(at[0] - on[0]), y: round(at[1] - on[1]) }
      };
    }
    paint();
  }

  /** The line this link would be drawn as with the bends it has right now. */
  function recompute(id) {
    var link = frame.links[id];
    var anchors = (frame.geometry && frame.geometry.anchors) || {};
    var source = anchors[link.endpoints[0]];
    var target = anchors[link.endpoints[1]];
    if (!source || !target) { return null; }
    var style = link.routing || (frame.geometry && frame.geometry.routing) || "spline";
    return routeOf(source, target, link.waypoints || [], style, link.fan || 0);
  }

  function release() {
    if (!drag) { return; }
    var finished = drag;
    drag = null;
    if (!finished.moved) {
      // A click, not a drag. Put back what the press speculatively changed and
      // write nothing: pressing a handle and letting go is how somebody decides
      // *not* to move it.
      var link = frame.links[finished.link];
      if (link) {
        link.waypoints = finished.was.waypoints;
        link.label = finished.was.label;
      }
      paint();
      return;
    }
    commit(finished.link, finished.kind === "label"
      ? "moved the label of " + finished.link
      : "moved a bend on " + finished.link);
  }

  /** Post what the canvas now says about one link. */
  function commit(id, said) {
    var link = frame.links[id];
    if (!link || !ctx) { return; }
    ctx.write({
      op: "set-link-geometry",
      view: ctx.view(),
      link: id,
      waypoints: (link.waypoints || []).map(function (point) {
        return { x: point.x, y: point.y };
      }),
      routing: link.routing || null,
      label: link.label || null
    }, said);
  }

  /** Double-clicking a link drops a bend where it was clicked. */
  function insert(event) {
    if (!ctx || !ctx.writable()) { return false; }
    var id = linkAt(event.target);
    if (!id) { return false; }
    select(id);
    var link = frame.links[id];
    var at = pointerAt(event);
    var where = nearest((link.route || []).map(pair), at[0], at[1]);
    var index = insertionFor(link, where);
    link.waypoints = (link.waypoints || []).slice();
    link.waypoints.splice(index, 0, { x: round(at[0]), y: round(at[1]) });
    paint();
    commit(id, "added a bend to " + id);
    return true;
  }

  /** Right-clicking a bend removes it. */
  function remove(event) {
    if (!ctx || !ctx.writable() || !picked) { return false; }
    var target = event.target;
    if (!target || !target.classList || !target.classList.contains("ng-handle-bend")) {
      return false;
    }
    var link = frame.links[picked];
    var index = Number(target.getAttribute("data-index"));
    link.waypoints = (link.waypoints || []).slice();
    link.waypoints.splice(index, 1);
    paint();
    commit(picked, "removed a bend from " + picked);
    return true;
  }

  /** The link an SVG element belongs to, whether ours or Graphviz's. */
  function linkAt(target) {
    if (!target || !target.closest) { return null; }
    var band = target.closest(".ng-link-hit");
    if (band) { return band.getAttribute("data-link"); }
    var group = target.closest("g.edge");
    if (!group) { return null; }
    var record = frame.details[group.id];
    var id = record && record.id;
    return id && frame.links[id] ? id : null;
  }

  /* ------------------------------------------------------------ commands */

  /** Drop a bend half way along the selected link. */
  function bend() {
    var id = picked;
    if (!id || !frame.links[id]) { return refuse(); }
    var link = frame.links[id];
    var middle = along((link.route || []).map(pair), 0.5);
    var where = nearest((link.route || []).map(pair), middle[0], middle[1]);
    link.waypoints = (link.waypoints || []).slice();
    link.waypoints.splice(insertionFor(link, where), 0,
      { x: round(middle[0]), y: round(middle[1]) });
    paint();
    commit(id, "added a bend to " + id);
    return true;
  }

  /** Clear every bend, keeping the routing style and the label. */
  function straighten() {
    var id = picked;
    if (!id || !frame.links[id]) { return refuse(); }
    frame.links[id].waypoints = [];
    paint();
    commit(id, "straightened " + id);
    return true;
  }

  /** Set this link's routing style, or clear it with "". */
  function route(style) {
    var id = picked;
    if (!id || !frame.links[id]) { return refuse(); }
    frame.links[id].routing = style || null;
    paint();
    commit(id, style ? id + " is now routed " + style : id + " takes the view's routing");
    return true;
  }

  /** Put a nudged label back on the line. */
  function resetLabel() {
    var id = picked;
    if (!id || !frame.links[id]) { return refuse(); }
    frame.links[id].label = null;
    paint();
    commit(id, "put the label of " + id + " back on the line");
    return true;
  }

  function refuse() {
    if (ctx) {
      ctx.refuse(
        frame.geometry && frame.geometry.links
          ? "select a link first: click one, or focus it and press Space"
          : "this diagram is not arranged yet, so a cable has nowhere to bend. " +
            "Run 'netgraph layout --write' to place it"
      );
    }
    return false;
  }

  /** Is there a link selected that these commands can act on? */
  function hasLink() { return !!(picked && frame.links[picked]); }

  return {
    attach: attach,
    annotate: annotate,
    select: select,
    selection: selection,
    linkAt: linkAt,
    grab: grab,
    move: move,
    release: release,
    insert: insert,
    remove: remove,
    bend: bend,
    straighten: straighten,
    route: route,
    resetLabel: resetLabel,
    hasLink: hasLink,
    dragging: function () { return !!drag; },
    /* Exposed for tests/test_browser.py, which drives the same table of cases
     * through netgraph.layout.routing and asserts the two agree. */
    routeOf: routeOf
  };
})();
