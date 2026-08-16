/* The keyboard, the command palette and the shortcut reference.
 *
 * One table drives all three, and the table is not in this file: it is
 * netviz.web.bindings, fetched from /api/bindings at boot. That is the point
 * of the arrangement -- the same tuple that says `Ctrl-K` opens the palette is
 * what `tools/gen_docs.py` writes into docs/commands/web.md, so the documented
 * shortcut and the working one cannot come apart. What this file adds is the
 * other half of each binding: the *handler*, registered by id.
 *
 * A binding with no handler and a handler with no binding are both bugs, and
 * tests/test_web.py fails on either, which is the only check the Python side
 * cannot make for itself.
 *
 * Three surfaces:
 *
 *   the keyboard   Every keystroke is matched against the table. A chord
 *                  without Ctrl or Alt is ignored while the caret is in a text
 *                  field -- `n` creates a device on the canvas and types an `n`
 *                  in the YAML pane -- and a `canvas` binding only fires while
 *                  the diagram has focus.
 *   the palette    Ctrl-K. Every command, plus everything the page can *go to*:
 *                  element addresses and file paths, from providers the rest of
 *                  the page registers. Fuzzy-matched, and every row shows its
 *                  own shortcut, so the palette teaches the keyboard.
 *   the reference  `?`. The table, grouped by section, rendered from what was
 *                  actually registered rather than from a second list.
 *
 * There is also a prompt: the small modal an edit gesture asks its arguments
 * with. It is here rather than in app.js because it is a focus trap and an
 * Escape handler, which is this file's business.
 *
 * Dependency-free, like the rest of this page.
 */

