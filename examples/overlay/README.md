# overlay — tunnels, and tunnels inside tunnels

Three sites homed to one provider edge, and five overlays running over that
one physical topology. It is the example for [`docs/schema.md` §14](../../docs/schema.md#14-tunnels):
what a `tunnel` document looks like, what nesting one inside another means, and
what the diagram does with both.

```console
$ netgraph -i examples/overlay validate
no problems found

$ netgraph -i examples/overlay list tunnels
NAME                STACK             VNI  ENCRYPTED  ENDS  ENDPOINTS
------------------  ----------------  ---  ---------  ----  ----------------------------------------------
tunnels/wg-mesh     wireguard           -  yes           3  rtr-branch-a:wg0, rtr-branch-b:wg0, rtr-hq:wg0
tunnels/ipsec-hq-b  ipsec               -  yes           2  rtr-branch-b:ipsec0, rtr-hq:ipsec0
tunnels/vx-100      vxlan over ipsec  100  underlay      2  rtr-branch-b:vxlan100, rtr-hq:vxlan100
tunnels/gre-mgmt    gre over ipsec      -  underlay      2  rtr-branch-b:gre1, rtr-hq:gre1
tunnels/ovpn-admin  openvpn             -  yes           2  pc-branch-b:tun0, rtr-hq:ovpn0
```

## What is in it

| Element | Role |
|---|---|
| `wan/wan-core` | The provider edge. Three /30 hand-offs and nothing else — every overlay runs *through* it, so it terminates none of them and does not appear in the overlay view at all. |
| `sites/hq/rtr-hq` | The hub. One end of every tunnel except the branch-a leg of the mesh. |
| `sites/branch-a/rtr-branch-a` | Mesh member only: one `wg0` and nothing else. |
| `sites/branch-b/rtr-branch-b` | The far end of the IPsec stack: `ipsec0`, and `vxlan100` and `gre1` inside it. |
| `sites/branch-b/pc-branch-b` | An administrator's workstation dialling into HQ over OpenVPN — a tunnel that terminates on a host, not on a router. |

## The five tunnels

* **`wg-mesh`** — WireGuard, **three endpoints**. A tunnel with more than two
  ends has no line shape, so it is drawn as a node with one leg per site. This
  is the point-to-multipoint case.
* **`ipsec-hq-b`** — IPsec in tunnel mode. Point-to-point, so below the overlay
  layer it is drawn as a line between the two routers.
* **`vx-100`** — VXLAN, VNI 100, `over: ipsec-hq-b`. **This is the nested case.**
  VXLAN encrypts nothing, so on its own it would be `W127`; carried inside the
  IPsec tunnel it is protected, and the diagram says `vxlan over ipsec`.
* **`gre-mgmt`** — GRE, also `over: ipsec-hq-b`. Two overlays sharing one
  underlay, which is what makes the encapsulation edge worth drawing separately
  from the tunnels themselves.
* **`ovpn-admin`** — OpenVPN, host to concentrator.

## Things to look at

**The MTU ladder.** Every tunnel's `mtu` is its underlay's minus its own
encapsulation overhead: 1500 → 1427 (IPsec, −73) → 1377 (VXLAN, −50) and
1403 (GRE, −24). Change one of them upwards and `W126` says exactly how much is
left. The overheads are in [§14.1](../../docs/schema.md#141-tunnel-types).

**VLAN 100 crosses a tunnel.** `vxlan100` is an access port in VLAN 100 on both
routers. VXLAN carries frames, so the tunnel carries the VLAN — `netgraph -i
examples/overlay render --layer l2 --vlan 100` draws the two routers as one
broadcast domain even though no cable between them carries VLAN 100.

**Nothing here holds a secret.** `auth: public-key` and `cipher:
chacha20-poly1305` say how the peers authenticate and what they negotiated;
there is nowhere to put the key, deliberately ([§14.2](../../docs/schema.md#142-what-a-tunnel-does-not-hold)).

## The four views

```console
$ netgraph -i examples/overlay render -o l1.svg                    # physical, tunnels dashed over it
$ netgraph -i examples/overlay render --layer l2 -o l2.svg         # VLAN 100 across the VXLAN
$ netgraph -i examples/overlay render --layer l3 -o l3.svg         # the prefixes, tunnel prefixes included
$ netgraph -i examples/overlay render --layer overlay -o ov.svg    # the encapsulation stack
```

The overlay view is the one that shows nesting: `vx-100` and `gre-mgmt` each
have a dotted `over` edge into `ipsec-hq-b`. That relation is undrawable at
layer 1, because it joins two *links* and a link cannot end on a link — which is
why a tunnel becomes a node there.
