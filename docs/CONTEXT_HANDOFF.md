# Context handoff policy

## Reporting task state and transient history

Reporting author/revision, module review/recheck, Cross, Chief, Final Chief,
Final Auditor/recheck and Aggregate Provider compositions now bind a
`TaskContextRebuilder`. Before each ordinary Provider dispatch it assembles:

1. The system instructions and current typed task input.
2. The latest durable draft parts and evidence bindings for that run/task/revision.
3. The current task's recent exchanges and, when needed, a history summary.

Module author/revision input is enriched once at binding from the normalized
evidence snapshot, enumerating the assigned module/submodule scope and unassigned
records without top-k ranking or a model search round. An unavailable snapshot
does not replace prepared input with a false empty scope. Other roles preserve
their prepared contracts, including the evidence/content those contracts already
contain; this does not automatically expand all referenced artifacts or standards.
Template distillation is outside this reporting-task projection slice.

Fact packets explicitly state the module and optional submodule scope. A complete
packet for one submodule is not evidence that other objects are absent from the
whole snapshot. Out-of-scope objects require targeted lookup; until queried they
remain unverified. This scope statement does not guarantee model tool use or
correct interpretation of a value that is already visible.

The authoritative state is placed once in the outgoing request and refreshed in
place. Old tasks and their obsolete draft capsules stay in durable conversations
but are excluded from the Provider projection. Same-task restarts retain completed
exchanges in the restored contiguous task segment. Ordinary recovery prompts refer
to the capsule instead of appending another full copy of the task input. Existing
tool-result caching, session identity and completed-result recovery remain in use.
Older raw tool observations may still repeat facts; this is not global text deduplication.

For these reconstructed tasks, `working_memory_tokens` and the target ratio apply
to **transient history**, before exact task state is added. The configured 36,000
and 0.6 are unchanged. Fixed facts and durable draft text are excluded from the
summary-model request; a large fixed packet alone does not trigger a new summary
on every small tool update. Semantic summarization is still useful for sufficiently
long process history. Repeated requests reuse its compacted working copy.

The existing full-request context-window check runs after reconstruction and
includes task state and tools. Mandatory state that does not fit returns
`context_budget_exhausted`; this change does not introduce automatic task splitting
or silently truncate the packet. A restart can rebuild exact state and drafts from
local data, but does not guarantee lossless retention of every old reasoning detail.

All retained task state still consumes input on each Provider request. Local
assembly avoids model search rounds; it does not imply free tokens, configured
prefix-cache coverage for task bodies, or measured end-to-end latency savings.
The following generic policy also applies to loops without this projection; their
working-memory trigger covers their ordinary message input as before.

## History policy

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

An accepted model summary is kept as generated: Runtime does not cut each prose
item at 400 characters or keep only its last 12 items. The summary request's
output limit still bounds generation; hitting that limit rejects compaction.
Empty model arrays can clear obsolete state rather than resurrect deterministic
fallback prose. Explicit task constraints remain authoritative. Durable restart
merging also retains the accepted model prose and adds bounded new observations;
it leaves semantic rewriting to the next summary. This may use more context than
the old tail clipping and does not guarantee that the model selected every fact.

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
