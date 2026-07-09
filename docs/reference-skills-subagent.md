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
  `forbidden_actions`, `done_criteria`, optional `max_tool_calls`, and
  optional boolean `patch_proposal_only`, and optional boolean
  `requires_adjudication`. The string-list fields (`allowed_paths`,
  `forbidden_actions`, `done_criteria`) must be JSON arrays of strings;
  scalar or non-string entries fail closed before any worker LLM call.
  Contract fields are bounded and validated before any worker LLM call.
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
`patch_proposals`, `uncertainty`, `next_action`, `transcript_path`,
`transcript_sha256`, `status`, optional `queue_path`/`queue_sha256`, and (when
any worker LLM calls were made) `worker_token_usage` fields. Full worker
prompts/responses/tool results are persisted locally under
`OMEGACLAW_SUBAGENT_RUN_DIR` (default `memory/subagent-runs`) and only the
bounded digest is returned to the parent. Each finished transcript also gets a
local `<transcript>.sha256` sidecar and a compact append-only `index.jsonl`
entry under the run directory.
Index entries include `previous_entry_sha256` and `entry_sha256` hash-chain
fields so supervisors can list runs and cheaply detect accidental corruption,
truncation, reordering, or later mutation during audit.
The read-only helper `subagent.verify_subagent_run_index()` verifies that hash
chain and any recorded local transcript SHA-256s without repairing files,
draining queues, or calling a worker LLM. Finished-run appends and read-only
audits reject symlink/non-regular `index.jsonl` and `index.jsonl.lock` paths,
and index rotation rewrites through random local temp files rather than
predictable `index.jsonl.tmp.<pid>` names, so the compact audit log cannot be
redirected through local link tricks.

`worker_token_usage` contains aggregated `input_tokens`, `output_tokens`, and
`total_tokens` across all worker LLM calls in the dispatch, for cost
accounting and audit. It is omitted from the structured return when no worker
LLM calls were made (e.g., setup errors before the loop).

When `OMEGACLAW_SUBAGENT_QUEUE_ONLY=1`, dispatch performs setup/contract/tool
validation and persists a durable task record under
`OMEGACLAW_SUBAGENT_RUN_DIR/queue/` instead of initializing or calling the
worker LLM. This is the first async/backpressure primitive: the parent receives
`status="queued"`, `queue_path`, `queue_sha256`, `queue_sha256_path`, and a
normal transcript; a separate local supervisor can later claim the queued task.
If the queue already has `OMEGACLAW_SUBAGENT_MAX_QUEUED_DISPATCHES` pending regular non-symlink JSON
tasks, dispatch fails closed with transcript status `queue_backpressure` before
any worker call.
The Python helper `subagent.run_queued_dispatch(queue_path)` is the current
single-task worker primitive: it atomically claims one queued regular non-symlink task, verifies the
required regular non-symlink queue-task `.sha256` sidecar, revalidates the task shape and task
contract, re-injects that contract into the synchronous dispatch goal while
queue-only mode is suppressed, writes a compact `*.result.json`, and leaves the
task as `*.done` plus a refreshed `.sha256` sidecar for audit instead of
silently re-running it. Symlink and non-regular `queue/*.json` entries are ignored by queue listing and rejected before claim. If checksum, validation, or execution fails after a task
has been claimed, the helper retains the claimed task as `*.failed`, writes a
fresh `.sha256` sidecar for the retained bytes when possible, and writes
`*.failed.result.json` so malformed or tampered queued records do not vanish
into a limbo state.
`subagent.drain_queued_dispatches(max_tasks=1)` is the bounded
operator-supervised wrapper: each call drains at most `max_tasks` pending queue
records in oldest-first order and returns compact JSON metadata. It deliberately
does not daemonize, sleep, poll forever, or auto-start from `dispatch`.
`subagent.run_queued_worker_loop(...)` is the corresponding supervised async
worker loop: it repeatedly claims pending queue records until an explicit bound
is reached (`max_tasks`, `max_idle_polls`, `max_runtime_s`, `max_consecutive_errors`, or a `stop_file`).
Worker stop-token and queued dispatch cancellation-token paths are resolved under
`OMEGACLAW_SUBAGENT_RUN_DIR` (relative values are interpreted there, absolute
values must remain there), and token checks only honor regular non-symlink files
so queued records/operator arguments cannot probe arbitrary host paths.
The loop uses a best-effort local lock (`.async-worker.lock`) to avoid two local
workers draining the same queue concurrently when `fcntl` is available. While
held, the lock file contains compact JSON metadata (`pid`, `started_at`, bounds,
`stop_file`, and status) so a supervisor/operator can distinguish an active
local worker from a stale prior run; completed loops leave a final `status` /
`stop_reason` summary in the same file. Stale-lock metadata reads are bounded by
`OMEGACLAW_SUBAGENT_ASYNC_WORKER_LOCK_METADATA_BYTES` before JSON parsing, and
symlink/non-regular lock files are ignored for stale metadata and rejected for
new worker acquisition, so a corrupt or redirected local lock file is ignored
rather than parsed/followed unbounded. SIGTERM/SIGINT are handled as graceful
stop requests for the current bounded run, and the module-local signal flag is
cleared before returning so a later same-process worker-loop invocation is not
poisoned by an earlier handled signal. It still does not start itself from
`dispatch` and is not a service manager; deployments must launch it deliberately
under their chosen supervisor. The repository also provides the conservative
operator entrypoint `scripts/run-subagent-worker-loop`, which imports `subagent`
after loading optional `--env-file KEY=VALUE` operator config files and applying
an optional `--run-dir`, invokes one bounded worker-loop run, and prints the
structured JSON result. Env files are parsed without shell expansion, and
malformed lines fail closed before import. Process-control keys that could
change interpreter/subprocess loading behavior (`PATH`, `PYTHONPATH`,
`PYTHONHOME`, `LD_*`, `DYLD_*`, `BASH_ENV`, `ENV`, `HOME`, `IFS`, `SHELL`) are
rejected from runner env files before import; `--max-tasks 0` is the intended
no-claim smoke for install/supervisor wiring checks.