var netvizKeys = (function () {
  "use strict";

  /** How many palette rows are built at once. A thousand-element inventory has
   *  a thousand "go to" entries and nobody reads past the first screen. */
  var MAX_RESULTS = 60;

  /** The table, as /api/bindings gave it. */
  var table = { sections: [], bindings: [], menus: [], kinds: [] };
  var byId = {};
  /** id -> { run, enabled } */
  var handlers = {};
  /** name -> function returning [{ id, title, detail, group, run, needs }] */
  var providers = {};
  var host = null;
  /** Open overlays, innermost last. Escape closes one at a time. */
  var stack = [];

  var palette = null;
  var reference = null;

  /* ----------------------------------------------------------- the chords */

  /** How this keystroke is spelled in the table.
   *
   * `Ctrl` stands for the platform's command modifier, so a Mac user's Meta
   * matches the same row -- the table says `Ctrl-S` once, not twice.
   *
   * Shift is a modifier only where it did not already change the character:
   * `Shift-l` is a chord and `?` is a key, even though the second is typed with
   * a shift held.
   */
  function chordOf(event) {
    var raw = event.key;
    if (!raw || raw === "Shift" || raw === "Control" || raw === "Alt" || raw === "Meta") {
      return "";
    }
    var named = raw.length > 1;
    var key = named ? raw : raw.toLowerCase();
    var parts = [];
    if (event.ctrlKey || event.metaKey) { parts.push("Ctrl"); }
    if (event.altKey) { parts.push("Alt"); }
    if (event.shiftKey && (named || /^[a-z0-9]$/.test(key))) { parts.push("Shift"); }
    if (key === " ") { key = "Space"; }
    else if (key === "+") { key = "Plus"; }
    else if (key === "-") { key = "Minus"; }
    parts.push(key);
    return parts.join("-");
  }

  /** The table's spelling, lower-cased where the case carries nothing.
   *  `Alt-I` and `Alt-i` are one chord; the table may write whichever reads. */
  function normalise(chord) {
    var parts = String(chord).split("-");
    var key = parts.pop();
    if (key.length === 1) { key = key.toLowerCase(); }
    return parts.concat([key]).join("-");
  }

  /** A chord as it should be printed. */
  function pretty(chord) {
    var mac = /Mac|iPhone|iPad/.test(window.navigator.platform || "");
    return String(chord)
      .split("-")
      .map(function (part) {
        if (part === "Ctrl") { return mac ? "⌘" : "Ctrl"; }
        if (part === "Plus") { return "+"; }
        if (part === "Minus") { return "−"; }
        if (part === "Space") { return "Space"; }
        return part.length === 1 ? part.toUpperCase() : part;
      })
      .join(mac ? "" : "-");
  }

  /** The chord to advertise for a command, or "" when it has none. */
  function chordFor(id) {
    var binding = byId[id];
    return binding && binding.keys.length ? pretty(binding.keys[0]) : "";
  }

  /* ------------------------------------------------------------ the table */

  /** Adopt what /api/bindings answered. */
  function load(payload) {
    table = {
      sections: (payload && payload.sections) || [],
      bindings: (payload && payload.bindings) || [],
      menus: (payload && payload.menus) || [],
      kinds: (payload && payload.kinds) || []
    };
    byId = {};
    table.bindings.forEach(function (binding) {
      byId[binding.id] = binding;
      binding.chords = binding.keys.map(normalise);
    });
    return table.bindings.length;
  }

  /** Say what a command *does*. Called once per command, at boot.
   *
   * `enabled` returns true, or a string saying why not -- which the palette
   * shows against the greyed row and an attempted keystroke announces. "Nothing
   * happened" is the one answer an interface must never give.
   */
  function define(id, spec) {
    handlers[id] = { run: spec.run, enabled: spec.enabled || null };
  }

  /** Register a source of palette entries: elements, files, layers.
   *
   * `options.live` marks a provider whose entries depend on what has been
   * *typed*, not only on what the page holds — a selector query is the case it
   * exists for. A live provider is called with the current needle on every
   * refresh and with a `redraw` callback it may call later, and its entries
   * skip the scorer: it has already decided what matches, and running a
   * subsequence match over "42 elements match this query" would rank it by the
   * wrong string.
   */
  function provide(name, fn, options) {
    providers[name] = fn;
    fn.live = !!(options && options.live);
  }

  /** Why `id` cannot run now, or "" when it can. */
  function refusal(id) {
    var binding = byId[id];
    var handler = handlers[id];
    if (!handler) { return "not available in this build"; }
    if (binding && host) {
      if (binding.needs === "session" && !host.isSession()) {
        return "open a folder with 'netviz web DIR' for this";
      }
      if (binding.needs === "write" && !host.isWritable()) {
        // Two ways to be unable to write, and they are fixed differently: a
        // scratchpad has no files at all, so "restart it with --write" would
        // send somebody looking for a flag that would not help them.
        return host.isSession()
          ? "this session is read-only; restart it with --write"
          : "open a folder with 'netviz web DIR --write' for this";
      }
      if (binding.needs === "focus" && !host.hasFocus()) {
        return "focus an element in the diagram first";
      }
    }
    if (handler.enabled) {
      var verdict = handler.enabled();
      if (verdict !== true) { return typeof verdict === "string" ? verdict : "not available now"; }
    }
    return "";
  }

  /** Run a command by id, saying why not when it cannot. */
  function run(id, context) {
    var why = refusal(id);
    if (why) {
      if (host) { host.refuse(why); }
      return false;
    }
    handlers[id].run(context || {});
    return true;
  }

  /* --------------------------------------------------------- the keyboard */

  function attach(bridge) {
    host = bridge;
    document.addEventListener("keydown", onKeyDown, true);
    return true;
  }

  function onKeyDown(event) {
    if (event.defaultPrevented) { return; }
    var chord = chordOf(event);
    if (!chord) { return; }
    // An overlay owns the keyboard while it is up: its own Escape, its own
    // Enter, its own arrows. Only the palette's toggle reaches past it.
    if (stack.length) {
      var top = stack[stack.length - 1];
      if (top.onKey(chord, event) !== false) { return; }
    }
    var inText = isTextEntry(document.activeElement);
    var inCanvas = host ? host.inCanvas(document.activeElement) : false;
    for (var i = 0; i < table.bindings.length; i++) {
      var binding = table.bindings[i];
      if (binding.chords.indexOf(chord) === -1) { continue; }
      if (binding.where === "canvas" && !inCanvas) { continue; }
      // A bare letter is a character before it is a command.
      if (inText && !event.ctrlKey && !event.metaKey && !event.altKey && chord !== "Escape") {
        continue;
      }
      event.preventDefault();
      run(binding.id, { chord: chord, key: event.key, event: event });
      return;
    }
  }

  function isTextEntry(node) {
    if (!node) { return false; }
    var name = (node.tagName || "").toLowerCase();
    if (name === "textarea" || name === "select") { return true; }
    if (name === "input") { return !/^(checkbox|radio|button|submit)$/i.test(node.type || "text"); }
    return node.isContentEditable === true;
  }

  /* ----------------------------------------------------------- overlays */

  /** Put an overlay up, remembering where the focus came from.
   *
   * Everything modal on this page goes through here, so there is one answer to
   * "what does Escape do", one focus trap, and one guarantee that focus comes
   * back to the control that opened it -- which is the difference between a
   * dialog and a dead end.
   */
  function openOverlay(node, spec) {
    var entry = {
      node: node,
      onKey: spec.onKey,
      close: spec.close,
      restore: document.activeElement
    };
    stack.push(entry);
    document.body.appendChild(node);
    node.addEventListener("keydown", function (event) {
      if (event.key !== "Tab") { return; }
      trap(node, event);
    });
    if (spec.focus) { spec.focus(); }
    return entry;
  }

  function closeOverlay(entry) {
    var at = stack.indexOf(entry);
    if (at === -1) { return; }
    stack.splice(at, 1);
    if (entry.node.parentNode) { entry.node.parentNode.removeChild(entry.node); }
    if (entry.restore && entry.restore.focus) {
      try { entry.restore.focus(); } catch (error) { /* it went away; never mind */ }
    }
  }

  /** Keep Tab inside the overlay. Escape is the way out, and it always works. */
  function trap(node, event) {
    var focusable = node.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
      'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    );
    if (!focusable.length) { return; }
    var first = focusable[0];
    var last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  /** Close the innermost overlay. Returns false when there was none. */
  function dismiss() {
    if (!stack.length) { return false; }
    stack[stack.length - 1].close();
    return true;
  }

  function overlayOpen() {
    return stack.length > 0;
  }

  /* ----------------------------------------------------------- the palette */

  /** Everything the palette can offer, commands first.
   *
   * `scope` narrows it to one provider, which is what Ctrl-G ("go to element")
   * and Ctrl-O ("open file") are: the same widget with a smaller universe.
   */
  function entries(scope, needle, redraw) {
    var found = [];
    if (!scope) {
      table.bindings.forEach(function (binding) {
        if (!handlers[binding.id]) { return; }
        found.push({
          key: binding.id,
          title: binding.title,
          detail: binding.detail,
          group: binding.section,
          chord: binding.keys.length ? pretty(binding.keys[0]) : "",
          why: refusal(binding.id),
          run: function () { run(binding.id, { from: "palette" }); }
        });
      });
    }
    Object.keys(providers).forEach(function (name) {
      if (scope && scope !== name) { return; }
      var provider = providers[name];
      var offered = (provider.live ? provider(needle || "", redraw) : provider()) || [];
      offered.forEach(function (entry) {
        found.push({
          key: name + ":" + entry.id,
          title: entry.title,
          detail: entry.detail || "",
          group: entry.group || name,
          chord: entry.chord || "",
          why: entry.why || "",
          run: entry.run,
          always: !!provider.live
        });
      });
    });
    return found;
  }

  /** Subsequence match, scored so that a prefix beats a scatter.
   *
   * Deliberately simple: the haystacks here are addresses and short titles, and
   * a scoring function nobody can predict is worse than one that always puts
   * `sw-home` first when you type `swh`.
   */
  function score(needle, entry) {
    if (!needle) { return 1; }
    var hay = (entry.title + " " + entry.detail + " " + entry.group).toLowerCase();
    var want = needle.toLowerCase();
    if (hay.indexOf(want) !== -1) {
      // A contiguous hit, best when it starts the title.
      return 1000 - hay.indexOf(want) - (entry.title.length / 100);
    }
    var at = 0;
    var runs = 0;
    var last = -2;
    for (var i = 0; i < want.length; i++) {
      if (want.charAt(i) === " ") { continue; }
      at = hay.indexOf(want.charAt(i), at);
      if (at === -1) { return 0; }
      if (at === last + 1) { runs += 1; }
      last = at;
      at += 1;
    }
    return 1 + runs;
  }

  function openPalette(scope, seed) {
    if (palette) { closeOverlay(palette.entry); palette = null; }
    var node = document.createElement("div");
    node.className = "overlay";

    var box = document.createElement("div");
    box.className = "palette";
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.setAttribute("aria-label", scope === "elements"
      ? "Go to element"
      : (scope === "files" ? "Open file" : "Command palette"));

    var label = document.createElement("label");
    label.className = "visually-hidden";
    label.setAttribute("for", "palette-input");
    label.textContent = "Type to search commands, elements and files";

    var input = document.createElement("input");
    input.type = "text";
    input.id = "palette-input";
    input.className = "palette-input";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.placeholder = scope === "elements"
      ? "go to element…"
      : (scope === "files" ? "open file…" : "type a command, an element or a file…");
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-expanded", "true");
    input.setAttribute("aria-controls", "palette-list");
    input.setAttribute("aria-autocomplete", "list");

    var list = document.createElement("ul");
    list.className = "palette-list";
    list.id = "palette-list";
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", "Results");

    var status = document.createElement("p");
    status.className = "palette-status";
    status.setAttribute("role", "status");

    box.appendChild(label);
    box.appendChild(input);
    box.appendChild(list);
    box.appendChild(status);
    node.appendChild(box);

    var shown = [];
    var cursor = 0;

    function refresh() {
      var needle = input.value.trim();
      // Recomputed per keystroke rather than once at open, because a live
      // provider's entries *are* a function of the needle. The static
      // providers rebuild a few hundred cheap objects, which is nothing beside
      // the scoring pass that follows.
      var universe = entries(scope, needle, function () {
        if (palette && palette.entry === entry) { refresh(); }
      });
      shown = universe
        .map(function (one) {
          // A live provider has already decided; scoring it again would rank a
          // sentence about the query above the query's own result.
          return { entry: one, score: one.always ? Infinity : score(needle, one) };
        })
        .filter(function (hit) { return hit.score > 0; })
        .sort(function (a, b) { return b.score - a.score; })
        .slice(0, MAX_RESULTS)
        .map(function (hit) { return hit.entry; });
      cursor = 0;
      draw();
    }

    function draw() {
      list.replaceChildren();
      shown.forEach(function (entry, index) {
        var item = document.createElement("li");
        item.className = "palette-item" + (entry.why ? " unavailable" : "") +
          (index === cursor ? " current" : "");
        item.id = "palette-item-" + index;
        item.setAttribute("role", "option");
        item.setAttribute("aria-selected", index === cursor ? "true" : "false");
        if (entry.why) { item.setAttribute("aria-disabled", "true"); }

        var main = document.createElement("span");
        main.className = "palette-title";
        main.textContent = entry.title;
        var group = document.createElement("span");
        group.className = "palette-group";
        group.textContent = entry.group;
        var chord = document.createElement("span");
        chord.className = "palette-chord";
        chord.textContent = entry.chord;
        var why = document.createElement("span");
        why.className = "palette-why";
        why.textContent = entry.why;

        item.appendChild(main);
        item.appendChild(group);
        item.appendChild(entry.why ? why : chord);
        item.addEventListener("mousedown", function (event) {
          event.preventDefault();
          cursor = index;
          choose();
        });
        list.appendChild(item);
      });
      input.setAttribute("aria-activedescendant", shown.length ? "palette-item-" + cursor : "");
      status.textContent = shown.length
        ? shown.length + " result" + (shown.length === 1 ? "" : "s") +
          (shown[cursor] ? ": " + shown[cursor].title : "")
        : "no command or element matches";
    }

    function step(delta) {
      if (!shown.length) { return; }
      cursor = (cursor + delta + shown.length) % shown.length;
      draw();
      var item = document.getElementById("palette-item-" + cursor);
      if (item && item.scrollIntoView) { item.scrollIntoView({ block: "nearest" }); }
    }

    function choose() {
      var entry = shown[cursor];
      if (!entry) { return; }
      if (entry.why) {
        if (host) { host.refuse(entry.why); }
        return;
      }
      close();
      entry.run();
    }

    function close() {
      if (!palette) { return; }
      var open = palette;
      palette = null;
      closeOverlay(open.entry);
    }

    input.addEventListener("input", refresh);

    var entry = openOverlay(node, {
      focus: function () { input.focus(); },
      close: close,
      onKey: function (chord, event) {
        if (chord === "Escape") { event.preventDefault(); close(); return true; }
        if (chord === "ArrowDown") { event.preventDefault(); step(1); return true; }
        if (chord === "ArrowUp") { event.preventDefault(); step(-1); return true; }
        if (chord === "Enter") { event.preventDefault(); choose(); return true; }
        if (chord === "Home" || chord === "End") { return false; }
        return true;   // everything else is typing
      }
    });
    palette = { entry: entry, close: close };
    input.value = seed || "";
    refresh();
    return palette;
  }

  /* --------------------------------------------------------- the reference */

  /** The shortcut sheet, rendered from the table that registered the bindings.
   *
   * Not a hand-written list of what the keys are believed to be: a binding
   * added without a row here is impossible, because there is no "here" to add a
   * row to.
   */
  function openReference() {
    if (reference) { return reference; }
    var node = document.createElement("div");
    node.className = "overlay";

    var box = document.createElement("div");
    box.className = "sheet";
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.setAttribute("aria-labelledby", "sheet-heading");
    box.tabIndex = -1;

    var heading = document.createElement("h2");
    heading.id = "sheet-heading";
    heading.textContent = "Keyboard shortcuts";
    box.appendChild(heading);

    var lede = document.createElement("p");
    lede.className = "sheet-lede";
    lede.textContent =
      "Every command this page has. The same table is documented in " +
      "docs/commands/web.md, generated from it.";
    box.appendChild(lede);

    table.sections.forEach(function (section) {
      var rows = table.bindings.filter(function (binding) {
        return binding.section === section;
      });
      if (!rows.length) { return; }
      var group = document.createElement("section");
      var title = document.createElement("h3");
      title.textContent = section;
      group.appendChild(title);
      var dl = document.createElement("dl");
      rows.forEach(function (binding) {
        var dt = document.createElement("dt");
        if (binding.keys.length) {
          binding.keys.forEach(function (key, index) {
            if (index) { dt.appendChild(document.createTextNode(" or ")); }
            var kbd = document.createElement("kbd");
            kbd.textContent = pretty(key);
            dt.appendChild(kbd);
          });
        } else {
          var only = document.createElement("span");
          only.className = "palette-only";
          only.textContent = "palette only";
          dt.appendChild(only);
        }
        var dd = document.createElement("dd");
        var strong = document.createElement("strong");
        strong.textContent = binding.title;
        dd.appendChild(strong);
        dd.appendChild(document.createTextNode(" — " + binding.detail));
        var why = refusal(binding.id);
        if (why) {
          var note = document.createElement("span");
          note.className = "sheet-why";
          note.textContent = " (" + why + ")";
          dd.appendChild(note);
        }
        dl.appendChild(dt);
        dl.appendChild(dd);
      });
      group.appendChild(dl);
      box.appendChild(group);
    });

    var close = document.createElement("button");
    close.type = "button";
    close.className = "sheet-close";
    close.textContent = "Close";
    close.addEventListener("click", function () { shut(); });
    box.appendChild(close);
    node.appendChild(box);

    function shut() {
      if (!reference) { return; }
      var open = reference;
      reference = null;
      closeOverlay(open.entry);
    }

    var entry = openOverlay(node, {
      focus: function () { box.focus(); },
      close: shut,
      onKey: function (chord, event) {
        if (chord === "Escape" || chord === "?") { event.preventDefault(); shut(); return true; }
        return chord === "Tab" || chord === "Shift-Tab" ? false : true;
      }
    });
    reference = { entry: entry, close: shut };
    return reference;
  }

  /* ------------------------------------------------------------- prompting */

  /** Ask for the arguments of an edit gesture, without a pointer.
   *
   * `fields` is a list of `{ name, label, type, value, options, list, hint }`.
   * `onSubmit` receives an object keyed by field name; returning nothing closes
   * the prompt, returning a string keeps it open and shows that as the error --
   * which is what a refused operation does, so the user does not lose what they
   * typed.
   */
  function prompt(spec) {
    var node = document.createElement("div");
    node.className = "overlay";
    var form = document.createElement("form");
    form.className = "prompt";
    form.setAttribute("role", "dialog");
    form.setAttribute("aria-modal", "true");
    form.setAttribute("aria-labelledby", "prompt-heading");

    var heading = document.createElement("h2");
    heading.id = "prompt-heading";
    heading.textContent = spec.title;
    form.appendChild(heading);
    if (spec.detail) {
      var detail = document.createElement("p");
      detail.className = "prompt-detail";
      detail.textContent = spec.detail;
      form.appendChild(detail);
    }

    var inputs = {};
    (spec.fields || []).forEach(function (field, index) {
      var id = "prompt-field-" + index;
      var wrap = document.createElement("p");
      wrap.className = "prompt-field";
      var label = document.createElement("label");
      label.setAttribute("for", id);
      label.textContent = field.label;
      wrap.appendChild(label);

      var control;
      if (field.type === "select") {
        control = document.createElement("select");
        (field.options || []).forEach(function (option) {
          var node_ = document.createElement("option");
          node_.value = option.value;
          node_.textContent = option.label;
          if (option.value === field.value) { node_.selected = true; }
          control.appendChild(node_);
        });
      } else {
        control = document.createElement("input");
        control.type = "text";
        control.autocomplete = "off";
        control.spellcheck = false;
        control.value = field.value || "";
        if (field.list && field.list.length) {
          var datalist = document.createElement("datalist");
          datalist.id = id + "-list";
          field.list.slice(0, 500).forEach(function (value) {
            var option = document.createElement("option");
            option.value = value;
            datalist.appendChild(option);
          });
          control.setAttribute("list", datalist.id);
          wrap.appendChild(datalist);
        }
      }
      control.id = id;
      if (field.hint) { control.setAttribute("aria-describedby", id + "-hint"); }
      wrap.appendChild(control);
      if (field.hint) {
        var hint = document.createElement("span");
        hint.id = id + "-hint";
        hint.className = "prompt-hint";
        hint.textContent = field.hint;
        wrap.appendChild(hint);
      }
      inputs[field.name] = control;
      form.appendChild(wrap);
    });

    var error = document.createElement("p");
    error.className = "prompt-error";
    error.setAttribute("role", "alert");
    form.appendChild(error);

    var actions = document.createElement("p");
    actions.className = "prompt-actions";
    var submit = document.createElement("button");
    submit.type = "submit";
    submit.textContent = spec.confirm || "Apply";
    var cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "ghost";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", function () { shut(); });
    actions.appendChild(submit);
    actions.appendChild(cancel);
    form.appendChild(actions);
    node.appendChild(form);

    function values() {
      var out = {};
      Object.keys(inputs).forEach(function (name) { out[name] = inputs[name].value.trim(); });
      return out;
    }

    function shut() {
      closeOverlay(entry);
      if (spec.onCancel) { spec.onCancel(); }
    }

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var complaint = spec.onSubmit(values(), {
        fail: function (text) { error.textContent = text; },
        close: function () { closeOverlay(entry); }
      });
      if (typeof complaint === "string" && complaint) { error.textContent = complaint; return; }
      if (complaint !== false) { closeOverlay(entry); }
    });

    var entry = openOverlay(node, {
      focus: function () {
        var names = Object.keys(inputs);
        (names.length ? inputs[names[0]] : submit).focus();
      },
      close: shut,
      onKey: function (chord, event) {
        if (chord === "Escape") { event.preventDefault(); shut(); return true; }
        return chord === "Tab" || chord === "Shift-Tab" ? false : true;
      }
    });
    return { close: shut };
  }

  return {
    load: load,
    define: define,
    provide: provide,
    attach: attach,
    run: run,
    refusal: refusal,
    chordFor: chordFor,
    palette: openPalette,
    reference: openReference,
    prompt: prompt,
    dismiss: dismiss,
    /* The modal machinery itself, for a panel this file does not own: the
     * guided tour's card is one focus trap and one Escape handler like every
     * other overlay, and there is to be only one implementation of both. */
    overlay: openOverlay,
    closeOverlay: closeOverlay,
    overlayOpen: overlayOpen,
    /** The element kinds this build has, for the create gesture's menu. */
    kinds: function () { return table.kinds.slice(); },
    /** What right-clicking each kind of shape offers, for menu.js. Layout only:
     *  every row names a binding above, and is drawn from the title, the chord
     *  and the refusal this file already answers for the palette. */
    menus: function () { return table.menus.slice(); },
    /** For tests and for the reference: what was actually registered. */
    bindings: function () { return table.bindings.slice(); },
    handled: function () { return Object.keys(handlers).sort(); }
  };
})();
