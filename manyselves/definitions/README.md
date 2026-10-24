# Shared definition libraries

A capability lists a library under `libraries:` in `capability.yaml`; the
library's definitions join that capability's registry. A library holds only
task-neutral definitions that two or more capabilities share. Nothing here
may name a domain field, module, role or output format.

`standard/`:

- `contracts/review-finding.yaml`: one reviewer finding about one record (problem, severity, suggestion).
- `contracts/review-findings.yaml`: a reviewer's submission, keyed by the record each finding is about, so one record gets at most one finding.
- `recovery/typed-turn-recovery.yaml`: the default recovery for typed Agent turns.
