#!/usr/bin/env python3
"""
generate_block.py — turn a validated block spec JSON into a KiCad design block.

Drives the kicad-sch-api Python library directly. Keeps generation off the
MCP path entirely: one Bash invocation instead of N tool calls.

Usage:
    python3 generate_block.py --spec /tmp/block.json [--out PATH] [--schema PATH]

Defaults:
    --schema  block_spec.schema.json next to this script
    --out     <spec.target_lib_path>/<spec.name>.kicad_sch

Exit codes:
    0  success
    1  generation failed (errors printed to stderr)
    2  invocation error (missing files, bad args)

The generator does the parts that are hard to get right by hand:
    * places hierarchical labels offset from the connected pin in the
      direction matching the port's `side`, then wires label -> pin
    * adds power symbols
    * wires every connection pin-to-pin (not coordinate-to-coordinate,
      which doesn't reliably hit pin endpoints)
    * AUTO-ADDS PWR_FLAGs on nets that have any Power-input pin and no
      Power-output pin — required for ERC of a standalone block to not
      complain about "Power input not driven by any output power pin"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import kicad_sch_api as ksa

# Workaround for kicad-sch-api 0.5.5: connectivity.Net is a @dataclass with no
# __hash__, but the library puts Net instances in a set() in
# connectivity._add_wire_to_net. Identity-based hashing matches the intent.
from kicad_sch_api.core.connectivity import Net as _ConnNet
_ConnNet.__hash__ = object.__hash__  # type: ignore[method-assign]


SHAPE_FOR_TYPE = {
    "power_in":       "input",
    "power_out":      "output",
    "ground":         "passive",
    "input":          "input",
    "output":         "output",
    "bidirectional":  "bidirectional",
    "tristate":       "tri_state",
    "passive":        "passive",
    "open_collector": "passive",
    "open_emitter":   "passive",
}

SIDE_OFFSET_MM = 12.7   # 5 grid units; label sits 5 squares from the pin
SIDE_DELTA = {
    "left":   (-SIDE_OFFSET_MM, 0),
    "right":  ( SIDE_OFFSET_MM, 0),
    "top":    (0, -SIDE_OFFSET_MM),
    "bottom": (0,  SIDE_OFFSET_MM),
}
SIDE_TO_ROTATION = {"left": 0, "right": 180, "top": 270, "bottom": 90}


def snap(value: float, grid: float = 1.27) -> float:
    return round(value / grid) * grid


def load_spec(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"x spec is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)


def validate_against_schema(spec: dict, schema_path: Path) -> None:
    try:
        import jsonschema
    except ImportError:
        print("! jsonschema not installed; skipping schema validation",
              file=sys.stderr)
        return
    schema = json.loads(schema_path.read_text())
    validator = jsonschema.Draft202012Validator(schema)
    errors = list(validator.iter_errors(spec))
    if errors:
        print(f"x spec failed schema validation ({len(errors)} errors):",
              file=sys.stderr)
        for e in errors:
            loc = ".".join(str(p) for p in e.absolute_path) or "<root>"
            print(f"   {loc}: {e.message}", file=sys.stderr)
        sys.exit(1)


def build_components(sch, spec: dict) -> None:
    for c in spec["components"]:
        extra = {"MPN": c["mpn"]} if c.get("mpn") else {}
        sch.components.add(
            lib_id=c["lib_id"],
            reference=c["ref"],
            value=c["value"],
            position=tuple(c["position"]),
            footprint=c["footprint"],
            rotation=c.get("rotation", 0),
            **extra,
        )


def build_power_symbols(sch, spec: dict) -> dict[str, str]:
    """Return {net_name: power_symbol_ref}."""
    refs: dict[str, str] = {}
    for idx, ps in enumerate(spec.get("power_symbols", []), start=1):
        net = ps["net"]
        ref = f"#PWR{idx:02d}"
        sch.components.add(
            lib_id=f"power:{net}",
            reference=ref,
            value=net,
            position=tuple(ps["position"]),
            rotation=ps.get("rotation", 0),
            footprint="",
        )
        refs[net] = ref
    return refs


def find_first_connected_pin_for_port(spec: dict, port_name: str):
    """Return (ref, pin) of the first component pin that connects to this port,
    or None if the port is not used in any connection."""
    for conn in spec["connections"]:
        a, b = conn["from"], conn["to"]
        if "port" in a and a["port"] == port_name and "ref" in b:
            return b["ref"], b["pin"]
        if "port" in b and b["port"] == port_name and "ref" in a:
            return a["ref"], a["pin"]
    return None


def build_ports(sch, spec: dict) -> dict[str, tuple]:
    """Place hierarchical labels offset from the pin they connect to,
    in the direction matching the port's `side`. Returns
    {port_name: (uuid, position_tuple)}."""
    ports: dict[str, tuple] = {}
    for p in spec["ports"]:
        anchor = find_first_connected_pin_for_port(spec, p["name"])
        if anchor is None:
            print(f"! port {p['name']} has no connection in spec; "
                  f"placing at (100, 100)", file=sys.stderr)
            label_pos = (100.0, 100.0)
        else:
            ref, pin = anchor
            pin_pos = sch.get_component_pin_position(ref, pin)
            if pin_pos is None:
                print(f"x cannot resolve pin position for {ref}.{pin}",
                      file=sys.stderr)
                sys.exit(1)
            dx, dy = SIDE_DELTA.get(p["side"], (-SIDE_OFFSET_MM, 0))
            label_pos = (snap(pin_pos.x + dx), snap(pin_pos.y + dy))
        uuid = sch.add_hierarchical_label(
            text=p["name"],
            position=label_pos,
            shape=SHAPE_FOR_TYPE.get(p["type"], "passive"),
            rotation=SIDE_TO_ROTATION.get(p["side"], 0),
        )
        ports[p["name"]] = (uuid, label_pos)
    return ports


def build_connections(sch, spec: dict, port_anchors: dict, power_refs: dict) -> None:
    for i, conn in enumerate(spec["connections"]):
        a, b = conn["from"], conn["to"]
        a_pin = "ref" in a
        b_pin = "ref" in b

        if a_pin and b_pin:
            sch.add_wire_between_pins(a["ref"], a["pin"], b["ref"], b["pin"])
            continue

        if (a_pin and "port" in b) or ("port" in a and b_pin):
            pin_ep   = a if a_pin else b
            port_ep  = b if a_pin else a
            label_pos = port_anchors[port_ep["port"]][1]
            sch.add_wire_to_pin(
                start=label_pos,
                component_ref=pin_ep["ref"],
                pin_number=pin_ep["pin"],
            )
            continue

        if (a_pin and "power" in b) or ("power" in a and b_pin):
            pin_ep = a if a_pin else b
            pwr_ep = b if a_pin else a
            sch.add_wire_between_pins(
                pin_ep["ref"], pin_ep["pin"],
                power_refs[pwr_ep["power"]], "1",
            )
            continue

        print(f"! unsupported connection shape at index {i}: {conn}",
              file=sys.stderr)


def auto_add_pwr_flags(sch, spec: dict) -> int:
    """Walk pin connectivity. For each net that has at least one Power-input
    pin and zero Power-output pins, add a PWR_FLAG and wire it pin-to-pin
    to a representative pin on the net. Returns the count of flags added."""
    # Build (ref, pin) -> electrical_type lookup for every component pin
    pin_types: dict[tuple, str] = {}
    pin_positions: dict[tuple, object] = {}
    component_refs = []
    for c in sch.components.all():
        component_refs.append(c.reference)
        for pin in sch.components.get_pins_info(c.reference):
            key = (c.reference, pin.number)
            pin_types[key] = pin.electrical_type.value
            pin_positions[key] = pin.position

    # Group pins into nets via get_net_for_pin (avoids the
    # get_connected_pins TypeError in kicad-sch-api 0.5.5 connectivity).
    visited: set[tuple] = set()
    nets: list[set[tuple]] = []
    for key in pin_types:
        if key in visited:
            continue
        ref, pin = key
        net = sch.get_net_for_pin(ref, pin)
        if net is None:
            visited.add(key)
            continue
        net_pins = {(p.reference, p.pin_number) for p in net.pins}
        nets.append(net_pins)
        visited |= net_pins

    flag_count = 0
    flag_idx = 1
    for net_pins in nets:
        types = {pin_types.get(k, "unknown") for k in net_pins}
        if "power_out" in types:
            continue  # rail is already driven from inside the block
        if "power_in" not in types:
            continue  # no power-input pins; ERC won't complain

        # Pick a representative pin (deterministic: lowest-sorted key)
        rep = sorted(net_pins)[0]
        rep_ref, rep_pin = rep
        rep_pos = pin_positions[rep]

        flag_ref = f"#FLG{flag_idx:02d}"
        flag_idx += 1
        # Place flag at a snapped offset above-left of the representative pin.
        # Position is cosmetic only; connectivity is by pin-to-pin wire below.
        flag_pos = (snap(rep_pos.x - 5.08), snap(rep_pos.y - 5.08))
        sch.components.add(
            lib_id="power:PWR_FLAG",
            reference=flag_ref,
            value="PWR_FLAG",
            position=flag_pos,
            footprint="",
        )
        sch.add_wire_between_pins(flag_ref, "1", rep_ref, rep_pin)
        flag_count += 1
    return flag_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--spec", type=Path, required=True,
                        help="block spec JSON file")
    parser.add_argument("--out", type=Path, default=None,
                        help="output .kicad_sch path "
                             "(default: <spec.target_lib_path>/<spec.name>.kicad_sch)")
    parser.add_argument("--schema", type=Path, default=None,
                        help="path to block_spec.schema.json "
                             "(default: next to this script)")
    args = parser.parse_args()

    if not args.spec.exists():
        print(f"x spec not found: {args.spec}", file=sys.stderr)
        return 2

    schema_path = args.schema or Path(__file__).parent / "block_spec.schema.json"
    if not schema_path.exists():
        print(f"! schema not found at {schema_path}; skipping schema check",
              file=sys.stderr)
        schema_path = None

    spec = load_spec(args.spec)
    if schema_path:
        validate_against_schema(spec, schema_path)

    out_path = args.out
    if out_path is None:
        out_path = Path(spec["target_lib_path"]) / f"{spec['name']}.kicad_sch"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    sch = ksa.create_schematic(spec["name"])
    sch.set_title_block(title=spec["name"],
                        comments={1: spec["description"]})
    if "schematic_size" in spec:
        sch.set_paper_size(spec["schematic_size"])

    build_components(sch, spec)
    power_refs = build_power_symbols(sch, spec)
    port_anchors = build_ports(sch, spec)
    build_connections(sch, spec, port_anchors, power_refs)

    n_flags = auto_add_pwr_flags(sch, spec)

    sch.save_as(str(out_path))

    n_comp = len(spec["components"])
    n_port = len(spec["ports"])
    n_pwr = len(spec.get("power_symbols", []))
    n_conn = len(spec["connections"])
    print(f"v Generated '{spec['name']}': "
          f"{n_comp} components, {n_port} ports, {n_pwr} power symbols, "
          f"{n_conn} connections, {n_flags} auto PWR_FLAGs")
    print(f"  -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
