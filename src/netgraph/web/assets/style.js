/* The style inspector: what the selection looks like, and where that came from.
 *
 * draw.io's core loop is "select a shape, change how it looks". This is that
 * loop, with one rule that draw.io does not have to keep: **appearance is
 * inventory data**. Every control here writes `spec.style.*` through the same
 * /api/ops the rest of the editor writes through, so the picture and the YAML
 * cannot disagree, a repaint appears in the changes drawer beside every other
 * edit, and Ctrl-Z undoes it.
 *
 * Three things make it more than a colour picker.
 *
 * **It shows the ladder, not just the answer.** Every row says which rung the
 * value came from -- this element, a theme rule, the icon set, or the built-in
 * palette (see netgraph/render/styles.py). A user looking at a navy switch needs
 * to know whether *they* made it navy before they can decide what to do about
 * it, and the SVG cannot tell them: by the time a colour reaches an attribute
 * every rung has collapsed into one hex literal.
 *
 * **Reset unsets.** "Reset to theme" removes `spec.style.fill`; it does not
 * write the inherited value back. Writing it would pin today's theme colour
 * into the document and quietly break the inheritance the button is named
 * after -- the element would stop following the theme at the exact moment
 * somebody asked it to.
 *
 * **A multi-selection is one batch.** Eleven switches painted at once is one
 * request, one changeset and one Ctrl-Z (§96). A field the eleven do not agree
 * about reads "mixed" and stays mixed until it is set, rather than showing the
 * first one's value and lying about the other ten.
 *
 * Everything it draws comes from the `styles` map the render response carries
 * (netgraph/web/preview.py): resolved values keyed by address, each with its
 * `from` provenance. Nothing is computed here -- a second implementation of the
 * ladder in JavaScript is exactly the drift this design is avoiding.
 *
 * Dependency-free, like the rest of this page.
 */

