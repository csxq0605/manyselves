# Phase 1 Protected-Core Protocol Exception

## Audit Boundary

- Previous protected-core base: `2a6864c5ff70881161fc0e79f7e7816f15e70304` (`2a6864c`).
- Audited stabilization range: `2a6864c5ff70881161fc0e79f7e7816f15e70304..a68636672abf8df1e3624a9e633faea12ceeee20`.
- Audited implementation commit: `a68636672abf8df1e3624a9e633faea12ceeee20`.
- The stabilization range covers the design and plan commits plus the inclusive implementation sequence `37b457c`, `82aa442`, `6a7800e`, `33f08cb`, `6431d67`, `955382b`, and the mechanical Ruff correction `a686366`.
- The protected-path name audit returned exactly `manyselves/core/loops/agent_loop.py` and no path under `manyselves/templates`.

## Authorized Exception

The user approved this minimal exception so recoverable tool and debug identity is assigned at the actual AgentLoop publication sites. Provider-issued tool IDs must follow each call into its matching result; the direct reporting-resume route must allocate one ID and reuse it for every result branch; and debug events must retain the emitting Agent identity. Assigning identity at the source removes ambiguous same-name call correlation without broadening the protected-core architecture.

The only authorized protected file is:

`manyselves/core/loops/agent_loop.py`

Within that file, the audited diff is limited to:

- importing `uuid4`, allocating one direct-route `tool_call_id`, and reusing it;
- supplying `tool_call_id` to `ToolCallMessage` and `ToolResult` construction sites; and
- supplying `agent_type` to `ApiDebugMessage` construction sites; and
- removing the single extra blank line after the `uuid4` import in mechanical Ruff commit `a686366`.

## Independent Review Conclusion

The independent reviewer for Task 1 commit `37b457c` found the change spec compliant and approved its task quality. The reviewer reported no Critical, Important, or Minor findings and confirmed that the protected-core diff contains only AgentLoop message construction and the required direct ID allocation, with no control-flow change.

The independent reviewer for mechanical Ruff commit `a68636672abf8df1e3624a9e633faea12ceeee20` also approved the change with no findings. Its only protected-core delta from `955382b` is deletion of the extra blank line after the `uuid4` import; it does not alter runtime behavior or expand this exception.

## Prohibited Scope

This exception does not authorize changes to AgentLoop control flow, prompts, provider management, tools, Reporting behavior, scheduling, persistence, any other file under `manyselves/core`, or any file under `manyselves/templates`. Future changes to those protected paths remain blocked by the regular Phase 1 core-freeze check and require a new explicit review and authorization.
