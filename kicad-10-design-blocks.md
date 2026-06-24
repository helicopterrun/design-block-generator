# KiCad 10 Design Blocks — structure, conventions, gotchas

Reference for the design-block-generator skill. Things that aren't immediately obvious from the KiCad UI.

## What a design block actually is

A KiCad 10 design block is, on disk, a **directory** named `<Block Name>.kicad_block/` containing two files:
- `<Block Name>.kicad_sch` — a normal schematic. Its **hierarchical labels** are the block's external connection points; when the block is placed in another schematic they become its pins (the label's electrical type drives ERC).
- `<Block Name>.json` — block metadata: `{"description": "...", "keywords": "...", "fields": {}}`.

> **Verified against KiCad 10.0 on this machine.** (The earlier "a block is just a bare `.kicad_sch`" is the KiCad-9-era shape — no longer accurate.) `kicad-cli` has no design-block command, so blocks are created in the GUI ("Save Selection as Design Block") or by writing this directory structure directly — see `extract_block.py`, which captures a finished hierarchical sheet into a native block.

## Library structure on disk

A design block library is a directory whose name ends in **`.kicad_blocks`**; inside it, each block is its own `.kicad_block/` directory:

```
<Library>.kicad_blocks/
├── Buck AP63203 12V to 3V3.kicad_block/
│   ├── Buck AP63203 12V to 3V3.kicad_sch
│   └── Buck AP63203 12V to 3V3.json
├── LDO 3V3 AMS1117.kicad_block/
│   └── ...
└── ...
```

The `.kicad_block` directory name (minus the extension) is the block's name in the picker; the `<name>.kicad_sch` and `<name>.json` inside must share that exact name.

## Library tables

Two scopes, same as symbol libraries:

- **Global** — `~/Library/Preferences/kicad/10.0/design-block-lib-table` (macOS; `~/.config/kicad/10.0/...` on Linux). Available to all projects.
- **Project-local** — a `design-block-lib-table` in the project directory. Only visible when that project is open.

Per-product blocks should live in project-local tables; cross-product blocks go global (and only after the promotion rule is met). Easiest way to register: the GUI — **Preferences → Manage Design Block Libraries → Add**, pointed at the `.kicad_blocks` directory.

The table is an S-expression — note the top symbol is `design_block_lib_table` with a `(version …)`:

```
(design_block_lib_table
	(version 7)
	(lib (name "example-board") (type "KiCad")
	     (uri "${KIPRJMOD}/blocks/example-board.kicad_blocks") (options "") (descr "…")))
```

`${KIPRJMOD}` resolves to the project root, keeping the entry portable.

`${KIPRJMOD}` resolves to the project root, so this entry stays portable across machines.

You normally don't write these by hand — KiCad's GUI manages them via Preferences → Manage Design Block Libraries. But if you want to commit pre-configured library tables to a repo (so collaborators get the libraries automatically), you can hand-edit the project-local file.

## Hierarchical labels = block ports

This is the key concept. Inside a design block schematic, every external connection point must be a hierarchical label, not a regular label. The hierarchical label's name becomes the port name when the block is placed; the label's electrical type (input / output / bidirectional / power_in / power_out / passive) becomes the pin's electrical type for ERC.

```
(hierarchical_label "VOUT_3V3" (shape output) (at 190 80 0)
  (effects (font (size 1.27 1.27)) (justify left))
  (uuid "..."))
```

Regular labels work for naming internal nets, but they don't appear when the block is instantiated.

The skill's spec format collapses this distinction: `ports[]` becomes hierarchical labels, `labels[]` becomes regular labels. Don't put internal net names in `ports[]`.

## Power symbols are global, not interface

Power symbols (`+3V3`, `GND`, `+5V`) are global nets. They connect to anything else with the same power symbol anywhere in the project, including inside other design blocks. This is convenient (you don't have to wire GND through every level of hierarchy) but it has a sharp edge:

If your block uses `+5V` internally, and the parent schematic *also* has a `+5V` rail, they're the same net. There's no isolation. Don't use power symbols for nets you want to remain block-internal — use named labels or, better, distinctly-named hierarchical labels.

This is why `examples/ldo_3v3.json` makes `VIN_5V` and `VOUT_3V3` ports (so the parent decides what 5V/3V3 they connect to) but uses GND as a power symbol (where global ground is fine and conventional).

## Versioning and updates

Design blocks aren't versioned by KiCad. When you change a block's `.kicad_sch`, every existing instance in any schematic continues to reference the file by path — but won't auto-update. KiCad has an "Update Design Block from Library" command that pulls the current version into a specific instance, similar to the symbol update flow.

For this skill's workflow:

- Every block change is a git commit. The file's git history *is* its version history.
- For backwards-incompatible changes (renamed ports, removed pins), bump the block's filename: `ldo_3v3.kicad_sch` → `ldo_3v3_v2.kicad_sch`. Keep the old block until all consumers have migrated.

## ERC inside a design block

`kicad-cli sch erc <block>.kicad_sch` works on a design block, but with caveats: it can't check connections that go through hierarchical labels (those are by definition unresolved at the block level), so it'll flag every hierarchical label as "unconnected." Run ERC with the `--severity-error` filter only — warnings about hierarchical labels are expected at the block level and resolve when the block is instantiated.

## Symbol library availability

A design block references symbols by `lib_id` (e.g., `Regulator_Linear:AMS1117-3.3`). When the block is opened, KiCad needs that library to be in the symbol library table — global or project-local.

For maximum portability of generated blocks:

- Prefer stock KiCad libraries (`Device:`, `Connector:`, `Regulator_Linear:`, etc.). They're guaranteed available.
- For non-stock symbols, place them in `<lib-dir>/lib/symbols.kicad_sym` alongside the blocks, and reference via a project-local symbol library table that uses `${KIPRJMOD}`.
- The pre-flight validator does NOT check symbol availability — kicad-sch-api MCP fails at generation time if a symbol can't be resolved. If a block fails to generate due to a missing `lib_id`, that's the failure mode.

## Limitations of this skill's coverage

- **No PCB side.** KiCad 10 design blocks can include PCB layout (a block can ship as schematic + matched layout, useful for RF or strict-impedance subcircuits). This skill produces schematic-only. Layout reuse for now stays manual.
- **No automatic sub-block hierarchy.** A design block can itself instantiate other design blocks. This skill emits flat blocks. To compose blocks, generate them individually and wire them together in a parent schematic by hand.
- **No simulator stubs.** KiCad 10 supports SPICE simulation models attached to symbols. The skill doesn't emit SPICE model bindings — add them manually in the symbol properties if needed.