When a JSON task contract sets `"patch_proposal_only": true`, `write-file` and
`append-file` calls do not mutate workspace files. Instead they append proposed
changes to the local transcript's `patch_proposals` list and the parent digest
receives only bounded `{action, path}` metadata. Each persisted proposal content
field is capped by `OMEGACLAW_SUBAGENT_MAX_PATCH_PROPOSAL_CHARS` (default
20,000) with an explicit truncation marker. Normal `write-file` / `append-file`
updates use atomic replace plus a per-target workspace lock when `fcntl` is
available; existing lock paths are rejected if they are symlinks or non-regular
files, so local workspace lock files cannot redirect file-tool synchronization
outside the workspace. The parent/supervisor is then responsible for review,
tests, and application.

When a JSON task contract sets `"requires_adjudication": true`, the subagent's
final `emit` is treated as a candidate output rather than an accepted result.
The transcript records `status=adjudication_required` with `candidate_summary`,
and the parent digest returns `status=needs_adjudication` with bounded
`adjudication` metadata (`required`, `status`, `candidate_summary`). The
parent/supervisor must route the candidate to an adjudicator before accepting
it. No second LLM call is made inside the dispatch loop.

`subagent.review_subagent_candidate(transcript_path)` is a non-mutating local
review helper for these two modes. It only accepts transcript JSON paths under
`OMEGACLAW_SUBAGENT_RUN_DIR`, verifies the optional `.sha256` sidecar with the
same bounded sidecar reader used by queued-worker integrity checks, and returns
compact JSON naming whether patch-proposal review and/or adjudication is
required. Review setup errors sanitize absolute local paths before returning to
the caller. It deliberately does not apply patches, accept final answers, call
an LLM, drain queues, or change live runtime behavior.

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
  records have a SHA-256 sidecar plus a hash-chained `index.jsonl` audit entry,
  and the parent digest returns the same transcript hash for audit checks. Use
  `verify_subagent_run_index()` for a bounded read-only audit of the compact
  index chain and transcript hashes.
  Queued tasks can be consumed one at a time by `run_queued_dispatch`, or in a
  small bounded batch by `drain_queued_dispatches(max_tasks=...)`, or by the
  bounded async loop `run_queued_worker_loop(max_tasks=..., poll_interval_s=..., max_idle_polls=..., stop_file=..., max_runtime_s=..., max_consecutive_errors=...)`.
  These paths use atomic claim/finish filenames, validate queue-record
  shape/contracts, and preserve the queued task contract through the worker
  dispatch rather than trusting or dropping queue-record contents. The async loop
  is real polling work, but it remains explicitly operator/supervisor launched;
  it does not self-schedule from a parent dispatch.
  Task contracts may also request patch-proposal-only mode, which records child
  file-change proposals without applying them.