window.netgraphStyle = (function () {
  "use strict";

  /** What a row reads when the selection does not agree about the field. */
  var MIXED = "— mixed —";

  /** The nine fields, in the order §22 lists them: what the shape is, then how
   *  it is outlined, then the label, then the two overrides that replace the
   *  shape outright. `key` is how a document spells the field, which is also
   *  how it is spelled in the resolved payload and in the edit path. */
  var FIELDS = [
    { key: "fill", label: "fill", kind: "color" },
    { key: "stroke", label: "outline", kind: "color" },
    { key: "strokeWidth", label: "width", kind: "number", min: 0.5, max: 20, step: 0.5 },
    { key: "dash", label: "line", kind: "choice", options: ["solid", "dashed", "dotted", "bold"] },
    { key: "fontColor", label: "text", kind: "color" },
    { key: "fontSize", label: "text size", kind: "number", min: 6, max: 96, step: 1 },
    {
      key: "shape",
      label: "shape",
      kind: "choice",
      options: [
        "box", "rounded", "ellipse", "circle", "diamond", "hexagon", "triangle",
        "cylinder", "box3d", "folder", "note", "parallelogram", "trapezium", "plaintext"
      ]
    },
    { key: "icon", label: "icon", kind: "text", hint: "a picture in the --icons theme, or none" },
    { key: "opacity", label: "opacity", kind: "number", min: 0, max: 1, step: 0.05 }
  ];

  /** A link has no shape and no picture: those two rows are hidden for one, so
   *  the panel offers nothing that would validate and then draw nothing. */
  var NODE_ONLY = { shape: true, icon: true };

  var host = null;
  var el = null;
  /** The last `styles` payload a render carried: nodes and edges by address.
   *  `carried` is false when the drawing on screen came with none at all, which
   *  is a different thing from a drawing whose elements have no styles: the
   *  first has nothing to say and the second says "the palette chose it". */
  var resolved = { nodes: {}, edges: {}, theme: null, enabled: true, diff: false, carried: false };
  var open = false;

  /** Wire the panel to the page. `host` answers the four things this file
   *  cannot know: what is selected, whether the session may write, how to send
   *  a batch of operations, and how to say no. */
  function attach(options) {
    host = options;
    el = options.el;
    if (!el.style || !el.styleBody) { return; }
    if (el.styleToggle) {
      el.styleToggle.addEventListener("click", function () { show(!open); });
    }
    el.styleClose.addEventListener("click", function () { show(false); });
    el.styleBody.addEventListener("change", onChange);
    // A colour input fires `input` continuously while the picker is dragged and
    // `change` once at the end. Only the end is written: a batch per pixel of
    // travel would be a hundred undo entries for one decision.
    el.styleBody.addEventListener("click", onClick);
  }

  /** Take the resolved styles of the drawing that has just arrived. */
  function annotate(payload) {
    resolved = payload && typeof payload === "object"
      ? {
        nodes: payload.nodes || {},
        edges: payload.edges || {},
        theme: payload.theme || null,
        enabled: payload.enabled !== false,
        diff: !!payload.diff,
        carried: true
      }
      : { nodes: {}, edges: {}, theme: null, enabled: true, diff: false, carried: false };
    if (open) { paint(); }
  }

  /** The selection changed; redraw if anybody is looking. */
  function refresh() { if (open) { paint(); } }

  function isOpen() { return open; }

  function show(next) {
    if (!el || !el.style) { return; }
    open = !!next;
    el.style.hidden = !open;
    if (el.styleToggle) { el.styleToggle.setAttribute("aria-expanded", open ? "true" : "false"); }
    if (open) { paint(); }
  }

  /* ------------------------------------------------------------------ */
  /* Reading the selection                                              */
  /* ------------------------------------------------------------------ */

  /** The addresses the panel acts on, and the resolved style of each.
   *
   * An address the current drawing does not hold is dropped rather than
   * guessed at: the selection survives a view switch (select.js), and a cable
   * that is not drawn at layer 3 has no appearance at layer 3 to inspect.
   */
  function subjects() {
    var chosen = window.netgraphSelect.targets();
    var found = [];
    chosen.forEach(function (address) {
      var style = resolved.nodes[address] || resolved.edges[address];
      if (style) { found.push({ address: address, style: style, link: !resolved.nodes[address] }); }
    });
    return found;
  }

  /** What the whole selection says about one field.
   *
   * `agreed` is false when two of them resolve differently, which is the state
   * the row has to be able to show: a single value would be a claim about
   * elements it is not true of.
   */
  function consensus(chosen, key) {
    var value = null;
    var origin = null;
    var agreed = true;
    var declared = 0;
    chosen.forEach(function (one, index) {
      var here = one.style[key];
      var from = (one.style.from || {})[key] || null;
      if (from === "element") { declared += 1; }
      if (index === 0) { value = here; origin = from; return; }
      if (String(here) !== String(value)) { agreed = false; }
      if (from !== origin) { origin = agreed ? origin : null; }
    });
    return { value: value, origin: origin, agreed: agreed, declared: declared };
  }

  /** The provenance of a value, in the reader's words.
   *
   * `theme:blueprint#3` is what the resolver publishes -- the theme's name and
   * the index of the rule that won -- and "rule 4 of theme blueprint" is what
   * that means to somebody about to go and look at the file. One-based, because
   * the file they will open is.
   */
  function describe(origin, agreed) {
    if (!agreed) { return "several sources"; }
    if (!origin) { return "not set"; }
    if (origin === "element") { return "this element"; }
    if (origin === "icons") { return "the icon theme"; }
    if (origin === "default") { return "the built-in palette"; }
    var parts = String(origin).split(":");
    if (parts[0] !== "theme") { return String(origin); }
    var rule = String(parts[1] || "").split("#");
    return rule.length > 1
      ? "rule " + (Number(rule[1]) + 1) + " of theme " + rule[0]
      : "theme " + rule[0];
  }

  /* ------------------------------------------------------------------ */
  /* Drawing the panel                                                  */
  /* ------------------------------------------------------------------ */

  function paint() {
    if (!el || !el.styleBody) { return; }
    var picked = window.netgraphSelect.targets();
    var chosen = subjects();
    el.styleBody.replaceChildren();
    el.styleSubject.textContent = summary(picked, chosen);
    if (!picked.length) {
      el.styleBody.appendChild(note("select an element or a link to see how it is drawn"));
      return;
    }
    // Something *is* selected and none of it resolved. Two different causes,
    // and telling them apart is the difference between a panel that explains
    // itself and one that reads as broken: either this drawing published no
    // appearances at all, or the selection is not in the picture on screen.
    if (!chosen.length) {
      el.styleBody.appendChild(note(resolved.carried
        ? "the selection is not in this drawing, so it has no appearance here to "
          + "show. Switch to a view that draws it."
        : "this drawing arrived without the resolved appearances the panel reads. "
          + "Re-render, or reload the page."));
      return;
    }
    if (!resolved.enabled) {
      // --no-style. What is on screen is the palette's answer, not the
      // inventory's, so an edit here would change a document and not the
      // picture in front of the user -- which is the one thing a direct
      // manipulation editor must never do.
      el.styleBody.appendChild(note(
        "this diagram was drawn with --no-style, so what is shown is the built-in "
        + "palette and not what the inventory says. Restart without it to edit."
      ));
      return;
    }
    var links = chosen.filter(function (one) { return one.link; }).length;
    FIELDS.forEach(function (field) {
      // A field only some of the selection can carry is left out entirely
      // rather than shown disabled: a greyed row invites a click that can never
      // do anything.
      if (NODE_ONLY[field.key] && links) { return; }
      el.styleBody.appendChild(row(field, chosen));
    });
    if (resolved.theme) {
      el.styleBody.appendChild(note("theme in force: " + resolved.theme));
    }
    if (resolved.diff) {
      // The changes drawer paints its own colours over the drawing. What is
      // below is still what the documents say -- and still editable -- but the
      // shape on screen is green, red or amber for a reason that has nothing to
      // do with any of it, and a panel that did not say so would look wrong.
      el.styleBody.appendChild(note(
        "a diff is on screen: the colours you can see are the changeset's marks, "
        + "not these values."
      ));
    }
  }

  /** The line above the rows: what is selected, and how much of it is drawn.
   *
   * `picked` is the selection and `chosen` the part of it this drawing holds.
   * They differ after a view switch, and the count that matters is the second
   * one -- it is what an edit here would act on -- so a selection only half of
   * which is on screen says both numbers rather than quietly acting on fewer
   * elements than the user is looking at.
   */
  function summary(picked, chosen) {
    if (!picked.length) { return "nothing selected"; }
    if (!chosen.length) {
      return picked.length === 1 ? picked[0] + " (not drawn here)" : picked.length + " not drawn";
    }
    if (chosen.length === 1 && picked.length === 1) { return chosen[0].address; }
    return chosen.length < picked.length
      ? chosen.length + " of " + picked.length + " selected"
      : chosen.length + " selected";
  }

  function note(text) {
    var line = document.createElement("p");
    line.className = "style-note";
    line.textContent = text;
    return line;
  }

  function row(field, chosen) {
    var state = consensus(chosen, field.key);
    var wrapper = document.createElement("div");
    wrapper.className = "style-row";

    var label = document.createElement("label");
    label.className = "style-label";
    label.textContent = field.label;
    label.setAttribute("for", "style-" + field.key);
    wrapper.appendChild(label);

    var input = control(field, state);
    input.id = "style-" + field.key;
    input.dataset.field = field.key;
    input.disabled = !host.writable();
    wrapper.appendChild(input);

    var from = document.createElement("span");
    from.className = "style-from";
    from.textContent = describe(state.origin, state.agreed);
    wrapper.appendChild(from);

    // Offered only when somebody has actually written the field on one of the
    // selected elements. With nothing declared there is nothing to unset, and a
    // button that would send an operation the server answers "no change" to is
    // a button that teaches people the panel is unreliable.
    var reset = document.createElement("button");
    reset.type = "button";
    reset.className = "ghost style-reset";
    reset.dataset.reset = field.key;
    reset.textContent = "↺";
    reset.title = "Unset " + field.key + " and inherit it again";
    reset.setAttribute("aria-label", "Reset " + field.label + " to the theme");
    reset.disabled = !host.writable() || !state.declared;
    wrapper.appendChild(reset);
    return wrapper;
  }

  /** The input for one field: a colour well, a number, a menu, or plain text.
   *
   * A colour is a native `<input type="color">`, which is a picker on every
   * platform and needs no library. It only speaks `#rrggbb`, so a named colour
   * a document wrote -- `navy` -- is shown as the hex it resolves to, and the
   * name is lost the moment somebody touches the picker. That is the honest
   * trade: the alternative is a text box, and a text box is not a colour
   * picker. `none` is the one value it cannot show at all and it falls back to
   * white, which is why the provenance beside it still says where it came from.
   */
  function control(field, state) {
    if (field.kind === "choice") {
      var select = document.createElement("select");
      select.className = "style-input";
      appendOption(select, "", state.agreed ? "inherit" : MIXED);
      field.options.forEach(function (option) { appendOption(select, option, option); });
      select.value = state.agreed && state.value ? String(state.value) : "";
      return select;
    }
    if (field.kind === "color") {
      var colour = document.createElement("input");
      colour.type = "color";
      colour.className = "style-input style-color";
      colour.value = hex(state.agreed ? state.value : null);
      if (!state.agreed) { colour.title = MIXED; }
      return colour;
    }
    var input = document.createElement("input");
    input.className = "style-input";
    if (field.kind === "number") {
      input.type = "number";
      input.min = String(field.min);
      input.max = String(field.max);
      input.step = String(field.step);
    } else {
      input.type = "text";
      if (field.hint) { input.placeholder = field.hint; }
    }
    input.value = state.agreed && state.value !== null && state.value !== undefined
      ? String(state.value)
      : "";
    if (!state.agreed) { input.placeholder = MIXED; }
    return input;
  }

  function appendOption(select, value, label) {
    var option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.appendChild(option);
  }

  /** A resolved colour as something `<input type="color">` will accept. */
  function hex(value) {
    var text = String(value || "");
    if (/^#[0-9a-fA-F]{6}$/.test(text)) { return text.toLowerCase(); }
    if (/^#[0-9a-fA-F]{3}$/.test(text)) {
      return "#" + text.slice(1).split("").map(function (c) { return c + c; }).join("");
    }
    // Either `none`, or a colour with an alpha pair on it from `opacity`. The
    // well shows the opaque form; the row beside it says what is really set.
    if (/^#[0-9a-fA-F]{8}$/.test(text)) { return text.slice(0, 7).toLowerCase(); }
    return "#ffffff";
  }

  /* ------------------------------------------------------------------ */
  /* Writing                                                            */
  /* ------------------------------------------------------------------ */

  function onChange(event) {
    var field = event.target && event.target.dataset ? event.target.dataset.field : null;
    if (!field) { return; }
    var raw = event.target.value;
    if (raw === "" || raw === null) { unset(field); return; }
    set(field, coerce(field, raw));
  }

  function onClick(event) {
    var button = event.target && event.target.dataset ? event.target.dataset.reset : null;
    if (button) { unset(button); }
  }

  /** A control's string as the type the schema wants.
   *
   * `strokeWidth`, `fontSize` and `opacity` are numbers in YAML, and sending
   * `"2"` would write a string the model then refuses. Everything else travels
   * as it was typed and is checked by the server, which owns the vocabulary --
   * this file does not restate the colour names or the shapes, so a value added
   * to §22 needs no change here beyond the menu it appears in.
   */
  function coerce(field, raw) {
    if (field === "fontSize") { return parseInt(raw, 10); }
    if (field === "strokeWidth" || field === "opacity") { return Number(raw); }
    return raw;
  }

  function set(field, value) {
    var chosen = subjects();
    if (!chosen.length) { host.refuse("nothing is selected"); return; }
    submit(chosen.map(function (one) {
      return { op: "set", address: element(one), path: "spec.style." + field, value: value };
    }), "set style." + field + " on " + subject(chosen));
  }

  /** Which fields *this element* declares, as opposed to inherits. */
  function declared(one) {
    var from = one.style.from || {};
    return Object.keys(from).filter(function (key) { return from[key] === "element"; });
  }

  /** Remove the field from every element that declares it.
   *
   * Only from those: an `unset` on an element that never had it is a no-op the
   * server would report as a change that changed nothing, and in a batch of
   * eleven that is ten confusing lines in the changes drawer.
   *
   * Removing the *last* field takes the block with it. An empty `style: {}` is
   * NG-Z002 -- it validates as a mapping, renders identically to no block at
   * all, and tells nobody it does nothing -- so the batch would be refused
   * whole. Which is the right answer for a hand-written document and the wrong
   * one here: "reset this to the theme" plainly means "stop saying anything
   * about it", and the block is the thing that was saying it.
   */
  function unset(field) {
    var chosen = subjects().filter(function (one) {
      return ((one.style.from || {})[field] || null) === "element";
    });
    if (!chosen.length) { host.refuse("nothing in the selection sets " + field); return; }
    submit(chosen.map(function (one) {
      var only = declared(one).length === 1;
      return {
        op: "unset",
        address: element(one),
        path: only ? "spec.style" : "spec.style." + field
      };
    }), "reset style." + field + " on " + subject(chosen));
    // The panel repaints when the render that follows the write arrives, so
    // there is nothing to do here: the new provenance is the server's answer,
    // not a guess this file could make.
  }

  /** The *document* behind a drawn thing.
   *
   * A node's address is its fully-qualified name and is already one. A link's
   * is the drawing's id for the line, which for an adapter attachment or a
   * tunnel leg carries a `#suffix` naming which leg it is -- and the style is
   * written on the document, not on the leg.
   */
  function element(one) {
    return one.link ? String(one.address).split("#")[0] : one.address;
  }

  function subject(chosen) {
    return chosen.length === 1 ? chosen[0].address : chosen.length + " elements";
  }

  function submit(operations, said) {
    if (!host.writable()) { host.refuse("this session is read-only"); return; }
    host.ops(operations, said).catch(function () { /* reported by session.js */ });
  }

  /* ------------------------------------------------------------------ */

  function defineCommands(K) {
    K.define("style.toggle", { run: function () { show(!open); } });
    K.define("style.inspect", {
      run: function () {
        show(true);
        if (!window.netgraphSelect.targets().length) {
          host.refuse("select an element first");
        }
      }
    });
  }

  return {
    attach: attach,
    defineCommands: defineCommands,
    annotate: annotate,
    refresh: refresh,
    show: show,
    isOpen: isOpen,
    /* For the browser tests: what the panel currently believes it is drawing. */
    subjects: subjects,
    describe: describe
  };
})();
