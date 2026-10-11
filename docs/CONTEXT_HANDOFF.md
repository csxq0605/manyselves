# Context handoff policy

The default `model_summary` policy still asks the configured Provider to summarize
progress, decisions and remaining work when working memory is compacted. Exact
task identity, structured artifact/evidence references and tool paging coordinates
are extracted by the Runtime and retained independently of that model response.
Previous structured handoffs are included in subsequent compactions without a
second 160-character truncation. The summary input contains those handoffs and
only the complete history units selected for eviction, plus active task metadata.
Retained recent exchanges are not reread by the summary model.

`working_memory_tokens` is the trigger, not the desired size after compaction.
The default `working_memory_compaction_target_ratio: 0.6` targets 60% of that
threshold (21,600 estimated tokens for a 36,000 trigger), leaving room for new
tool exchanges. The Runtime reserves the current task boundary before filling
the remaining history budget. The eviction partition is frozen before requesting
the summary: a larger generated summary must not cause additional unsummarized
messages to be dropped. The ratio is a soft target, and a larger summary can
exceed it. It retains the latest complete protocol unit intact, even
when mandatory content prevents reaching the target. If no historical content
can be removed, it skips the useless summary request; this does not solve an
oversized current task input, which must be addressed at its source.

This ratio is configurable between 0 and 1, excluding both endpoints. Lower
values leave more headroom but retain fewer old messages verbatim, increasing
reliance on the semantic summary and readable artifacts. The estimate uses the
Runtime's existing character heuristic, not an exact model tokenizer.

If the complete serialized summary input (including tool arguments) exceeds the
estimated available window, or the summary fails, is malformed, is refused, or
hits its output limit, working history remains unchanged. No failed summary is
used as permission to discard raw history. This preserves evidence; it does not
solve a task whose required input itself exceeds the model window.

The default policy remains semantic summarization:

```yaml
agents:
  defaults:
    working_memory_tokens: 36000
    working_memory_compaction_target_ratio: 0.6
    context_compaction_strategy: model_summary
    context_compaction_model: claude-haiku-5-5
```

`context_compaction_model` is optional; `null` inherits the task model. It uses
the same Provider endpoint, credentials, SDK client, retry/admission machinery
and usage ledger. A request-local Provider copy prevents changing the model used
by other agents. Both admission and usage identify the summary model; the task
model is restored even on cancellation. The updated example config selects
`https://api.anthropic.com` with an empty key and `claude-haiku-5-5` for both tasks
and summaries. Set `api_key` or `ANTHROPIC_API_KEY` before starting a run.

Haiku 5.5 requests omit legacy sampling parameters. Independent summary requests
use adaptive thinking at low effort. Tool loops currently use the model's
supported disabled-thinking mode (medium effort by default), because this
Runtime does not yet preserve the signed thinking blocks required for adaptive
tool replay. A low-reasoning hint uses low effort in that mode. This is an
explicit compatibility limitation, not support for adaptive-thinking tool loops;
such support needs signed-block persistence and append-only/boundary handling.
See the official [migration guide](https://platform.claude.com/docs/en/models/haiku-5-5/migration-guide),
[thinking configuration](https://platform.claude.com/docs/en/build-with-claude/thinking),
and [tool round-trip rules](https://platform.claude.com/docs/en/build-with-claude/thinking-tool-workflows).

For an optional controlled comparison, set this field in the existing application
configuration (preserve its other settings):

```yaml
agents:
  defaults:
    context_compaction_strategy: structured_handoff
```

`structured_handoff` uses deterministic bounded state extraction and makes no
additional summary-model request. It still compacts working memory, preserves
complete recent tool exchanges, persists handoff state and allows readable
artifacts to be reopened. It does not disable ordinary task-model requests.
Set the field back to `model_summary` to restore the default policy. Both values
are persisted by the normal configuration manager; no UI control is added.

The structured policy is experimental, not a recommendation to remove semantic
summarization: a list of references and tool states may
omit useful reasoning that a semantic summary would retain. Neither policy
guarantees that every old detail remains model-visible, or that a model will use
an available source correctly. References in `input_refs` identify task inputs;
only entries in `readable_refs` are advertised as reopenable task artifacts.

Compare policies using identical frozen inputs and the approved private inference
environment. Record total elapsed time, Provider tokens and requests by phase,
repeated reads, interruptions/recovery, omitted abnormal measurements and whether
each report claim is supported by its cited source. Removing summary requests
alone does not establish lower total cost: later task turns may do more work.

Customer evidence search separately supports `query: ""` with optional exact
`module_id` / `submodule_id` filters and `offset` / `limit` (maximum 12). Follow
`next_offset` with the same query and filters until it is null. Counts describe
the normalized snapshot, not all material in the customer's original files.
Unfiltered enumeration includes unassigned records. A keyword miss never proves
that the customer failed to provide the requested information.
