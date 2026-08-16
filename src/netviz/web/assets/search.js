/* The search box, and what makes it worth having: it takes a query.
 *
 * This box used to be a substring match over element names — which is a fine
 * way to find `sw-core-01` and no way at all to ask "which access switches have
 * no uplink". So what is typed here now is a *selector query*
 * (`netviz.query`), exactly the string `netviz query` answers, exactly the
 * string `render --select` narrows a drawing with, and exactly the string an
 * `assert: query` claims. One language, four places; a person who learns it
 * here can put it in a test suite, and a query that passes in CI can be pasted
 * into this box to see what it means.
 *
 * The substring search did not go away, it became a rule of the grammar: a bare
 * word with no operator in it is `name ~ *word*`. So the reader who types
 * `sw-core` and expects the old behaviour gets the old behaviour, and the one
 * who types `label.role = access and not neighbors of (label.role = core)`
 * gets an answer no substring match could have given.
 *
 * Three things happen with the answer, and they are deliberately separate:
 *
 *   **Highlight** is the default. The matches keep their halo-less ring and
 *   everything else dims, so the query is read *against* the diagram — which is
 *   the point of asking it here rather than at a shell. Nothing is hidden, so
 *   what a query does *not* select stays visible and the answer can be checked.
 *
 *   **Filter** — the toggle beside the box — sends the query to the server as
 *   the view's `select`, so the drawing itself is narrowed by the same
 *   `FilterSpec` `netviz render --select` narrows one with. That is a
 *   different picture, not a differently painted one, and it is why it is a
 *   toggle rather than the default.
 *
 *   **Select** — Enter — puts the matches in select.js's set. From there they
 *   are what every bulk gesture already acts on: `element.set` over twelve
 *   switches, an alignment, a delete, a copy. That is the whole reason the
 *   server hands back `addresses` rather than node ids: a query's answer has to
 *   be postable to /api/ops without anything in between reinterpreting it.
 *
 * The evaluation is the server's. It has the resolved model — the VRF bindings,
 * the netns tree, the routing views — and the browser has an SVG; a query
 * answered here would be a second, worse implementation of `selects`, which is
 * the thing this whole feature exists to stop there being. It costs one GET per
 * debounce interval and the server answers a ten-term query over a thousand
 * devices in single-digit milliseconds.
 *
 * Dependency-free, like the rest of this page.
 */
