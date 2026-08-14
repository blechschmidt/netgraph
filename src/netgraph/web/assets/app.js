/* The netgraph web interface.
 *
 * Dependency-free on purpose: this page is served by a local Python process
 * that has no build step, and a bundler would put a toolchain between a user
 * and the diagram they asked for.
 *
 * This file is the shell -- the canvas, the info box, the problems list, the
 * status line and the editor -- and it works the same either way the command
 * was started. What is *behind* the editor differs, and the page asks the
 * server which it is at boot:
 *
 *   stream    `netgraph web` on a file, a pipe or nothing. The text lives in
 *             the browser and is posted to /api/render whenever it settles.
 *             Nothing is on disk; nothing is written.
 *   session   `netgraph web DIR`. The server holds the inventory tree, and
 *             session.js takes over the file list, saving, undo and the
 *             reconciliation with whatever else is editing the same files.
 *
 * Three things happen here and nothing else:
 *
 *   1. The diagram is fetched -- posted as text in stream mode, requested by
 *      view in session mode -- and the SVG that comes back replaces the one on
 *      screen.
 *   2. The pointer's position over that SVG is turned into an info box, by
 *      looking up the id of the <g> under it in the records the same response
 *      carried. No request is made on hover; the details are already here.
 *   3. The canvas pans and zooms, which is CSS on a wrapper -- the SVG itself
 *      is never rewritten, so the ids the info box depends on stay put.
 *
 * What an info box looks like is not here: it is netgraphDetail, in
 * netgraph/render/assets/detail.js, shared with the page `netgraph render
 * -f html` writes. Everything a record contains is inserted with textContent
 * there. The only markup this file ever assigns is the SVG the server
 * sanitised.
 */