- The subagent cannot call `send`, `remember`, `pin`, `metta`,
  `query`, `episodes`, or `delegate` in v1 (excluded by design —
  see §4.5.2 of the design doc). Tool execution is capped both per dispatch
  and per worker response, so one malformed turn cannot spend the whole quota
  in a single batch. If `shell` is explicitly enabled,
  it still uses argv-list execution (`shell=False`), an executable
  allowlist, command-name-only executable tokens (no explicit paths), a
  minimal child environment (no inherited API keys/tokens), a sanitized `PATH`
  that excludes the workspace/current directory, no stdin, bounded argv count,
  defensively parsed output/timeout caps, explicit truncation markers, and `cwd`
  fixed to `OMEGACLAW_SUBAGENT_WORKSPACE`.
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
or non-finite values fall back to the documented default, and below-minimum
values are clamped instead of crashing the module or disabling guards
accidentally.

| Env var | Default | Meaning |
|---|---|---|
| `OMEGACLAW_SUBAGENT_PERSONA_DIR` | `./memory/personas-subagent` | Directory holding `<key>.json` configs and persona prompt files. |
| `OMEGACLAW_SUBAGENT_MAX_TURNS` | `8` | Hard cap on iterations per dispatch. |
| `OMEGACLAW_SUBAGENT_MAX_DIGEST_CHARS` | `2000` | Length cap on the JSON digest returned to the parent. |
| `OMEGACLAW_SUBAGENT_RUN_DIR` | `memory/subagent-runs` | Directory for persistent JSON transcript/run records, `index.jsonl`, checksum sidecars, worker rate/concurrency state, and optional queued dispatch tasks. |
| `OMEGACLAW_SUBAGENT_MAX_INDEX_AUDIT_BYTES` | `1048576` | Maximum `index.jsonl` bytes scanned by `verify_subagent_run_index()`; returns `index_audit_too_large` before reading oversized indexes. Finished-run appends also read only a bounded tail of `index.jsonl` when linking/rotating the hash chain, so append cost does not scale with an intentionally unrotated index. `0` disables only the audit cap. |
| `OMEGACLAW_SUBAGENT_MAX_TRANSCRIPT_AUDIT_BYTES` | `1048576` | Maximum bytes read from each transcript referenced by `verify_subagent_run_index()` while checking transcript SHA-256s; oversized transcripts are reported as `transcript_too_large`, and transcript hashes are streamed in fixed-size chunks instead of using one unbounded `read()`. `0` disables only the cap. |
| `OMEGACLAW_SUBAGENT_MAX_SHA256_SIDECAR_BYTES` | `4096` | Maximum bytes read from required local regular non-symlink `.sha256` sidecars before parsing the digest; symlink, oversized, or malformed sidecars fail closed without echoing local paths. |
| `OMEGACLAW_SUBAGENT_MAX_ESCALATION_POLICY_BYTES` | `1048576` | Maximum bytes read from pinned `escalation.metta` before SHA-256 hashing during cloud-delegation integrity checks; oversized policies deny escalation before worker LLM calls, and integrity errors avoid echoing local paths. `0` disables this read cap. |
| `OMEGACLAW_SUBAGENT_MAX_PERSONA_CONFIG_BYTES` | `65536` | Maximum bytes read from one `<persona_key>.json` config before JSON parsing; oversized configs fail closed before worker LLM calls and avoid echoing local paths. |
| `OMEGACLAW_SUBAGENT_MAX_PERSONA_PROMPT_BYTES` | `262144` | Maximum bytes read from one persona prompt before optional SHA-256 hashing and prompt construction; oversized prompts fail closed before worker LLM calls and avoid echoing local paths. `0` disables this read cap. |
| `OMEGACLAW_SUBAGENT_QUEUE_ONLY` | unset/false | If true, validate and enqueue the dispatch under `OMEGACLAW_SUBAGENT_RUN_DIR/queue/` without calling the worker LLM. |
| `OMEGACLAW_SUBAGENT_MAX_QUEUED_DISPATCHES` | `32` | Maximum pending queued dispatch task records before returning `queue_backpressure`; `0` means no pending queue capacity. |
| `OMEGACLAW_SUBAGENT_ASYNC_WORKER_MAX_TASKS` | `32` | Default maximum tasks for one explicit `run_queued_worker_loop(...)` invocation; `0` exits without claiming work. |
| `OMEGACLAW_SUBAGENT_ASYNC_WORKER_MAX_IDLE_POLLS` | `3` | Default number of empty queue polls before an explicit worker-loop invocation exits idle. |
| `OMEGACLAW_SUBAGENT_ASYNC_WORKER_POLL_INTERVAL_S` | `2.0` | Default sleep interval between empty queue polls inside the supervised worker loop. |
| `OMEGACLAW_SUBAGENT_ASYNC_WORKER_MAX_RUNTIME_S` | `600.0` | Default wall-clock cap for one explicit worker-loop invocation; `0` disables the runtime cap. |
| `OMEGACLAW_SUBAGENT_ASYNC_WORKER_STOP_FILE` | unset | Optional stop-token path under `OMEGACLAW_SUBAGENT_RUN_DIR`; relative values are interpreted there, absolute values must stay there, and only regular non-symlink files count as present. Malformed explicit worker-loop bounds and invalid stop-file values fail closed before the worker lock/queue claim. |
| `OMEGACLAW_SUBAGENT_ASYNC_WORKER_MAX_CONSECUTIVE_ERRORS` | `3` | Max consecutive `queue_worker_error` results before the worker loop exits early; `0` disables the consecutive-error limit. |
| `OMEGACLAW_SUBAGENT_ASYNC_WORKER_MAX_RESULTS` | `16` | Max result entries kept in the worker-loop structured return; older entries are dropped and counted in `results_truncated`. `0` disables the cap. |
| `OMEGACLAW_SUBAGENT_ASYNC_WORKER_LOCK_METADATA_BYTES` | `8192` | Maximum bytes read from `.async-worker.lock` stale-lock metadata before JSON parsing; oversized metadata is ignored rather than surfaced as stale-lock evidence. |
| `OMEGACLAW_SUBAGENT_LLM_TIMEOUT_S` | `180` | Timeout for each worker LLM call. |
| `OMEGACLAW_SUBAGENT_LLM_RETRIES` | `1` | Retry count after the first worker LLM attempt. |
| `OMEGACLAW_SUBAGENT_LLM_BACKOFF_S` | `1.0` | Exponential backoff base between worker retries; jitter (up to 25% of the base delay) is added to each retry. |
| `OMEGACLAW_SUBAGENT_LLM_CALLS_PER_MINUTE` | `60` | Per-endpoint worker LLM calls/minute cap; `0` disables locally. |
| `OMEGACLAW_SUBAGENT_MAX_CONCURRENT_LLM_CALLS` | `4` | Per-endpoint cross-process in-flight worker LLM cap; `0` disables locally. |
| `OMEGACLAW_SUBAGENT_MAX_LLM_STATE_BYTES` | `65536` | Maximum bytes read from local per-endpoint rate-limit/concurrency JSON state files before resetting malformed or oversized state; prevents corrupted guard files from becoming unbounded reads. |
| `OMEGACLAW_SUBAGENT_MAX_TOOL_CALLS` | `24` | Per-dispatch tool-call quota; JSON task contracts may narrow this with non-negative `max_tool_calls`. |
| `OMEGACLAW_SUBAGENT_MAX_TOOL_CALLS_PER_TURN` | `3` | Per-response tool-call batch cap; extra parsed calls return `TURN_QUOTA_EXCEEDED` before execution. |
| `OMEGACLAW_SUBAGENT_CANCEL_FILE` | unset | If the file exists, dispatch stops with `status=cancelled`. |
| `OMEGACLAW_SUBAGENT_MAX_PATH_ARG_CHARS` | `512` | Maximum path argument length for file tools. |
| `OMEGACLAW_SUBAGENT_MAX_TOOL_ARG_CHARS` | `20000` | Maximum string length for any single tool argument. Tool arguments must already be strings; JSON arrays/objects/numbers/booleans are rejected before tool execution rather than coerced with `str()`. |
| `OMEGACLAW_SUBAGENT_SHELL_MAX_ARGV` | `32` | Maximum argv token count for the optional allowlisted `shell` tool. |
| `OMEGACLAW_SUBAGENT_SHELL_OUTPUT_CAP` | `4000` | Maximum combined stdout/stderr preview returned by one optional shell call before a truncation marker is appended; subprocess output is captured to a temporary file and only `cap + 1` bytes are read back into memory. |
| `OMEGACLAW_SUBAGENT_SHELL_TIMEOUT_S` | `30.0` | Timeout in seconds for one optional shell subprocess; below-minimum or malformed values use a safe bounded value. |
| `OMEGACLAW_SUBAGENT_MAX_READ_FILE_CHARS` | `20000` | Maximum text returned by one subagent `read-file` call before a truncation marker is appended. |
| `OMEGACLAW_SUBAGENT_MAX_CONTRACT_ITEMS` | `32` | Maximum entries in each task-contract list field. |
| `OMEGACLAW_SUBAGENT_MAX_CONTRACT_ITEM_CHARS` | `512` | Maximum length of each task-contract list item. |
| `OMEGACLAW_SUBAGENT_MAX_CONTRACT_OBJECTIVE_CHARS` | `4000` | Maximum task-contract objective length. |
| `OMEGACLAW_SUBAGENT_DISPATCH_TIMEOUT_S` | `600` | Dispatch-level wall-clock timeout in seconds; checked before each LLM call and tool execution. `0` disables. |
| `OMEGACLAW_SUBAGENT_MAX_TOKENS_PER_DISPATCH` | `0` (disabled) | Dispatch-level token budget cap. When non-zero, the dispatch loop checks total accumulated tokens (input + output) after each worker LLM call and returns a structured `token_budget_exceeded` record if the cap is exceeded. `0` disables. |
| `OMEGACLAW_SUBAGENT_MAX_TRANSCRIPT_TURNS` | `0` (disabled) | Maximum turns retained in the local transcript file. `0` disables the cap. Older turns are dropped when exceeded. |
| `OMEGACLAW_SUBAGENT_MAX_TRANSCRIPT_FIELD_CHARS` | `0` (disabled) | Maximum per-field string size (prompt, raw_response, tool_results) in each transcript turn entry. `0` disables the cap. |
| `OMEGACLAW_SUBAGENT_MAX_PATCH_PROPOSAL_CHARS` | `20000` | Maximum persisted content length for each patch-proposal-only write/append proposal before a truncation marker is appended. |
| `OMEGACLAW_SUBAGENT_MAX_EMIT_CHARS` | `20000` | Maximum final `emit` argument length accepted as successful worker output; oversized emits return `EMIT_PROTOCOL_VIOLATION` before becoming a transcript summary/adjudication candidate. |
| `OMEGACLAW_SUBAGENT_MAX_RESPONSE_CHARS` | `50000` | Maximum raw worker response length parsed/persisted for one turn; oversized responses return `response_too_large` before tool parsing/execution. |
| `OMEGACLAW_SUBAGENT_MAX_LLM_HTTP_RESPONSE_BYTES` | `1048576` | Maximum raw HTTP response body read from the native Ollama-compatible worker transport before JSON decoding. `0` disables. OpenAI-compatible SDK calls remain bounded after parsed content return by `OMEGACLAW_SUBAGENT_MAX_RESPONSE_CHARS`. |
| `OMEGACLAW_SUBAGENT_WORKSPACE` | current working directory | Sandbox root for subagent file tools. |
| `OMEGACLAW_ESCALATION_METTA_SHA256` | unset | Optional SHA-256 pin for `escalation.metta`; mismatch denies cloud delegation. |