window.netvizSearch = (function () {
  "use strict";

  /** How long the box sits still before the query is sent, in ms. Long enough
   *  that typing a twenty-character expression is one request and not twenty,
   *  short enough that it feels like it is keeping up. */
  var DEBOUNCE = 180;

  /** Most matches announced by name in the status line before it counts instead. */
  var MAX_NAMED = 3;

  var host = null;
  var el = null;

  /** The last answer from the server, or null. */
  var answer = null;
  /** The query that produced it, so a stale response can be dropped. */
  var asked = "";
  /** The debounce timer. */
  var timer = 0;
  /** Is the drawing itself narrowed, rather than the matches highlighted? */
  var filtering = false;
  /** How many requests have been sent, so an out-of-order answer is ignored. */
  var sequence = 0;

  /* --------------------------------------------------------------- attach */

  /** Take over the search box. `bridge` is app.js's side:
   *
   *    el          the elements shared with app.js
   *    layer()     which layer is being drawn
   *    refuse(why) say no, visibly
   *    rerender()  ask for the drawing again; the view options have moved
   */
  function attach(bridge) {
    host = bridge;
    el = bridge.el;
    if (!el.search) { return false; }
    el.search.addEventListener("input", function () { schedule(); });
    el.search.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        // Enter is "act on it", and waiting out the debounce first is the
        // difference between a box that answers and one that seems to ignore
        // the key that means go.
        if (pending()) { run(true); } else { chooseMatches(); }
        return;
      }
      if (event.key === "Escape") { event.preventDefault(); clear(); }
    });
    if (el.searchFilter) {
      el.searchFilter.addEventListener("click", function () { setFiltering(!filtering); });
    }
    return true;
  }

  /** Is there typing the server has not been asked about yet? */
  function pending() {
    return el.search.value.trim() !== asked;
  }

  /* ------------------------------------------------------------ the query */

  function text() { return el.search ? el.search.value.trim() : ""; }

  /** The query the *view* is narrowed by: the text, or "" when highlighting. */
  function selector() { return filtering ? text() : ""; }

  function schedule() {
    window.clearTimeout(timer);
    timer = window.setTimeout(function () { run(false); }, DEBOUNCE);
  }

  /** Ask the server, and paint whatever comes back.
   *
   * `andSelect` carries Enter through the round trip, so pressing it while a
   * request is still in flight selects the answer to *that* request rather than
   * to the one before it.
   */
  function run(andSelect) {
    window.clearTimeout(timer);
    var wanted = text();
    asked = wanted;
    if (!wanted) { reset(); return; }
    var mine = ++sequence;
    fetch("/api/query?q=" + encodeURIComponent(wanted) +
          "&layer=" + encodeURIComponent(host.layer()), { cache: "no-store" })
      .then(function (response) { return response.json(); })
      .then(function (result) {
        if (mine !== sequence) { return; }   // a later keystroke already won
        answer = result;
        paint();
        announce();
        if (andSelect && !result.error) { chooseMatches(); }
      })
      .catch(function () {
        if (mine !== sequence) { return; }
        answer = null;
        say("the query could not be sent", true);
      });
  }

  /** Forget the query: the box, the paint and the narrowing all go. */
  function clear() {
    if (!el.search) { return false; }
    var had = !!text() || filtering;
    el.search.value = "";
    asked = "";
    answer = null;
    window.clearTimeout(timer);
    sequence += 1;
    reset();
    if (filtering) { setFiltering(false); }
    return had;
  }

  function reset() {
    answer = null;
    say("", false);
    paint();
  }

  /* ------------------------------------------------------------ the paint */

  /** Mark the matching shapes, and dim the rest.
   *
   * Against cull.js's index, like every other per-element mark on this page:
   * the shape of an off-screen element is parked, so asking the DOM would find
   * nothing and asking it for a thousand elements would force a layout per
   * element. The class goes on the canvas so the whole rule set is one
   * stylesheet toggle rather than a class written to every shape.
   */
  function paint() {
    if (!el || !el.canvas) { return; }
    var matched = matchedIds();
    el.canvas.classList.toggle("searching", matched !== null);
    Object.keys(shapes()).forEach(function (id) {
      var node = document.getElementById(id);
      if (!node) { return; }
      node.classList.toggle("search-hit", matched !== null && !!matched[id]);
    });
  }

  /** id -> true for every shape drawing a matched element, or null when the
   *  box is empty or the query did not parse. */
  function matchedIds() {
    if (!answer || answer.error || !answer.addresses || !answer.addresses.length) {
      return answer && !answer.error && asked ? {} : null;
    }
    var found = {};
    answer.addresses.forEach(function (address) {
      window.netvizSelect.shapesOf(address).forEach(function (id) { found[id] = true; });
    });
    return found;
  }

  /** Every shape id the drawing currently holds, as a set.
   *
   * cull.js's index, not the DOM: a parked element is still part of the answer,
   * and the index is the only thing that knows about one.
   */
  function shapes() {
    return window.netvizCull.ids();
  }

  /* ------------------------------------------------------------- the news */

  function announce() {
    if (!answer) { say("", false); return; }
    if (answer.error) {
      // The caret block is several lines and the status strip is one, so the
      // strip carries the sentence and the title carries the whole thing.
      say(firstLine(answer.error), true, answer.error);
      return;
    }
    var count = answer.count || 0;
    if (!count) { say("no match", false, ""); return; }
    var named = (answer.addresses || []).slice(0, MAX_NAMED).map(shortName).join(", ");
    var more = count > MAX_NAMED ? " and " + (count - MAX_NAMED) + " more" : "";
    say(count + (count === 1 ? " match" : " matches") + ": " + named + more, false, "");
  }

  function firstLine(message) {
    var at = String(message).indexOf("\n");
    return at === -1 ? String(message) : String(message).slice(0, at);
  }

  function shortName(address) {
    var parts = String(address).split("/");
    return parts[parts.length - 1];
  }

  function say(message, bad, title) {
    if (!el.searchStatus) { return; }
    el.searchStatus.textContent = message;
    el.searchStatus.classList.toggle("bad", !!bad);
    el.searchStatus.title = title || "";
    if (el.search) { el.search.classList.toggle("invalid", !!bad); }
  }

  /* --------------------------------------------------------- the gestures */

  /** Put every match in the selection, so a query feeds a bulk edit. */
  function chooseMatches() {
    if (!answer || answer.error) {
      host.refuse(answer && answer.error ? firstLine(answer.error) : "type a query first");
      return 0;
    }
    var addresses = (answer.addresses || []).filter(function (address) {
      // Only what is drawn can be selected: select.js holds addresses and
      // re-resolves them against the drawing, and one it cannot resolve would
      // sit in the set contributing nothing to a bulk edit but a refusal.
      return window.netvizSelect.shapesOf(address).length > 0;
    });
    if (!addresses.length) {
      host.refuse(answer.count
        ? "the matches are not in this view; clear the VLAN filter or change layer"
        : "nothing matches that query");
      return 0;
    }
    window.netvizSelect.set(addresses);
    return addresses.length;
  }

  /** Turn the drawing-narrowing on or off, and redraw when it moved. */
  function setFiltering(wanted) {
    if (filtering === !!wanted) { return; }
    filtering = !!wanted;
    if (el.searchFilter) {
      el.searchFilter.setAttribute("aria-pressed", filtering ? "true" : "false");
      el.searchFilter.classList.toggle("on", filtering);
    }
    host.rerender();
  }

  /* --------------------------------------------------------- the commands */

  /** Register the search's own commands, and the palette's query entry. */
  function defineCommands() {
    var K = window.netvizKeys;

    K.define("search.focus", {
      run: function () {
        if (!el.search) { return; }
        el.search.focus();
        el.search.select();
      }
    });
    K.define("search.select", {
      run: function () {
        var count = chooseMatches();
        if (count) {
          window.netvizA11y.announce(count + " selected from the query", false);
        }
      },
      enabled: function () {
        return answer && !answer.error && answer.count ? true : "type a query that matches first";
      }
    });
    K.define("search.filter", {
      run: function () { setFiltering(!filtering); }
    });
    K.define("search.clear", {
      run: function () {
        if (!clear()) { window.netvizA11y.announce("the search box is already empty", false); }
      },
      enabled: function () { return text() || filtering ? true : "the search box is empty"; }
    });

    /* The palette, given a query.
     *
     * A live provider: its entries are a function of what has been typed, so it
     * is consulted per keystroke and its answer is fetched rather than
     * computed. Anything that parses offers three things — select the matches,
     * put it in the box, filter the drawing by it — and anything that does not
     * offers the parse error, with the caret, as an entry that says why. That
     * last one is the point: a palette that silently showed no results for a
     * mistyped query would be the worst possible teacher of a new language.
     */
    K.provide("query", function (needle, redraw) {
      if (!looksLikeQuery(needle)) { return []; }
      var known = cached(needle, redraw);
      if (!known) {
        return [{
          id: "pending",
          title: "…",
          detail: "asking the server about “" + needle + "”",
          group: "query",
          run: function () {}
        }];
      }
      if (known.error) {
        return [{
          id: "error",
          title: firstLine(known.error),
          detail: "see docs/query.md for the grammar",
          group: "query",
          why: "that is not a query yet",
          run: function () {}
        }];
      }
      return [
        {
          id: "select",
          title: "Select " + known.count + (known.count === 1 ? " element" : " elements"),
          detail: needle,
          group: "query",
          run: function () { el.search.value = needle; asked = needle; answer = known;
                             paint(); announce(); chooseMatches(); }
        },
        {
          id: "search",
          title: "Search for it, and keep it in the box",
          detail: needle,
          group: "query",
          run: function () { el.search.value = needle; run(false); el.search.focus(); }
        },
        {
          id: "filter",
          title: "Draw only what it selects",
          detail: needle,
          group: "query",
          run: function () { el.search.value = needle; run(false); setFiltering(true); }
        }
      ];
    }, { live: true });
  }

  /** Does this look like somebody reaching for the query language?
   *
   * Deliberately conservative. Typing `save` into the palette must not fire a
   * query request per keystroke and must not push three query rows above the
   * Save command — so a bare word is *not* a query here, even though it is one
   * at the search box, where there is nothing else it could be.
   */
  function looksLikeQuery(needle) {
    if (!needle || needle.length < 3) { return false; }
    return /[=~<>[]|\b(and|or|not|has|in|under|within|neighbou?rs|reachable)\b/.test(needle);
  }

  /** The answer for `needle`, fetching it once if it is not held yet.
   *
   * One entry, not a map: the palette asks about the string in the box and the
   * string in the box changes on every keystroke, so a cache of every prefix
   * ever typed would be a leak with a hit rate of nothing.
   */
  var held = { needle: null, result: null, asking: false };

  function cached(needle, redraw) {
    if (held.needle === needle) { return held.result; }
    if (held.asking !== needle) {
      held.asking = needle;
      fetch("/api/query?q=" + encodeURIComponent(needle) +
            "&layer=" + encodeURIComponent(host.layer()), { cache: "no-store" })
        .then(function (response) { return response.json(); })
        .then(function (result) {
          if (held.asking !== needle) { return; }
          held = { needle: needle, result: result, asking: false };
          if (redraw) { redraw(); }
        })
        .catch(function () { held.asking = false; });
    }
    return null;
  }

  return {
    attach: attach,
    defineCommands: defineCommands,
    paint: paint,
    clear: clear,
    text: text,
    selector: selector,
    filtering: function () { return filtering; },
    matches: function () { return answer && !answer.error ? (answer.addresses || []) : []; }
  };
})();
