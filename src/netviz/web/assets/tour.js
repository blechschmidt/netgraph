/* The guided tour: sixty seconds that prove the diagram and the files are one
 * thing.
 *
 * A first-time visitor opens this page to a canvas, a file list and a palette
 * with four dozen entries, and nothing on screen says which of them is the
 * point. The point is that every shape is a document: create a device here and
 * a YAML file appears, move it and the file moves, undo and the bytes come
 * back. That is hard to say in a paragraph and obvious in a minute, so this
 * does it in a minute.
 *
 * It is not a mime. Every step posts a real batch to /api/ops, which goes
 * through netviz.edit exactly as `netviz edit create` does -- a tour that
 * faked its writes would demonstrate the one thing it is here to demonstrate
 * least well. What makes that safe is that the writes land somewhere else:
 * POST /api/tour copies the inventory into a temporary directory and opens a
 * second, writable session over the copy (netviz/web/tour.py), and while the
 * tour runs every request this page makes carries `?scratch=<token>` and is
 * answered from there.
 *
 * How the redirection is arranged, and why it is a page reload rather than a
 * re-attach: the token is put in sessionStorage and the page is reloaded, so
 * app.js and session.js boot against the scratch the ordinary way and there is
 * no second code path in which a page can be half attached to two sessions.
 * The wrapping of fetch and EventSource below happens at parse time, before
 * app.js has run, which is why this script is loaded before it.
 *
 * Because the copy is always writable, a read-only session can take the tour --
 * which is the session somebody who is just looking is most likely to have
 * open.
 *
 * Dependency-free, like the rest of this page.
 */

