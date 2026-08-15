/* The context menu: what right-clicking the diagram offers.
 *
 * The page already had every command, and no way to find one without knowing
 * that Ctrl-K exists. This file is the other door -- the one somebody who has
 * used draw.io tries first -- and it is deliberately *only* a door: every row
 * runs a command through netgraphKeys.run, under the id netgraph.web.bindings
 * declares, so there is no gesture here that the keyboard and the palette do not
 * also have. A menu with its own implementation of "delete" would be a second
 * write path, and the whole editor has one.
 *
 * What it adds, then, is three things a palette cannot:
 *
 *   it knows what you pointed at   Right-clicking a shape focuses it first, so
 *                                  the commands that act on "the focused
 *                                  element" act on the one under the cursor.
 *                                  The heading names it, because a menu that
 *                                  does not say what it will delete is a menu
 *                                  nobody should click Delete in.
 *   it is shorter than the palette The table in bindings.py picks a handful per
 *                                  target. Everything else is one row away
 *                                  ("All commands...") rather than buried here.
 *   it teaches the keyboard        Every row prints its own chord, the same way
 *                                  palette rows do. Use the menu for a week and
 *                                  you stop needing it.
 *
 * A row that cannot run now is drawn greyed with the reason, never hidden --
 * "why is Delete grey" is a better question for the interface to answer than
 * "where did Delete go", and it is the same refusal string the palette shows.
 *
 * It is an overlay in keys.js's stack, so Escape leaves it, Tab stays inside it
 * and the focus goes back where it came from. Dependency-free, like the rest of
 * this page.
 */
