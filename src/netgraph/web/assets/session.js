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
 *   3. **A moved revision is never papered over.** The stream says so, and the
 *      files that moved, the diagram and the open file are brought up to date. A
 *      file that is dirty and has also changed underneath is marked conflicted
 *      and left alone -- the user's unsaved text is not something to throw away
 *      quietly.
 *
 * How it hears about a change
 * ---------------------------
 *
 * /api/events, a server-sent-events stream, says what moved the moment it
 * moves: which files, which way the history went, who else is connected. Two
 * things follow, and they are the point of it:
 *
 *   * **A single-file save refetches a single file.** The event names the paths,
 *     so `/api/tree?path=...` answers for those and the other thousand rows stay
 *     as they are.
 *   * **A tree that moved is not a diagram that moved.** Every render sends the
 *     fingerprint of the picture it is already showing; the server compares it
 *     with the one this revision would produce and answers `unchanged` instead
 *     of running Graphviz. app.js keeps the SVG it has, per view, so switching
 *     back to a layer nothing touched costs a round trip and no render.
 *
 * **The stream is an optimisation, and the page works without it.** A proxy that
 * buffers, a browser without EventSource, a stream that will not open: any of
 * them drops this file back to polling /api/state, which replays the very same
 * events out of the server's ring buffer into the very same handlers. Nothing
 * below is reachable only from one of the two paths.
 *
 * Dependency-free, like the rest of this page: a local Python process serves it
 * and there is no build step to put a bundler in.
 */

