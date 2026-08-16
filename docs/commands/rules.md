# `netgraph rules`

List the validation rules, their severity and their schema aliases. The table is
printed from `netgraph.rules.RULES` — the same source the validator reads — so it
always describes the build you are running rather than the build whose
documentation you happen to have open. It needs no inventory and takes no options.

## Synopsis

<!-- generated: synopsis rules -->
```text
netgraph [GLOBAL OPTIONS] rules [OPTIONS]
```
<!-- /generated -->

## Why you would ask

`netgraph rules` is the **vocabulary** for every place a rule can be named:
`--disable` on a command line, `ignore` and `[validate.severity]` in
`netgraph.toml`, and the `netgraph/ignore` annotation on an element. Two questions
it answers directly:

* *A finding says `W113` — what is that, and what is it called in the
  specification?* The row gives the one-line summary and the `NG-*` alias, and
  either spelling works in every suppression mechanism.
* *What could this inventory possibly be told about?* Reading the sixty-three
  summaries end to end takes a couple of minutes and is a surprisingly good way to
  find out what netgraph considers worth knowing about a network.

For the *why* behind a rule — what it exempts, what it costs to ignore, which
element to annotate — go to its section in
[`docs/validation-rules.md`](../validation-rules.md). This command is the index;
that page is the text.

Shell completion knows the same list, so `netgraph validate --disable <TAB>`
offers it without your having to run this command at all — `netgraph completion
bash|zsh|fish` prints the script that installs it.

## What it prints

Four columns: the short id, the default severity, the `NG-*` aliases and the
summary. The order is the report order of the catalogue — errors, then warnings,
then infos, each numbered in the order they were added.

