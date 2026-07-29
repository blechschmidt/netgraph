/* The client of the self-contained page `netgraph render -f html` writes.
 *
 * Hand-written, dependency-free and inlined into the document at render time.
 * There is no build step and no framework on purpose: the output of this
 * renderer is a file you email, commit or publish, and every byte of runtime
 * in it is a byte the reader downloads to look at a network diagram.
 *
 * What is on the page when this runs:
 *
 *   - one <div class="view"> per rendering, each holding an <svg> Graphviz
 *     laid out. Several exist when the document holds more than one layer, or
 *     when the address and VLAN annotations can be turned off -- switching is
 *     showing a different one, never a re-layout, because a browser cannot lay
 *     a graph out and this page carries no layout engine;
 *   - a <script type="application/json"> holding one record per drawn element.
 *     A record is the same structure `netgraph render -f json` exports and the
 *     same one the web preview's info boxes use. It is stored in two pools --
 *     `records` and `links` -- with each layer holding a pair of indices per
 *     element id, so that a device drawn at three layers is written once and
 *     not three times; `records()` below puts the two back together, once per
 *     layer, the first time that layer is asked for.
 *
 * Everything below is reading those two and moving classes around. No markup
 * is ever assigned -- no innerHTML, no document.write, no new Function -- so
 * the page needs neither 'unsafe-inline' beyond its own hash nor 'unsafe-eval'
 * in the Content-Security-Policy it ships with, and a device name carrying
 * markup is text wherever it appears.
 */