var netgraphSession = (function () {
  "use strict";

  /** How often the tree revision is checked when there is no stream, in ms. */
  var POLL_MS = 1000;
  /** How long the stream has to say hello before we give up on it. */
  var STREAM_TIMEOUT_MS = 4000;
  /** How many times a stream may drop and reconnect before we stop trusting it.
   *  EventSource retries by itself, so a couple of failures is a hiccup and a
   *  steady trickle of them is a proxy that will not carry this. */
  var STREAM_FAILURES = 3;

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
  /** The push channel: the connection, where we are in its numbering, and
   *  whether we have given up on it and fallen back to polling. */
  var link = { source: null, id: 0, live: false, failures: 0, timer: null, why: "" };
  /** Who this page is to the server, and what it last told it. */
  var me = { id: null, label: null, selection: [], reported: "" };
  /** Everyone else, and the soft locks derived from them: path -> [label]. */
  var peers = [];
  var locks = {};

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
    link.id = (initial.events && initial.events.lastEventId) || 0;
    setPeers(initial.clients || []);
    bindControls();
    refreshTree();
    refreshChanges();
    connect();
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

  /* ------------------------------------------------------ the push channel */

  /** Open the event stream, or fall back to polling if it will not open.
   *
   * Everything the stream can tell us, /api/state can also tell us; the stream
   * only tells us sooner and in smaller pieces. So a failure here is a
   * degradation, never a breakage, and it is deliberately easy to reach: no
   * EventSource, a construction that throws, four seconds without a hello, or a
   * connection that keeps dropping.
   */
  function connect() {
    if (!window.EventSource) { fallback("this browser has no EventSource"); return; }
    var url = "/api/events" + (me.id ? "?client=" + encodeURIComponent(me.id) : "");
    try {
      link.source = new EventSource(url);
    } catch (error) {
      fallback("the event stream could not be opened");
      return;
    }
    window.clearTimeout(link.timer);
    link.timer = window.setTimeout(function () {
      // Opened the socket and said nothing: a proxy holding the response until
      // it ends, which for a stream is never. Polling is the honest answer.
      if (!link.live) { drop(); fallback("the event stream did not deliver"); }
    }, STREAM_TIMEOUT_MS);
    ["hello", "tree-changed", "file-changed", "history-changed", "disk-changed",
     "presence", "resync"].forEach(function (name) {
      link.source.addEventListener(name, receive);
    });
    link.source.onerror = function () {
      link.failures += 1;
      if (link.live && link.failures <= STREAM_FAILURES) {
        // EventSource reconnects on its own, resending Last-Event-ID; the
        // server replays what we missed. Nothing to do but say so.
        paintLink("reconnecting");
        return;
      }
      drop();
      fallback("the event stream keeps dropping");
    };
  }

  function receive(event) {
    var data;
    try { data = JSON.parse(event.data); } catch (error) { return; }
    dispatch(data);
  }

  /** One event, whether it arrived on the stream or in a poll's replay. */
  function dispatch(data) {
    if (data.id) { link.id = Math.max(link.id, data.id); }
    var handler = handlers[data.event];
    if (handler) { handler(data); }
  }

  var handlers = {
    "hello": function (data) {
      window.clearTimeout(link.timer);
      link.live = true;
      link.failures = 0;
      me.id = data.client;
      me.label = data.label;
      setPeers(data.clients || []);
      paintLink("live");
      // Whatever this page had selected or half-typed before the stream came up
      // is news to everybody else.
      announce(true);
      if (data.resync) { fullRefresh(); }
    },

    /* The tree moved. `files` names what moved, so only those rows are
     * refetched; `outside` says something changed that is not a document of
     * this inventory -- netgraph.toml, most likely -- and there is no row for
     * that, so the list is refetched whole. */
    "tree-changed": function (data) {
      if (data.client && data.client === me.id) { return; }   // we did this one
      if (data.revision <= state.revision) { return; }        // already caught up
      state.revision = data.revision;
      var moved = data.files || [];
      (moved.length && !data.outside ? patchTree(moved, true) : refreshTree());
      host.render();
      refreshChanges();
    },

    /* One file's bytes are different. The only thing this page has to decide is
     * what to do with the *open* file, which the file list cannot answer: the
     * text on screen may be the only copy of something. */
    "file-changed": function (data) {
      if (!open.path || data.path !== open.path) { return; }
      if (data.hash === null) { mark("gone", "deleted on disk"); return; }
      if (data.hash === open.hash) { return; }
      if (open.dirty) {
        open.conflicted = true;
        mark("conflict", "changed on disk since you opened it");
        paintTree();
        host.toast(open.path + " changed on disk and has unsaved edits here", "error");
        return;
      }
      openFile(open.path);
    },

    /* The undo stack is the server's, so a Ctrl-Z in another tab moves this
     * one's buttons. No fetch: the event carries the depths. */
    "history-changed": function (data) {
      state.undo = data.undo;
      state.redo = data.redo;
      state.undoLabel = data.undoLabel;
      state.redoLabel = data.redoLabel;
      paintHistory();
    },

    /* Something outside this editor wrote to the tree. Worth saying, except
     * about the file in the pane: `file-changed` has already said something
     * more specific about that one, and a second toast would replace it. */
    "disk-changed": function (data) {
      var names = (data.files || []).filter(function (path) { return path !== open.path; });
      if (names.length) {
        host.toast(names.join(", ") + " changed on disk", "ok");
      } else if (!data.files.length && data.outside) {
        host.toast("something changed in the folder outside the inventory", "ok");
      }
    },

    "presence": function (data) { setPeers(data.clients || []); },

    /* The server could not tell us what we missed -- a reconnect after too long
     * away, or this stream falling behind. Everything we hold may be stale, so
     * nothing we hold is patched: it is all fetched again. */
    "resync": function () { fullRefresh(); }
  };

  function drop() {
    window.clearTimeout(link.timer);
    if (link.source) { link.source.close(); link.source = null; }
    link.live = false;
  }

  /* ---------------------------------------------------------------- polling */

  /** Give up on the stream and watch /api/state instead.
   *
   * Not a lesser code path: `?since=` replays the same events, with the same
   * ids, into the same handlers, so a polling page behaves exactly like a
   * streaming one a fraction of a second later. What it loses is the fraction of
   * a second -- and it says so, because "why is this tab behind" deserves an
   * answer on screen. */
  function fallback(why) {
    if (link.why) { return; }   // already fell back; do not restart the timer
    link.why = why;
    paintLink("polling");
    poll();
  }

  function poll() {
    window.clearTimeout(timer);
    var url = "/api/state?since=" + link.id + (me.id ? "&client=" + encodeURIComponent(me.id) : "");
    fetch(url, { cache: "no-store" })
      .then(function (response) { return response.json(); })
      .then(function (next) {
        var moved = next.revision !== state.revision;
        var replay = (next.events && next.events.replay) || [];
        state.writable = next.writable;
        state.undo = next.undo;
        state.redo = next.redo;
        state.undoLabel = next.undoLabel;
        state.redoLabel = next.redoLabel;
        setPeers(next.clients || []);
        paintHistory();
        if (replay.length) {
          replay.forEach(dispatch);
        } else if (moved) {
          // Nothing to replay and yet the revision moved: the ring wrapped, or
          // the server restarted under us. Fetch everything.
          state.revision = next.revision;
          fullRefresh();
        }
        if (!me.id) { announce(true); }
      })
      .catch(function () {})
      .then(function () { timer = window.setTimeout(poll, POLL_MS); });
  }

  /** Everything we hold may be wrong: fetch all of it again.
   *
   * The decision about the open file waits for the refetch rather than racing
   * it. Made against the tree still in hand, it is made against the hashes from
   * *before* the change -- which always compare equal, so the file that just
   * moved on disk was the one thing the reconciliation left alone. */
  function fullRefresh() {
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

  /* --------------------------------------------------------------- presence */

  /** Tell the server what this page has selected and is editing.
   *
   * Sent only when it has actually changed, because every one of these wakes
   * every other page. Advisory throughout: nothing here is a lock, and a save is
   * refused by the content hash or not at all. */
  function announce(force) {
    var payload = {
      client: me.id,
      selection: me.selection,
      editing: open.dirty && open.path ? [open.path] : [],
      view: host.layer ? host.layer() : null
    };
    var fingerprint = JSON.stringify([payload.selection, payload.editing, payload.view]);
    if (!force && fingerprint === me.reported) { return; }
    me.reported = fingerprint;
    fetch("/api/presence", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
      body: JSON.stringify(payload)
    })
      .then(readBody)
      .then(function (body) {
        me.id = body.client;
        me.label = body.label;
        setPeers(body.clients || []);
      })
      .catch(function () { me.reported = ""; });   // try again next time
  }

  /** Note what this page has selected, and let the others draw it. */
  function select(address) {
    me.selection = address ? [address] : [];
    announce(false);
  }

  /** Take everyone else's word for where they are and what they are in. */
  function setPeers(clients) {
    peers = (clients || []).filter(function (entry) { return entry.id !== me.id; });
    locks = {};
    var selected = [];
    peers.forEach(function (entry) {
      (entry.editing || []).forEach(function (path) {
        (locks[path] = locks[path] || []).push(entry.label);
      });
      (entry.selection || []).forEach(function (address) {
        if (selected.indexOf(address) === -1) { selected.push(address); }
      });
    });
    if (host.remote) { host.remote(selected); }
    paintPeers();
    paintTree();
  }

  function paintPeers() {
    el.clients.replaceChildren();
    el.clients.hidden = !peers.length;
    peers.forEach(function (entry) {
      var chip = document.createElement("span");
      chip.className = "client" + (entry.streaming ? "" : " lagging");
      chip.textContent = entry.label;
      var what = (entry.editing || []).length
        ? "editing " + entry.editing.join(", ")
        : ((entry.selection || []).length ? "looking at " + entry.selection.join(", ") : "connected");
      chip.title = what + (entry.streaming ? "" : " (polling; may be a moment behind)");
      el.clients.appendChild(chip);
    });
  }

  /** Say which channel this page is on. A tab that is behind should look it. */
  function paintLink(kind) {
    el.linkState.hidden = false;
    el.linkState.className = "hint link " + kind;
    el.linkState.textContent = kind;
    el.linkState.title = kind === "live"
      ? "changes arrive as they happen"
      : (kind === "polling"
        ? "the event stream could not be used (" + link.why + "); checking once a second"
        : "the event stream dropped; reconnecting");
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

  /** Bring just these files up to date, leaving the rest of the list alone.
   *
   * The whole point of the push channel on a large inventory: a save moves one
   * file, and walking a thousand of them to learn what that one now hashes to is
   * the cost this replaces. `diagnostics` is asked for only when we do not
   * already have this revision's -- an applied change comes back with them --
   * because computing them means validating the whole tree either way.
   *
   * Anything unexpected falls back to the full fetch. A partial update that
   * silently failed would leave a stale hash in the list, and a stale hash is a
   * save refused for no visible reason. */
  function patchTree(paths, diagnostics) {
    if (!paths || !paths.length) { return refreshTree(); }
    var query = paths.map(function (path) {
      return "path=" + encodeURIComponent(path);
    }).join("&");
    return fetch("/api/tree?" + query + "&diagnostics=" + (diagnostics ? "1" : "0"),
      { cache: "no-store" })
      .then(readBody)
      .then(function (next) {
        tree.revision = next.revision;
        merge(next.files || [], next.missing || []);
        paintTree();
        if (diagnostics && host.diagnostics) { host.diagnostics(next.diagnostics || []); }
      })
      .catch(function () { return refreshTree(); });
  }

  /** Put fresh rows in place of the old ones, and drop the ones that are gone.
   *
   * Insertion keeps the list in path order, which is the order the server walks
   * the tree in, so a file created elsewhere lands where a full refetch would
   * have put it rather than at the end. */
  function merge(files, missing) {
    (missing || []).forEach(function (path) {
      tree.files = tree.files.filter(function (file) { return file.path !== path; });
    });
    (files || []).forEach(function (file) {
      var at = -1;
      for (var i = 0; i < tree.files.length; i++) {
        if (tree.files[i].path === file.path) { at = i; break; }
      }
      if (at >= 0) { tree.files[at] = file; return; }
      var before = tree.files.findIndex(function (other) { return other.path > file.path; });
      tree.files.splice(before === -1 ? tree.files.length : before, 0, file);
    });
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
    var lock = lockBadge(file);
    if (lock) { row.appendChild(lock); }
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

  /** Somebody else has unsaved edits in this file.
   *
   * A courtesy, not a lock: the row still opens, the file still saves, and the
   * only thing that can refuse the save is the content hash. Saying so is worth
   * doing because the alternative is finding out by being refused. */
  function lockBadge(file) {
    var who = locks[file.path];
    if (!who || !who.length) { return null; }
    var badge = document.createElement("span");
    badge.className = "badge elsewhere";
    badge.textContent = "in use";
    badge.title = who.join(", ") + " has unsaved edits here";
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
        var wasDirty = open.dirty;
        open = { path: body.path, hash: body.hash, dirty: false, conflicted: false };
        if (wasDirty) { announce(false); }   // the file we were in is free again
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
    // The first keystroke is what puts the "in use" badge on this file in
    // everybody else's list; the ones after it change nothing and send nothing.
    announce(false);
  }

  function save(force) {
    if (!open.path || !state.writable) { return; }
    var body = { text: el.source.value, force: !!force, client: me.id };
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
    var query = me.id ? "?client=" + encodeURIComponent(me.id) : "";
    fetch("/api/" + verb + query, { method: "POST", cache: "no-store" })
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
      announce(false);   // this file is no longer "in use" by us
      if (rewrote) { openFile(open.path); }
    }
    host.toast(what, "ok");
    // Only the files the change touched, and no diagnostics: the response
    // already carries this revision's, and recomputing them would mean
    // validating the whole tree a second time for the same answer.
    patchTree(Object.keys(result.files), false).then(function () {
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
      body: JSON.stringify({ id: id, revision: state.revision, client: me.id })
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

  /* A closing tab is worth one more request: without it the others keep this
   * page's selection and its "in use" badge on screen until the presence entry
   * expires. sendBeacon because a fetch started in pagehide is not guaranteed to
   * leave. The expiry is still there as the backstop, for the tab that crashes
   * or the laptop that closes. */
  window.addEventListener("pagehide", function () {
    if (!me.id || !navigator.sendBeacon) { return; }
    var payload = JSON.stringify({ client: me.id, leaving: true });
    navigator.sendBeacon("/api/presence", new Blob([payload], { type: "application/json" }));
  });

  return {
    attach: attach,
    markDirty: markDirty,
    reveal: reveal,
    locate: locate,
    select: select,
    isOpen: function () { return !!open.path; },
    save: save,
    graphPath: graphPath,
    showChanges: showChanges,
    refreshChanges: refreshChanges,
    isDiffing: function () { return changes.open; },
    isLive: function () { return link.live; }
  };
})();