See [`tutorial-09-subagents.md`](./tutorial-09-subagents.md) for an
end-to-end walkthrough.

### Failure modes

| Failure | Returned digest |
|---|---|
| `persona_key` config missing | Structured JSON `status=error`; `summary` contains `(subagent error: persona config '<key>.json' not found)`; transcript status `setup_error`. |
| Config JSON malformed or oversized | Structured JSON `status=error`; `summary` contains `(subagent error: persona config '<key>.json' <reason>)`; transcript status `setup_error`; persona config reads are capped by `OMEGACLAW_SUBAGENT_MAX_PERSONA_CONFIG_BYTES`. |
| Persona prompt file missing/hash mismatch/path escape/oversized | Structured JSON `status=error`; `summary` contains `(subagent error: persona prompt <reason>)`; transcript status `persona_prompt_invalid`; persona prompt reads are capped by `OMEGACLAW_SUBAGENT_MAX_PERSONA_PROMPT_BYTES`. |
| `api_key_env` env var unset | Structured JSON `status=error`; `summary` contains `(subagent error: env var '<NAME>' is unset; cannot reach endpoint for provider '<P>')`; transcript status `provider_invalid`. |
| OpenAI-compatible provider client cannot initialize | Structured JSON `status=error`; `summary` names the provider initialization failure; transcript status `provider_invalid`; no worker LLM call is attempted. |
| Tool subset includes unknown skill | Structured JSON `status=error`; `summary` contains `(subagent error: unknown skill(s) [...]; registered subagent tools: [...])`; transcript status `tool_subset_invalid`. |
| Tool subset includes v1-excluded skill | Structured JSON `status=error`; `summary` contains `(subagent error: skill(s) [...] are not callable by subagents in v1)`; transcript status `tool_subset_invalid`. |
| Task contract is oversized, path-escaping, uses unsafe action identifiers, or has invalid `max_tool_calls` / `patch_proposal_only` | Structured JSON `status=error`; `summary` contains `(subagent error: task contract <reason>)`; transcript status `contract_invalid`. |
| Task contract enables `patch_proposal_only` and worker calls `write-file` / `append-file` | Workspace file is not changed; transcript records full `patch_proposals`; parent digest includes bounded proposal metadata. |
| Task contract enables `requires_adjudication` and worker emits a final answer | Structured JSON `status=needs_adjudication`; digest includes bounded `adjudication` metadata (`required`, `status`, `candidate_summary`); transcript status `adjudication_required`. |
| Queue-only mode accepts a dispatch | Structured JSON `status=queued`; digest includes `queue_path`/`queue_sha256`/`queue_sha256_path`; transcript status `queued`; no worker LLM call is attempted. |
| Queue-only mode is at capacity | Structured JSON `status=error`; `summary` contains `queue backpressure`; transcript status `queue_backpressure`; no worker LLM call is attempted. |
| Queued worker sees an escaping task path | `run_queued_dispatch(...)` returns JSON `status=queue_worker_error`; no worker LLM call is attempted. |
| Queued worker sees a missing/mismatched queue-task checksum sidecar after claiming a task | `run_queued_dispatch(...)` returns JSON `status=queue_worker_error`; claimed task is retained as `*.failed` with a fresh checksum sidecar when possible; no worker LLM call is attempted. |
| Queued worker sees a symlink, oversized, or malformed queue-task checksum sidecar | `run_queued_dispatch(...)` returns JSON `status=queue_worker_error`; the sidecar must be regular/non-symlink and reads are bounded by `OMEGACLAW_SUBAGENT_MAX_SHA256_SIDECAR_BYTES`; no worker LLM call is attempted. |
| Queued worker sees bad queued JSON/shape after claiming a task | `run_queued_dispatch(...)` returns JSON `status=queue_worker_error`; claimed task is retained as `*.failed` with `*.failed.result.json`; no worker LLM call is attempted for validation failures. |
| Async worker loop sees malformed explicit bounds or an invalid stop-file path/config value | `run_queued_worker_loop(...)` returns JSON `status=worker_config_invalid`; no worker lock is acquired and no queue record is claimed. |
| Async worker loop sees an existing worker lock | `run_queued_worker_loop(...)` returns JSON `status=worker_already_running` plus any compact `worker_lock` metadata readable from `.async-worker.lock`; no queue record is claimed. |
| Async worker loop sees its stop-file token before claiming work | `run_queued_worker_loop(...)` returns JSON `status=worker_stopped`; pending queue records remain pending. |
| Async worker loop reaches `max_consecutive_errors` | `run_queued_worker_loop(...)` stops early with `stop_reason=max_consecutive_errors` as soon as the cap is reached; `consecutive_errors` and `error_count` in the structured return; remaining queue tasks stay pending. |
| Escalation policy denies cloud delegation | Structured JSON `status=error`; `summary` contains `(escalation denied) ...`; transcript status `escalation_denied`. |
| Subagent endpoint times out / errors | Structured JSON `status=error`; `summary` contains `(subagent LLM call failed: <ExceptionType>: <reason>)`; transcript status `llm_failed`. |
| Worker mixes `emit` with other parsed calls or multiple emits | Structured JSON `status=error` and `EMIT_PROTOCOL_VIOLATION`; transcript status `emit_protocol_violation`. |
| Worker emits a non-string final digest argument | Structured JSON `status=error` and `EMIT_PROTOCOL_VIOLATION`; transcript status `emit_protocol_violation`; typed JSON/object/list values are not coerced into successful summaries/candidates. |
| Worker emits an oversized final digest beyond `OMEGACLAW_SUBAGENT_MAX_EMIT_CHARS` | Structured JSON `status=error` and `EMIT_PROTOCOL_VIOLATION`; transcript status `emit_protocol_violation`; oversized text is not accepted as a successful summary/candidate. |
| Worker raw response exceeds `OMEGACLAW_SUBAGENT_MAX_RESPONSE_CHARS` | Structured JSON `status=error`; transcript status `response_too_large`; the response is bounded in the transcript and no tool calls are parsed or executed. |
| `verify_subagent_run_index()` sees an `index.jsonl` larger than `OMEGACLAW_SUBAGENT_MAX_INDEX_AUDIT_BYTES` | Structured JSON `status=index_audit_too_large`; no index entries or transcript files are read. |
| `verify_subagent_run_index()` sees a referenced transcript larger than `OMEGACLAW_SUBAGENT_MAX_TRANSCRIPT_AUDIT_BYTES` | Structured JSON `status=index_tampered` with issue `transcript_too_large`; the oversized transcript is not read into memory. |
| Worker response exceeds the per-turn tool-call cap | Structured JSON `status=error`; `summary` contains `TURN_QUOTA_EXCEEDED`; transcript status `turn_quota_exceeded`. |
| Optional `shell` output exceeds `OMEGACLAW_SUBAGENT_SHELL_OUTPUT_CAP` | Tool result is truncated in the worker context with an explicit `(shell output truncated at <N> chars)` marker. |
| `read-file` target is larger than `OMEGACLAW_SUBAGENT_MAX_READ_FILE_CHARS` | Tool result is truncated in the worker context with an explicit `(read-file truncated at <N> chars)` marker. |
| Loop exceeds `max_turns` without `emit` | Structured JSON `status=incomplete`; `summary` contains `(subagent: max_turns (<N>) reached without emit; last_results: <clip>)`; transcript status `max_turns`. |
| Dispatch exceeds `OMEGACLAW_SUBAGENT_DISPATCH_TIMEOUT_S` wall-clock limit | Structured JSON `status=error`; `summary` contains `(subagent: dispatch wall-clock timeout (<N>s) exceeded at turn <T>)`; transcript status `dispatch_timeout`. |
| Dispatch exceeds `OMEGACLAW_SUBAGENT_MAX_TOKENS_PER_DISPATCH` token budget | Structured JSON `status=error`; `summary` contains `(subagent: dispatch token budget (<N>) exceeded at turn <T> with <N> total tokens)`; transcript status `token_budget_exceeded`. |
