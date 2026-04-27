# design-block-generator

A Claude Code skill that turns a screenshot of a circuit, a textual description, or an existing schematic into a KiCad 10 **design block** (`.kicad_sch` in a design block library), with approved-parts validation gates on both ends.

Companion to [`kicad-product-workflow`](../kicad-product-workflow/) — that skill handles full products and boards; this one handles individual reusable subcircuits.

## How it works

```
input → Claude analyzes → JSON spec → pre-flight validator → kicad-sch-api MCP → .kicad_sch → kicad-cli ERC + check_approved_parts.py
```

The intermediate JSON spec is the contract. Everything before it is judgment (Claude looking at the input); everything after is mechanical (validators and tool calls).

## Install

```bash
./scripts/install.sh
```

This installs the [`kicad-sch-api`](https://pypi.org/project/kicad-sch-api/) MCP server, registers it with Claude Code, and installs `jsonschema` for the pre-flight validator. Restart Claude Code afterwards. Verify with `claude mcp list` — you should see `kicad-sch-api` among the registered servers.

## Use

In Claude Code, paste a screenshot of a circuit and ask: "Make a design block for this." Or describe one: "Make me a design block for a USB-C 5V input with CC pulldowns and ESD protection." The skill triggers automatically on subcircuit-reuse phrasing.

The output lands in `<your-product>/hardware/blocks/<n>.kicad_sch` (or wherever you specify). Register the directory once in KiCad via Preferences → Manage Design Block Libraries, and the block is available in every schematic via Place → Design Block.

## Layout

```
design-block-generator/
├── SKILL.md                                # the skill itself (read by Claude)
├── README.md                               # this file (read by humans)
├── scripts/
│   ├── install.sh                          # one-shot setup
│   └── validate_block_spec.py              # pre-flight validator
├── templates/
│   └── block_spec.schema.json              # JSON schema for specs
├── examples/
│   └── ldo_3v3.json                        # worked AMS1117-3.3 LDO example
└── references/
    └── kicad-10-design-blocks.md           # KiCad 10 design block format reference
```

## Requirements

- KiCad 10.0+ (design blocks exist in 9 too, but this skill targets 10's library format)
- Python 3.10+
- Claude Code with `claude` on PATH
- A product repo following the `kicad-product-workflow` layout (specifically: an `approved_parts.csv` at the product root)
