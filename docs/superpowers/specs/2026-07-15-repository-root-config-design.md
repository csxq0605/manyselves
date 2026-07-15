# Repository-root AutoReport configuration

## Goal

AutoReport must always load and save one configuration file inside this repository, regardless of the directory from which the executable is launched. The canonical file is:

`/Users/zzymima0000/Documents/Codex/autoreport-power-distribution/autoreport.config.yaml`

No default configuration, cache, or migration file will be created outside the repository by this change.

## Design

`autoreport.config.schema` will derive the repository root from the installed source file location and expose a repository-root default configuration path. `Settings.config_path` will use that absolute path as its default instead of the current working directory relative `Path("autoreport.config.yaml")`.

Callers that explicitly pass `config_path` keep their current behavior. This preserves isolated test configurations and any intentional embedding use case while making the normal GUI startup independent of the shell working directory.

The existing repository-root `autoreport.config.yaml` remains the canonical user configuration. The stray `autoreport/autoreport.config.yaml` created by the old behavior will no longer be read automatically; it will not be deleted as part of the code change because it contains user credentials.

## Data flow

1. Normal startup creates `ConfigManager()`.
2. `ConfigManager` creates `Settings()` without an explicit path.
3. `Settings` resolves the absolute repository-root configuration path.
4. Loading, validation, and saving all use that same absolute path, regardless of `cwd`.
5. Tests or integrations may still provide an explicit path, which takes precedence.

## Error handling and security

The configuration file remains covered by `.gitignore` because it contains API keys. Logs may include the configuration path but must not include configuration contents or credentials. Existing YAML loading and validation behavior is unchanged.

## Verification

A regression test will change the process working directory and assert that separate `Settings()` instances still select the same repository-root configuration file. Existing tests will verify that an explicit temporary `config_path` still overrides the default. Focused configuration tests and the full test suite will run after implementation.
