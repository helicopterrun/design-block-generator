#!/usr/bin/env python3
"""extract_block.py — capture an existing hierarchical sheet as a NATIVE KiCad 10 design block.

The reverse of generate_block.py: instead of spec-JSON -> block, this takes a finished
leaf `.kicad_sch` (whose hierarchical labels already ARE its inter-block interface) and
writes it as a native KiCad 10 design block, so it shows up in the GUI design-block
browser and can be dropped into the next board. This is how you harvest proven
subcircuits off a board you just finished.

Native on-disk format (verified against KiCad 10.0 — `kicad-cli` has no block command):

  <Library>.kicad_blocks/                 a design-block LIBRARY directory; register it
    <Block Name>.kicad_block/                in the design-block-lib-table (global or
      <Block Name>.kicad_sch                 project). NOTE: each block is a *directory*.
      <Block Name>.json                   {"description","keywords","fields"}

The block schematic's hierarchical labels become the block's ports/pins on placement.

  extract_block.py --source path/to/buck.kicad_sch \
      --lib  /path/to/MyLib.kicad_blocks \
      --name "Buck AP63203 12V to 3V3" \
      [--description "..."] [--keywords "buck 3v3 ..."] [--force] [--erc]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

_V9, _V10 = "20250114", "20260306"


def _patch_v10(text: str) -> tuple[str, bool]:
    """kicad-sch-api emits KiCad-9 markers that KiCad 10 silently wipes. Patch them if
    present. Returns (text, was_patched)."""
    patched = text.replace(f"(version {_V9})", f"(version {_V10})")
    patched = re.sub(r'\(generator_version\s+"9\.0"\)', '(generator_version "10.0")', patched)
    return patched, (patched != text)


def _ports(text: str) -> list[str]:
    return sorted(set(re.findall(r'\(hierarchical_label\s+"([^"]+)"', text)))


def _refs(text: str) -> list[str]:
    refs = re.findall(r'\(property "Reference" "([^"]+)"', text)
    # drop power/flag refs (#PWR…) and the bare symbol-class names (R/C/U…) from lib_symbols
    return sorted({r for r in refs if not r.startswith("#") and not re.fullmatch(r"[A-Za-z]+", r)})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, help="leaf .kicad_sch to capture")
    ap.add_argument("--lib", required=True, help="target *.kicad_blocks library directory")
    ap.add_argument("--name", required=True, help="block name (becomes the .kicad_block dir + files)")
    ap.add_argument("--description", default="", help="block description (shown in the browser)")
    ap.add_argument("--keywords", default="", help="space-separated search keywords")
    ap.add_argument("--force", action="store_true", help="overwrite an existing block")
    ap.add_argument("--erc", action="store_true", help="run kicad-cli ERC on the captured block")
    args = ap.parse_args()

    src = Path(args.source)
    if not src.exists():
        print(f"error: source not found: {src}", file=sys.stderr)
        return 2
    lib = Path(args.lib)
    if lib.suffix != ".kicad_blocks":
        print("error: --lib must be a directory ending in .kicad_blocks", file=sys.stderr)
        return 2

    text, patched = _patch_v10(src.read_text())
    ports = _ports(text)
    refs = _refs(text)

    block_dir = lib / f"{args.name}.kicad_block"
    if block_dir.exists() and not args.force:
        print(f"error: {block_dir} already exists (use --force)", file=sys.stderr)
        return 2
    block_dir.mkdir(parents=True, exist_ok=True)
    sch_out = block_dir / f"{args.name}.kicad_sch"
    sch_out.write_text(text)
    (block_dir / f"{args.name}.json").write_text(
        json.dumps({"description": args.description, "keywords": args.keywords, "fields": {}},
                   indent=0) + "\n")

    print(f"wrote design block: {block_dir}")
    if patched:
        print("  (patched KiCad-9 version markers -> 10.0)")
    print(f"  ports ({len(ports)}): {', '.join(ports) or '(none — block will have no pins!)'}")
    print(f"  parts ({len(refs)}): {', '.join(refs) or '(none)'}")
    if not ports:
        print("  warning: no hierarchical labels — add them in the source sheet first.",
              file=sys.stderr)

    if args.erc:
        rpt = block_dir / f"{args.name}.erc.rpt"
        r = subprocess.run(
            ["kicad-cli", "sch", "erc", "--severity-error", "--output", str(rpt), str(sch_out)],
            capture_output=True, text=True)
        # a standalone block reports one pin_not_connected per port (no parent) — expected.
        unconn = rpt.read_text().count("pin_not_connected") if rpt.exists() else "?"
        print(f"  ERC ran (exit {r.returncode}); {unconn} port 'unconnected' notice(s) — "
              "expected for a standalone block, they resolve when it's placed.")
        rpt.unlink(missing_ok=True)

    print("Register the library once (KiCad closed), adding to design-block-lib-table:")
    print(f'  (lib (name "{lib.stem}") (type "KiCad") (uri "{lib}") (options "") (descr "{args.description}"))')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
