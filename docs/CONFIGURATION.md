# Configuration

ManySelves separates configuration into three scopes:

1. **Process/server settings** — environment variables for networking, storage, authentication, and bootstrap secrets.
2. **Runtime settings** — `manyselves.config.yaml` for providers and Agent defaults.
3. **Capability definitions** — versioned Markdown/YAML identities, Skills, schemas, policies, and templates consumed by the runtime.

Secrets belong in environment variables or runtime-managed private configuration, never in Identity or Skill documents.

## Files

| File | Purpose | Commit active values? |
| --- | --- | --- |
| `manyselves.config.example.yaml` | Local runtime template | Yes |
| `manyselves.config.yaml` | Active local runtime configuration | No |
| `.env.example` | Local web/server environment template | Yes |
| `.env` | Active local environment | No |
| `deploy/env.example` | Compose environment template | Yes |
| `deploy/.env` | Active Compose environment | No |
| `deploy/accounts.example.yaml` | Multi-account manifest template | Yes |
| `deploy/accounts.yaml` | Active account manifest | No |

Initialize only the mode you use:

```bash
cp manyselves.config.example.yaml manyselves.config.yaml
cp .env.example .env
cp deploy/env.example deploy/.env
chmod 600 deploy/.env
```

## Runtime and provider configuration

Minimal `manyselves.config.yaml`:

```yaml
agents:
  defaults:
    model: openai/example-model
    provider: auto
    temperature: 0.1
    max_tokens: 16384
    max_tool_iterations: 200
    timezone: Asia/Shanghai

providers:
  active: example-provider
  configurations:
    - id: example-provider
      name: Example OpenAI-compatible provider
      provider: openai
      api_key: null
      api_base: https://provider.example/v1
      default_model: example-model
      enabled: true
```

`providers.active` is the unique configuration ID, not a protocol name or preset ID.

Supported provider keys can be supplied through the settings UI or environment variables:

```bash
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
DEEPSEEK_API_KEY=
OPENROUTER_API_KEY=
MIMO_API_KEY=

# Compose/server-facing equivalents
MANYSELVES_OPENAI_API_KEY=
MANYSELVES_ANTHROPIC_API_KEY=
MANYSELVES_DEEPSEEK_API_KEY=
MANYSELVES_OPENROUTER_API_KEY=
MANYSELVES_MIMO_API_KEY=
```

OpenAI-compatible bootstrap example:

```bash
MANYSELVES_BOOTSTRAP_PROVIDER=openai
MANYSELVES_BOOTSTRAP_API_BASE=https://provider.example/v1
MANYSELVES_BOOTSTRAP_MODEL=example-model
MANYSELVES_OPENAI_API_KEY=replace-me
```

Environment-owned keys take precedence over YAML, are not returned to the browser, and require a process/container restart after replacement.

## Web/server settings

Direct Python process:

```bash
MANYSELVES_DATA_ROOT=.manyselves
MANYSELVES_HTTP_BIND=127.0.0.1
MANYSELVES_HTTP_PORT=9090
MANYSELVES_ALLOWED_ORIGINS='["http://127.0.0.1:9090"]'
MANYSELVES_ADMIN_USERNAME=admin
MANYSELVES_ADMIN_PASSWORD=replace-with-a-strong-password
```

For Compose, `MANYSELVES_DATA_DIR` is the host directory mounted at `/data/manyselves`; `deploy/compose.yaml` sets the container's `MANYSELVES_DATA_ROOT` to that mount.

Use the exact browser origin. Do not use `*` with authenticated browser sessions. Replace development credentials before any shared or networked deployment.

## Single-account and multi-account modes

Without `MANYSELVES_ACCOUNTS_FILE`, the server creates one `default` account from `MANYSELVES_ADMIN_USERNAME` and `MANYSELVES_ADMIN_PASSWORD`.

Multi-account manifest:

```yaml
version: 1
accounts:
  - id: account-a
    username: alice
    passwordEnv: MANYSELVES_ACCOUNT_A_PASSWORD

  - id: account-b
    username: bob
    passwordEnv: MANYSELVES_ACCOUNT_B_PASSWORD
```

Set the referenced passwords in the process environment:

```bash
export MANYSELVES_ACCOUNT_A_PASSWORD='replace-me'
export MANYSELVES_ACCOUNT_B_PASSWORD='replace-me'
export MANYSELVES_ACCOUNTS_FILE=/absolute/path/to/accounts.yaml
chmod 600 /absolute/path/to/accounts.yaml
```

The manifest contains IDs, usernames, and password environment-variable names only. It must not contain provider API keys. On non-Windows systems it must be mode `0600`; multi-account mode requires one worker.

Each account receives an independent root:

```text
<data-root>/
└── accounts/
    ├── account-a/
    └── account-b/
```

Each account owns its own `RuntimeHost`, provider configuration, project registry, message bus, event broker, and workflow state.

## Project state

A selected project normally contains:

```text
project/
├── Inputs/
├── Knowledge/
├── Templates/
├── Work/
└── Outputs/
```

`Work/` is authoritative runtime state, not temporary documentation. Do not remove it while a task may need resume, audit, or rollback.

## Current Identity contract

Packaged reporting identities load from `manyselves/templates/reporting/agents/*.md`. Each file contains YAML frontmatter followed by instructions:

```markdown
---
name: reviewer
description: Review one result against evidence and policy.
model: inherit
tools: [read]
disallowedTools: [delete_file]
maxTurns: 8
maxTokens: 16384
effort: high
memory: task
background: true
reads: [module_drafts, claim_ledger]
writes: [review_findings]
---

Review the assigned result and return a structured finding record.
```

The current loader validates names/descriptions, model policy, tool grants, turn/token limits, effort, memory scope, background execution, named read/write carriers, and the instruction body. Unknown carriers and overlapping allowed/disallowed tools are rejected before execution.

This is the **implemented** Identity format. A repository-wide capability manifest that declares workflow topology, gates, schemas, policies, compatibility, and extensions is the target architecture described in the main README; it is not yet a fully implemented loader contract.

## Skills and templates

- Packaged Skills: `manyselves/templates/reporting/skills/`
- Agent identities: `manyselves/templates/reporting/agents/`
- Delivery templates: `manyselves/templates/reporting/`
- Product capability material: `ProductCapabilities/skills/`

These files are runtime inputs and should remain versioned even though ordinary design and handoff documents are removed.

## Precedence and safety

Practical provider-secret precedence:

1. server/process environment,
2. runtime-managed private configuration,
3. committed template defaults.

Operational rules:

- Never commit real keys, passwords, `.env`, `manyselves.config.yaml`, or active account manifests.
- Restrict account manifests and environment files to the service account.
- Use HTTPS beyond a trusted local environment and configure an exact allowed origin.
- Keep one writable data root per account or Compose stack.
- Back up the data root before upgrades.
- Do not edit or delete active `Work/` state.
