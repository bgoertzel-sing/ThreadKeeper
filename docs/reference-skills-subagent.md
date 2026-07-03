# Reference — Subagent Dispatch

Defined in `src/skills.metta`; dispatch code lives in `src/subagent.py`;
per-persona configuration lives in `memory/personas-subagent/<key>.json`.

The subagent dispatch primitive lets the parent agent delegate a
bounded sub-task to a specialist subagent — typically a smaller,
cheaper, or more-specialized model — and receive a single-string
digest in return. The parent's identity, persona, and memory are
unaffected by the dispatch.

See [`subagent-design.md`](./subagent-design.md) for the architectural
rationale, the per-component design, and the measurement methodology
behind the v1 feature set.

---

## `delegate`

### Signature

```metta
(delegate "<goal>" "<tools_csv>" "<persona_key>" <max_turns>)
(delegate "<goal>" "<tools_csv>" "<persona_key>")           ;; uses default max_turns
```

Keyword (JSON-mode) form:

```metta
(delegate (goal "<...>") (tools "<csv>") (persona "<key>") (max_turns 8))
(delegate (goal "<...>") (tools "<csv>") (persona "<key>"))
```

### Purpose

Dispatch a bounded sub-task to a subagent identified by
`persona_key`. The subagent loads its own persona prompt + endpoint
binding from `memory/personas-subagent/<persona_key>.json`, runs an
internal mini-loop for up to `max_turns` iterations against its
configured LLM endpoint, calls only the tools listed in `tools_csv`,
and returns a single-string digest via its own `emit` instruction.

### Parameters

- `goal` — the task description the subagent should pursue. Should
  be specific enough that a focused specialist model with the
  given tool subset can make progress within the turn budget. May
  also be a JSON task contract with `objective`, `allowed_paths`,
  `forbidden_actions`, `done_criteria`, and optional `max_tool_calls`;
  contract fields are bounded and validated before any worker LLM call.
