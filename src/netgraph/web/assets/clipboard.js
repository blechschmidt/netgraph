/* Copy, cut, paste and duplicate — the four chords, and the one clipboard.
 *
 * What is on the clipboard is a *fragment*: the JSON the server built out of
 * the selected documents, the cables between them and where the view puts them.
 * See netgraph/edit/clipboard.py, which decides all of that; nothing here knows
 * what an element is, and nothing here writes YAML.
 *
 * The clipboard, twice
 * --------------------
 *
 * There are two, and both are needed:
 *
 *   the system clipboard   What makes a fragment leave this page. Ctrl-C writes
 *                          the JSON to it, so it can be pasted into another
 *                          netgraph window, another inventory, or a text editor
 *                          where it reads as data rather than as a broken file.
 *   this page's own        What makes Ctrl-V work at all. Reading the system
 *                          clipboard needs a permission the browser may refuse
 *                          or prompt for, and a paste that opened a permission
 *                          dialog every time would be a paste nobody uses. So
 *                          the last thing this page copied is kept in memory
 *                          and used whenever the system clipboard cannot be
 *                          read or does not hold a netgraph fragment.
 *
 * The order is deliberate: the system clipboard *wins* when it holds a
 * fragment, because that is what "I copied something in the other tab" means.
 * Memory is the fallback, not the preference.
 *
 * The anchor
 * ----------
 *
 * A paste from the keyboard is offset from the originals; a paste from the
 * canvas context menu lands where the pointer was. The second is the whole
 * reason `menu.js` remembers the point it opened at: dropping a fragment in the
 * middle of the diagram and making the user drag it back is not a paste.
 *
 * Dependency-free, like the rest of this page.
 */

