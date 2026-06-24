#!/usr/bin/env python3
"""
validate_block_spec.py — pre-flight validation for a design block spec
before it gets fed to kicad-sch-api MCP.

Called by the design-block-generator skill with:
    python3 validate_block_spec.py \\
        --spec /tmp/block_spec.json \\
        --approved <product>/approved_parts.csv \\
        --schema templates/block_spec.schema.json

Checks performed (in order, fast-fail):
    1. JSON well-formedness
    2. Schema conformance (jsonschema)
    3. Reference designators are unique within the block
    4. Every component MPN is in the approved list (or has no MPN AND is
       a power-flag/test-point ref pattern that doesn't need one)
    5. Every connection endpoint resolves: ports must be declared,
       (ref, pin) endpoints must reference an existing component
    6. No port is left dangling (each declared port has ≥1 connection)
    7. No power symbol is referenced that wasn't declared

Exit codes:
    0  all checks pass
    1  validation failure (errors printed to stderr)
    2  invocation error (missing files, bad args)

Dependencies:
    - Python 3.8+
    - jsonschema (pip install jsonschema). Falls back to a structural-only
      check if jsonschema isn't available, with a warning.

This complements check_approved_parts.py from the kicad-product-workflow
skill — that one runs on the GENERATED .kicad_sch file as post-flight;
this one runs on the JSON spec as pre-flight, before generation.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path


# Refs that conventionally don't carry an MPN (power flags, test points,
# fiducials, mechanical-only). Mirrors the skip list in check_approved_parts.py.
NO_MPN_PREFIXES = ("#PWR", "#FLG", "TP", "FID", "H")

SHARED_APPROVED_FILENAME = "example-block-library/jlcpcb_basic_approved.csv"


# ---------------------------------------------------------------------------
# Approved-parts loading (mirrors check_approved_parts.py)
# ---------------------------------------------------------------------------

def _load_one_csv(csv_path: Path) -> set[str]:
    approved: set[str] = set()
    with csv_path.open(newline="") as f:
        # Strip leading comment lines so DictReader treats the header row correctly.
        rows = [ln for ln in f if not ln.lstrip().startswith("#")]
    reader = csv.DictReader(rows)
    for row in reader:
        mpn_cell = (row.get("mpn") or "").strip()
        if not mpn_cell or mpn_cell.startswith("#"):
            continue
        for key in ("mpn", "alt_mpn_1", "alt_mpn_2"):
            val = (row.get(key) or "").strip()
            if val and not val.startswith("#"):
                approved.add(val)
    return approved


def discover_shared_approved(start: Path) -> Path | None:
    """Walk up from `start` looking for example-block-library/jlcpcb_basic_approved.csv."""
    if os.environ.get("APPROVED_PARTS_NO_SHARED"):
        return None
    cur = start.resolve()
    if cur.is_file():
        cur = cur.parent
    for ancestor in [cur, *cur.parents]:
        candidate = ancestor / SHARED_APPROVED_FILENAME
        if candidate.is_file():
            return candidate
    return None


def load_approved_mpns(csv_paths: Path | list[Path]) -> set[str]:
    if isinstance(csv_paths, Path):
        csv_paths = [csv_paths]
    approved: set[str] = set()
    for path in csv_paths:
        n_before = len(approved)
        approved |= _load_one_csv(path)
        print(f"  loaded {len(approved) - n_before} MPNs from {path}",
              file=sys.stderr)
    return approved


# ---------------------------------------------------------------------------
# Validators — each appends to `errors`, returns nothing
# ---------------------------------------------------------------------------

def validate_schema(spec: dict, schema_path: Path, errors: list[str]) -> None:
    """Schema check via jsonschema. Soft-skip with warning if unavailable."""
    try:
        import jsonschema  # type: ignore
    except ImportError:
        print("⚠ jsonschema not installed — skipping schema check "
              "(pip install jsonschema for full validation)", file=sys.stderr)
        return

    with schema_path.open() as f:
        schema = json.load(f)

    validator = jsonschema.Draft202012Validator(schema)
    for err in validator.iter_errors(spec):
        loc = ".".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"schema: at {loc}: {err.message}")


def validate_unique_refs(spec: dict, errors: list[str]) -> None:
    seen: dict[str, int] = {}
    for c in spec.get("components", []):
        ref = c.get("ref", "")
        seen[ref] = seen.get(ref, 0) + 1
    for ref, n in seen.items():
        if n > 1:
            errors.append(f"refs: duplicate reference designator '{ref}' "
                          f"({n} components share it)")


def validate_mpns(spec: dict, approved: set[str], errors: list[str]) -> None:
    for c in spec.get("components", []):
        ref = c.get("ref", "?")
        mpn = (c.get("mpn") or "").strip()

        if any(ref.startswith(p) for p in NO_MPN_PREFIXES):
            continue  # power flags etc. legitimately have no MPN

        if not mpn:
            errors.append(f"mpn: {ref} ({c.get('value', '')}) has no MPN field")
            continue

        if mpn not in approved:
            errors.append(f"mpn: {ref} MPN '{mpn}' not in approved_parts.csv")


def validate_connections(spec: dict, errors: list[str]) -> None:
    component_refs = {c["ref"] for c in spec.get("components", [])}
    declared_ports = {p["name"] for p in spec.get("ports", [])}
    declared_powers = {p["net"] for p in spec.get("power_symbols", [])}
    port_usage: dict[str, int] = {p: 0 for p in declared_ports}

    for i, conn in enumerate(spec.get("connections", [])):
        for which in ("from", "to"):
            ep = conn.get(which, {})
            if "ref" in ep:
                if ep["ref"] not in component_refs:
                    errors.append(f"connections[{i}].{which}: "
                                  f"ref '{ep['ref']}' is not in components")
                # We don't validate pin numbers — that requires symbol lib lookup;
                # MCP generation will catch invalid pins.
            elif "port" in ep:
                if ep["port"] not in declared_ports:
                    errors.append(f"connections[{i}].{which}: "
                                  f"port '{ep['port']}' is not declared in ports[]")
                else:
                    port_usage[ep["port"]] += 1
            elif "power" in ep:
                if ep["power"] not in declared_powers:
                    errors.append(f"connections[{i}].{which}: "
                                  f"power net '{ep['power']}' "
                                  f"has no matching power_symbol")

    for port, count in port_usage.items():
        if count == 0:
            errors.append(f"ports: declared port '{port}' is not used "
                          f"in any connection (dangling)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--spec",     type=Path, required=True,
                   help="Block spec JSON file")
    p.add_argument("--approved", type=Path, default=None,
                   help="approved_parts.csv from the product root "
                        "(required unless --skip-mpn-gate is set)")
    p.add_argument("--schema",   type=Path, required=True,
                   help="block_spec.schema.json from this skill bundle")
    p.add_argument("--skip-mpn-gate", action="store_true",
                   help="skip the approved-parts MPN check; useful for "
                        "standalone or shared blocks not tied to a product")
    args = p.parse_args()

    if not args.skip_mpn_gate and args.approved is None:
        print("✗ --approved is required unless --skip-mpn-gate is set",
              file=sys.stderr)
        return 2

    paths_to_check = [(args.spec, "spec"), (args.schema, "schema")]
    if not args.skip_mpn_gate:
        paths_to_check.append((args.approved, "approved-parts CSV"))
    for path, label in paths_to_check:
        if not path.exists():
            print(f"✗ {label} not found: {path}", file=sys.stderr)
            return 2

    try:
        spec = json.loads(args.spec.read_text())
    except json.JSONDecodeError as e:
        print(f"✗ spec is not valid JSON: {e}", file=sys.stderr)
        return 1

    if args.skip_mpn_gate:
        approved: set[str] = set()
    else:
        sources = [args.approved]
        shared = discover_shared_approved(args.approved)
        if shared:
            sources.append(shared)
        approved = load_approved_mpns(sources)

    errors: list[str] = []
    validate_schema(spec, args.schema, errors)
    # Only run structural checks if schema passed — otherwise the spec
    # may not have the expected shape and we'd cascade nonsense errors.
    if not errors:
        validate_unique_refs(spec, errors)
        if not args.skip_mpn_gate:
            validate_mpns(spec, approved, errors)
        validate_connections(spec, errors)

    name = spec.get("name", "<unnamed>")
    n_comp = len(spec.get("components", []))
    n_port = len(spec.get("ports", []))
    n_conn = len(spec.get("connections", []))

    gate_note = (f"{len(approved)} approved MPNs"
                 if not args.skip_mpn_gate else "MPN gate skipped")
    print(f"Validated spec '{name}': "
          f"{n_comp} components, {n_port} ports, {n_conn} connections "
          f"({gate_note})")

    if errors:
        print(f"\n✗ {len(errors)} error(s):", file=sys.stderr)
        for e in errors:
            print(f"   {e}", file=sys.stderr)
        return 1

    print("\n✓ Pre-flight validation passed — spec ready for generation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