<!-- run: -->
```console
$ netgraph rules
RULE  SEVERITY  ALIASES           SUMMARY
----  --------  ----------------  ------------------------------------------------------------------------------------
E001  error     NG-C002, NG-C003  A cable endpoint references an unknown device or interface.
E002  error     NG-C005           An interface is terminated by more than one cable.
E003  error     NG-I008           The same MAC address is used by two interfaces in the inventory.
E004  error     NG-A004           The same IP address is assigned twice within one subnet and VLAN.
E005  error     NG-C011           The two ends of a link disagree about VLANs, so it carries less than it seems.
E006  error     NG-X008           An adapter declares more downstream interfaces than it has ports.
E007  error     NG-I004           Interface stacking through 'parent'/'members' contains a cycle.
E008  error     NG-I005           A lag/bridge member is itself aggregated or carries a sub-interface.
E009  error     NG-V005           A 'vlan' sub-interface's VID is not carried by its parent interface.
E010  error     NG-I009           A MAC address has the multicast bit set, so no interface can own it.
E011  error     NG-C006           A cable's medium disagrees with the radio/wired type of an endpoint.
E012  error     NG-C009           A cable endpoint is a loopback, vlan or bridge interface.
E013  error     NG-X005           A cable lands on an adapter's upstream port that 'attached_to' claims.
E014  error     NG-X006           Adapter 'attached_to' attachments form a cycle.
E015  error     NG-X001           An adapter's 'attached_to' names no element that could host it.
E016  error     NG-T002           A tunnel endpoint references an unknown element or interface.
E017  error     NG-T003           A tunnel endpoint is not an interface of type 'tunnel'.
E018  error     NG-T004           A tunnel's 'over' names no tunnel of this inventory.
E019  error     NG-T005           Tunnel 'over' references form a cycle, so nothing reaches the underlay.
E020  error     NG-A013           An interface's 'gateway' lies outside every prefix configured on it.
E021  error     NG-P001           A cable terminates on a position the patch panel does not have.
E022  error     NG-P003           A patch-panel position terminates more than one cable.
E023  error     NG-P004           A patch panel is named where an active element is required.
E024  error     NG-P005           A patch run leaves a panel and is patched back into the same one.
E025  error     NG-U001           Two elements occupy the same unit of one rack.
E026  error     NG-U002           An element extends past the top of the rack it is mounted in.
E027  error     NG-U003           One rack is declared with two different heights.
E028  error     NG-W007           A wireless link does not join one 'ap' radio to a client radio.
E029  error     NG-W008           The same BSSID is advertised by two radios in the inventory.
E030  error     NG-W009           An SSID is mapped to a VLAN the access point carries nowhere.
E031  error     NG-W010           A client radio is associated to an SSID its access point does not advertise.
E032  error     NG-F008           A route's next hop lies in no prefix the device configures in that VRF.
E033  error     NG-F009           A route's 'dev' names an interface the device does not have.
E034  error     NG-F010           An OSPF interface is not in the device's interface list.
E035  error     NG-F011           The two ends of a resolved BGP session disagree about an AS number.
E036  error     NG-F012           Two elements claim the same router id.
E037  error     NG-E010           One PDU outlet is claimed by two power supplies.
E038  error     NG-E011           A power input names an outlet that does not exist.
E039  error     NG-E012           The declared load on a PDU exceeds its capacity.
E040  error     NG-E013           The PoE allocated on a device's ports exceeds its budget.
E041  error     NG-E014           A PoE-powered device's uplink offers no PoE, or too little.
E042  error     NG-E015           A device claims redundant power but its feeds are not independent.
E043  error     NG-S010           A group names a member the inventory does not declare.
E044  error     NG-S011           A group names a member that is not a user or a group.
E045  error     NG-S012           Group membership forms a cycle.
E046  error     NG-S013           Two identities claim the same login, uid or gid.
E047  error     -                 An element declares it must keep its gateway under any single failure, and does not.
E048  error     -                 An element declares it must keep power under any single failure, and does not.
E049  error     NG-N024           A cable terminates on one end of a veth pair.
E050  error     NG-N025           A bridge or lag aggregates a member in another network namespace.
W101  warning   NG-I013           An interface has neither IPv4 nor IPv6 and is not a switchport.
W102  warning   NG-C010           The two endpoints of a cable disagree about the MTU.
W103  warning   NG-C016           A device terminates no cable and hosts no adapter: an orphan node.
W104  warning   NG-V009           An access port of a layer-2-only switch carries an IP address.
W105  warning   NG-A008           A subnet holds exactly one element, so its prefix length may be wrong.
W106  warning   NG-A009           Two elements claim the same address in one subnet, in different VLANs.
W107  warning   NG-I006           A lag/bridge member carries its own IPv4 or IPv6 addresses.
W108  warning   NG-I007           A loopback interface declares a MAC address.
W109  warning   NG-I012           A device declares no ethernet, wifi or lag interface, so it cannot be cabled.
W110  warning   NG-A005           An address is the network or broadcast address of its own prefix.
W111  warning   NG-A006           Two interfaces on one element hold overlapping prefixes.
W112  warning   NG-A007           A loopback interface carries a prefix other than /32 or /128.
W113  warning   NG-V004           A port references a VLAN the device's 'vlans' database does not declare.
W114  warning   NG-V006           A trunk's 'native_vlan' is not listed in its 'trunk_vlans'.
W115  warning   NG-V007           A port trunking every VLAN faces a host rather than another switch.
W116  warning   NG-V008           A lag member declares a 'vlan' block that differs from the aggregate's.
W117  warning   NG-C004           Both endpoints of one cable land on the same element.
W118  warning   NG-C008           A cable's 'speed' disagrees with the speed an endpoint declares.
W119  warning   NG-C012           A cable endpoint is a lag aggregate rather than one of its members.
W120  warning   NG-C013           A cable is 'duplex: half' on a link that involves no hub.
W121  warning   NG-C014           The topology graph is disconnected: it falls into separate islands.
W122  warning   NG-H005           Two elements on one hub are addressed in different subnets.
W123  warning   NG-X002           An adapter has cabled downstream ports but no 'attached_to' host.
W124  warning   NG-X007           An adapter's 'attached_to' points at a hub or a switch, not a host.
W125  warning   NG-T006           An overlay terminates where its underlay tunnel does not reach.
W126  warning   NG-T011           A tunnel's MTU does not fit inside its underlay after encapsulation.
W127  warning   NG-T012           A tunnel encrypts nothing and no tunnel it runs inside does either.
W128  warning   NG-T013           A 'tunnel' interface is named by no tunnel document.
W129  warning   NG-T014           Two tunnels terminating on one element use the same VNI.
W130  warning   NG-A010           One prefix is claimed by two broadcast domains that cannot reach each other.
W131  warning   NG-A011           A prefix nested inside another is used in a different broadcast domain.
W132  warning   NG-A012           Two directly linked interfaces are addressed in prefixes that do not meet.
W133  warning   NG-P002           A cabled patch-panel position is coupled to one nothing is patched into.
W134  warning   NG-W011           Two access points in one broadcast domain share overlapping channels.
W135  warning   NG-F013           A BGP neighbour address resolves to no element of the inventory.
W136  warning   NG-F014           A VRF is declared that no interface of the device is bound to.
W137  warning   NG-E016           A device declares a power draw but no power path.
W138  warning   NG-Y001           Diagram geometry names an element the inventory does not declare.
W139  warning   NG-S014           A group has no members.
W140  warning   NG-S015           A group still lists a user who has departed.
W141  warning   -                 A redundancy expectation names something the tool does not understand.
W142  warning   NG-G001           A diagram annotation names an element the inventory does not declare.
W143  warning   NG-G004           An area's selector matches no element of the inventory.
W144  warning   NG-Z003           A style fades an element to nothing, so it is drawn invisibly.
W145  warning   NG-Z005           A style draws an element's label in the colour of the box behind it.
W146  warning   NG-N026           A declared network namespace holds no interface.
I001  info      NG-I010           A MAC address is locally administered rather than vendor-assigned.
I002  info      NG-C015           An interface is enabled but terminates no cable.
I003  info      NG-T015           A tunnel listens on a port other than the registered one for its type.
I004  info      NG-S016           A person's account is a member of no group.
I005  info      NG-N027           Both ends of a veth pair are in the same network namespace.
```

