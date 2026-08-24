# Defining a Manyselves team

Manyselves separates the stable desktop runtime from the documents that define a
team. This makes a new identity inexpensive when its work can use the existing
files, conversation, task, review, and delivery primitives.

## Document-only customization

Start with the document layer when the runtime already has the required tools:

1. Rewrite the Main identity in `manyselves/templates/agents/main_agent.md`.
2. Add specialist identity documents with explicit responsibilities, accepted
   inputs, required outputs, refusal boundaries, and handoffs.
3. Add Skill documents for domain methods, evidence rules, and acceptance checks.
4. Update the Main identity's activation rules so it routes matching work to the
   new team.
5. Test the assembled prompt and a representative project before distribution.

A good identity document says what the Agent owns and what it must not claim. A
good Skill document describes a repeatable method rather than a one-off answer.

## When Python is required

Extend the runtime only when the team needs a new executable capability, such as:

- parsing a new structured source format;
- enforcing typed state transitions or dependency rules;
- calling an external service;
- generating a new artifact type;
- validating invariants that cannot safely depend on prompt compliance.

Put reusable execution tools under `manyselves/runtime/tools/`. Capability-specific
typed state, Python tools, and file workflows belong to a dedicated Capability
package such as `manyselves/capabilities/distribution_reporting/`.

## Bundled team as an example

`manyselves/templates/reporting/` shows a larger team: intake, evidence
normalization, module specialists, auditors, reviewers, an editor, delivery, and
Skill governance. Its power-distribution vocabulary belongs to that capability
and should remain there. It should not leak into the Manyselves product name,
icon, generic welcome screens, or package description.

## Review checklist

- Visible product surfaces say Manyselves.
- Team documents define ownership and handoff boundaries.
- Domain terms stay inside the capability that needs them.
- Project data and generated state remain inside the selected repository/project.
- New tools validate paths and preserve explicit configuration overrides.
- A representative workflow is covered by tests and produces inspectable output.
