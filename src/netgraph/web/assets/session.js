/* The tree-backed half of the netgraph web interface.
 *
 * `netgraph web DIR` opens a folder rather than a string, and this file is what
 * the page does with one: the file list, the open document, saving it back, the
 * server-side undo stack, and the reconciliation that keeps all three honest
 * when somebody edits the same tree in $EDITOR.
 *
 * It is loaded on every page and does nothing until app.js attaches it, which
 * it only does when /api/state says the server is in session mode. The
 * scratchpad -- one document stream, nothing on disk -- never comes through
 * here.
 *
 * Three rules this file exists to keep:
 *
 *   1. **The server owns the truth.** Every list, every hash and every undo
 *      depth on screen came from a response; nothing is inferred locally. The
 *      one thing the page knows that the server does not is whether the text in
 *      the textarea has been typed into since it arrived.
 *   2. **A write states what it is replacing.** Saving sends the content hash
 *      the file was opened at. The server refuses a stale one, and the refusal
 *      is shown as a conflict rather than resolved by guessing.
 *   3. **A moved revision is never papered over.** The poll notices, and the
 *      file list, the diagram and the open file are refetched. A file that is
 *      dirty and has also changed underneath is marked conflicted and left
 *      alone -- the user's unsaved text is not something to throw away quietly.
 *
 * Dependency-free, like the rest of this page: a local Python process serves it
 * and there is no build step to put a bundler in.
 */

