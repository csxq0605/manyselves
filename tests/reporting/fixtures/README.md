# Reporting fixture policy

Unit tests build minimal synthetic workbooks under pytest temporary directories. The real workbook integration test reads the three V2 handoff workbooks from `/Users/zzymima0000/Documents/Codex/work/写作上传材料`, copies them into the test project's `Inputs/`, and never writes to the handoff directory. The test is skipped when those local files are unavailable.

`report-730c5d83f6-table-placement.json` is a small, content-reduced regression fixture derived from the archived edited submission of that run. It preserves only the handwritten/structured-table collision needed to verify deterministic, single-placement no-model rerendering; tests never read or modify the archived run.