var netgraphClipboard = (function () {
  "use strict";

  /** The `format` the server stamps a fragment with. A payload without it is
   *  somebody else's JSON and is left alone. */
  var FORMAT = "netgraph.dev/clipboard/v1";

  /** The last fragment this page copied. See the header: this is the fallback
   *  for a system clipboard the browser will not let us read. */
  var held = null;

  var host = null;

  function attach(bridge) {
    host = bridge;
    return true;
  }

  /* ------------------------------------------------------------- writing */

  /** Put a fragment on the system clipboard, and always in memory.
   *
   * Memory first and unconditionally: the system write can fail — no permission,
   * an insecure context, a browser that has never heard of `navigator.clipboard`
   * — and Ctrl-C followed by Ctrl-V has to work anyway. Failing to reach the
   * system clipboard costs the *between-windows* case only, and says so.
   */
  function remember(fragment) {
    held = fragment;
    var text = JSON.stringify(fragment, null, 2);
    return write(text).then(function () { return true; }, function () { return false; });
  }

  function write(text) {
    if (window.navigator.clipboard && window.navigator.clipboard.writeText) {
      return window.navigator.clipboard.writeText(text);
    }
    return legacyWrite(text) ? Promise.resolve() : Promise.reject(new Error("no clipboard"));
  }

  /** The pre-`navigator.clipboard` route: a hidden textarea and execCommand.
   *
   * Kept because it is the only thing that works in a page served over plain
   * HTTP to something other than localhost, which is a `netgraph web --host`
   * away and not worth breaking copy over. */
  function legacyWrite(text) {
    var node = document.createElement("textarea");
    node.value = text;
    node.setAttribute("readonly", "readonly");
    node.style.position = "fixed";
    node.style.left = "-9999px";
    document.body.appendChild(node);
    var selected = document.activeElement;
    node.select();
    var done = false;
    try { done = document.execCommand("copy"); } catch (error) { done = false; }
    document.body.removeChild(node);
    if (selected && selected.focus) { selected.focus(); }
    return done;
  }

  /* ------------------------------------------------------------- reading */

  /** The fragment to paste, as a promise. Resolves to null when there is none.
   *
   * Never rejects: "there is nothing on the clipboard" is an answer, not a
   * failure, and a rejected promise here would surface as an error toast for
   * somebody who pressed Ctrl-V by habit.
   */
  function read() {
    return system().then(function (fragment) {
      return fragment || held;
    }, function () {
      return held;
    });
  }

  function system() {
    if (!window.navigator.clipboard || !window.navigator.clipboard.readText) {
      return Promise.resolve(null);
    }
    return window.navigator.clipboard.readText().then(parse, function () { return null; });
  }

  /** JSON that is a netgraph fragment, or null for anything else.
   *
   * Anything else is the common case — a URL, a line of YAML, a password — and
   * it must not become an error. A fragment is recognised by its `format` and
   * by nothing else, so a hand-edited one still pastes and a plausible-looking
   * object from another tool does not. */
  function parse(text) {
    if (!text || text.length > MAX_FRAGMENT) { return null; }
    var payload = null;
    try { payload = JSON.parse(text); } catch (error) { return null; }
    if (!payload || payload.format !== FORMAT || !Array.isArray(payload.documents)) {
      return null;
    }
    return payload;
  }

  /** Largest text this page will try to read as a fragment. A clipboard holding
   *  a megabyte is a clipboard holding something else. */
  var MAX_FRAGMENT = 4 * 1024 * 1024;

  /* ------------------------------------------------------ what it holds */

  /** How many elements the fragment in hand carries, for a disabled reason. */
  function size(fragment) {
    return fragment && Array.isArray(fragment.documents) ? fragment.documents.length : 0;
  }

  /** Whether this page has copied anything yet. `read` may still find one on
   *  the system clipboard, so a command is never disabled on this alone. */
  function isEmpty() { return !held; }

  /* --------------------------------------------------------- the commands */

  /** Register the four chords. Called at boot beside every other gesture. */
  function defineCommands(bridge) {
    host = bridge;
    var K = netgraphKeys;

    K.define("clipboard.copy", {
      run: function () { copy(false); },
      enabled: chosen
    });
    K.define("clipboard.cut", {
      run: function () { copy(true); },
      enabled: chosen
    });
    K.define("clipboard.duplicate", {
      run: duplicate,
      enabled: chosen
    });
    K.define("clipboard.paste", {
      // `context.at` is a *screen* point -- the pointer, as menu.js remembered
      // it -- and the server places in the diagram's own coordinates, so the
      // transform happens here and exactly once. The keyboard supplies no
      // point, and a keyboard paste is offset from the originals instead.
      run: function (context) { paste(anchor(context && context.at)); }
    });
  }

  /** A remembered pointer position in the diagram's coordinates, or null. */
  function anchor(at) {
    if (!at || !window.netgraphNotes || !netgraphNotes.graphPoint) { return null; }
    return netgraphNotes.graphPoint(at.x, at.y);
  }

  /** Why a clipboard gesture cannot run: it needs something to act on. */
  function chosen() {
    return targets().length ? true : "select something first";
  }

  /** What the gesture acts on: the selection, as addresses the server knows.
   *
   * The `#n` a parallel link's id carries is the *drawing's* way of telling two
   * cables between the same pair apart; the inventory has one name for it, and
   * that is what a copy is about. The same trim the delete gesture makes.
   */
  function targets() {
    return netgraphSelect.targets().map(function (address) {
      return String(address).split("#")[0];
    });
  }

  function copy(cutting) {
    var addresses = targets();
    if (!addresses.length) { return; }
    if (cutting) { cut(addresses); return; }
    host.post("/api/copy", { addresses: addresses, view: host.layer() })
      .then(function (result) {
        return remember(result.clipboard).then(function (reached) {
          announce(result.clipboard, reached, "copied");
        });
      })
      .catch(function () {});
  }

  /** Cut: the same fragment, and the documents deleted, as one change.
   *
   * Asked about first, and with the same words Delete uses, because it *is* a
   * delete — the difference is only that what goes is also on the clipboard,
   * and a clipboard is not a backup of a file. */
  function cut(addresses) {
    var lines = [
      "Cut " + count(addresses.length, "element") + "?",
      "",
      "The documents are removed from the inventory and put on the clipboard.",
      "This is one change: Ctrl-Z puts all of it back."
    ];
    if (!window.confirm(lines.join("\n"))) { return; }
    host.post("/api/cut", { addresses: addresses, view: host.layer() })
      .then(function (result) {
        netgraphSelect.clear({ quiet: true });
        return remember(result.clipboard).then(function (reached) {
          host.applied(result, "cut " + count(size(result.clipboard), "element"));
          announce(result.clipboard, reached, "cut");
        });
      })
      .catch(function () {});
  }

  function duplicate() {
    var addresses = targets();
    if (!addresses.length) { return; }
    host.post("/api/duplicate", { addresses: addresses, view: host.layer() })
      .then(function (result) {
        host.applied(result, "duplicated " + count(addresses.length, "element"));
      })
      .catch(function () {});
  }

  /** Paste whatever is on the clipboard, at `at` when the pointer named one. */
  function paste(at) {
    read().then(function (fragment) {
      if (!fragment) {
        host.refuse("the clipboard holds no netgraph elements");
        return;
      }
      var body = { payload: fragment, view: host.layer() };
      if (at) { body.at = { x: at.x, y: at.y }; }
      host.post("/api/paste", body)
        .then(function (result) {
          host.applied(result, "pasted " + count(size(fragment), "element"));
        })
        .catch(function () {});
    });
  }

  /** Say what happened, and say it honestly when only half of it did.
   *
   * A fragment that reached this page's memory but not the system clipboard is
   * still pasteable *here*, and the one thing it cannot do is travel — so that
   * is what the message says, rather than "copy failed". */
  function announce(fragment, reached, verb) {
    var dropped = (fragment && fragment.dropped) || [];
    var said = verb + " " + count(size(fragment), "element");
    if (dropped.length) {
      said += ", without " + count(dropped.length, "link")
        + " with only one end in the selection";
    }
    if (!reached) {
      said += " (this window only: the system clipboard could not be written)";
    }
    // "ok" either way: the copy *worked*, and the parenthesis above says which
    // half of it did. An error styling for a fragment that is pasteable in this
    // very window would be telling somebody something went wrong when it did not.
    host.toast(said, "ok");
  }

  function count(number, noun) {
    return number + " " + noun + (number === 1 ? "" : "s");
  }

  return {
    attach: attach,
    defineCommands: defineCommands,
    /* For tour.js and the tests: what this page is holding, and putting
     * something there without going through a keystroke. */
    held: function () { return held; },
    remember: remember,
    read: read,
    parse: parse,
    isEmpty: isEmpty,
    format: FORMAT
  };
})();