var netgraphSession = (function () {
  "use strict";

  /** How often the tree revision is checked, in milliseconds. */
  var POLL_MS = 1000;

  var host = null;
  var el = null;
  var state = { revision: 0, writable: false, undo: 0, redo: 0 };
  var tree = { files: [], revision: 0 };
  /** The file in the textarea: its path, the hash it was read at, and whether
   *  the text has been typed into or has moved on disk since. */
  var open = { path: null, hash: null, dirty: false, conflicted: false };
  var timer = null;
  /** The changes drawer: is it showing, what has it been told, and what is the
   *  diagram being drawn against while it is. */
  var changes = { open: false, against: "session", entries: [], commands: [], baselines: [] };

  /* ------------------------------------------------------------- attaching */

  /** Take over the page. `bridge` is app.js's side of the contract:
   *
   *    el          the elements shared with app.js
   *    render()    ask for a fresh diagram
   *    problems()  redraw the problems list from a diagnostics array
   *    toast(text, kind)  say something, briefly
   */
  function attach(bridge, initial) {
    host = bridge;
    el = bridge.el;
    state = initial;
    el.files.hidden = false;
    el.actions.hidden = !initial.writable;
    el.filesRoot.textContent = initial.root || "";
    el.filesMode.textContent = initial.writable ? "read-write" : "read-only";
    el.filesMode.className = "hint " + (initial.writable ? "rw" : "ro");
    el.editorTitle.textContent = "no file open";
    el.editorHint.textContent = "choose a document on the left";
    el.source.readOnly = true;
    bindControls();
    refreshTree();
    refreshChanges();
    poll();
    return true;
  }

  function bindControls() {
    el.save.addEventListener("click", function () { save(false); });
    el.undo.addEventListener("click", function () { step("undo"); });
    el.redo.addEventListener("click", function () { step("redo"); });
    el.changesToggle.addEventListener("click", function () { showChanges(!changes.open); });
    el.changesClose.addEventListener("click", function () { showChanges(false); });
    el.changesCopy.addEventListener("click", copyCommands);
    el.changesAgainst.addEventListener("change", function () {
      changes.against = el.changesAgainst.value;
      if (changes.open) { host.render(); }
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && changes.open) { showChanges(false); return; }
      if (!(event.ctrlKey || event.metaKey)) { return; }
      var key = event.key.toLowerCase();
      if (key === "s") { event.preventDefault(); save(false); }
      if (key === "z" && !event.shiftKey) { event.preventDefault(); step("undo"); }
      if (key === "y" || (key === "z" && event.shiftKey)) { event.preventDefault(); step("redo"); }
    });
  }

  /* ---------------------------------------------------------------- polling */

  /* A poll rather than server-sent events: it is a dozen lines, it recovers
   * from a server restart by itself, and one integer a second over loopback is
   * not a cost worth engineering away. The number it watches is bumped by every
   * change to the tree, whoever made it. */
  function poll() {
    window.clearTimeout(timer);
    fetch("/api/state", { cache: "no-store" })
      .then(function (response) { return response.json(); })
      .then(function (next) {
        var moved = next.revision !== state.revision;
        state = next;
        paintHistory();
        if (moved) { reconcile(); }
      })
      .catch(function () {})
      .then(function () { timer = window.setTimeout(poll, POLL_MS); });
  }

  /** The tree moved: refetch everything that was derived from it.
   *
   * The decision about the open file waits for the refetch rather than racing
   * it. Made against the tree still in hand, it is made against the hashes from
   * *before* the change -- which always compare equal, so the file that just
   * moved on disk was the one thing the reconciliation left alone. */
  function reconcile() {
    host.render();
    // The drawer is a view of the session's own log, but the *diff* it paints
    // is against the tree -- which anything may have moved. Refetch both.
    refreshChanges();
    return refreshTree().then(function () {
      if (!open.path) { return; }
      var entry = fileEntry(open.path);
      if (!entry) {
        // Deleted under us. The text stays on screen -- it may be the only copy
        // left -- but it is no longer a file, so saving it would be a create.
        mark("gone", "deleted on disk");
        return;
      }
      if (entry.hash === open.hash) { return; }
      if (open.dirty) {
        open.conflicted = true;
        mark("conflict", "changed on disk since you opened it");
        host.toast(open.path + " changed on disk and has unsaved edits here", "error");
        return;
      }
      openFile(open.path);
    });
  }

  /* -------------------------------------------------------------- the tree */

  function refreshTree() {
    return fetch("/api/tree", { cache: "no-store" })
      .then(function (response) { return response.json(); })
      .then(function (next) {
        tree = next;
        paintTree();
        if (host.diagnostics) { host.diagnostics(next.diagnostics || []); }
      })
      .catch(function () {});
  }

  function fileEntry(path) {
    for (var i = 0; i < tree.files.length; i++) {
      if (tree.files[i].path === path) { return tree.files[i]; }
    }
    return null;
  }

  /** Draw the file list: one row per file, its documents indented under it.
   *
   * A flat list of paths, not a collapsible tree of directories: a namespace is
   * a folder here, the paths are short, and a control that hides half the
   * inventory behind a triangle is a control that hides half the inventory. */
  function paintTree() {
    var list = el.fileList;
    list.replaceChildren();
    if (!tree.files.length) {
      var none = document.createElement("p");
      none.className = "empty";
      none.textContent = "no YAML documents below " + (tree.root || "the root");
      list.appendChild(none);
      return;
    }
    var namespace = null;
    tree.files.forEach(function (file) {
      if (file.namespace !== namespace) {
        namespace = file.namespace;
        var heading = document.createElement("div");
        heading.className = "ns";
        heading.textContent = namespace || "/";
        list.appendChild(heading);
      }
      list.appendChild(fileRow(file));
      file.documents.forEach(function (document_) {
        list.appendChild(documentRow(file, document_));
      });
    });
  }

  function fileRow(file) {
    var row = document.createElement("div");
    row.className = "file";
    if (file.path === open.path) { row.classList.add("current"); }
    if (file.error) { row.classList.add("broken"); }
    row.dataset.path = file.path;
    var name = document.createElement("span");
    name.className = "name";
    name.textContent = file.path.split("/").pop();
    row.appendChild(name);
    var badge = stateBadge(file);
    if (badge) { row.appendChild(badge); }
    row.title = file.error ? file.path + " — " + file.error : file.path;
    row.addEventListener("click", function () { openFile(file.path); });
    return row;
  }

  /** The honest per-file state: what the browser has done to it and what the
   *  disk has done underneath, never merged into one cheerful dot. */
  function stateBadge(file) {
    if (file.path !== open.path) { return null; }
    var text = open.conflicted ? "conflict" : (open.dirty ? "unsaved" : null);
    if (!text) { return null; }
    var badge = document.createElement("span");
    badge.className = "badge " + (open.conflicted ? "conflict" : "dirty");
    badge.textContent = text;
    return badge;
  }

  function documentRow(file, entry) {
    var row = document.createElement("div");
    row.className = "doc";
    row.dataset.address = entry.address || "";
    var kind = document.createElement("span");
    kind.className = "kind";
    kind.textContent = entry.kind;
    var name = document.createElement("span");
    name.className = "name";
    name.textContent = entry.name;
    row.appendChild(kind);
    row.appendChild(name);
    row.title = (entry.address || entry.name) + " — " + file.path + ":" + (entry.line || 1);
    row.addEventListener("click", function () { openFile(file.path, entry.line); });
    return row;
  }

  /* ------------------------------------------------------------- one file */

  /** Open `path`, optionally putting the cursor on `line`. */
  function openFile(path, line) {
    if (open.dirty && open.path && open.path !== path) {
      if (!window.confirm(open.path + " has unsaved changes. Discard them?")) { return; }
    }
    return fetch("/api/file/" + encodePath(path), { cache: "no-store" })
      .then(readBody)
      .then(function (body) {
        open = { path: body.path, hash: body.hash, dirty: false, conflicted: false };
        el.source.value = body.text;
        el.source.readOnly = !state.writable;
        el.editorTitle.textContent = body.path;
        el.editorHint.textContent = state.writable
          ? "Ctrl-S saves · Ctrl-Z undoes on the server"
          : "read-only session";
        mark(null, "");
        paintTree();
        if (line) { host.goToLine(line); }
      })
      .catch(function (error) { host.toast(String(error.message || error), "error"); });
  }

  /** app.js calls this on every keystroke in the textarea. */
  function markDirty() {
    if (!open.path || open.dirty) { return; }
    open.dirty = true;
    mark("dirty", "unsaved changes");
    paintTree();
    paintHistory();
  }

  function save(force) {
    if (!open.path || !state.writable) { return; }
    var body = { text: el.source.value, force: !!force };
    // A conflicted file is being saved deliberately over somebody else's work,
    // so it goes without the precondition -- but only after the user was told.
    if (!open.conflicted) { body.hash = open.hash; }
    el.save.disabled = true;
    fetch("/api/file/" + encodePath(open.path), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
      body: JSON.stringify(body)
    })
      .then(readBody)
      .then(function (result) { applied(result, "saved " + open.path); })
      .catch(function (error) { refused(error, force); })
      .then(paintHistory);
  }

  function step(verb) {
    if (!state.writable) { return; }
    if (verb === "undo" && !state.undo) { return; }
    if (verb === "redo" && !state.redo) { return; }
    fetch("/api/" + verb, { method: "POST", cache: "no-store" })
      .then(readBody)
      .then(function (result) { applied(result, verb + "ne", true); })
      .catch(function (error) { host.toast(String(error.message || error), "error"); })
      .then(paintHistory);
  }

  /** What every successful write does: adopt the new revision and redraw.
   *
   * `rewrote` says the text on screen is not what was just written -- true of an
   * undo, which puts back a file the pane is showing the *new* version of, and
   * false of a save, where the pane is already the file. Without it an undo left
   * the editor showing text that was nowhere on disk, under a clean badge. */
  function applied(result, what, rewrote) {
    state.revision = result.revision;
    state.undo = result.undo;
    state.redo = result.redo;
    var mine = result.files[open.path];
    if (mine && mine.state === "written") {
      open.hash = mine.hash;
      open.dirty = false;
      open.conflicted = false;
      mark(null, "");
      if (rewrote) { openFile(open.path); }
    }
    host.toast(what, "ok");
    refreshTree().then(function () {
      // An undo can rewrite the file that is open; reload it unless the user is
      // in the middle of typing something else into it.
      if (open.path && !open.dirty) {
        var entry = fileEntry(open.path);
        if (entry && entry.hash !== open.hash) { openFile(open.path); }
      }
    });
    host.render();
    refreshChanges();
    if (result.diagnostics && host.diagnostics) { host.diagnostics(result.diagnostics); }
  }

  /** A refusal is information, not a failure to hide: say which kind it was. */
  function refused(error, wasForced) {
    var body = error.body || {};
    if (body.conflict) {
      open.conflicted = true;
      mark("conflict", "changed on disk since you opened it");
      paintTree();
      host.toast(body.message + " — save again to overwrite it", "error");
    } else if (body.problems && !wasForced) {
      host.toast(body.message, "error");
      if (host.diagnostics) {
        host.diagnostics(body.problems.map(function (problem) {
          return {
            severity: "error",
            location: problem.location,
            rule: problem.rule,
            message: problem.message
          };
        }));
      }
      if (window.confirm(body.message + "\n\nWrite it anyway?")) { save(true); return; }
    } else {
      host.toast(String(body.message || error.message || error), "error");
    }
    paintHistory();
  }

  /* ---------------------------------------------------------- the diagram */

  /** Reveal the document that declares `address`, from a click in the diagram.
   *
   * This is the mapping the whole command is for: the shape under the pointer
   * carries the element's address, the tree says which file and line declares
   * it, and the editor goes there. */
  function reveal(address) {
    if (!address) { return false; }
    for (var i = 0; i < tree.files.length; i++) {
      var file = tree.files[i];
      for (var j = 0; j < file.documents.length; j++) {
        if (file.documents[j].address === address) {
          openFile(file.path, file.documents[j].line || 1);
          return true;
        }
      }
    }
    return false;
  }

  /** Where a diagnostic points: `switches/sw.yaml#0:17` is file and line. */
  function locate(location) {
    var match = /^(.+?)#\d+(?::(\d+))?$/.exec(location || "");
    if (!match) { return false; }
    var path = match[1];
    if (!fileEntry(path)) { return false; }
    if (path === open.path) {
      if (match[2]) { host.goToLine(parseInt(match[2], 10)); }
      return true;
    }
    openFile(path, match[2] ? parseInt(match[2], 10) : 0);
    return true;
  }

  /* ------------------------------------------------------- changes drawer */

  /* The drawer is a log of *gestures*, not of operations: deleting a switch is
   * one entry even though the mutation layer made it four operations. Each one
   * carries the YAML it produced, because that is the thing being reviewed --
   * this editor's whole claim is that the picture and the text are one document,
   * and a change log that showed only the picture would quietly give that up.
   *
   * Opening it also repaints the canvas as a diff against the baseline, which is
   * why it is a mode rather than a panel: the drawer and the diagram are two
   * views of one answer. */

  /** Which URL app.js should fetch the diagram from, given the view options.
   *
   * The drawer decides, not app.js: whether the canvas is showing a state or a
   * change is this file's business, and app.js only has to draw what comes back.
   */
  function graphPath(query) {
    if (!changes.open) { return "/api/graph?" + query; }
    return "/api/diff?" + query + "&against=" + encodeURIComponent(changes.against);
  }

  function showChanges(next) {
    changes.open = !!next;
    el.changes.hidden = !changes.open;
    el.changesToggle.setAttribute("aria-expanded", changes.open ? "true" : "false");
    el.changesToggle.classList.toggle("on", changes.open);
    el.legend.hidden = !changes.open;
    if (changes.open) { refreshChanges(); }
    host.render();
  }

  function refreshChanges() {
    return fetch("/api/changes", { cache: "no-store" })
      .then(readBody)
      .then(function (next) {
        changes.entries = next.entries || [];
        changes.commands = next.commands || [];
        changes.baselines = next.baselines || ["session"];
        paintBaselines();
        paintChanges();
      })
      .catch(function () {});
  }

  /** Offer git only when the server says the tree is in a repository. */
  function paintBaselines() {
    var labels = { session: "this session started", git: "git HEAD" };
    if (changes.baselines.indexOf(changes.against) === -1) { changes.against = "session"; }
    el.changesAgainst.replaceChildren();
    changes.baselines.forEach(function (name) {
      var option = document.createElement("option");
      option.value = name;
      option.textContent = labels[name] || name;
      option.selected = name === changes.against;
      el.changesAgainst.appendChild(option);
    });
  }

  function paintChanges() {
    var live = changes.entries.filter(function (entry) { return !entry.reverted; }).length;
    el.changesCount.textContent = live ? "(" + live + ")" : "";
    el.changesCopy.disabled = !changes.commands.length;
    var list = el.changesList;
    list.replaceChildren();
    if (!changes.entries.length) {
      var none = document.createElement("p");
      none.className = "empty";
      none.textContent = "nothing changed yet in this session";
      list.appendChild(none);
      return;
    }
    // Newest first: the thing just done is the thing being looked for.
    changes.entries.slice().reverse().forEach(function (entry) {
      list.appendChild(changeRow(entry));
    });
  }

  function changeRow(entry) {
    var row = document.createElement("div");
    row.className = "change" + (entry.reverted ? " reverted" : "");
    row.dataset.id = String(entry.id);

    var head = document.createElement("div");
    head.className = "change-head";
    var number = document.createElement("span");
    number.className = "n";
    number.textContent = "#" + entry.id;
    var label = document.createElement("span");
    label.className = "label";
    label.textContent = entry.label + (entry.reverted ? " (put back)" : "");
    // Click-to-reveal: the gesture names an element, the tree says which file
    // and line declares it, and the editor goes there. The same mapping the
    // diagram uses when a shape is clicked. A gesture that named no element --
    // a whole-file save, a deletion whose document is gone -- falls back to the
    // file it wrote, which is the next most useful place to be.
    var address = (entry.addresses || [])[0];
    if (address || (entry.files || []).length) {
      label.classList.add("revealable");
      label.title = "reveal " + (address || (entry.files || [])[0]);
      label.addEventListener("click", function () {
        if (!reveal(address) && !revealFile(entry)) {
          host.toast("nothing left to reveal for this change", "error");
        }
      });
    }
    head.appendChild(number);
    head.appendChild(label);
    if (entry.revertible && state.writable) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "ghost";
      button.textContent = "Revert";
      button.title = "Apply the inverse of this change as a new change";
      button.addEventListener("click", function () { revert(entry.id); });
      head.appendChild(button);
    }
    row.appendChild(head);

    var where = document.createElement("div");
    where.className = "where";
    where.textContent = (entry.files || []).join(", ");
    row.appendChild(where);

    if (entry.hunk) { row.appendChild(hunkBlock(entry.hunk)); }
    return row;
  }

  /** The unified diff, one line per element so each can carry its own colour.
   *
   * textContent throughout: a hunk is file content, and file content is the last
   * thing to hand to innerHTML. */
  function hunkBlock(hunk) {
    var pre = document.createElement("pre");
    hunk.split("\n").forEach(function (line, index, all) {
      if (index === all.length - 1 && line === "") { return; }
      var span = document.createElement("span");
      span.className = hunkClass(line);
      span.textContent = line + "\n";
      pre.appendChild(span);
    });
    return pre;
  }

  function hunkClass(line) {
    if (line.indexOf("+++") === 0 || line.indexOf("---") === 0) { return "file"; }
    if (line.charAt(0) === "+") { return "add"; }
    if (line.charAt(0) === "-") { return "del"; }
    if (line.charAt(0) === "@") { return "at"; }
    return "";
  }

  /** Fall back to opening the file a gesture touched, when its element is gone.
   *
   * A deletion is the case: there is no document left to reveal, and the file
   * it was removed from is the next most useful place to be. */
  function revealFile(entry) {
    var path = (entry.files || []).find(function (file) { return !!fileEntry(file); });
    if (!path) { return false; }
    openFile(path);
    return true;
  }

  function revert(id) {
    if (!state.writable) { return; }
    fetch("/api/revert", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
      body: JSON.stringify({ id: id, revision: state.revision })
    })
      .then(readBody)
      .then(function (result) { applied(result, "put change #" + id + " back", true); })
      .catch(function (error) { refused(error, false); })
      .then(refreshChanges);
  }

  /** The handover: the session as a script somebody else can run or review. */
  function copyCommands() {
    var text = changes.commands.join("\n") + (changes.commands.length ? "\n" : "");
    if (!text) { host.toast("nothing to copy yet", "error"); return; }
    var done = function () {
      host.toast("copied " + changes.commands.length + " command(s)", "ok");
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text, done); });
      return;
    }
    fallbackCopy(text, done);
  }

  /* No clipboard API over plain HTTP in some browsers, and this page is served
   * over loopback without TLS. A hidden textarea and execCommand is the old way
   * and still the working one. */
  function fallbackCopy(text, done) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (error) { ok = false; }
    document.body.removeChild(area);
    if (ok) { done(); } else { host.toast("could not copy; select the text instead", "error"); }
  }

  /* ------------------------------------------------------------- painting */

  function paintHistory() {
    el.actions.hidden = !state.writable;
    el.save.disabled = !state.writable || !open.path || !open.dirty;
    el.undo.disabled = !state.writable || !state.undo;
    el.redo.disabled = !state.writable || !state.redo;
    el.undo.title = state.undoLabel ? "Undo: " + state.undoLabel : "Nothing to undo";
    el.redo.title = state.redoLabel ? "Redo: " + state.redoLabel : "Nothing to redo";
  }

  function mark(kind, text) {
    el.editorState.hidden = !kind;
    el.editorState.className = "badge " + (kind || "");
    el.editorState.textContent = text;
    paintHistory();
  }

  /* --------------------------------------------------------------- fetch */

  /** Read a JSON body and turn a refusal into an Error carrying it.
   *
   * The server answers every refusal with a JSON object holding a message and,
   * where it has one, the thing the page can act on -- the hash that is really
   * there, or the problems a validation gate objected to. */
  function readBody(response) {
    return response.json().then(function (body) {
      if (response.ok) { return body; }
      var error = new Error(body.message || response.statusText);
      error.body = body;
      throw error;
    });
  }

  /** Percent-encode a path without encoding the separators. */
  function encodePath(path) {
    return path.split("/").map(encodeURIComponent).join("/");
  }

  return {
    attach: attach,
    markDirty: markDirty,
    reveal: reveal,
    locate: locate,
    isOpen: function () { return !!open.path; },
    save: save,
    graphPath: graphPath,
    showChanges: showChanges,
    refreshChanges: refreshChanges,
    isDiffing: function () { return changes.open; }
  };
})();
