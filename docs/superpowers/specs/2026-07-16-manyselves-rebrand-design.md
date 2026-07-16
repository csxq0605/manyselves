# Manyselves Rebrand Design

## Objective

Rebrand the product from AutoReport to **Manyselves** without presenting the
current power-distribution reporting package as the boundary of the product.
Manyselves is a local workspace whose Agent identities, responsibilities,
knowledge, and delivery behavior are defined by documents and Skills.

The public brand promise is:

> **One runtime. Many selves.**

The explanatory descriptor is:

> **A local workspace for document-defined agent teams.**

## Product Positioning

The reusable product is the desktop runtime: project workspaces, conversations,
tools, providers, task coordination, checkpoints, previews, and durable state.
Markdown identity documents, reporting Agent definitions, Skills, templates,
and workflow configuration turn that runtime into a particular professional
team. Power-distribution reporting is the first bundled domain package and a
concrete example, not the product's permanent identity.

User-facing copy must therefore lead with the configurable workspace and
describe the distribution-reporting system as a bundled capability. It must not
claim that Manyselves is only a report generator, a physics-experiment tool, or
an electrical-industry application.

## Naming System

- Public product name: `Manyselves`
- Primary command: `manyselves`
- Compatibility command: `autoreport`
- Tagline: `One runtime. Many selves.`
- English descriptor: `A local workspace for document-defined agent teams.`
- Chinese descriptor: `由文档定义 Agent 团队的本地工作空间`

Window titles, welcome and onboarding screens, menus, CLI help, package
metadata, user-facing prompts, screenshot captions, and README prose use
Manyselves.

The existing Python import package `autoreport`, the legacy `autoreport` CLI,
the repository-root `autoreport.config.yaml`, and project-local `.autoreport`
state directory remain compatible. They are implementation and migration
surfaces, not the public brand. The canonical configuration location remains
inside the repository and independent of the process working directory.

## Visual Identity

The icon represents one stable runtime becoming several identities. It uses a
single central form that opens into multiple connected facets. It must remain
recognizable at 16 px and must not contain a robot face, chat bubble, report
page, electrical symbol, physics formula, or LaTeX mark.

The visual system uses:

- midnight indigo for the stable runtime;
- electric violet for differentiated identities;
- clear cyan for connection and active work;
- restrained off-white for documentation surfaces.

The icon asset is square and suitable for the application window. The brand
banner combines the icon, Manyselves wordmark, tagline, and descriptor. The
workflow graphic depicts document definitions and capabilities entering the
runtime, a configurable Agent team collaborating, and project artifacts leaving
the workspace.

## Asset Scope

The rebrand replaces or removes every misleading legacy asset:

- `autoreport/resources/icon.png` becomes the Manyselves application icon;
- `assets/screenshots/title.png` becomes the Manyselves brand banner;
- `assets/screenshots/workflow.png` becomes the domain-neutral product model;
- screenshots that visibly show AutoReport, physics experiments, LaTeX, or the
  old project layout are regenerated when they reflect the current application;
  otherwise they are removed from the README instead of being presented as
  current product evidence.

The generic provider configuration screenshot may remain only if its visible
content still matches the current application and contains no legacy brand.

## Documentation Scope

`README.md` and `README_zh.md` are rewritten around the same structure:

1. brand promise and configurable-workspace overview;
2. how documents, Skills, tools, and workflows define an Agent team;
3. core local runtime capabilities;
4. bundled power-distribution reporting capability;
5. installation and the new primary command;
6. compatibility notes for existing AutoReport projects;
7. architecture, development, and license information.

Outdated upstream screenshots, physics-experiment descriptions, stale agent
lists, LaTeX workflow claims, and upstream AutoReport star-history marketing are
removed. Historical attribution remains factual and concise: AutoReport is the
implementation origin, not the active product name or a runtime dependency.

Agent instruction documents are changed only where they describe the hosting
product. Domain-specific instructions remain domain-specific because they are
the bundled power-distribution capability, not global product copy.

## Compatibility and Migration

The rebrand must not invalidate existing projects or force users to re-enter
configuration. The implementation therefore:

- adds `manyselves` as the canonical CLI entry point;
- retains `autoreport` as a compatibility alias;
- keeps the `autoreport` Python package import path;
- keeps reading the repository-root `autoreport.config.yaml`;
- keeps project-local `.autoreport` state and conversation directories;
- does not rename existing customer project folders or generated report files;
- preserves explicit configuration path overrides used by tests and special
  deployments.

No compatibility surface is advertised as the preferred new brand.

## Verification

Completion requires all of the following evidence:

- a repository-wide search shows no unintended user-facing `AutoReport` or
  physics-experiment branding;
- intentional compatibility occurrences are documented or code-level only;
- both `manyselves --help` and `autoreport --help` start the same application;
- brand-related GUI and CLI tests pass;
- the application icon and brand assets are inspected at full size and small
  size;
- Markdown links and local assets referenced by both READMEs exist;
- the focused test suite and the full suite pass with
  `QT_QPA_PLATFORM=offscreen`;
- Ruff passes without folding unrelated dependency or lockfile changes into the
  rebrand.

## Non-Goals

- Renaming the internal Python package in this iteration.
- Reformatting domain report outputs that do not expose the application brand.
- Rewriting the working power-distribution workflow into a generic workflow
  engine as part of the rebrand.
- Reverting, replacing, or committing unrelated work already present in the
  working tree.
