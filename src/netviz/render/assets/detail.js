/* One detail record, drawn.
 *
 * The records come from netviz.render.details.build_details -- the JSON
 * export plus an element id and a links cross-reference -- and two front ends
 * show them: the info box of `netviz web`, and the detail panel of the
 * self-contained page `netviz render -f html` writes. The records are built
 * once in Python for exactly the reason this file exists in one copy: two
 * views of one inventory that disagree about what a device is are worse than
 * one view.
 *
 * Everything a record contains is inserted with textContent. This file assigns
 * no markup at all -- not innerHTML, not insertAdjacentHTML -- so a name, a
 * description or a label carrying markup is text wherever it lands, and there
 * is no escaping contract for a caller to get wrong.
 */

var netvizDetail = (function () {
  "use strict";

  /* -------------------------------------------------------------- helpers */

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

  function append(parent, child) {
    if (child) { parent.appendChild(child); }
  }

  /* --------------------------------------------------------------- record */

  /** Which columns to draw. The defaults are "everything the record holds":
   *  a record was already narrowed in Python by the options the diagram was
   *  rendered with, and a front end offering a live toggle narrows it again. */
  function settings(options) {
    var given = options || {};
    return {
      showIps: given.showIps !== false,
      showVlans: given.showVlans !== false,
      hint: given.hint || ""
    };
  }

  function heading(record, name, kind, view) {
    var head = element("h2", null, name);
    head.appendChild(element("span", "kind", "[" + kind + "]"));
    if (view.hint) { head.appendChild(element("span", "pinhint", view.hint)); }
    return head;
  }

  // A tunnel is the one record where the interesting facts are neither
  // physical nor addressing: what it encapsulates, what carries it, and -- the
  // one a reader most needs -- whether anything in the stack encrypts.
  function tunnelSection(tunnel) {
    if (!tunnel) { return null; }
    var protection = tunnel.encrypted
      ? "yes" + (tunnel.cipher ? " (" + tunnel.cipher + ")" : "")
      : (tunnel.encryptedBy ? "by " + tunnel.encryptedBy : "no — cleartext");
    return section("tunnel", definitions([
      ["stack", tunnel.stack.join(" over ")],
      ["carries", "layer " + tunnel.layer],
      ["transport", tunnel.transport + (tunnel.port ? "/" + tunnel.port : "")],
      ["mode", tunnel.mode],
      ["vni", tunnel.vni],
      ["encrypted", protection],
      ["auth", tunnel.auth],
      ["mtu", tunnel.mtu ? tunnel.mtu + " (overhead " + tunnel.overheadBytes + " B)" : ""],
      ["over", tunnel.over]
    ]));
  }

  function describeNode(record, view) {
    var box = document.createDocumentFragment();
    box.appendChild(heading(record, record.name, record.kind, view));

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
        ["addresses", view.showIps ? join(record.subnet.addresses) : ""],
        ["elements", join(record.subnet.elements)]
      ])));
    }

    append(box, tunnelSection(record.tunnel));

    if (view.showVlans) {
      append(box, section("vlans", tags((record.vlans || []).map(function (id) {
        return "vlan " + id;
      }))));
    }

    append(box, section("interfaces", table(
      ["interface", "type", "addresses", "vlan", "mac / mtu"],
      (record.interfaces || []).map(function (port) {
        return {
          muted: port.enabled === false,
          cells: [
            port.name,
            port.type,
            view.showIps ? join(port.addresses) : "",
            view.showVlans && port.vlan ? port.vlan.mode + " " + join(port.vlan.vlans) : "",
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
            [link.stack || link.medium || link.kind, link.speedText].filter(Boolean).join(" "),
            view.showVlans ? join(link.vlans) : ""
          ]
        };
      })
    )));

    if (!(record.interfaces || []).length && !(record.links || []).length) {
      append(box, element("p", "note", "no interfaces and no links"));
    }
    return box;
  }

  function describeLink(record, view) {
    var box = document.createDocumentFragment();
    var ends = record.endpoints || [];
    var name = ends.map(function (end) {
      return end.node + (end.interface ? ":" + end.interface : "");
    }).join("  —  ");
    box.appendChild(heading(record, name, record.kind, view));

    append(box, section("link", definitions([
      ["id", record.id],
      ["medium", record.medium],
      ["speed", record.speedText],
      ["label", record.label],
      ["length", record.lengthM ? record.lengthM + " m" : ""],
      ["addresses", view.showIps ? join(record.addresses) : ""]
    ])));

    append(box, tunnelSection(record.tunnel));

    append(box, section("endpoints", table(["element", "interface"], ends.map(function (end) {
      return { cells: [end.node, end.interface || "—"] };
    }))));

    if (view.showVlans) {
      append(box, section("vlans", tags((record.vlans || []).map(function (id) {
        return "vlan " + id;
      }))));
    }
    return box;
  }

  /** The record, as a document fragment ready to be dropped into a box.
   *
   * options.showIps / options.showVlans narrow it the way the render flags of
   * the same name do; options.hint adds a note to the heading.
   */
  function describe(record, options) {
    var view = settings(options);
    return record.type === "edge" ? describeLink(record, view) : describeNode(record, view);
  }

  /** Everything a search should look at, lower-cased, one string per record. */
  function haystack(record) {
    var parts = [record.id, record.name, record.kind, record.namespace, record.description];
    (record.vlans || []).forEach(function (id) { parts.push("vlan " + id, String(id)); });
    (record.interfaces || []).forEach(function (port) {
      parts.push(port.name, port.type, port.mac, port.description);
      (port.addresses || []).forEach(function (address) { parts.push(address); });
      if (port.vlan) {
        (port.vlan.vlans || []).forEach(function (id) { parts.push("vlan " + id, String(id)); });
      }
    });
    (record.endpoints || []).forEach(function (end) {
      parts.push(end.node, end.interface);
    });
    (record.addresses || []).forEach(function (address) { parts.push(address); });
    Object.keys(record.labels || {}).forEach(function (key) {
      parts.push(key, record.labels[key]);
    });
    if (record.subnet) {
      parts.push(record.subnet.prefix, record.subnet.family);
      (record.subnet.addresses || []).forEach(function (address) { parts.push(address); });
    }
    if (record.tunnel) {
      parts.push(record.tunnel.type, record.tunnel.transport, record.tunnel.cipher);
      (record.tunnel.stack || []).forEach(function (layer) { parts.push(layer); });
    }
    parts.push(record.medium, record.label, record.speedText);
    return parts.filter(Boolean).join("\n").toLowerCase();
  }

  /** How a record should be listed: its name, and what sort of thing it is. */
  function label(record) {
    if (record.type !== "edge") { return record.name; }
    return (record.endpoints || []).map(function (end) {
      return end.node + (end.interface ? ":" + end.interface : "");
    }).join(" — ");
  }

  return {
    append: append,
    definitions: definitions,
    describe: describe,
    element: element,
    haystack: haystack,
    join: join,
    label: label,
    section: section,
    table: table,
    tags: tags
  };
})();