- `tools_csv` — comma-separated list of tool names the subagent may
  call. Must be a subset of the v1 registered tools (see
  [§4.5](./subagent-design.md#45-tool-registry-for-subagents-v1)).
  Cannot include any v1-excluded tool. May be empty if the persona
  config specifies a `default_tool_subset`.
- `persona_key` — name of the persona config (without `.json`
  extension), resolved against `memory/personas-subagent/`.
- `max_turns` — hard cap on subagent iterations. Bounded by
  `OMEGACLAW_SUBAGENT_MAX_TURNS` (default 8). Optional in the
  three-argument form; defaults to 8.

### Returns

A single-line JSON string of at most `OMEGACLAW_SUBAGENT_MAX_DIGEST_CHARS`
(default 2,000) with `summary`, `files_changed`, `tests_run`,
`uncertainty`, `next_action`, `transcript_path`, `transcript_sha256`,
and `status` fields. Full worker prompts/responses/tool results are
persisted locally under `OMEGACLAW_SUBAGENT_RUN_DIR` (default
`memory/subagent-runs`) and only the bounded digest is returned to the
parent. Each finished transcript also gets a local `<transcript>.sha256`
sidecar and a compact append-only `index.jsonl` entry under the run directory,
so supervisors can list runs and cheaply detect accidental corruption or later
mutation during audit.

Early setup, contract, provider, tool-subset, and escalation failures also
return the same structured JSON shape and persist a minimal local transcript;
the JSON `summary` includes `"(subagent error: <reason>)"` or the escalation
denial reason. Errors are never raised into the parent's MeTTa interpreter.

### Examples

```metta
;; Multi-step research delegated to a local Ollama specialist
(delegate "find recent papers on Non-Axiomatic Logic and summarize themes"
          "search,read-file"
          "researcher"
          8)

;; Cheap routine sub-task delegated with the persona's default tools
(delegate "summarize the most recent entries in memory/notes.md"
          ""
          "researcher")
```

### Notes / limits

- The subagent's persona, tool subset, and provider/model are
  declared at dispatch time. The subagent cannot expand its own
  permissions inside the loop.
- The subagent's loop runs in the parent's Python process; its LLM
  endpoint can live anywhere the deployment configures (local
  Ollama, remote API, etc.). The parent receives only bounded state;
  full prompts, responses, tool calls/results, task contracts, and
  history digests are saved in the local transcript record. Finished
  records have a SHA-256 sidecar plus an `index.jsonl` audit entry, and the
  parent digest returns the same transcript hash for audit checks.
- The subagent cannot call `send`, `remember`, `pin`, `metta`,
  `query`, `episodes`, or `delegate` in v1 (excluded by design —
  see §4.5.2 of the design doc). Tool execution is capped both per dispatch
  and per worker response, so one malformed turn cannot spend the whole quota
  in a single batch. If `shell` is explicitly enabled,
  it still uses argv-list execution (`shell=False`), an executable
  allowlist, command-name-only executable tokens (no explicit paths), a
  minimal child environment (no inherited API keys/tokens), a sanitized `PATH`
  that excludes the workspace/current directory, no stdin, bounded output/timeout,
  and `cwd` fixed to
  `OMEGACLAW_SUBAGENT_WORKSPACE`.
- The subagent persona config must reference an API key via an
  env-var name; key material is never read from the config file
  itself. OpenAI-compatible endpoints also require the local OpenAI
  SDK/client to initialize before the worker loop starts; native
  Ollama endpoints do not.
- If the endpoint is unreachable, the API key env var is unset, the
  provider client cannot initialize, the persona config is missing or
  malformed, or any tool name is unknown / v1-excluded, the dispatcher
  returns a structured error digest naming the cause.

### Configuration

Optional env vars control v1 behavior. Defaults are bounded and fail-closed
for safety-sensitive paths. Numeric knobs are parsed defensively: malformed
values fall back to the documented default, and below-minimum values are
clamped instead of crashing the module or disabling guards accidentally.

| Env var | Default | Meaning |
|---|---|---|
| `OMEGACLAW_SUBAGENT_PERSONA_DIR` | `./memory/personas-subagent` | Directory holding `<key>.json` configs and persona prompt files. |
| `OMEGACLAW_SUBAGENT_MAX_TURNS` | `8` | Hard cap on iterations per dispatch. |
| `OMEGACLAW_SUBAGENT_MAX_DIGEST_CHARS` | `2000` | Length cap on the JSON digest returned to the parent. |
| `OMEGACLAW_SUBAGENT_RUN_DIR` | `memory/subagent-runs` | Directory for persistent JSON transcript/run records, `index.jsonl`, checksum sidecars, and worker rate/concurrency state. |
| `OMEGACLAW_SUBAGENT_LLM_TIMEOUT_S` | `180` | Timeout for each worker LLM call. |
| `OMEGACLAW_SUBAGENT_LLM_RETRIES` | `1` | Retry count after the first worker LLM attempt. |
| `OMEGACLAW_SUBAGENT_LLM_BACKOFF_S` | `1.0` | Exponential backoff base between worker retries. |
| `OMEGACLAW_SUBAGENT_LLM_CALLS_PER_MINUTE` | `60` | Per-endpoint worker LLM calls/minute cap; `0` disables locally. |
| `OMEGACLAW_SUBAGENT_MAX_CONCURRENT_LLM_CALLS` | `4` | Per-endpoint cross-process in-flight worker LLM cap; `0` disables locally. |
| `OMEGACLAW_SUBAGENT_MAX_TOOL_CALLS` | `24` | Per-dispatch tool-call quota; JSON task contracts may narrow this with non-negative `max_tool_calls`. |
| `OMEGACLAW_SUBAGENT_MAX_TOOL_CALLS_PER_TURN` | `3` | Per-response tool-call batch cap; extra parsed calls return `TURN_QUOTA_EXCEEDED` before execution. |
| `OMEGACLAW_SUBAGENT_CANCEL_FILE` | unset | If the file exists, dispatch stops with `status=cancelled`. |
| `OMEGACLAW_SUBAGENT_MAX_PATH_ARG_CHARS` | `512` | Maximum path argument length for file tools. |
| `OMEGACLAW_SUBAGENT_MAX_TOOL_ARG_CHARS` | `20000` | Maximum string length for any single tool argument. |
| `OMEGACLAW_SUBAGENT_MAX_READ_FILE_CHARS` | `20000` | Maximum text returned by one subagent `read-file` call before a truncation marker is appended. |
| `OMEGACLAW_SUBAGENT_MAX_CONTRACT_ITEMS` | `32` | Maximum entries in each task-contract list field. |
| `OMEGACLAW_SUBAGENT_MAX_CONTRACT_ITEM_CHARS` | `512` | Maximum length of each task-contract list item. |
| `OMEGACLAW_SUBAGENT_MAX_CONTRACT_OBJECTIVE_CHARS` | `4000` | Maximum task-contract objective length. |
| `OMEGACLAW_SUBAGENT_WORKSPACE` | current working directory | Sandbox root for subagent file tools. |
| `OMEGACLAW_ESCALATION_METTA_SHA256` | unset | Optional SHA-256 pin for `escalation.metta`; mismatch denies cloud delegation. |

See [`tutorial-09-subagents.md`](./tutorial-09-subagents.md) for an
end-to-end walkthrough.

### Failure modes

| Failure | Returned digest |
|---|---|
| `persona_key` config missing | Structured JSON `status=error`; `summary` contains `(subagent error: persona config '<key>.json' not found at <path>)`; transcript status `setup_error`. |
| Config JSON malformed | Structured JSON `status=error`; `summary` contains `(subagent error: persona config '<key>.json' is malformed JSON: <reason>)`; transcript status `setup_error`. |
| Persona prompt file missing/hash mismatch/path escape | Structured JSON `status=error`; `summary` contains `(subagent error: persona prompt <reason>)`; transcript status `persona_prompt_invalid`. |
| `api_key_env` env var unset | Structured JSON `status=error`; `summary` contains `(subagent error: env var '<NAME>' is unset; cannot reach endpoint for provider '<P>')`; transcript status `provider_invalid`. |
| OpenAI-compatible provider client cannot initialize | Structured JSON `status=error`; `summary` names the provider initialization failure; transcript status `provider_invalid`; no worker LLM call is attempted. |
| Tool subset includes unknown skill | Structured JSON `status=error`; `summary` contains `(subagent error: unknown skill(s) [...]; registered subagent tools: [...])`; transcript status `tool_subset_invalid`. |
| Tool subset includes v1-excluded skill | Structured JSON `status=error`; `summary` contains `(subagent error: skill(s) [...] are not callable by subagents in v1)`; transcript status `tool_subset_invalid`. |
| Task contract is oversized, path-escaping, uses unsafe action identifiers, or has invalid `max_tool_calls` | Structured JSON `status=error`; `summary` contains `(subagent error: task contract <reason>)`; transcript status `contract_invalid`. |
| Escalation policy denies cloud delegation | Structured JSON `status=error`; `summary` contains `(escalation denied) ...`; transcript status `escalation_denied`. |
| Subagent endpoint times out / errors | Structured JSON `status=error`; `summary` contains `(subagent LLM call failed: <ExceptionType>: <reason>)`; transcript status `llm_failed`. |
| Worker mixes `emit` with other parsed calls or multiple emits | Structured JSON `status=error` and `EMIT_PROTOCOL_VIOLATION`; transcript status `emit_protocol_violation`. |
| Worker response exceeds the per-turn tool-call cap | Structured JSON `status=error`; `summary` contains `TURN_QUOTA_EXCEEDED`; transcript status `turn_quota_exceeded`. |
| `read-file` target is larger than `OMEGACLAW_SUBAGENT_MAX_READ_FILE_CHARS` | Tool result is truncated in the worker context with an explicit `(read-file truncated at <N> chars)` marker. |
| Loop exceeds `max_turns` without `emit` | Structured JSON `status=incomplete`; `summary` contains `(subagent: max_turns (<N>) reached without emit; last_results: <clip>)`; transcript status `max_turns`. |
