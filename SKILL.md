---
name: design-block-generator
description: Generate KiCad 10 design blocks (reusable schematic fragments) from a screenshot of a circuit, a textual description, or an existing schematic. Use this skill whenever the user wants to create, capture, or "make a design block" for a subcircuit (LDO, buck converter, USB-C input, MCU minimum-system, op-amp stage, ESD protection, etc.); when the user shows a screenshot of a schematic and asks Claude to recreate it as a reusable block; or when the user says things like "turn this into a block," "save this as a building block," "add this to my design block library." Companion to kicad-product-workflow — that skill handles full products and boards; this one handles the smaller reusable pieces. Trigger this even if the user doesn't say "design block" explicitly, when the request is clearly to capture a subcircuit for reuse rather than design a complete board. Requires the kicad-sch-api Python library; install once via install.sh.
---

# KiCad 10 Design Block Generator

Captures a subcircuit — from a screenshot, a description, or an existing schematic — as a KiCad 10 design block (`.kicad_sch` file in a design block library), with approved-parts validation gates on both ends.

## Architecture

```
input (screenshot/description)
    │
    ▼
Claude analysis ──────► structured spec (JSON)
                                │
                                ▼
                    validate_block_spec.py   ◄── approved_parts.csv
                                │  (pre-flight: schema + MPN allow-list)
                                ▼
                    generate_block.py        ◄── kicad_sch_api Python lib
                                │  (one-shot schematic generation)
                                ▼
                       <name>.kicad_sch   ──► design block library
                                │
                                ▼
                    kicad-cli sch erc   +   check_approved_parts.py
                       (post-flight: ERC + final MPN check)
```

The intermediate JSON spec is the contract. Everything before it is judgment (Claude looking at a screenshot); everything after it is mechanical (validator + generator).

## Prerequisites

Run `install.sh` once. It:

1. Installs `kicad-sch-api` from PyPI (used by `generate_block.py`)
2. Registers the kicad-sch-api MCP server with Claude Code (used for interactive editing of existing blocks; not the primary generation path)
3. Verifies `kicad-cli` is on PATH (KiCad 10+)

After install, restart Claude Code so the MCP server is registered. The MCP server is optional for generation — `generate_block.py` drives the underlying Python library directly.

## Workflow

### Trigger phrases

- "Make a design block for this [LDO / buck / USB-C / MCU minimum / op-amp / ...]"
- "Recreate this circuit as a reusable block" + screenshot
- "Add this subcircuit to my block library"
- "I want to reuse this circuit across products"
- A screenshot of a schematic with no other instruction (ask whether they want it captured as a block)

### When NOT to use this skill

- Full-product or full-board scaffolding → use `kicad-product-workflow`
- Schematic review or critique without recreation → answer directly
- Component selection / sizing / value calculation questions → answer directly
- Editing an existing design block → use kicad-sch-api MCP directly (or `kicad_sch_api` Python lib) to load and modify; this skill is for fresh generation
- Anything for KiCad 8 or earlier (design blocks are KiCad 9+, this skill targets v10)

### Step 1 — Analyze the input

If a screenshot: identify every component, its reference designator, value, and (if visible) MPN. Identify all nets and connections. Identify which nets are external ports (entering/leaving the block) vs. internal.

If a description: ask for the missing specifics before proceeding — supply voltage, output spec, tolerance class, any specific MPNs the user wants. Don't guess silently on values that drive part selection.

If neither is enough to fully specify the block, stop and ask. Don't fabricate components.

### Step 2 — Produce a structured spec

Output a JSON object matching `block_spec.schema.json`. The spec format is documented in the schema; the gist:

- `name` — block identifier (kebab-case)
- `description` — one-line summary
- `target_lib_path` — absolute or product-relative path to the design block library directory (ask the user if not specified; default to `<product>/hardware/blocks/`)
- `ports` — external connections, each with name, side (left/right/top/bottom), and electrical type
- `components` — every part, with `ref`, `lib_id` (KiCad symbol library identifier like `Regulator_Linear:AMS1117-3.3`), `value`, `mpn`, `footprint`, and `position` (mm, schematic coords)
- `connections` — wire-level netlist; each connection lists endpoints as either `{ref, pin}` or `{port: name}`
- `power_symbols` — explicit GND/VCC symbols at given positions
- `labels` — net labels at given positions

See `ldo_3v3.json` for a complete worked spec.

Note: don't bother declaring PWR_FLAGs in the spec. The generator auto-adds them on input rails (any net with Power-input pins and no Power-output driver) so standalone ERC won't spuriously flag those rails as undriven.

#### Layout hints

Place ports on the named side of the block, components in a roughly left-to-right signal flow. Standard grid is 2.54 mm (100 mil). Power symbols go above (VCC) or below (GND) their respective rails. Don't sweat optimal placement — the user can move things in KiCad. The goal is a syntactically correct, electrically correct block; aesthetic refinement is post-generation.

### Step 3 — Pre-flight validation

Run `validate_block_spec.py`:

```bash
python3 validate_block_spec.py \
    --spec /tmp/block_spec.json \
    --schema block_spec.schema.json \
    --approved <product>/approved_parts.csv
```

For standalone or shared blocks not tied to a specific product, swap `--approved` for `--skip-mpn-gate`:

```bash
python3 validate_block_spec.py \
    --spec /tmp/block_spec.json \
    --schema block_spec.schema.json \
    --skip-mpn-gate
```

This checks:

- Spec is well-formed against the JSON schema
- Every `mpn` in the spec is in `approved_parts.csv` (or one of the alt MPNs) — skipped under `--skip-mpn-gate`
- Every `ref` is unique within the block
- Every connection endpoint references a real component pin or declared port
- No floating ports (every declared port is connected to at least one component)

If validation fails, fix the spec — don't proceed to generation.

### Step 4 — Generate via `generate_block.py`

```bash
python3 generate_block.py --spec /tmp/block_spec.json
```

The script:

1. Re-validates the spec against the schema (defense-in-depth)
2. Creates the schematic, places components, hierarchical labels (ports), and power symbols
3. Wires every connection pin-to-pin (avoids the coordinate-based wires that don't reliably hit pin endpoints)
4. **Auto-adds PWR_FLAGs** on input rails (Power-input pins with no Power-output driver) so standalone ERC doesn't spuriously fail
5. Saves to `<spec.target_lib_path>/<spec.name>.kicad_sch` (override with `--out PATH`)

The generator drives `kicad_sch_api` directly — one Bash invocation per block, no MCP tool calls during generation.

If you need to **interactively edit an existing block** (rename a component, tweak a wire), use the kicad-sch-api MCP tools instead — those are still the right tool for incremental edits.

### Step 5 — Post-flight validation

After generation:

```bash
# (Skip if you used --skip-mpn-gate at pre-flight)
# Approved-parts check on the actual generated schematic
python3 <product>/tools/check_approved_parts.py \
    --schematic <target_lib_path>/<name>.kicad_sch \
    --approved  <product>/approved_parts.csv \
    --allow-missing-mpn false

# ERC on the design block (it's a valid .kicad_sch on its own)
kicad-cli sch erc \
    --severity-error --exit-code-violations \
    --output <target_lib_path>/<name>.erc.rpt \
    <target_lib_path>/<name>.kicad_sch
```

#### Expected standalone-ERC artifacts

A design block ERC'd in isolation will report **one `pin_not_connected` error per declared port**:

> Hierarchical label '\<NAME\>' in root sheet cannot be connected to non-existent parent sheet

These are inherent to standalone ERC — hierarchical labels reference a parent sheet that exists only when the block is instantiated as a sub-sheet. They disappear automatically when the block is dropped into a product schematic. **Treat them as a pass, not a blocker.**

Other ERC categories ARE real blockers:

- `power_pin_not_driven` — should not occur; the generator's auto-PWR_FLAG pass should cover this. If it appears, the block likely has an input rail the generator didn't recognize. Report.
- `pin_to_pin: Power output and Power output` — likely a bad spec or a bug in the generator. Report.
- floating wires, unrecognized symbols, missing footprints — fix in the spec and regenerate.

If only the expected hierarchical-label errors remain, the block is ready.

### Step 6 — Register the block in the library

If this is the first block in `target_lib_path`, the user needs to add the directory as a design block library in KiCad once:

> KiCad → Preferences → Manage Design Block Libraries → Add → point to `<target_lib_path>` → name it (e.g., `<product>-blocks`)

This is a one-time GUI step per library. Subsequent blocks in the same library are picked up automatically.

After that, the block is available in any schematic via Place → Design Block.

## Spec format quick reference

```json
{
  "name": "ldo_3v3_ams1117",
  "description": "AMS1117-3.3 5V→3.3V LDO with input/output decoupling",
  "target_lib_path": "hardware/blocks",
  "ports": [
    {"name": "VIN_5V",  "side": "left",  "type": "power_in"},
    {"name": "VOUT_3V3","side": "right", "type": "power_out"},
    {"name": "GND",     "side": "bottom","type": "ground"}
  ],
  "components": [
    {"ref": "U1", "lib_id": "Regulator_Linear:AMS1117-3.3",
     "value": "AMS1117-3.3", "mpn": "AMS1117-3.3",
     "footprint": "Package_TO_SOT_SMD:SOT-223-3_TabPin2",
     "position": [120, 80]}
  ],
  "connections": [
    {"from": {"port": "VIN_5V"},   "to": {"ref": "U1", "pin": "3"}},
    {"from": {"ref": "U1", "pin": "2"}, "to": {"port": "VOUT_3V3"}}
  ]
}
```

Full schema in `block_spec.schema.json`. Worked example in `ldo_3v3.json`.

## Integration with kicad-product-workflow

This skill assumes the product layout from `kicad-product-workflow`:

- Product root contains `approved_parts.csv` (this skill validates against it)
- Product root contains `tools/check_approved_parts.py` (this skill runs it for post-flight)
- Design blocks live under `hardware/blocks/` by convention (per-product library) or in a shared location like `~/Documents/KiCad/blocks/<org>/` for cross-product reuse

Per-product blocks come first. Promote to a shared library only after a block has been used in 2+ products without modification.

## Reference files

All files live flat at the skill root:

- `block_spec.schema.json` — JSON schema for design block specs
- `ldo_3v3.json` — worked AMS1117 LDO example
- `kicad-10-design-blocks.md` — KiCad 10 design block library structure, gotchas
- `install.sh` — one-time setup
- `validate_block_spec.py` — pre-flight validator
- `generate_block.py` — generator (driven directly, not via MCP)

## Limitations / future work

- **Symbol library availability:** every `lib_id` in the spec must resolve in the user's KiCad symbol library setup. The pre-flight validator does NOT check this — `generate_block.py` fails at generation time if a symbol is missing. If a needed symbol isn't available, either (a) install the relevant KiCad library, (b) use a project-local `lib/` symbol, or (c) pick a different MPN whose symbol exists.
- **Layout quality:** positions in the spec are produced by Claude's best guess, which is functional but not beautiful. Aesthetic refinement is a manual KiCad step after generation. A future iteration could add a layout pass using a library like `dagre` or a constraint solver.
- **No PCB side:** this skill produces schematic-only design blocks. KiCad 10 design blocks can include PCB layout, but that's out of scope here. Layout reuse is better handled by hierarchical PCB groups for now.
- **No iteration loop:** this is a one-shot generator. If the result is wrong, regenerate from a corrected spec or edit the `.kicad_sch` directly.
