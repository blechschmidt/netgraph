/* The netgraph web interface.
 *
 * Dependency-free on purpose: this page is served by a local Python process
 * that has no build step, and a bundler would put a toolchain between a user
 * and the diagram they asked for.
 *
 * Three things happen here and nothing else:
 *
 *   1. The editor's text is posted to /api/render whenever it settles, and the
 *      SVG that comes back replaces the one on screen.
 *   2. The pointer's position over that SVG is turned into an info box, by
 *      looking up the id of the <g> under it in the records the same response
 *      carried. No request is made on hover; the details are already here.
 *   3. The canvas pans and zooms, which is CSS on a wrapper -- the SVG itself
 *      is never rewritten, so the ids the info box depends on stay put.
 *
 * Everything a record contains is inserted with textContent. The only markup
 * this file ever assigns is the SVG the server sanitised.
 */

(function () {
  "use strict";

  /** How long the editor has to settle before a render is asked for. */
  var DEBOUNCE_MS = 450;
  /** Zoom bounds. Below the first the diagram is a smudge; above the second a pixel. */
  var MIN_SCALE = 0.1;
  var MAX_SCALE = 12;

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
    layer: document.getElementById("layer"),
    vlans: document.getElementById("vlans"),
    showIps: document.getElementById("show-ips"),
    showVlans: document.getElementById("show-vlans"),
    group: document.getElementById("group"),
    strict: document.getElementById("strict"),
    render: document.getElementById("render"),
    fit: document.getElementById("fit"),
    splitter: document.getElementById("splitter")
  };

  var details = {};
  var pending = null;
  var inFlight = false;
  var queued = false;
  var pinned = null;
  var view = { x: 0, y: 0, k: 1, placed: false };

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
    fetch("/api/render", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
      body: JSON.stringify(options())
    }).then(function (response) {
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
      var line = lineOf(problem.location);
      if (line) {
        row.classList.add("locatable");
        row.title = "go to line " + line;
        row.addEventListener("click", function () { jumpTo(line); });
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

  /** The 1-based line a location names, or 0 when it names none.
   *
   * A location reads ``stream.yaml#3:17`` -- file, document index, line -- and the line is what the editor can act on. See
   * netgraph.loader.LoadError.location for where the shape comes from.
   */
  function lineOf(location) {
    var match = /#\d+:(\d+)/.exec(location || "");
    return match ? parseInt(match[1], 10) : 0;
  }

  /** Put the cursor on ``line`` of the editor and select it. */
  function jumpTo(line) {
    var lines = el.source.value.split("\n");
    if (line > lines.length) { return; }
    var start = 0;
    for (var i = 0; i < line - 1; i++) { start += lines[i].length + 1; }
    el.source.focus();
    el.source.setSelectionRange(start, start + lines[line - 1].length);
    // No API positions a textarea's scroll on a line, so estimate it from the
    // line height and centre the selection in the visible part.
    var height = parseFloat(window.getComputedStyle(el.source).lineHeight) || 18;
    el.source.scrollTop = Math.max(0, (line - 1) * height - el.source.clientHeight / 2);
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

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  function definitions(pairs) {
    var list = document.createElement("dl");
    pairs.forEach(function (pair) {
      if (pair[1] === undefined || pair[1] === null || pair[1] === "") { return; }
      list.appendChild(element("dt", null, pair[0]));
      list.appendChild(element("dd", null, pair[1]));
    });
    return list.children.length ? list : null;
  }

  function section(title, body) {
    if (!body) { return null; }
    var wrapper = document.createElement("section");
    wrapper.appendChild(element("h3", null, title));
    wrapper.appendChild(body);
    return wrapper;
  }

  function table(headings, rows) {
    if (!rows.length) { return null; }
    var node = document.createElement("table");
    var head = document.createElement("tr");
    headings.forEach(function (heading) { head.appendChild(element("th", null, heading)); });
    node.appendChild(head);
    rows.forEach(function (row) {
      var line = document.createElement("tr");
      if (row.muted) { line.className = "off"; }
      row.cells.forEach(function (cell) { line.appendChild(element("td", null, cell)); });
      node.appendChild(line);
    });
    return node;
  }

  function tags(values) {
    if (!values || !values.length) { return null; }
    var wrapper = document.createElement("div");
    values.forEach(function (value) { wrapper.appendChild(element("span", "tag", value)); });
    return wrapper;
  }

  function join(values) {
    return values && values.length ? values.join(", ") : "";
  }

  function describe(record) {
    return record.type === "edge" ? describeLink(record) : describeNode(record);
  }

  function heading(name, kind) {
    var head = element("h2", null, name);
    head.appendChild(element("span", "kind", "[" + kind + "]"));
    var hint = element("span", "pinhint", pinned ? "click to unpin" : "click to pin");
    head.appendChild(hint);
    return head;
  }

  function describeNode(record) {
    var box = document.createDocumentFragment();
    box.appendChild(heading(record.name, record.kind));

    var identity = [["id", record.id]];
    if (record.namespace) { identity.push(["namespace", record.namespace]); }
    if (record.description) { identity.push(["description", record.description.trim()]); }
    Object.keys(record.labels || {}).forEach(function (key) {
      identity.push(["label " + key, record.labels[key]]);
    });
    append(box, section("element", definitions(identity)));

    if (record.subnet) {
      append(box, section("subnet", definitions([
        ["prefix", record.subnet.prefix],
        ["family", record.subnet.family],
        ["addresses", join(record.subnet.addresses)],
        ["elements", join(record.subnet.elements)]
      ])));
    }

    append(box, section("vlans", tags((record.vlans || []).map(function (id) {
      return "vlan " + id;
    }))));

    append(box, section("interfaces", table(
      ["interface", "type", "addresses", "vlan", "mac / mtu"],
      (record.interfaces || []).map(function (port) {
        return {
          muted: port.enabled === false,
          cells: [
            port.name,
            port.type,
            join(port.addresses),
            port.vlan ? port.vlan.mode + " " + join(port.vlan.vlans) : "",
            [port.mac, port.mtu ? "mtu " + port.mtu : ""].filter(Boolean).join(" / ")
          ]
        };
      })
    )));

    append(box, section("links", table(
      ["via", "to", "port", "medium", "vlan"],
      (record.links || []).map(function (link) {
        return {
          cells: [
            link.interface || "—",
            link.peer,
            link.peerInterface || "—",
            [link.medium || link.kind, link.speedText].filter(Boolean).join(" "),
            join(link.vlans)
          ]
        };
      })
    )));

    if (!(record.interfaces || []).length && !(record.links || []).length) {
      append(box, element("p", "note", "no interfaces and no links"));
    }
    return box;
  }

  function describeLink(record) {
    var box = document.createDocumentFragment();
    var ends = record.endpoints || [];
    var name = ends.map(function (end) {
      return end.node + (end.interface ? ":" + end.interface : "");
    }).join("  —  ");
    box.appendChild(heading(name, record.kind));

    append(box, section("link", definitions([
      ["id", record.id],
      ["medium", record.medium],
      ["speed", record.speedText],
      ["label", record.label],
      ["length", record.lengthM ? record.lengthM + " m" : ""],
      ["addresses", join(record.addresses)]
    ])));

    append(box, section("endpoints", table(["element", "interface"], ends.map(function (end) {
      return { cells: [end.node, end.interface || "—"] };
    }))));

    append(box, section("vlans", tags((record.vlans || []).map(function (id) {
      return "vlan " + id;
    }))));
    return box;
  }

  function append(parent, child) {
    if (child) { parent.appendChild(child); }
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

  el.source.addEventListener("input", schedule);
  el.source.addEventListener("keydown", function (event) {
    if (event.key !== "Tab") { return; }
    // A YAML editor that loses focus on Tab is not an editor.
    event.preventDefault();
    var start = el.source.selectionStart;
    var end = el.source.selectionEnd;
    el.source.setRangeText("  ", start, end, "end");
    schedule();
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

  fetch("/api/source", { cache: "no-store" })
    .then(function (response) { return response.json(); })
    .then(function (body) { el.source.value = body.source || ""; })
    .catch(function () {})
    .then(render);
})();
