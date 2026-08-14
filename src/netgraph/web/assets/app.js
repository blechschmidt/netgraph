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
    editorState: document.getElementById("editor-state")
  };

  var details = {};
  var pending = null;
  var inFlight = false;
  var queued = false;
  var pinned = null;
  var view = { x: 0, y: 0, k: 1, placed: false };
  /** "stream" until /api/state says otherwise. */
  var mode = "stream";
  var toastTimer = null;

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
    request().then(function (response) {
      return response.json().then(function (body) {
        if (!response.ok) { throw new Error(body.message || response.statusText); }
        return body;
      });
    }).then(apply).catch(function (error) {
      setStatus("failed", String(error.message || error));
      showProblems([]);
    }).then(function () {
      inFlight = false;
      if (queued) { queued = false; render(); }
    });
  }

  function request() {
    if (mode === "session") {
      return fetch("/api/graph?" + query(), { cache: "no-store" });
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

  function apply(result) {
    details = result.details || {};
    setStatus(result.status, result.message, result.counts, result.durationMs);
    showProblems(result.problems || [], result.dangling || []);
    hideInfo(true);
    if (result.svg) {
      el.viewport.innerHTML = result.svg;
      el.placeholder.hidden = true;
      if (!view.placed) { view.placed = true; resetView(); }
    } else {
      el.viewport.replaceChildren();
      el.placeholder.hidden = false;
      el.placeholder.textContent = result.message || "nothing rendered";
    }
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
      var row = document.createElement("div");
      row.className = "problem " + problem.severity;
      var target = navigation(problem.location);
      if (target) {
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
      el.problems.appendChild(row);
    });
    el.problemCounts.textContent =
      counts.error + " errors, " + counts.warning + " warnings, " + counts.info + " notes";
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

  /** Put the cursor on `line` of the editor and select it. */
  function goToLine(line) {
    var lines = el.source.value.split("\n");
    if (!line || line > lines.length) { return; }
    var start = 0;
    for (var i = 0; i < line - 1; i++) { start += lines[i].length + 1; }
    el.source.focus();
    el.source.setSelectionRange(start, start + lines[line - 1].length);
    // No API positions a textarea's scroll on a line, so estimate it from the
    // line height and centre the selection in the visible part.
    var height = parseFloat(window.getComputedStyle(el.source).lineHeight) || 18;
    el.source.scrollTop = Math.max(0, (line - 1) * height - el.source.clientHeight / 2);
  }

  /* --------------------------------------------------------------- toast */

  /** Say something for a moment: what was saved, what was refused, why. */
  function toast(text, kind) {
    window.clearTimeout(toastTimer);
    el.toast.textContent = text;
    el.toast.className = "toast " + (kind || "");
    el.toast.hidden = false;
    toastTimer = window.setTimeout(function () { el.toast.hidden = true; }, TOAST_MS);
  }

  /* ------------------------------------------------------------ info box */

  function recordAt(target) {
    var group = target.closest ? target.closest("g.node, g.edge") : null;
    return group && details[group.id] ? { group: group, record: details[group.id] } : null;
  }

  function showInfo(hit, event) {
    el.info.replaceChildren(describe(hit.record));
    el.info.hidden = false;
    place(event);
    highlight(hit.record);
  }

  function hideInfo(force) {
    if (pinned && !force) { return; }
    pinned = null;
    el.info.hidden = true;
    el.info.classList.remove("pinned");
    highlight(null);
  }

  function place(event) {
    var margin = 14;
    var box = el.info.getBoundingClientRect();
    var x = event.clientX + margin;
    var y = event.clientY + margin;
    if (x + box.width > window.innerWidth - margin) { x = event.clientX - box.width - margin; }
    if (y + box.height > window.innerHeight - margin) { y = event.clientY - box.height - margin; }
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
    if (mode === "session") { netgraphSession.reveal(hit.record.id); }
    if (pinned === hit.record.element) { hideInfo(true); return; }
    pinned = hit.record.element;
    showInfo(hit, event);
    el.info.classList.add("pinned");
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") { hideInfo(true); }
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

  /* --------------------------------------------------------------- boot */

  /* What session.js is given: the elements it shares with this file, and the
   * four things it needs this file to do. Everything else -- the file list,
   * the hashes, the undo depth -- is its own. */
  var bridge = {
    el: el,
    render: render,
    toast: toast,
    goToLine: goToLine,
    diagnostics: function (problems) { showProblems(problems || []); }
  };

  fetch("/api/state", { cache: "no-store" })
    .then(function (response) { return response.json(); })
    .then(function (state) {
      if (state.mode !== "session") { return bootStream(); }
      mode = "session";
      netgraphSession.attach(bridge, state);
      render();
    })
    .catch(bootStream);

  function bootStream() {
    mode = "stream";
    return fetch("/api/source", { cache: "no-store" })
      .then(function (response) { return response.json(); })
      .then(function (body) { el.source.value = body.source || ""; })
      .catch(function () {})
      .then(render);
  }
})();