var netvizTour = (function () {
  "use strict";

  /** The running tour, in this tab only: a reload keeps it, a new tab does not. */
  var STORE = "netviz.tour";
  /** That the invitation has been answered, in this browser, for good. */
  var SEEN = "netviz.tour.seen";

  var START_PATH = "/api/tour";
  var END_PATH = "/api/tour/end";

  /** The element the tour creates, and the port it is cabled by. Fixed names,
   *  so the tour reads the same twice and a test can look for them. */
  var DEVICE = "sw-tour";
  var PORT = "eth0";
  var PEER_PORT = "eth-tour";
  var FILE = "tour/sw-tour.yaml";
  /** Where the tour's device is moved to, and the far end when the inventory
   *  has no device to cable to. */
  var LONE_PEER = "pc-tour";

  /** { token, origin, root, files, peer } while a tour is running. */
  var running = read();
  var host = null;
  var panel = null;
  var entry = null;
  var spot = null;
  var index = 0;
  /** How many batches have been applied, and therefore how many undos it takes
   *  to put the copy back exactly as it was found. */
  var batches = 0;
  var busy = false;

  /* --------------------------------------------------- the redirection */

  /** This tab's tour, or null. Unreadable storage is "no tour", not an error:
   *  a page that will not boot because of a private-browsing quirk is worse
   *  than one that simply does not offer the tour. */
  function read() {
    try {
      var raw = window.sessionStorage.getItem(STORE);
      return raw ? JSON.parse(raw) : null;
    } catch (error) {
      return null;
    }
  }

  function write(value) {
    try {
      if (value) { window.sessionStorage.setItem(STORE, JSON.stringify(value)); }
      else { window.sessionStorage.removeItem(STORE); }
    } catch (error) { /* nothing to do about it, and nothing to break */ }
  }

  function seen() {
    try { return window.localStorage.getItem(SEEN) === "yes"; }
    catch (error) { return true; }
  }

  function markSeen() {
    try { window.localStorage.setItem(SEEN, "yes"); }
    catch (error) { /* the invitation will come back; that is all */ }
  }

  /** Send this request to the scratch instead of the tree.
   *
   * Only our own routes, only while a tour is running, and never the two routes
   * that start and end one -- those are about the real session by definition.
   */
  function redirect(url) {
    var text = String(url);
    if (!running || text.indexOf("/api/") !== 0) { return text; }
    if (text.indexOf(START_PATH) === 0) { return text; }
    return text + (text.indexOf("?") === -1 ? "?" : "&")
      + "scratch=" + encodeURIComponent(running.token);
  }

  if (running) {
    var realFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
      return realFetch(typeof input === "string" ? redirect(input) : input, init);
    };
    if (window.EventSource) {
      var RealSource = window.EventSource;
      var Wrapped = function (url, config) { return new RealSource(redirect(url), config); };
      Wrapped.prototype = RealSource.prototype;
      window.EventSource = Wrapped;
    }
    /* The tab that is closed mid-tour: without this the copy sits in the
     * temporary directory until the server stops or the hour is up. */
    window.addEventListener("pagehide", function () {
      if (!running || !navigator.sendBeacon) { return; }
      navigator.sendBeacon(
        END_PATH + "?scratch=" + encodeURIComponent(running.token), new Blob([], {})
      );
    });
  }

  /* --------------------------------------------------------- the steps */

  /** One card. `run` is the edit it makes, and is what the Next button waits
   *  for; a card without one is prose. */
  function steps() {
    var peer = (running && running.peer) || "";
    var far = peer || LONE_PEER;
    return [
      {
        id: "welcome",
        title: "A sixty-second tour",
        body: function () {
          return "This page draws an inventory of YAML files, and every shape in it "
            + "is a document. The next five steps create a device, cable it up, move "
            + "its document to another file, show you the YAML that changed, and put "
            + "it all back.";
        },
        note: function () {
          return "Working on a copy of " + (running.files || 0) + " file"
            + (running.files === 1 ? "" : "s") + " made a moment ago. Your inventory is "
            + "not open for writing here at all.";
        },
        spot: "#files"
      },
      {
        id: "create",
        title: "Create a device",
        body: function () {
          return "One batch through the same write path <code>netviz edit create</code> "
            + "uses. Watch the file list on the left: a document has to be written "
            + "for the shape to exist, and netviz chooses where by looking at where "
            + "the inventory already puts things like it.";
        },
        spot: "#file-list",
        run: function () {
          return apply([{
            op: "create",
            kind: "switch",
            name: DEVICE,
            namespace: "",
            spec: { interfaces: [{ name: PORT, type: "ethernet" }] }
          }], "created switch " + DEVICE);
        },
        after: function () { return "created " + DEVICE + ", and the file that declares it"; }
      },
      {
        id: "connect",
        title: "Cable it to " + far,
        body: function () {
          return peer
            ? "A port on <code>" + esc(peer) + "</code>, and a cable between the two. "
              + "Two operations, one batch — so it is also one thing to undo. The link "
              + "appears in the diagram because a <code>kind: cable</code> document now "
              + "says it is there."
            : "This inventory has no device to cable to, so the tour creates the far "
              + "end as well. Three operations, one batch — so it is also one thing to "
              + "undo.";
        },
        spot: "#viewport",
        run: function () {
          var list = peer
            ? [{
                op: "add-interface",
                address: peer,
                interface: { name: PEER_PORT, type: "ethernet" }
              }]
            : [{
                op: "create",
                kind: "computer",
                name: LONE_PEER,
                namespace: "",
                spec: { interfaces: [{ name: PEER_PORT, type: "ethernet" }] }
              }];
          list.push({ op: "connect", a: DEVICE + ":" + PORT, b: far + ":" + PEER_PORT });
          return apply(list, "connected " + DEVICE + " to " + far);
        },
        after: function () { return "cabled " + DEVICE + " to " + far; }
      },
      {
        id: "move",
        title: "Move its document",
        body: function () {
          return "<code>" + esc(DEVICE) + "</code> does not change: the same element, "
            + "the same cable, the same diagram. Only the file it is declared in moves, "
            + "to <code>" + esc(FILE) + "</code>, which did not exist a second ago. "
            + "This is what it means for the layout of the tree to be yours and not "
            + "netviz's.";
        },
        spot: "#file-list",
        run: function () {
          return apply(
            [{ op: "move", address: DEVICE, file: FILE }],
            "moved " + DEVICE + " to " + FILE
          );
        },
        after: function () { return "moved the document to " + FILE; }
      },
      {
        id: "diff",
        title: "The YAML that changed",
        body: function () {
          return "Every gesture is logged with the unified diff it produced, and with "
            + "the <code>netviz edit</code> command that would repeat it from a "
            + "script. The diagram behind is now drawn as a diff too: what was added "
            + "is marked <span class=\"tour-sigil\">+</span>.";
        },
        spot: "#changes",
        run: function () {
          netvizSession.showChanges(true);
          return Promise.resolve(true);
        },
        after: function () {
          return batches + " change" + (batches === 1 ? "" : "s")
            + ", with the YAML each one wrote";
        }
      },
      {
        id: "undo",
        title: "Undo the lot",
        body: function () {
          return batches + " batch" + (batches === 1 ? "" : "es") + ", "
            + batches + " undo" + (batches === 1 ? "" : "s")
            + " — the history is the server's, so it survives a reload and is the "
            + "same history <code>Ctrl-Z</code> walks. "
            + "When it finishes, the copy is byte for byte what it was when the tour "
            + "started.";
        },
        spot: "#undo",
        run: function () {
          netvizSession.showChanges(false);
          return undoAll();
        },
        after: function () { return "undone; the files are back as they were"; }
      },
      {
        id: "done",
        title: "That is the whole idea",
        body: function () {
          return "The picture and the files are one thing, in both directions: edit "
            + "the YAML on the left and the diagram follows. Everything the tour did "
            + "is in the command palette — <kbd>Ctrl-K</kbd> — and everything the "
            + "palette does is a <code>netviz edit</code> command you can run from a "
            + "script. Press <kbd>?</kbd> for the keys.";
        },
        note: function () {
          return "Finishing deletes the copy and puts you back on "
            + (running.origin || "your inventory") + ".";
        },
        spot: "#commands",
        last: true
      }
    ];
  }

  /* -------------------------------------------------------- the driving */

  /** Post a batch to the scratch and count it, so undo knows how far to go. */
  function apply(list, said) {
    return netvizSession.ops(list, said).then(function (result) {
      batches += 1;
      return result;
    });
  }

  /** Undo every batch this tour applied, oldest last, one at a time. */
  function undoAll() {
    if (batches <= 0) { return Promise.resolve(true); }
    return netvizSession.step("undo").then(function (moved) {
      if (!moved) { batches = 0; return false; }
      batches -= 1;
      return undoAll();
    });
  }

  /* ---------------------------------------------------------- the panel */

  function esc(text) {
    var node = document.createElement("span");
    node.textContent = String(text === undefined || text === null ? "" : text);
    return node.innerHTML;
  }

  function build() {
    var node = document.createElement("aside");
    node.className = "tour";
    node.id = "tour";
    node.setAttribute("role", "dialog");
    node.setAttribute("aria-labelledby", "tour-title");
    node.setAttribute("aria-describedby", "tour-body");
    node.innerHTML =
      '<p class="tour-progress" id="tour-progress"></p>'
      + '<h2 id="tour-title"></h2>'
      + '<div id="tour-body" class="tour-body"></div>'
      + '<p id="tour-note" class="tour-note"></p>'
      + '<p id="tour-outcome" class="tour-note" role="status"></p>'
      + '<p class="tour-actions">'
      + '<button type="button" id="tour-next" class="tour-next"></button>'
      + '<button type="button" id="tour-skip" class="ghost">Skip <kbd>Esc</kbd></button>'
      + "</p>"
      + '<p class="tour-safe" id="tour-safe"></p>';
    node.querySelector("#tour-next").addEventListener("click", function () { advance(); });
    node.querySelector("#tour-skip").addEventListener("click", function () { finish(); });
    return node;
  }

  /** Outline the thing the current card is about, and nothing else. */
  function highlight(selector) {
    if (spot) { spot.classList.remove("tour-spot"); spot = null; }
    if (!selector) { return; }
    var target = document.querySelector(selector);
    if (!target) { return; }
    target.classList.add("tour-spot");
    spot = target;
  }

  function paint() {
    var list = steps();
    var step = list[index];
    panel.querySelector("#tour-progress").textContent =
      "Step " + (index + 1) + " of " + list.length;
    panel.querySelector("#tour-title").textContent = step.title;
    panel.querySelector("#tour-body").innerHTML = step.body();
    panel.querySelector("#tour-note").innerHTML = step.note ? step.note() : "";
    var outcome = panel.querySelector("#tour-outcome");
    outcome.className = "tour-note";
    outcome.textContent = "";
    var next = panel.querySelector("#tour-next");
    next.disabled = false;
    next.innerHTML = step.last
      ? 'Finish <kbd>&crarr;</kbd>'
      : (step.run ? "Do it" : "Next") + ' <kbd>&crarr;</kbd>';
    panel.querySelector("#tour-safe").innerHTML =
      "Editing a scratch copy in <code>" + esc(running.root) + "</code>. Nothing "
      + "under <code>" + esc(running.origin) + "</code> is written.";
    highlight(step.spot);
    netvizA11y.announce(step.title + ". " + panel.querySelector("#tour-body").textContent, false);
  }

  /** Do this card's edit, say what happened, and move on to the next. */
  function advance() {
    if (busy) { return; }
    var list = steps();
    var step = list[index];
    if (step.last) { finish(); return; }
    var next = panel.querySelector("#tour-next");
    if (!step.run) { index += 1; paint(); return; }
    busy = true;
    next.disabled = true;
    next.textContent = "working…";
    Promise.resolve()
      .then(step.run)
      .then(function () {
        busy = false;
        index += 1;
        paint();
        if (step.after) {
          var outcome = panel.querySelector("#tour-outcome");
          outcome.className = "tour-note done";
          outcome.textContent = step.after();
          netvizA11y.announce(step.after(), false);
        }
      })
      .catch(function (error) {
        // A refused edit is worth showing rather than hiding: this is a real
        // write path and it is allowed to say no. The tour goes on to the next
        // card, because a stuck tour is worse than an incomplete one.
        busy = false;
        index += 1;
        paint();
        var outcome = panel.querySelector("#tour-outcome");
        outcome.className = "tour-note failed";
        outcome.textContent =
          "that step was refused: " + String((error && error.message) || error);
      });
  }

  /** End the tour: drop the copy, forget the token, and reload onto the tree. */
  function finish() {
    var token = running && running.token;
    markSeen();
    write(null);
    highlight(null);
    if (entry) { netvizKeys.closeOverlay(entry); entry = null; }
    var done = function () { window.location.reload(); };
    if (!token) { done(); return; }
    // Deliberately not through the wrapped fetch's redirect: the token is the
    // body of this request, not the session it is answered from.
    window.fetch(END_PATH + "?scratch=" + encodeURIComponent(token), {
      method: "POST",
      cache: "no-store"
    }).then(done, done);
  }

  /* ------------------------------------------------------ the invitation */

  /** The one thing a first-time visitor is shown without asking. */
  function invite() {
    var node = document.createElement("aside");
    node.className = "tour tour-invite";
    node.id = "tour-invite";
    node.setAttribute("role", "dialog");
    node.setAttribute("aria-labelledby", "tour-invite-title");
    node.innerHTML =
      '<h2 id="tour-invite-title">First time here?</h2>'
      + '<div class="tour-body">Sixty seconds that create a device, cable it up, move '
      + "its document and undo the lot — on a throwaway copy of this inventory, so "
      + "your files are not touched.</div>"
      + '<p class="tour-actions">'
      + '<button type="button" id="tour-take" class="tour-next">Take the tour <kbd>&crarr;</kbd></button>'
      + '<button type="button" id="tour-not-now" class="ghost">No thanks <kbd>Esc</kbd></button>'
      + "</p>";
    var handle = null;
    var shut = function () {
      markSeen();
      netvizKeys.closeOverlay(handle);
    };
    var take = function () { shut(); start(); };
    node.querySelector("#tour-take").addEventListener("click", take);
    node.querySelector("#tour-not-now").addEventListener("click", shut);
    handle = netvizKeys.overlay(node, {
      focus: function () { node.querySelector("#tour-take").focus(); },
      close: shut,
      onKey: function (chord, event) {
        if (chord === "Escape") { event.preventDefault(); shut(); return true; }
        if (chord === "Enter") { event.preventDefault(); take(); return true; }
        return false;
      }
    });
  }

  /* ------------------------------------------------------------ starting */

  /** Ask for a scratch copy, then reload onto it. */
  function start() {
    if (running) { return Promise.resolve(true); }
    return window.fetch(START_PATH, { method: "POST", cache: "no-store" })
      .then(function (response) {
        return response.json().then(function (body) {
          if (!response.ok) { throw new Error(body.message || response.statusText); }
          return body;
        });
      })
      .then(function (body) {
        write({
          token: body.scratch,
          root: body.root,
          origin: body.origin,
          peer: body.peer,
          files: body.files
        });
        window.location.reload();
        return true;
      })
      .catch(function (error) {
        if (host) { host.toast("the tour could not start: " + error.message, "error"); }
        return false;
      });
  }

  /** Put the panel up for a tour this page has already reloaded into. */
  function resume() {
    panel = build();
    index = 0;
    batches = 0;
    entry = netvizKeys.overlay(panel, {
      focus: function () { panel.querySelector("#tour-next").focus(); },
      close: finish,
      onKey: function (chord, event) {
        if (chord === "Escape") { event.preventDefault(); finish(); return true; }
        if (chord === "Enter" || chord === "ArrowRight") {
          // Not while the focus is on Skip: Enter there means Skip.
          if (chord === "Enter" && document.activeElement
              && document.activeElement.id === "tour-skip") {
            return false;
          }
          event.preventDefault();
          advance();
          return true;
        }
        // Everything else still reaches the page underneath, which is the
        // difference between a tour and a hostage situation.
        return false;
      }
    });
    paint();
  }

  /* --------------------------------------------------------- the command */

  function defineCommands(bridge) {
    host = bridge;
    netvizKeys.define("tour", {
      run: function () {
        if (running) { netvizA11y.announce("the tour is already running", false); return; }
        start();
      }
    });
  }

  /** Called once, after the page knows which face it is and which session
   *  answered it.
   *
   * The check on `state.scratch` is the load-bearing line in this file. A token
   * the server no longer has -- it restarted, or the hour expired -- is
   * deliberately *not* a refusal: every route answers such a request from the
   * real tree instead, so that a reloaded tab gets a working page rather than a
   * dead one. Which means a tour that resumed without looking would drive its
   * create, connect and move against the inventory it exists to protect. So it
   * resumes only when the server confirms the copy is the thing answering.
   */
  function boot(mode, state) {
    if (running) {
      if (mode !== "session" || !state || state.scratch !== running.token) {
        write(null);
        window.location.reload();
        return false;
      }
      resume();
      return true;
    }
    if (mode === "session" && !seen()) { invite(); return true; }
    return false;
  }

  return {
    defineCommands: defineCommands,
    boot: boot,
    start: start,
    finish: finish,
    /** For tests: what this page thinks it is editing. */
    scratch: function () { return running ? running.token : ""; },
    isRunning: function () { return !!running; }
  };
})();