Two things the table does not say, both deliberate:

* **The severity is the default**, not necessarily the one your tree uses.
  `[validate.severity]` in [`netgraph.toml`](../configuration.md#validate--how-findings-are-graded)
  re-grades any of these, and `--strict` promotes every warning to an error.
  `netgraph config show` prints what actually resolved for an inventory, and
  [where it came from](../configuration.md#seeing-what-resolved-and-why).
* **The letter of the id is history**, not state. `W` means the rule was *first*
  assigned `warning`; a rule keeps its id when an inventory re-grades it, because
  ids are permanent and a suppression written today has to keep meaning what it
  meant.

Only the semantic rules listed here can be disabled or re-graded. The loading and
schema constraints have `NG-*` ids too — they appear in the `RULE` column of a
report as `NG-D005` or `load` — but they are not suppressible and so are not part
of this vocabulary. See [Pass 2 — schema](../validation-rules.md#pass-2--schema).

## Arguments

<!-- generated: arguments rules -->
*Takes no positional arguments.*
<!-- /generated -->

## Options

<!-- generated: options rules -->
| Flag | Value | Default | Meaning |
|---|---|---|---|
| `--fixable` | — | off | List only the rules 'netgraph validate --fix' can repair, and what each repair does. |
<!-- /generated -->

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The table was printed. |
| `2` | Usage error — an unknown option. |
| `141` | The downstream end of a pipe closed first. |

## See also

* [`docs/validation-rules.md`](../validation-rules.md) — one section per rule:
  why it matters, what it exempts, and how to suppress it.
* [`docs/validation.md`](../validation.md) — the three passes, severities, and the
  four ways to silence a finding.
* [`netgraph validate`](validate.md) — the command that reports these rules.
* [`docs/configuration.md`](../configuration.md#validate--how-findings-are-graded) —
  `ignore` and `[validate.severity]` in `netgraph.toml`.
