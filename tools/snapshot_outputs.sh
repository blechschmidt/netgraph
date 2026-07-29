#!/bin/sh
# Capture every user-visible output netgraph produces over every inventory the
# repository owns, so a performance change can be proved to have altered none of
# them: `snapshot_outputs.sh before && ...change... && snapshot_outputs.sh after
# && diff -r before after`.
#
# Both YAML parser paths are captured, because a change in the loader or the
# model layer could plausibly reach one and not the other. The benchmark tree is
# included because it is the only inventory here with thousands of addresses.
#
#     tools/snapshot_outputs.sh OUTDIR [BENCH_TREE]
set -eu

out=${1:?usage: snapshot_outputs.sh OUTDIR [BENCH_TREE]}
bench=${2:-}
root=$(cd "$(dirname "$0")/.." && pwd)
netgraph=${NETGRAPH:-$root/.venv/bin/netgraph}

mkdir -p "$out"

capture() {
    label=$1
    inventory=$2
    for loader in libyaml python; do
        dir="$out/$loader/$label"
        mkdir -p "$dir"
        for sub in validate "validate --strict" "list subnets" "list devices" \
                   "list cables" "list tunnels" "list vlans"; do
            name=$(echo "$sub" | tr ' -' '__')
            # shellcheck disable=SC2086
            NETGRAPH_YAML_LOADER=$loader "$netgraph" --no-color -i "$inventory" $sub \
                >"$dir/$name.out" 2>"$dir/$name.err" || echo "exit=$?" >>"$dir/$name.out"
        done
        for fmt in dot mermaid json; do
            for layer in l1 l2 l3; do
                NETGRAPH_YAML_LOADER=$loader "$netgraph" --no-color -i "$inventory" \
                    render -f "$fmt" --layer "$layer" \
                    >"$dir/render_${fmt}_${layer}.out" 2>"$dir/render_${fmt}_${layer}.err" \
                    || echo "exit=$?" >>"$dir/render_${fmt}_${layer}.out"
            done
        done
        # A trace between the first and the last element the inventory declares.
        # Which two they are does not matter -- what is being captured is that
        # the answer did not change -- but they must be picked the same way on
        # both runs, and load order is deterministic.
        names=$(NETGRAPH_YAML_LOADER=$loader "$netgraph" --no-color -i "$inventory" \
                list devices 2>/dev/null | awk 'NR>2 {print $1}')
        first=$(echo "$names" | head -n 1)
        last=$(echo "$names" | tail -n 1)
        if [ -n "$first" ] && [ -n "$last" ] && [ "$first" != "$last" ]; then
            for fmt in text json; do
                NETGRAPH_YAML_LOADER=$loader "$netgraph" --no-color -i "$inventory" \
                    path --all --force -F "$fmt" "$first" "$last" \
                    >"$dir/path_${fmt}.out" 2>"$dir/path_${fmt}.err" \
                    || echo "exit=$?" >>"$dir/path_${fmt}.out"
            done
        fi
    done
}

for inventory in "$root"/examples/*/; do
    [ -d "$inventory" ] || continue
    capture "example-$(basename "$inventory")" "$inventory"
done

for fixture in "$root"/tests/fixtures/invalid/*.yaml; do
    capture "invalid-$(basename "$fixture" .yaml)" "$fixture"
done

if [ -n "$bench" ]; then
    capture "benchmark" "$bench"
fi

echo "captured $(find "$out" -type f | wc -l) files under $out"
