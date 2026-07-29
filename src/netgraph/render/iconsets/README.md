# Bundled icon themes

One directory per theme, holding one image per element kind. The file name is
the kind — `router`, `switch`, `hub`, `computer`, `server`, `adapter` — plus
`subnet` for the prefix nodes of the layer-3 view. Anything else in a directory
is ignored, so a theme may cover only some kinds; the kinds it does not cover
fall back to netgraph's plain Graphviz shapes.

Each kind is present twice, as `.svg` and as `.png`. That is not redundancy:
Graphviz reads an SVG image only when it was built against librsvg, which its
`png` and `pdf` outputs frequently were not, so those formats get the raster
file and SVG output gets the vector one. See `netgraph/render/icons.py`.

The SVG is the source. After editing one, re-run

```bash
pip install cairosvg        # not a netgraph dependency; only this tool needs it
python tools/render_icons.py
```

to bring its PNG back into step — `--check` reports staleness without writing.

## cisco

Icons drawn in the network-topology idiom Cisco made the industry convention: a
router is a cylinder with opposed arrows, a switch and a hub are flat slabs, a
subnet is a cloud. Each carries the same accent colour netgraph gives that kind
in its plain shapes, so a diagram stays colour-coded either way.

**The artwork is netgraph's own** and is covered by the project's MIT licence.
Cisco's published icon library is copyrighted by Cisco and is not redistributed
here. If you have that library and would rather use it, you do not need this
theme — point `--icons` at a directory of your own:

<!-- norun: copies the reader's own icon library and writes an SVG into their directory -->
```bash
mkdir cisco-official
cp .../Router.png cisco-official/router.png     # name each file for its kind
netgraph render --icons ./cisco-official -f svg -o topology.svg
```
