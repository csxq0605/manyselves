# Manyselves brand assets

The master mark represents one stable workspace assembled from several distinct
selves. Its four facets form a shared center without using industry-specific
symbols, so the product identity remains reusable when the agent documents and
skills change.

## Files

- `manyselves-mark.png`: transparent master mark generated with OpenAI ImageGen.
- `manyselves-mark-chroma.png`: original generated source retained for provenance.
- `manyselves-app-icon.png`: deterministic application-icon export.
- `../screenshots/title.png`: repository banner.
- `../diagrams/architecture.svg`: current runtime architecture diagram used in README.
- `../screenshots/workflow.png`: legacy identity-to-deliverable sketch (superseded by architecture.svg).

Run `uv run python scripts/build_brand_assets.py` to rebuild deterministic
exports after changing the master mark.

## Palette

- Night: `#090B1A`
- Indigo: `#696CF5`
- Violet: `#9A63F7`
- Cyan: `#2FD8EF`
- Paper: `#F7F8FF`

Generated master-mark prompt: create a clean geometric symbol for a local agent
workspace, with several rounded facets converging around one shared center; no
text, people, robots, power symbols, laboratory imagery, formulas, or document
sheet icons.