(function () {
  "use strict";

  /** How long the editor has to settle before a render is asked for. */
  var DEBOUNCE_MS = 450;
  /** Zoom bounds. Below the first the diagram is a smudge; above the second a pixel. */
  var MIN_SCALE = 0.1;
  var MAX_SCALE = 12;
  /** How long a toast stays up, in milliseconds. */
  var TOAST_MS = 4000;

  var el = {
    source: document.getElementById("source"),
    status: document.getElementById("status"),
    summary: document.getElementById("summary"),
    problems: document.getElementById("problems"),
    problemCounts: document.getElementById("problem-counts"),
    viewport: document.getElementById("viewport"),
    canvas: document.getElementById("canvas"),
    placeholder: document.getElementById("placeholder"),
    info: document.getElementById("info"),
    toast: document.getElementById("toast"),
    layer: document.getElementById("layer"),
    vlans: document.getElementById("vlans"),
    showIps: document.getElementById("show-ips"),
    showVlans: document.getElementById("show-vlans"),
    group: document.getElementById("group"),
    strict: document.getElementById("strict"),
    render: document.getElementById("render"),
    fit: document.getElementById("fit"),
    splitter: document.getElementById("splitter"),
    files: document.getElementById("files"),
    fileList: document.getElementById("file-list"),
    filesRoot: document.getElementById("files-root"),
    filesMode: document.getElementById("files-mode"),
    actions: document.getElementById("session-actions"),
    save: document.getElementById("save"),
    undo: document.getElementById("undo"),
    redo: document.getElementById("redo"),
    editorTitle: document.getElementById("editor-title"),
    editorHint: document.getElementById("editor-hint"),
    editorState: document.getElementById("editor-state"),
    changes: document.getElementById("changes"),
    changesList: document.getElementById("changes-list"),
    changesToggle: document.getElementById("changes-toggle"),
    changesClose: document.getElementById("changes-close"),
    changesCopy: document.getElementById("changes-copy"),
    changesCount: document.getElementById("changes-count"),
    changesAgainst: document.getElementById("changes-against"),
    legend: document.getElementById("legend"),
    clients: document.getElementById("clients"),
    linkState: document.getElementById("link-state"),
    /* Accessibility furniture. The two live regions are the only place this
     * page says anything to a screen reader; the outline is the diagram as
     * text. See a11y.js. */
    announcer: document.getElementById("announcer"),
    alert: document.getElementById("alert"),
    outline: document.getElementById("outline"),
    outlineList: document.getElementById("outline-list"),
    outlineSummary: document.getElementById("outline-summary"),
    commands: document.getElementById("commands"),
    commandsKey: document.getElementById("commands-key"),
    shortcuts: document.getElementById("shortcuts"),
    shortcutsKey: document.getElementById("shortcuts-key")
  };

  /** How many rendered views are kept in memory, most recently drawn first.
   *  A handful: the layer menu has nine entries and nobody cycles all of them,
   *  and each entry holds an SVG document. */
  var MAX_VIEWS = 6;

  var details = {};
  var pending = null;
  var inFlight = false;
  var queued = false;
  var pinned = null;
  var view = { x: 0, y: 0, k: 1, placed: false };
  /** "stream" until /api/state says otherwise. */
  var mode = "stream";
  var toastTimer = null;
  /** What has been drawn, by request URL: the fingerprint the server gave it,
   *  the SVG and the records. This is what makes "the tree moved but this layer
   *  did not" cost a round trip instead of a Graphviz run -- and what makes
   *  switching back to a layer nothing touched instant. */
  var views = {};
  var viewOrder = [];
  var currentView = null;
  /** Element addresses somebody else has selected, drawn faintly. */
  var remote = {};

  /* ------------------------------------------------------------ requests */

  function options() {
    return {
      source: el.source.value,
      layer: el.layer.value,
      vlans: parseVlans(el.vlans.value),
      show_ips: el.showIps.checked,
      show_vlans: el.showVlans.checked,
      group_by_namespace: el.group.checked,
      strict: el.strict.checked
    };
  }

  /** The same view options as a query string, for the session's GET. */
  function query() {
    var parts = [
      "view=" + encodeURIComponent(el.layer.value),
      "show_ips=" + (el.showIps.checked ? "1" : "0"),
      "show_vlans=" + (el.showVlans.checked ? "1" : "0"),
      "group_by_namespace=" + (el.group.checked ? "1" : "0"),
      "strict=" + (el.strict.checked ? "1" : "0")
    ];
    var vlans = parseVlans(el.vlans.value);
    if (vlans.length) { parts.push("vlans=" + vlans.join(",")); }
    return parts.join("&");
  }

  function parseVlans(text) {
    var ids = [];
    text.split(/[\s,]+/).forEach(function (part) {
      if (!/^\d+$/.test(part)) { return; }
      var id = parseInt(part, 10);
      if (id >= 1 && id <= 4094 && ids.indexOf(id) === -1) { ids.push(id); }
    });
    return ids;
  }

  function render() {
    if (inFlight) { queued = true; return; }
    inFlight = true;
    setStatus("rendering", "");
    // Which of the two the session wants -- the tree, or the tree as a diff
    // against a baseline -- is session.js's decision; this file only draws what
    // comes back, and a diff comes back in the same shape. The URL doubles as
    // the cache key: two requests that differ in any way that could change the
    // picture differ here too.
    var key = mode === "session" ? netgraphSession.graphPath(query()) : null;
    request(key).then(function (response) {
      return response.json().then(function (body) {
        if (!response.ok) { throw new Error(body.message || response.statusText); }
        return body;
      });
    }).then(function (body) { apply(body, key); }).catch(function (error) {
      setStatus("failed", String(error.message || error));
      showProblems([]);
    }).then(function () {
      inFlight = false;
      if (queued) { queued = false; render(); }
    });
  }

  function request(key) {
    if (key !== null) {
      // Send the fingerprint of the picture already in hand. When the server
      // works out that this revision would draw the same one, it says so and
      // runs no layout -- which on a large inventory is the whole cost of an
      // edit that did not touch the drawn layer.
      var held = views[key];
      return fetch(key + (held ? "&known=" + encodeURIComponent(held.hash) : ""),
        { cache: "no-store" });
    }
    return fetch("/api/render", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
      body: JSON.stringify(options())
    });
  }

  function schedule() {
    window.clearTimeout(pending);
    pending = window.setTimeout(render, DEBOUNCE_MS);
  }

  /** Put one rendering on screen.
   *
   * `result.unchanged` says the server recognised the fingerprint we sent and
   * stopped before Graphviz: there is no SVG in the response because we already
   * have it. Two cases follow, and the difference between them matters:
   *
   *   * it is the view already on screen -- the DOM is not touched at all, so
   *     the pan, the zoom and the scroll position survive an edit elsewhere in
   *     the tree;
   *   * it is a view we drew earlier -- the SVG comes back out of the cache,
   *     with no round trip to Graphviz on either side.
   *
   * Everything that is *not* the picture -- the status line, the problems, the
   * counts -- comes from the response either way, because those move when the
   * drawing does not. That is the whole reason `unchanged` is a flag on a real
   * answer rather than a 304.
   */
  function apply(result, key) {
    var held = key ? views[key] : null;
    var reuse = !!(result.unchanged && held);
    if (result.unchanged && !held) {
      // We claimed to hold a drawing we do not: only possible if the cache was
      // evicted between the request and the answer. Ask again without the claim.
      queued = true;
      return;
    }
    details = reuse ? held.details : (result.details || {});
    // A diff is drawn by the same renderer into the same canvas; what marks the
    // page as showing one is the legend, which is furniture without it.
    el.canvas.classList.toggle("diffing", !!result.diff);
    el.legend.hidden = !result.diff;
    setStatus(result.status, result.message, result.counts, result.durationMs);
    showProblems(result.problems || [], result.dangling || []);
    hideInfo(true);
    var svg = reuse ? held.svg : result.svg;
    if (svg) {
      if (!reuse || key !== currentView) {
        el.viewport.innerHTML = svg;
        if (!view.placed) { view.placed = true; resetView(); }
      }
      el.placeholder.hidden = true;
      if (key) { remember(key, result.graphHash || held.hash, svg, details); }
      currentView = key;
      paintRemote();
    } else {
      el.viewport.replaceChildren();
      el.placeholder.hidden = false;
      el.placeholder.textContent = result.message || "nothing rendered";
      currentView = null;
    }
    // The picture is inert until this runs: roles, labels, the outline and the
    // focus ring all come off the records above. Done on every apply, including
    // the ones that reuse a cached SVG -- a view switched back to has to be as
    // legible as one drawn fresh.
    netgraphA11y.annotate(details, { view: el.layer.value });
  }

  /** Keep this view's drawing, dropping the least recently drawn if need be. */
  function remember(key, hash, svg, records) {
    if (!hash) { return; }
    views[key] = { hash: hash, svg: svg, details: records };
    viewOrder = viewOrder.filter(function (other) { return other !== key; });
    viewOrder.unshift(key);
    while (viewOrder.length > MAX_VIEWS) { delete views[viewOrder.pop()]; }
  }

  /* ------------------------------------------------------ other people */

  /** Draw what somebody else has selected, faintly.
   *
   * Their selection, not ours: it is drawn as an outline rather than the
   * highlight a hover gives, so that the two are never mistaken for each other.
   * Advisory, like everything else about presence -- nothing here stops a click.
   */
  function setRemote(addresses) {
    remote = {};
    (addresses || []).forEach(function (address) { remote[address] = true; });
    paintRemote();
  }

  function paintRemote() {
    var svg = el.viewport.firstElementChild;
    if (!svg) { return; }
    svg.querySelectorAll("g.remote").forEach(function (group) {
      group.classList.remove("remote");
    });
    svg.querySelectorAll("g.node, g.edge").forEach(function (group) {
      var record = details[group.id];
      if (record && remote[record.id]) { group.classList.add("remote"); }
    });
  }

  /* -------------------------------------------------------------- status */

  function setStatus(status, message, counts, durationMs) {
    el.status.className = "pill " + status;
    el.status.textContent = status;
    // The message already counts what was drawn; see preview.Preview.message.
    var parts = [];
    if (message) { parts.push(message); }
    if (counts && (counts.errors || counts.warnings)) {
      parts.push(counts.errors + " errors, " + counts.warnings + " warnings");
    }
    if (typeof durationMs === "number") { parts.push(durationMs + " ms"); }
    el.summary.textContent = parts.join("  ·  ");
  }

  function showProblems(problems, dangling) {
    el.problems.replaceChildren();
    (dangling || []).forEach(function (text) {
      problems = problems.concat([
        { severity: "warning", location: "-", rule: "graph", message: "dropped: " + text }
      ]);
    });
    if (!problems.length) {
      var none = document.createElement("p");
      none.className = "empty";
      none.textContent = "nothing to report";
      el.problems.appendChild(none);
      el.problemCounts.textContent = "";
      return;
    }
    var counts = { error: 0, warning: 0, info: 0 };
    problems.forEach(function (problem) {
      counts[problem.severity] = (counts[problem.severity] || 0) + 1;
      var target = navigation(problem.location);
      // A row that does something is a <button>: in the tab order, activated by
      // Enter and by Space, announced as a control. A row that does nothing is
      // a <div>, because a button that does nothing is a lie.
      var row = document.createElement(target ? "button" : "div");
      row.className = "problem " + problem.severity;
      if (target) {
        row.type = "button";
        row.classList.add("locatable");
        row.title = target.title;
        row.addEventListener("click", target.go);
      }
      [
        ["severity", problem.severity],
        ["where", problem.location],
        ["rule", problem.rule],
        ["message", problem.message]
      ].forEach(function (pair) {
        var cell = document.createElement("span");
        cell.className = pair[0];
        cell.textContent = pair[1];
        row.appendChild(cell);
      });
      var repairs = fixButtons(problem);
      if (!repairs) {
        el.problems.appendChild(row);
        return;
      }
      // A button inside a button is not markup a browser will keep, so the row
      // and its repairs are siblings in a wrapper rather than nested.
      var entry = document.createElement("div");
      entry.className = "problem-entry";
      entry.appendChild(row);
      entry.appendChild(repairs);
      el.problems.appendChild(entry);
    });
    el.problemCounts.textContent =
      counts.error + " errors, " + counts.warning + " warnings, " + counts.info + " notes";
  }

  /** The Fix control for a diagnostic, or null when nothing can repair it.
   *
   * One button when the rule offers one repair, and one per repair when it
   * offers several -- because choosing between them is the user's to make, and
   * a single button that picked for them would be picking for them. Each says
   * what it would do in its tooltip, which is the same sentence
   * `netgraph validate --fix` prints.
   */
  function fixButtons(problem) {
    var offered = problem.fixes || [];
    if (!offered.length || mode !== "session" || !netgraphSession.isWritable()) { return null; }
    var box = document.createElement("div");
    box.className = "fixes";
    offered.forEach(function (offer) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "fix";
      button.textContent = offered.length > 1 ? "fix: " + offer.key : "fix";
      button.title = offer.title;
      button.setAttribute("aria-label", offer.title);
      button.addEventListener("click", function () {
        netgraphSession.fix(problem, offered.length > 1 ? offer.key : null);
      });
      box.appendChild(button);
    });
    return box;
  }

  /** What clicking a problem should do, or null when it points nowhere.
   *
   * A location reads `switches/sw.yaml#3:17` -- file, document index, line. In
   * a session both halves are actionable, so the file is opened and the cursor
   * put on the line; in the scratchpad there is only one document and only the
   * line means anything. See netgraph.loader.LoadError.location for the shape.
   */
  function navigation(location) {
    if (mode === "session") {
      return {
        title: "open " + location,
        go: function () {
          if (!netgraphSession.locate(location)) { toast("no file at " + location, "error"); }
        }
      };
    }
    var line = lineOf(location);
    return line ? { title: "go to line " + line, go: function () { goToLine(line); } } : null;
  }

  function lineOf(location) {
    var match = /#\d+:(\d+)/.exec(location || "");
    return match ? parseInt(match[1], 10) : 0;
  }

  /** Put the cursor on `line` of the editor and select it.
   *
   * `options.focus === false` leaves the keyboard where it is. That is what
   * revealing from the *diagram* wants: somebody who pressed Enter on a node
   * asked to see the document, not to be dropped into the middle of it -- and
   * dragging the focus out of the canvas would end the arrow-key navigation
   * they were in the middle of. Clicking a diagnostic is the other case, and
   * does move the caret, because "go to this line" is the whole request.
   */
  function goToLine(line, options) {
    var lines = el.source.value.split("\n");
    if (!line || line > lines.length) { return; }
    var start = 0;
    for (var i = 0; i < line - 1; i++) { start += lines[i].length + 1; }
    if (!options || options.focus !== false) { el.source.focus(); }
    el.source.setSelectionRange(start, start + lines[line - 1].length);
    // No API positions a textarea's scroll on a line, so estimate it from the
    // line height and centre the selection in the visible part.
    var height = parseFloat(window.getComputedStyle(el.source).lineHeight) || 18;
    el.source.scrollTop = Math.max(0, (line - 1) * height - el.source.clientHeight / 2);
  }

  /* --------------------------------------------------------------- toast */

  /** Say something for a moment: what was saved, what was refused, why.
   *
   * The bubble is `aria-hidden`; the words go to a live region instead, so they
   * are heard once rather than twice and are heard even after the bubble has
   * gone. A refusal interrupts, because "that was not applied" is not something
   * to learn three actions later.
   */
  function toast(text, kind) {
    window.clearTimeout(toastTimer);
    el.toast.textContent = text;
    el.toast.className = "toast " + (kind || "");
    el.toast.hidden = false;
    toastTimer = window.setTimeout(function () { el.toast.hidden = true; }, TOAST_MS);
    netgraphA11y.announce(text, kind === "error");
  }

  /* ------------------------------------------------------------ info box */

  function recordAt(target) {
    var group = target.closest ? target.closest("g.node, g.edge") : null;
    return group && details[group.id] ? { group: group, record: details[group.id] } : null;
  }

  function showInfo(hit, at) {
    el.info.replaceChildren(describe(hit.record));
    el.info.hidden = false;
    place(at);
    highlight(hit.record);
  }

  /** The inspector for whatever the keyboard has focused.
   *
   * Same box, same records, same highlight as the hover path -- the only thing
   * that differs is where it is anchored, because there is no pointer to anchor
   * it to. It is pinned, since a keyboard user has nothing to move away.
   */
  function inspectFocused() {
    var here = netgraphA11y.focused();
    if (!here) { toast("nothing is focused in the diagram", "error"); return false; }
    var group = el.viewport.querySelector('[id="' + cssEscape(here.element) + '"]');
    var box = group ? group.getBoundingClientRect() : null;
    pinned = here.element;
    showInfo({ record: here.record }, box
      ? { clientX: box.right, clientY: box.bottom }
      : { clientX: window.innerWidth / 2, clientY: window.innerHeight / 3 });
    el.info.classList.add("pinned");
    if (mode === "session") {
      netgraphSession.reveal(here.record.id);
      netgraphSession.select(here.record.id);
      netgraphA11y.select(here.element);
    }
    netgraphA11y.announce("inspecting " + netgraphA11y.label(here.record), false);
    return true;
  }

  function cssEscape(value) {
    return window.CSS && window.CSS.escape ? window.CSS.escape(value) : value;
  }

  function hideInfo(force) {
    if (pinned && !force) { return; }
    pinned = null;
    el.info.hidden = true;
    el.info.classList.remove("pinned");
    highlight(null);
  }

  /** `at` is anything with clientX/clientY: a mouse event, or a corner of the
   *  focused shape when the keyboard opened the box. */
  function place(at) {
    var margin = 14;
    var box = el.info.getBoundingClientRect();
    var x = at.clientX + margin;
    var y = at.clientY + margin;
    if (x + box.width > window.innerWidth - margin) { x = at.clientX - box.width - margin; }
    if (y + box.height > window.innerHeight - margin) { y = at.clientY - box.height - margin; }
    el.info.style.left = Math.max(margin, x) + "px";
    el.info.style.top = Math.max(margin, y) + "px";
  }

  /** Lift the hovered element and everything it touches out of the diagram. */
  function highlight(record) {
    var svg = el.viewport.firstElementChild;
    if (!svg) { return; }
    svg.querySelectorAll("g.hot, g.faded").forEach(function (group) {
      group.classList.remove("hot", "faded");
    });
    if (!record) { return; }
    var related = {};
    related[record.element] = true;
    (record.links || []).forEach(function (link) {
      related[link.element] = true;
      if (link.peerElement) { related[link.peerElement] = true; }
    });
    (record.endpoints || []).forEach(function (endpoint) {
      if (endpoint.element) { related[endpoint.element] = true; }
    });
    svg.querySelectorAll("g.node, g.edge").forEach(function (group) {
      group.classList.add(related[group.id] ? "hot" : "faded");
    });
  }

  /* ------------------------------------------------- info box: rendering */

  /* How a record is drawn is netgraphDetail's, in
   * netgraph/render/assets/detail.js, and is shared with the self-contained
   * page `netgraph render -f html` writes: the two front ends show the same
   * records, and one of them quietly growing a column the other lacks is
   * exactly the drift that file exists to prevent.
   *
   * The preview shows everything a record holds -- the diagram's own --show-ips
   * and --show-vlans decide what the *picture* prints, and hiding an address
   * from a label is a decision about legibility, not about secrecy -- so only
   * the pin hint is passed. */
  function describe(record) {
    return netgraphDetail.describe(record, {
      hint: pinned ? "click to unpin" : "click to pin"
    });
  }

  /* --------------------------------------------------------- pan & zoom */

  function applyView() {
    el.viewport.style.transform =
      "translate(" + view.x + "px, " + view.y + "px) scale(" + view.k + ")";
  }

  function resetView() {
    view.x = 0;
    view.y = 0;
    view.k = 1;
    applyView();
  }

  function zoomAt(clientX, clientY, factor) {
    var box = el.canvas.getBoundingClientRect();
    var x = clientX - box.left;
    var y = clientY - box.top;
    var next = Math.min(MAX_SCALE, Math.max(MIN_SCALE, view.k * factor));
    // Keep the point under the cursor where it is: solve for the translation
    // that maps it to the same screen position at the new scale.
    view.x = x - (x - view.x) * (next / view.k);
    view.y = y - (y - view.y) * (next / view.k);
    view.k = next;
    applyView();
  }

  /** Zoom about the middle of the canvas: what a keyboard means by "zoom in",
   *  there being no cursor to keep a point under. */
  function zoomCentre(factor) {
    var box = el.canvas.getBoundingClientRect();
    zoomAt(box.left + box.width / 2, box.top + box.height / 2, factor);
  }

  /** Pan until `box` -- a shape's rectangle in client coordinates -- is on
   *  screen, with a margin. Called when the keyboard focuses something that has
   *  been scrolled or zoomed out of sight; a no-op when it is already visible.
   */
  function bringIntoView(box) {
    var frame = el.canvas.getBoundingClientRect();
    var margin = 40;
    var dx = 0;
    var dy = 0;
    if (box.left < frame.left + margin) { dx = frame.left + margin - box.left; }
    else if (box.right > frame.right - margin) { dx = frame.right - margin - box.right; }
    if (box.top < frame.top + margin) { dy = frame.top + margin - box.top; }
    else if (box.bottom > frame.bottom - margin) { dy = frame.bottom - margin - box.bottom; }
    if (!dx && !dy) { return; }
    view.x += dx;
    view.y += dy;
    applyView();
  }

  /* ---------------------------------------------------------- listeners */

  el.source.addEventListener("input", function () {
    if (mode === "session") {
      // A session writes files, so nothing is written until Save. The diagram
      // keeps showing what is on disk, which is what it is a diagram of.
      netgraphSession.markDirty();
      return;
    }
    schedule();
  });

  el.source.addEventListener("keydown", function (event) {
    if (event.key !== "Tab") { return; }
    // A YAML editor that loses focus on Tab is not an editor.
    event.preventDefault();
    var start = el.source.selectionStart;
    var end = el.source.selectionEnd;
    el.source.setRangeText("  ", start, end, "end");
    if (mode === "session") { netgraphSession.markDirty(); } else { schedule(); }
  });

  [el.layer, el.showIps, el.showVlans, el.group, el.strict].forEach(function (control) {
    control.addEventListener("change", render);
  });
  el.vlans.addEventListener("input", schedule);
  el.render.addEventListener("click", render);
  el.fit.addEventListener("click", resetView);

  el.canvas.addEventListener("mousemove", function (event) {
    if (pinned) { return; }
    var hit = recordAt(event.target);
    if (hit) { showInfo(hit, event); } else { hideInfo(); }
  });

  el.canvas.addEventListener("mouseleave", function () { hideInfo(); });

  el.canvas.addEventListener("click", function (event) {
    var hit = recordAt(event.target);
    if (!hit) { hideInfo(true); return; }
    // In a session, clicking a shape reveals the document that declares it:
    // that mapping is the whole point of the command. `record.id` is the
    // element's address, which is what the tree keys documents by --
    // `record.element` is the SVG id, and matched nothing.
    if (mode === "session") {
      netgraphSession.reveal(hit.record.id);
      // What this page is looking at, so the other tabs can draw it faintly.
      netgraphSession.select(hit.record.id);
    }
    if (pinned === hit.record.element) { hideInfo(true); netgraphA11y.select(null); return; }
    pinned = hit.record.element;
    showInfo(hit, event);
    el.info.classList.add("pinned");
    netgraphA11y.select(hit.record.element);
  });

  el.canvas.addEventListener("wheel", function (event) {
    event.preventDefault();
    zoomAt(event.clientX, event.clientY, event.deltaY < 0 ? 1.12 : 1 / 1.12);
  }, { passive: false });

  (function draggable() {
    var origin = null;
    el.canvas.addEventListener("mousedown", function (event) {
      if (event.button !== 0) { return; }
      origin = { x: event.clientX - view.x, y: event.clientY - view.y };
      el.canvas.classList.add("panning");
    });
    window.addEventListener("mousemove", function (event) {
      if (!origin) { return; }
      view.x = event.clientX - origin.x;
      view.y = event.clientY - origin.y;
      applyView();
    });
    window.addEventListener("mouseup", function () {
      origin = null;
      el.canvas.classList.remove("panning");
    });
  })();

  (function resizable() {
    var dragging = false;
    el.splitter.addEventListener("mousedown", function () { dragging = true; });
    window.addEventListener("mousemove", function (event) {
      if (!dragging) { return; }
      var fraction = Math.min(0.8, Math.max(0.15, event.clientX / window.innerWidth));
      document.documentElement.style.setProperty("--pane", (fraction * 100).toFixed(1) + "%");
    });
    window.addEventListener("mouseup", function () { dragging = false; });
  })();

  /* ------------------------------------------------------------ commands */

  /* Every command the page has, registered against the ids
   * netgraph.web.bindings declares. The keyboard, the palette and the shortcut
   * sheet all reach them through here, so there is exactly one implementation
   * of "switch to the next layer" and exactly one place that says which key
   * runs it.
   *
   * The edit gestures are session.js's -- they write files -- and are
   * registered from there. The split is by what the command touches, not by
   * which face is running: a scratchpad registers them too, and the palette
   * shows them greyed with "open a folder with 'netgraph web DIR' for this"
   * rather than pretending netgraph cannot do them.
   */
  function defineCommands() {
    var K = netgraphKeys;

    K.define("palette", { run: function () { K.palette(null, ""); } });
    K.define("help", { run: function () { K.reference(); } });
    K.define("dismiss", {
      run: function () {
        // In order of how modal each thing is. Anything else and Escape becomes
        // a key you have to think about.
        if (K.dismiss()) { return; }
        if (!el.info.hidden) { hideInfo(true); netgraphA11y.select(null); return; }
        if (mode === "session" && netgraphSession.isDiffing()) {
          netgraphSession.showChanges(false);
          return;
        }
        // Last: put the keyboard back on the page. A text pane you can only
        // leave with a mouse is a text pane you are stuck in, and the canvas
        // holds the arrow keys, so both need a way out that is not Tab.
        if (document.activeElement === el.canvas || document.activeElement === el.source) {
          document.activeElement.blur();
        }
      }
    });

    K.define("focus.files", { run: function () { focusPane(el.fileList); } });
    K.define("focus.editor", { run: function () { el.source.focus(); } });
    K.define("focus.canvas", { run: function () { el.canvas.focus(); } });
    K.define("focus.outline", { run: function () { focusPane(el.outline); } });
    K.define("render", { run: render });
    K.define("validate", {
      run: function () {
        render();
        focusPane(el.problems);
        netgraphA11y.announce(el.problemCounts.textContent || "nothing to report", false);
      }
    });

    K.define("node.move", {
      run: function (context) {
        var direction = {
          ArrowRight: "right", ArrowLeft: "left", ArrowUp: "up", ArrowDown: "down"
        }[context.key];
        if (direction) { netgraphA11y.move(direction); }
      }
    });
    K.define("node.link", {
      run: function (context) { netgraphA11y.cycleLink(context.chord === "Shift-l" ? -1 : 1); }
    });
    K.define("node.first", { run: function () { netgraphA11y.first({ quiet: false }); } });
    K.define("node.last", { run: function () { netgraphA11y.last({ quiet: false }); } });
    K.define("node.inspect", { run: inspectFocused });
    K.define("node.select", {
      run: function () {
        var here = netgraphA11y.focused();
        if (!here) { return; }
        pinned = here.element;
        netgraphA11y.select(here.element);
        if (mode === "session") { netgraphSession.select(here.record.id); }
        inspectFocused();
      }
    });
    K.define("element.goto", { run: function () { K.palette("elements", ""); } });

    K.define("view.layer", { run: function () { K.palette("layers", ""); } });
    K.define("view.layer.next", { run: function () { stepLayer(1); } });
    K.define("view.layer.previous", { run: function () { stepLayer(-1); } });
    /* Spelled out one by one rather than looped over a table of triples: the
     * id has to be a literal in this file, because tests/test_web.py reads the
     * registrations out of it to prove that every binding netgraph declares has
     * something behind it. A loop would hide four of them from that check. */
    K.define("view.ips", { run: function () { toggle(el.showIps, "IP addresses"); } });
    K.define("view.vlans", { run: function () { toggle(el.showVlans, "VLANs"); } });
    K.define("view.group", { run: function () { toggle(el.group, "namespace grouping"); } });
    K.define("view.strict", { run: function () { toggle(el.strict, "strict"); } });
    K.define("view.vlanFilter", {
      run: function () {
        K.prompt({
          title: "Filter by VLAN",
          detail: "Comma-separated ids, 1 to 4094. Empty keeps every element.",
          fields: [{ name: "vlans", label: "VLAN ids", value: el.vlans.value }],
          confirm: "Apply",
          onSubmit: function (values) {
            el.vlans.value = values.vlans;
            render();
          }
        });
      }
    });
    K.define("view.fit", {
      run: function () { resetView(); netgraphA11y.announce("diagram fitted", false); }
    });
    K.define("view.zoomIn", { run: function () { zoomCentre(1.25); } });
    K.define("view.zoomOut", { run: function () { zoomCentre(1 / 1.25); } });

    /* The palette's other half: not commands but destinations. Each provider is
     * asked afresh every time the palette opens, so what it offers is what is
     * drawn and loaded now rather than what was there at boot. */
    K.provide("layers", function () {
      return Array.prototype.map.call(el.layer.options, function (option) {
        return {
          id: option.value,
          title: "Layer: " + option.textContent,
          detail: "switch the diagram to the " + option.value + " view",
          group: "layer",
          run: function () { el.layer.value = option.value; render(); }
        };
      });
    });
    K.provide("elements", function () {
      return netgraphA11y.elements().map(function (entry) {
        return {
          id: entry.element,
          title: entry.record.id || entry.record.name || entry.element,
          detail: netgraphA11y.label(entry.record),
          group: entry.record.type === "edge" ? "link" : "element",
          run: function () {
            el.canvas.focus();
            netgraphA11y.focus(entry.element, { quiet: false });
            inspectFocused();
          }
        };
      });
    });
  }

  /** Move the keyboard into a pane and say what it landed on.
   *
   * A pane is a container, not a control, so it is focused with `tabindex="-1"`
   * and the first thing inside it that *is* a control takes over from there.
   */
  function focusPane(pane) {
    if (!pane) { return; }
    var first = pane.querySelector("button, [href], input, select, textarea");
    (first || pane).focus();
  }

  /** Flip a display checkbox, redraw, and say which way it went. */
  function toggle(control, said) {
    control.checked = !control.checked;
    render();
    netgraphA11y.announce(said + (control.checked ? " on" : " off"), false);
  }

  function stepLayer(delta) {
    var options = el.layer.options;
    var next = (el.layer.selectedIndex + delta + options.length) % options.length;
    el.layer.selectedIndex = next;
    render();
    netgraphA11y.announce(options[next].textContent, false);
  }

  /* --------------------------------------------------------------- boot */

  /* What session.js is given: the elements it shares with this file, and the
   * four things it needs this file to do. Everything else -- the file list,
   * the hashes, the undo depth -- is its own. */
  var bridge = {
    el: el,
    render: render,
    toast: toast,
    goToLine: goToLine,
    diagnostics: function (problems) { showProblems(problems || []); },
    remote: setRemote,
    layer: function () { return el.layer.value; },
    /** Put the focus ring back on an element after a change redrew the SVG. */
    focusElement: function (address) {
      var found = netgraphA11y.elements().filter(function (entry) {
        return entry.record.id === address;
      })[0];
      if (!found) { return false; }
      netgraphA11y.focus(found.element, { quiet: false });
      return true;
    }
  };

  /* What keys.js is given: the questions only this file can answer about the
   * state of the page, and the one thing a refused command has to do. */
  var keyHost = {
    isSession: function () { return mode === "session"; },
    isWritable: function () { return mode === "session" && netgraphSession.isWritable(); },
    hasFocus: function () { return !!netgraphA11y.focused(); },
    inCanvas: function (node) {
      return !!node && (node === el.canvas || el.canvas.contains(node));
    },
    refuse: function (why) { toast(why, "error"); }
  };

  netgraphA11y.attach({ el: el, bringIntoView: bringIntoView });
  defineCommands();
  netgraphSession.defineCommands(bridge);
  netgraphKeys.attach(keyHost);

  el.commands.addEventListener("click", function () { netgraphKeys.palette(null, ""); });
  el.shortcuts.addEventListener("click", function () { netgraphKeys.reference(); });

  /* The bindings are the page's own contract with the keyboard, so they are
   * fetched before anything else: a page that draws before it can be driven is
   * a page somebody presses Ctrl-K at and nothing happens. A failure here is
   * survivable -- the pointer still works -- and says so once. */
  fetch("/api/bindings", { cache: "no-store" })
    .then(function (response) { return response.json(); })
    .then(function (payload) {
      netgraphKeys.load(payload);
      el.commandsKey.textContent = netgraphKeys.chordFor("palette");
      el.shortcutsKey.textContent = netgraphKeys.chordFor("help");
    })
    .catch(function () { toast("the keyboard bindings could not be loaded", "error"); })
    .then(bootFace);

  function bootFace() {
    return fetch("/api/state", { cache: "no-store" })
      .then(function (response) { return response.json(); })
      .then(function (state) {
        if (state.mode !== "session") { return bootStream(); }
        mode = "session";
        netgraphSession.attach(bridge, state);
        render();
      })
      .catch(bootStream);
  }

  function bootStream() {
    mode = "stream";
    return fetch("/api/source", { cache: "no-store" })
      .then(function (response) { return response.json(); })
      .then(function (body) { el.source.value = body.source || ""; })
      .catch(function () {})
      .then(render);
  }
})();
