# Reporting fixture policy

Unit tests build minimal synthetic workbooks under pytest temporary directories. The real Phase A integration test reads the three V2 handoff workbooks from `/Users/zzymima0000/Documents/Codex/work/写作上传材料`, copies them into the test project's `Inputs/`, and never writes to the handoff directory. The test is skipped when those local files are unavailable.