(function () {
  "use strict";

  /** Zoom bounds. Below the first the diagram is a smudge; above the second a pixel. */
  var MIN_SCALE = 0.1;
  var MAX_SCALE = 12;
  /** How far an arrow key pans, in screen pixels; Shift multiplies it. */
  var PAN_STEP = 60;
  var PAN_LEAP = 4;
  /** Matches listed before the rest are counted off. A list longer than this is
   *  not a result set, it is the inventory again. */
  var MAX_RESULTS = 60;

  var data = JSON.parse(document.getElementById("netgraph-data").textContent);

  var el = {
    stage: document.getElementById("ng-stage"),
    viewport: document.getElementById("ng-viewport"),
    layer: document.getElementById("ng-layer"),
    namespace: document.getElementById("ng-namespace"),
    ips: document.getElementById("ng-ips"),
    vlans: document.getElementById("ng-vlans"),
    search: document.getElementById("ng-search"),
    results: document.getElementById("ng-results"),
    matches: document.getElementById("ng-matches"),
    detail: document.getElementById("ng-detail"),
    counts: document.getElementById("ng-counts"),
    card: document.getElementById("ng-card"),
    keys: document.getElementById("ng-keys"),
    fit: document.getElementById("ng-fit"),
    reset: document.getElementById("ng-reset"),
    help: document.getElementById("ng-help")
  };

  var state = {
    layer: 0,
    showIps: data.options.showIps,
    showVlans: data.options.showVlans,
    namespace: "",
    query: "",
    selected: null
  };

  var view = { x: 0, y: 0, k: 1 };
  /** The id prefix every shape of the visible drawing carries; see html.py. */
  var prefix = "";
  /** Lazily built per layer: element id -> lower-cased searchable text. */
  var indexes = {};
  /** Lazily built per layer: element id -> record, put back together from the
   *  two pools the page carries. A record is written once for the whole page
   *  even when several layers draw the same device; only its `links` -- which
   *  edges reach it, which is precisely what a layer decides -- is stored per
   *  layer. See "a view costs its drawing, and nothing else" in html.py. */
  var assembled = {};

  /* ----------------------------------------------------------------- data */

  function layer() {
    return data.layers[state.layer];
  }

  function records() {
    if (!assembled[state.layer]) {
      var built = {};
      var elements = layer().elements;
      Object.keys(elements).forEach(function (id) {
        var where = elements[id];
        var record = data.records[where[0]];
        if (where[1] < 0) {
          built[id] = record;
          return;
        }
        // A shallow copy, so that two layers drawing one device each get their
        // own `links` and neither writes over the record they share.
        var merged = {};
        Object.keys(record).forEach(function (key) { merged[key] = record[key]; });
        merged.links = data.links[where[1]];
        built[id] = merged;
      });
      assembled[state.layer] = built;
    }
    return assembled[state.layer];
  }

  function recordOf(id) {
    return records()[id] || null;
  }

  function index() {
    if (!indexes[state.layer]) {
      var built = {};
      var elements = records();
      Object.keys(elements).forEach(function (id) {
        built[id] = netgraphDetail.haystack(elements[id]);
      });
      indexes[state.layer] = built;
    }
    return indexes[state.layer];
  }

  /** The namespace a record belongs to. An edge has none of its own, so it
   *  answers with its endpoints': a cable between two racks is in both. */
  function namespacesOf(record) {
    if (record.type !== "edge") { return [record.namespace || ""]; }
    return (record.endpoints || []).map(function (end) {
      var peer = end.element ? recordOf(end.element) : null;
      return peer ? (peer.namespace || "") : "";
    });
  }

  function inNamespace(record, wanted) {
    if (!wanted) { return true; }
    return namespacesOf(record).some(function (name) {
      return name === wanted || name.indexOf(wanted + "/") === 0;
    });
  }

  /* ----------------------------------------------------------------- view */

  /** The drawing that matches the current toggles, exactly or as closely as
   *  the document holds one. */
  function viewId() {
    var views = layer().views;
    var best = views[0];
    views.forEach(function (candidate) {
      if (candidate.showIps === state.showIps && candidate.showVlans === state.showVlans) {
        best = candidate;
      }
    });
    return best.view;
  }

  function showView() {
    var wanted = viewId();
    prefix = wanted;
    var panes = el.viewport.querySelectorAll(".view");
    for (var i = 0; i < panes.length; i++) {
      panes[i].hidden = panes[i].getAttribute("data-view") !== wanted;
    }
  }

  function groups() {
    var pane = el.viewport.querySelector('.view[data-view="' + prefix + '"]');
    return pane ? pane.querySelectorAll("g.node, g.edge") : [];
  }

  function groupFor(id) {
    return document.getElementById(prefix + "-" + id);
  }

  function idOf(group) {
    return group.id.indexOf(prefix + "-") === 0 ? group.id.slice(prefix.length + 1) : "";
  }

  function applyView() {
    el.viewport.style.transform =
      "translate(" + view.x + "px, " + view.y + "px) scale(" + view.k + ")";
  }

  /** Fit the whole diagram in the window.
   *
   * The <svg> has no width or height of its own -- the renderer takes them off
   * and leaves the viewBox -- so it already fills the stage at scale 1, and
   * fitting is putting the transform back to nothing. */
  function fit() {
    view.x = 0;
    view.y = 0;
    view.k = 1;
    applyView();
  }

  function zoomAt(clientX, clientY, factor) {
    var box = el.stage.getBoundingClientRect();
    var x = clientX - box.left;
    var y = clientY - box.top;
    var next = Math.min(MAX_SCALE, Math.max(MIN_SCALE, view.k * factor));
    // Keep the point under the pointer where it is: solve for the translation
    // that maps it to the same screen position at the new scale.
    view.x = x - (x - view.x) * (next / view.k);
    view.y = y - (y - view.y) * (next / view.k);
    view.k = next;
    applyView();
  }

  function panBy(dx, dy) {
    view.x += dx;
    view.y += dy;
    applyView();
  }

  /** Bring ``group`` into the window, if it is not already in it. */
  function reveal(group) {
    var stage = el.stage.getBoundingClientRect();
    var box = group.getBoundingClientRect();
    if (
      box.left >= stage.left && box.right <= stage.right &&
      box.top >= stage.top && box.bottom <= stage.bottom
    ) {
      return;
    }
    panBy(
      stage.left + stage.width / 2 - (box.left + box.width / 2),
      stage.top + stage.height / 2 - (box.top + box.height / 2)
    );
  }

  /* --------------------------------------------------------------- search */

  function matches() {
    var query = state.query.trim().toLowerCase();
    var text = index();
    var found = [];
    Object.keys(records()).forEach(function (id) {
      var record = records()[id];
      if (!inNamespace(record, state.namespace)) { return; }
      if (query && text[id].indexOf(query) === -1) { return; }
      found.push(id);
    });
    return found;
  }

  /** Dim everything the current query and namespace leave out. */
  function paint(found) {
    var filtering = Boolean(state.query.trim() || state.namespace);
    var wanted = {};
    found.forEach(function (id) { wanted[id] = true; });
    // The drawing that was on screen a moment ago is still in the document,
    // hidden, wearing the classes it was last painted with. Clearing them here
    // rather than on the way out means there is one place where what a shape
    // looks like is decided.
    var stale = el.viewport.querySelectorAll("g.ng-dim, g.ng-match, g.ng-selected");
    for (var s = 0; s < stale.length; s++) {
      stale[s].classList.remove("ng-dim", "ng-match", "ng-selected");
    }
    var all = groups();
    for (var i = 0; i < all.length; i++) {
      var group = all[i];
      var id = idOf(group);
      var hit = Boolean(wanted[id]);
      group.classList.toggle("ng-dim", filtering && !hit);
      group.classList.toggle("ng-match", filtering && hit && Boolean(state.query.trim()));
      group.classList.toggle("ng-selected", id === state.selected);
    }
  }

  function listResults(found) {
    el.results.replaceChildren();
    var query = state.query.trim();
    if (!query) {
      el.matches.textContent = "";
      return;
    }
    el.matches.textContent = found.length + (found.length === 1 ? " match" : " matches");
    found.slice(0, MAX_RESULTS).forEach(function (id) {
      var record = recordOf(id);
      var item = document.createElement("li");
      var button = document.createElement("button");
      button.type = "button";
      button.appendChild(netgraphDetail.element("span", "name", netgraphDetail.label(record)));
      button.appendChild(document.createTextNode(" "));
      button.appendChild(netgraphDetail.element("span", "kind", "[" + record.kind + "]"));
      if (id === state.selected) { button.setAttribute("aria-current", "true"); }
      button.addEventListener("click", function () { select(id, true); });
      item.appendChild(button);
      el.results.appendChild(item);
    });
    if (found.length > MAX_RESULTS) {
      var note = document.createElement("li");
      note.className = "empty";
      note.textContent = found.length - MAX_RESULTS + " more not listed";
      el.results.appendChild(note);
    }
  }

  /* --------------------------------------------------------------- detail */

  /** The document that declares this element, when --link-template built one.
   *
   * The URL is not in the records: it is an attribute of the drawing, on the
   * anchor Graphviz wrapped the shape in, so it is read back off the shape
   * rather than carried a second time. */
  function sourceUrl(id) {
    var group = groupFor(id);
    var anchor = group ? group.querySelector("a") : null;
    if (!anchor) { return ""; }
    return anchor.getAttribute("href") ||
      anchor.getAttributeNS("http://www.w3.org/1999/xlink", "href") || "";
  }

  function showDetail() {
    el.detail.replaceChildren();
    var record = state.selected ? recordOf(state.selected) : null;
    if (!record) {
      el.detail.appendChild(
        netgraphDetail.element("p", "empty", "select an element to see its configuration")
      );
      return;
    }
    el.detail.appendChild(
      netgraphDetail.describe(record, { showIps: state.showIps, showVlans: state.showVlans })
    );
    var url = sourceUrl(state.selected);
    if (url) {
      var link = netgraphDetail.element("a", "source", "open the document that declares it");
      link.href = url;
      link.rel = "noreferrer noopener";
      el.detail.appendChild(link);
    }
  }

  /* ------------------------------------------------------------ selection */

  function select(id, bring) {
    state.selected = records()[id] ? id : null;
    if (state.selected && bring) {
      var group = groupFor(state.selected);
      if (group) { reveal(group); }
    }
    writeHash();
    refresh();
  }

  /* ---------------------------------------------------------- deep links */

  /** ``#node-office_sw-core`` in a one-layer document, ``#l3:node-...`` when
   *  the page holds several: an id is only unique within its own layer. */
  function fragmentFor(id) {
    return data.layers.length > 1 ? layer().layer + ":" + id : id;
  }

  var writingHash = false;

  function writeHash() {
    writingHash = true;
    try {
      var wanted = state.selected ? "#" + fragmentFor(state.selected) : "";
      if (wanted) {
        if (window.location.hash !== wanted) { window.location.hash = wanted; }
      } else if (window.location.hash) {
        try {
          // Clearing the fragment without a history entry for the empty one.
          window.history.replaceState(null, "", window.location.pathname + window.location.search);
        } catch (error) {
          // A page opened from a file:// URL is the case this format exists
          // for, and that is the one where some browsers refuse to rewrite the
          // history entry. Leaving a stale "#" behind is a far better outcome
          // than an exception halfway through deselecting.
          window.location.hash = "";
        }
      }
    } finally {
      writingHash = false;
    }
  }

  function readHash() {
    if (writingHash) { return; }
    var raw = decodeURIComponent(window.location.hash.replace(/^#/, ""));
    if (!raw) { return; }
    var wanted = raw;
    var colon = raw.indexOf(":");
    if (colon > 0) {
      var named = raw.slice(0, colon);
      data.layers.forEach(function (candidate, position) {
        if (candidate.layer === named) {
          state.layer = position;
          if (el.layer) { el.layer.value = String(position); }
        }
      });
      wanted = raw.slice(colon + 1);
    } else {
      // A fragment written before this page grew a layer switcher, or one
      // copied from an SVG deep link: take the first layer that draws it.
      data.layers.forEach(function (candidate, position) {
        if (candidate.elements[wanted] && !data.layers[state.layer].elements[wanted]) {
          state.layer = position;
          if (el.layer) { el.layer.value = String(position); }
        }
      });
    }
    showView();
    state.selected = records()[wanted] ? wanted : null;
    refresh();
    if (state.selected) {
      var group = groupFor(state.selected);
      if (group) { reveal(group); }
    }
  }

  /* --------------------------------------------------------------- render */

  function refresh() {
    showView();
    var found = matches();
    paint(found);
    listResults(found);
    showDetail();
    el.counts.textContent =
      layer().nodes + (layer().nodes === 1 ? " node, " : " nodes, ") +
      layer().edges + (layer().edges === 1 ? " edge" : " edges");
  }

  /* ----------------------------------------------------------- hover card */

  function hideCard() {
    el.card.hidden = true;
    el.card.replaceChildren();
  }

  function showCard(id, event) {
    var record = recordOf(id);
    if (!record) { hideCard(); return; }
    el.card.replaceChildren(
      netgraphDetail.describe(record, { showIps: state.showIps, showVlans: state.showVlans })
    );
    el.card.hidden = false;
    var margin = 14;
    var box = el.card.getBoundingClientRect();
    var x = event.clientX + margin;
    var y = event.clientY + margin;
    if (x + box.width > window.innerWidth - margin) { x = event.clientX - box.width - margin; }
    if (y + box.height > window.innerHeight - margin) { y = event.clientY - box.height - margin; }
    el.card.style.left = Math.max(margin, x) + "px";
    el.card.style.top = Math.max(margin, y) + "px";
  }

  /* ------------------------------------------------------------- controls */

  function groupAt(target) {
    var group = target && target.closest ? target.closest("g.node, g.edge") : null;
    return group ? idOf(group) : "";
  }

  if (el.layer) {
    el.layer.addEventListener("change", function () {
      state.layer = parseInt(el.layer.value, 10) || 0;
      state.selected = null;
      writeHash();
      refresh();
    });
  }

  if (el.namespace) {
    el.namespace.addEventListener("change", function () {
      state.namespace = el.namespace.value;
      refresh();
    });
  }

  if (el.ips) {
    el.ips.addEventListener("change", function () {
      state.showIps = el.ips.checked;
      refresh();
    });
  }

  if (el.vlans) {
    el.vlans.addEventListener("change", function () {
      state.showVlans = el.vlans.checked;
      refresh();
    });
  }

  el.search.addEventListener("input", function () {
    state.query = el.search.value;
    refresh();
  });

  el.search.addEventListener("keydown", function (event) {
    if (event.key !== "Enter") { return; }
    var found = matches();
    if (found.length) { select(found[0], true); }
  });

  el.fit.addEventListener("click", fit);

  el.reset.addEventListener("click", function () {
    state.query = "";
    state.namespace = "";
    state.selected = null;
    state.showIps = data.options.showIps;
    state.showVlans = data.options.showVlans;
    el.search.value = "";
    if (el.namespace) { el.namespace.value = ""; }
    if (el.ips) { el.ips.checked = state.showIps; }
    if (el.vlans) { el.vlans.checked = state.showVlans; }
    fit();
    writeHash();
    refresh();
  });

  el.help.addEventListener("click", function () {
    var hidden = !el.keys.hidden;
    el.keys.hidden = hidden;
    el.help.setAttribute("aria-expanded", hidden ? "false" : "true");
  });

  el.stage.addEventListener("click", function (event) {
    var anchor = event.target.closest ? event.target.closest("a") : null;
    if (anchor && (event.metaKey || event.ctrlKey || event.shiftKey)) {
      return;  // a modified click follows the --link-template link
    }
    // A click always follows a drag, and letting go of the mouse over a device
    // you happened to drag past is not a request to select it.
    if (anchor) { event.preventDefault(); }
    if (dragged) { dragged = false; return; }
    select(groupAt(event.target), false);
    el.stage.focus({ preventScroll: true });
  });

  el.stage.addEventListener("pointermove", function (event) {
    if (!data.options.tooltips || event.pointerType !== "mouse" || pointers.size) { return; }
    var id = groupAt(event.target);
    if (id) { showCard(id, event); } else { hideCard(); }
  });

  el.stage.addEventListener("pointerleave", hideCard);

  el.stage.addEventListener("wheel", function (event) {
    event.preventDefault();
    zoomAt(event.clientX, event.clientY, event.deltaY < 0 ? 1.12 : 1 / 1.12);
  }, { passive: false });

  el.stage.addEventListener("keydown", function (event) {
    var step = PAN_STEP * (event.shiftKey ? PAN_LEAP : 1);
    var handled = true;
    switch (event.key) {
      case "ArrowLeft": panBy(step, 0); break;
      case "ArrowRight": panBy(-step, 0); break;
      case "ArrowUp": panBy(0, step); break;
      case "ArrowDown": panBy(0, -step); break;
      case "+":
      case "=": zoomAt(centreX(), centreY(), 1.2); break;
      case "-":
      case "_": zoomAt(centreX(), centreY(), 1 / 1.2); break;
      case "0":
      case "f": fit(); break;
      default: handled = false;
    }
    if (handled) { event.preventDefault(); }
  });

  function centreX() {
    var box = el.stage.getBoundingClientRect();
    return box.left + box.width / 2;
  }

  function centreY() {
    var box = el.stage.getBoundingClientRect();
    return box.top + box.height / 2;
  }

  document.addEventListener("keydown", function (event) {
    var typing = /^(input|textarea|select)$/i.test((event.target.tagName || ""));
    if (event.key === "Escape") {
      hideCard();
      if (typing) { event.target.blur(); }
      if (state.query || state.selected) {
        state.query = "";
        el.search.value = "";
        state.selected = null;
        writeHash();
        refresh();
      }
      return;
    }
    if (typing || event.metaKey || event.ctrlKey || event.altKey) { return; }
    if (event.key === "/") {
      event.preventDefault();
      el.search.focus();
      el.search.select();
    } else if (event.key === "?") {
      el.help.click();
    }
  });

  window.addEventListener("hashchange", readHash);

  /* --------------------------------------------------- pointer: pan, pinch */

  var pointers = new Map();
  var origin = null;
  var pinch = null;
  /** Did the pointer move between going down and coming up? Read, and reset,
   *  by the click that follows. */
  var dragged = false;
  /** How far a pointer may travel and still count as a click, in pixels. */
  var SLOP = 4;

  // Deliberately no setPointerCapture: a captured pointer retargets the click
  // that follows it to the capturing element, and this page decides what was
  // clicked by looking at the shape under the pointer. The move and up
  // listeners are on the window instead, so a drag that leaves the diagram
  // still pans.
  el.stage.addEventListener("pointerdown", function (event) {
    if (event.pointerType === "mouse" && event.button !== 0) { return; }
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.size === 1) {
      origin = { x: event.clientX - view.x, y: event.clientY - view.y, from: event.clientX };
      dragged = false;
      el.stage.classList.add("panning");
    } else if (pointers.size === 2) {
      origin = null;
      pinch = spread();
      hideCard();
    }
  });

  window.addEventListener("pointermove", function (event) {
    if (!pointers.has(event.pointerId)) { return; }
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.size === 2 && pinch) {
      var now = spread();
      if (pinch.distance > 0 && now.distance > 0) {
        zoomAt(now.x, now.y, now.distance / pinch.distance);
      }
      pinch = now;
      return;
    }
    if (!origin) { return; }
    var next = { x: event.clientX - origin.x, y: event.clientY - origin.y };
    if (Math.abs(next.x - view.x) + Math.abs(next.y - view.y) > SLOP) { dragged = true; }
    view.x = next.x;
    view.y = next.y;
    hideCard();
    applyView();
  });

  function release(event) {
    pointers.delete(event.pointerId);
    if (pointers.size < 2) { pinch = null; }
    if (!pointers.size) {
      origin = null;
      el.stage.classList.remove("panning");
    }
  }

  window.addEventListener("pointerup", release);
  window.addEventListener("pointercancel", release);

  /** The distance between the two live pointers, and the point between them. */
  function spread() {
    var live = Array.from(pointers.values());
    var dx = live[0].x - live[1].x;
    var dy = live[0].y - live[1].y;
    return {
      distance: Math.sqrt(dx * dx + dy * dy),
      x: (live[0].x + live[1].x) / 2,
      y: (live[0].y + live[1].y) / 2
    };
  }

  /* ----------------------------------------------------------------- boot */

  showView();
  refresh();
  readHash();
})();