window.netgraphMenu = (function () {
  "use strict";

  /** What app.js lends: the canvas, the hit test, and how to refuse. */
  var host = null;
  /** The open menu, or null. */
  var open = null;

  function attach(bridge) {
    host = bridge;
    return true;
  }

  function isOpen() { return !!open; }

  /* ------------------------------------------------------------- opening */

  /** Open on whatever a pointer event landed on.
   *
   * Answers false when there is nothing to show, which is what tells app.js to
   * leave the browser's own menu alone -- suppressing it and then offering
   * nothing is the one outcome worse than either.
   */
  function openAt(event) {
    if (!host) { return false; }
    return show(host.recordAt(event.target), { x: event.clientX, y: event.clientY });
  }

  /** Open on whatever the keyboard has focused, anchored under its shape.
   *
   * The menu key and Shift-F10 are what a screen-reader user presses, and a
   * context menu reachable only by right-click is a set of commands they do not
   * have. With nothing focused this is the canvas menu, which is the honest
   * reading of "there is no element here".
   */
  function openFocused() {
    if (!host) { return false; }
    var here = window.netgraphA11y.focused();
    var box = here ? host.boxOf(here.element) : null;
    var canvas = host.el.canvas.getBoundingClientRect();
    // A shape scrolled out of the drawn window has a box, and it is the empty
    // one. Anchoring on that would put the menu in the corner of the screen.
    if (box && !box.width && !box.height) { box = null; }
    return show(here ? { record: here.record } : null, box
      ? { x: box.left + Math.min(box.width / 2, 80), y: box.bottom }
      : { x: canvas.left + canvas.width / 2, y: canvas.top + canvas.height / 3 });
  }

  /** Draw the menu for one target at one point. */
  function show(hit, at) {
    close();
    var record = hit ? hit.record : null;
    var target = record ? (record.type === "edge" ? "link" : "node") : "canvas";
    var groups = rowsFor(target);
    if (!groups.length) { return false; }

    // Point the commands at what was clicked before any of them is run. They
    // all default to "the focused element", so this is the whole of how the
    // pointer and the keyboard end up meaning the same thing.
    if (record) {
      var element = record.element || (hit.group ? hit.group.id : "");
      if (element) { window.netgraphA11y.focus(element, { quiet: true, scroll: false }); }
    }

    var layer = document.createElement("div");
    layer.className = "menu-layer";
    layer.addEventListener("mousedown", function (event) {
      if (event.target === layer) { close(); }
    });
    // A second right-click outside re-aims rather than stacking a menu on a
    // menu; app.js sees the event once the layer has gone.
    layer.addEventListener("contextmenu", function (event) {
      if (event.target !== layer) { return; }
      event.preventDefault();
      close();
    });

    var root = panel(groups, caption(record, target), target);
    layer.appendChild(root);

    var entry = window.netgraphKeys.overlay(layer, {
      focus: function () { step(root, -1, 1); },
      close: close,
      onKey: onKey
    });
    open = { entry: entry, layer: layer, root: root, sub: null };
    place(root, at);
    return true;
  }

  function close() {
    if (!open) { return; }
    var going = open;
    open = null;
    window.netgraphKeys.closeOverlay(going.entry);
  }

  /* ------------------------------------------------------------- the rows */

  /** The declared layout for one target, resolved against the binding table.
   *
   * The table names commands; a row needs a title, a chord and — when it cannot
   * run — the reason. All three come from keys.js, which is where they are
   * already answered for the palette.
   */
  function rowsFor(target) {
    var K = window.netgraphKeys;
    var declared = null;
    K.menus().forEach(function (menu) { if (menu.target === target) { declared = menu; } });
    if (!declared) { return []; }
    var byId = {};
    K.bindings().forEach(function (binding) { byId[binding.id] = binding; });
    return declared.groups.map(function (group) {
      return group.map(function (item) {
        var binding = byId[item.binding] || { title: item.binding, keys: [] };
        return {
          id: item.binding,
          title: item.label || binding.title,
          chord: K.chordFor(item.binding),
          why: K.refusal(item.binding),
          submenu: item.submenu
        };
      });
    }).filter(function (group) { return group.length > 0; });
  }

  /** What the menu says it is about to act on.
   *
   * The element's address, because that is what the commands take and what the
   * file list keys documents by — a heading that said "switch" would leave two
   * switches indistinguishable at the moment it matters most.
   */
  function caption(record, target) {
    if (!record) { return "the diagram"; }
    var address = String(record.id || "");
    if (!address) { return target === "link" ? "this link" : "this element"; }
    return address;
  }

  /** How many panels have been drawn, so each heading has an id to be named by. */
  var seq = 0;

  /** A panel: the heading, then the rows.
   *
   * The heading is *outside* the list rather than a presentational row inside
   * it, because a `role="menu"` owns what its children mean and a caption is not
   * a menu item. It labels the menu instead, which is the same fact said in the
   * place that makes it useful to a screen reader.
   */
  function panel(groups, label, target) {
    var box = document.createElement("div");
    box.className = "menu";

    seq += 1;
    var head = document.createElement("p");
    head.className = "menu-head";
    head.id = "menu-head-" + seq;
    head.textContent = label;
    if (target) { head.setAttribute("data-target", target); }
    box.appendChild(head);

    var ul = document.createElement("ul");
    ul.className = "menu-list";
    ul.setAttribute("role", "menu");
    ul.setAttribute("aria-labelledby", head.id);
    groups.forEach(function (group, index) {
      if (index) {
        var rule = document.createElement("li");
        rule.className = "menu-sep";
        rule.setAttribute("role", "separator");
        ul.appendChild(rule);
      }
      group.forEach(function (spec) { ul.appendChild(row(spec)); });
    });
    box.appendChild(ul);
    return box;
  }

  function row(spec) {
    var li = document.createElement("li");
    li.setAttribute("role", "none");

    var button = document.createElement("button");
    button.type = "button";
    button.className = "menu-item" + (spec.why ? " unavailable" : "");
    button.setAttribute("role", "menuitem");
    button.setAttribute("data-command", spec.id);
    if (spec.context && spec.context.kind) { button.setAttribute("data-kind", spec.context.kind); }
    button.tabIndex = -1;
    if (spec.why) {
      button.setAttribute("aria-disabled", "true");
      button.title = spec.why;
    }

    var title = document.createElement("span");
    title.className = "menu-title";
    title.textContent = spec.title;
    button.appendChild(title);

    // The reason wins over both the arrow and the chord: a row that cannot run
    // has one thing left to say, and a ▸ promising a submenu that will not open
    // is the one thing it must not say instead.
    var aside = document.createElement("span");
    if (spec.why) {
      aside.className = "menu-why";
      aside.textContent = spec.why;
    } else if (spec.submenu) {
      aside.className = "menu-more";
      aside.textContent = "▸";
      button.setAttribute("aria-haspopup", "menu");
      button.setAttribute("aria-expanded", "false");
    } else {
      aside.className = "menu-chord";
      aside.textContent = spec.chord;
    }
    button.appendChild(aside);

    button.addEventListener("click", function (event) {
      event.preventDefault();
      choose(button, spec);
    });
    // Moving along the menu moves the focus, so the arrow keys and the pointer
    // agree on where you are — and a submenu closes when the pointer leaves the
    // row that opened it for a different one. Not when the pointer reaches the
    // submenu's *own* rows, which is the whole journey it was opened for.
    button.addEventListener("mouseenter", function () {
      button.focus();
      if (spec.submenu || inSub(button)) { return; }
      shutSub();
    });
    li.appendChild(button);
    return li;
  }

  /** Run a row, or say why it cannot be run. */
  function choose(button, spec) {
    if (spec.why) {
      if (host && host.refuse) { host.refuse(spec.why); }
      return;
    }
    if (spec.submenu) { toggleSub(button, spec); return; }
    close();
    window.netgraphKeys.run(spec.id, spec.context || { from: "menu" });
  }

  /* --------------------------------------------------------- the submenu */

  /** The one submenu there is: an element kind per row.
   *
   * Kinds come from netgraph.models.KINDS by way of /api/bindings rather than
   * from a list in this file, for the reason every other list here is fetched:
   * a page with its own copy stops offering the thirteenth kind the day it is
   * added.
   */
  function toggleSub(button, spec) {
    if (open && open.sub && open.sub.owner === button) { shutSub(); button.focus(); return; }
    shutSub();
    var kinds = window.netgraphKeys.kinds();
    if (!kinds.length) { return; }
    // Every row is the same command with its kind already chosen. The prompt
    // still opens — a name is still wanted — but with one field answered and
    // the rest of the form saying what else it could have been.
    var ul = panel([kinds.map(function (kind) {
      return {
        id: spec.id,
        title: kind,
        chord: "",
        why: "",
        submenu: "",
        context: { from: "menu", kind: kind }
      };
    })], "New element", "");
    ul.classList.add("menu-sub");
    open.layer.appendChild(ul);
    open.sub = { node: ul, owner: button };
    button.setAttribute("aria-expanded", "true");
    beside(ul, button);
    step(ul, -1, 1);
  }

  function inSub(node) {
    return !!(open && open.sub && open.sub.node.contains(node));
  }

  function shutSub() {
    if (!open || !open.sub) { return; }
    open.sub.owner.setAttribute("aria-expanded", "false");
    if (open.sub.node.parentNode) { open.sub.node.parentNode.removeChild(open.sub.node); }
    open.sub = null;
  }

  /* -------------------------------------------------------- where it goes */

  /** Put the menu at the pointer, and inside the window.
   *
   * Measured after it is in the DOM rather than estimated: the rows are as wide
   * as their longest label and there is no arithmetic here that would get that
   * right. A menu whose last row is off the bottom of the screen is a menu
   * missing whichever command it was.
   */
  function place(node, at) {
    node.style.left = "0px";
    node.style.top = "0px";
    var box = node.getBoundingClientRect();
    node.style.left = clamp(at.x, box.width, window.innerWidth) + "px";
    node.style.top = clamp(at.y, box.height, window.innerHeight) + "px";
  }

  /** Put a submenu against its row, flipping to the other side when it would
   *  run off the edge. */
  function beside(node, button) {
    node.style.left = "0px";
    node.style.top = "0px";
    var box = node.getBoundingClientRect();
    var owner = button.getBoundingClientRect();
    var right = owner.right - 2;
    var x = right + box.width <= window.innerWidth - 4 ? right : owner.left - box.width + 2;
    node.style.left = clamp(x, box.width, window.innerWidth) + "px";
    node.style.top = clamp(owner.top - 4, box.height, window.innerHeight) + "px";
  }

  function clamp(want, size, limit) {
    return Math.max(4, Math.min(want, limit - size - 4));
  }

  /* -------------------------------------------------------- the keyboard */

  function active() {
    if (!open) { return null; }
    return open.sub ? open.sub.node : open.root;
  }

  function items(node) {
    return Array.prototype.slice.call(node.querySelectorAll(".menu-item"));
  }

  /** Move the focus `delta` rows on from `from`, wrapping. */
  function step(node, from, delta) {
    var list = items(node);
    if (!list.length) { return; }
    var at = ((from + delta) % list.length + list.length) % list.length;
    list[at].focus();
  }

  function onKey(chord, event) {
    var node = active();
    if (!node) { return true; }
    var list = items(node);
    var at = list.indexOf(document.activeElement);
    if (chord === "Escape") {
      event.preventDefault();
      if (open.sub) { var owner = open.sub.owner; shutSub(); owner.focus(); } else { close(); }
      return true;
    }
    if (chord === "ArrowDown" || chord === "ArrowUp") {
      event.preventDefault();
      step(node, at === -1 ? (chord === "ArrowDown" ? -1 : 0) : at, chord === "ArrowDown" ? 1 : -1);
      return true;
    }
    if (chord === "Home" || chord === "End") {
      event.preventDefault();
      step(node, chord === "Home" ? -1 : 0, chord === "Home" ? 1 : -1);
      return true;
    }
    if (chord === "ArrowRight" && !open.sub && at !== -1) {
      var button = list[at];
      if (button.getAttribute("aria-haspopup")) {
        event.preventDefault();
        button.click();
        return true;
      }
    }
    if (chord === "ArrowLeft" && open.sub) {
      event.preventDefault();
      var back = open.sub.owner;
      shutSub();
      back.focus();
      return true;
    }
    // Tab is the overlay's own trap; everything else is swallowed, because `n`
    // must not create an element while a menu is deciding what to do to one.
    return chord !== "Tab" && chord !== "Shift-Tab";
  }

  return {
    attach: attach,
    openAt: openAt,
    openFocused: openFocused,
    close: close,
    isOpen: isOpen
  };
})();
