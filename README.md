# AI Auto Orchestrator — Slice C

Slice C adds an independent, SHA-bound reviewer and bounded review/fix loop around the Slice A contract and Slice B runner. The offline MVP architecture is:

`Objective → bounded Codex runner → validation → immutable diff → independent reviewer → bounded fix/re-review loop → AI approved OR human decision required`

## Run offline

```bash
python3 -m unittest discover -v
python3 -m orchestrator.simulator --scenario all
```

The suite uses temporary local Git repositories and `FakeCodexAdapter`; it makes no network calls and requires no Codex account, API, provider, credential, or production system.

## Runner architecture

`RunnerConfig` is the bounded objective contract: run ID, repository, exact source SHA, permitted paths, exact allowed argv commands, an explicit non-empty `required_checks` list, objective, timeouts, output/command limits, explicit environment, network request, and review-cycle limit. `WorkspaceManager` rejects a dirty source checkout, verifies the SHA, clones it without hardlinks, creates a dedicated branch in the temporary clone, and always cleans up. The source checkout is never used as the worker directory.

`CommandPolicy` requires an exact allowlisted argv tuple, rejects shell wrappers and shell composition tokens, and uses `shell=False`. `PathPolicy` compares the before/after workspace inventory and rejects traversal, symlink changes, and out-of-scope files. `EnvironmentPolicy` constructs a new allowlisted environment rather than inheriting the parent environment. The runner records every required check, fails closed when one is not executed successfully, and sets `validation_passed` only after all required checks pass. `RunnerResult` contains bounded, untrusted output summaries, exit status, changed files, attempted checks, validation status, timestamps, duration, workspace/branch identity, failure reason, and structured audit records.

The `CodexAdapter` protocol isolates the future CLI invocation detail. Slice B provides only `FakeCodexAdapter`, executed in a dedicated killable process so an adapter that never calls `commands.run()` cannot retain the lease or workspace past the overall deadline. Adapter timeout handling also terminates active command process groups. The parent drains adapter IPC while the worker is running, and command plus adapter output share one hard aggregate cap. `commands_executed` retains commands that started, with statuses for successful completion, non-zero exit, timeout, and output-limit termination; only completed zero-exit required checks can pass validation. An in-process, thread-safe `RunLeaseRegistry` rejects competing attempts for the same run ID. Durable leases and crash recovery belong to a later persistence/bridge slice.

## Security boundaries

Network access is denied by default. A request for network access is rejected because this portable Python runner cannot provide a hard network sandbox. Application policy is not a substitute for OS/container isolation. Before any production use, the execution environment must supply process, filesystem, network, resource, credential, and identity isolation.

The command and path policies are intentionally fail closed. They are policy checks around a development worker, not a security boundary against a compromised kernel, interpreter, dependency, or host. No unrestricted shell, shell wrapper, production credentials, or automatic merge capability is provided.

## Slice A integration

`RunnerCoordinator` emits `START`, runs the bounded adapter, then emits `IMPLEMENTED` with trusted `tests_pass=true` only when the runner completed and all required checks passed. Otherwise it emits `RUNNER_FAILED` and enters Slice A's blocked state. Adapter stdout/stderr is never copied into trusted state-machine fields. Existing immutable events, idempotency, source-SHA protection, optimistic concurrency, durable RED gates, bounded reviews, human escalation, and deterministic replay remain unchanged. Slice C's independent reviewer can consume the reviewing state later; it is deliberately not implemented here.

## Simulator scenarios

`--scenario all` includes Slice A GREEN/AMBER/RED paths plus Slice B runner GREEN, command failure, timeout/process termination, and out-of-scope modification scenarios. Each runner scenario uses a throwaway local repository and cleans it up.

## Slice C reviewer boundary

`ReviewInputPreparer` resolves exact Git base and head commits and binds the actual untruncated `base...head` diff to a SHA-256 digest. `ReviewRequest` carries the objective, repository, both SHAs, diff, validation evidence, risk context, and cycle. `ReviewResult` uses bounded verdict/severity enums and is validated before it can affect the state machine. An approval for one head is never valid for another; a changed head during review is escalated.

`ReviewFixLoop` uses the deterministic Slice A reducer, records durable-review-shaped events through `FakeGitHubCoordinator`, limits cycles, fingerprints equivalent findings, and escalates repeated findings, malformed results, provider failures, ambiguous decisions, and RED risk. Slice B validation results now carry the run ID, exact validated head SHA, and review diff digest; both the initial implementation and every fix must match those immutable bindings before state transitions. Repository/diff text is explicitly untrusted data. The `OpenAIResponsesReviewer` is only a structural Responses API boundary using JSON Schema, `store=false`, configured model/limits, and no tools; it has no default transport and no live call is made in this repository.

Slice C does not authorise production execution. It does not connect to LayMatched, OpenAI, GitHub mutation APIs, credentials, Stripe, email, DNS, databases, webhooks, CI, or external providers. Production deployment would still require a durable external orchestrator/bridge, separated GitHub App/token identities, secret management, hard network/container isolation, real provider credentials, persistence/durable leases, monitoring, and an explicit production approval path. This repository alone cannot wake or control an existing ChatGPT conversation.

## Deliberate limitations

Slice C is the final offline MVP slice, not a production executor. It does not implement autonomous merging, sophisticated container resource isolation, durable leases, persisted audit logs, live OpenAI calls, live GitHub mutation, or a real Codex CLI adapter. Oversized review input is rejected rather than silently chunked. Those capabilities require explicit deployment controls and a separate activation decision.
