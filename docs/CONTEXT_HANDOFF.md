# Context handoff policy

The default `model_summary` policy still asks the configured Provider to summarize
progress, decisions and remaining work when working memory is compacted. Exact
task identity, structured artifact/evidence references and tool paging coordinates
are extracted by the Runtime and retained independently of that model response.
Previous structured handoffs are included in subsequent compactions.

`working_memory_tokens` is the trigger, not the desired size after compaction.
The default `working_memory_compaction_target_ratio: 0.6` targets 60% of that
threshold (21,600 estimated tokens for a 36,000 trigger), leaving room for new
tool exchanges. The Runtime reserves the current task boundary before filling
the remaining history budget and selects retained units again using the actual
model summary size. It retains the latest complete protocol unit intact, even
when mandatory content prevents reaching the target. If no historical content
can be removed, it skips the useless summary request; this does not solve an
oversized current task input, which must be addressed at its source.

This ratio is configurable between 0 and 1, excluding both endpoints. Lower
values leave more headroom but retain fewer old messages verbatim, increasing
reliance on the semantic summary and readable artifacts. The estimate uses the
Runtime's existing character heuristic, not an exact model tokenizer.

The default policy remains semantic summarization:

```yaml
agents:
  defaults:
    working_memory_tokens: 36000
    working_memory_compaction_target_ratio: 0.6
    context_compaction_strategy: model_summary
```

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
