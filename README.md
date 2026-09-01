# AI Auto Orchestrator — Slice B

Slice B adds a bounded, unattended development runner around the merged Slice A coordination contract. It proves that a fake Codex adapter can work on an exact immutable Git SHA in a clean temporary clone, execute only explicit argv commands, and return a structured result that can drive `IMPLEMENTED` or fail-closed `RUNNER_FAILED` events.

## Run offline

```bash
python3 -m unittest discover -v
python3 -m orchestrator.simulator --scenario all
```

The suite uses temporary local Git repositories and `FakeCodexAdapter`; it makes no network calls and requires no Codex account, API, provider, credential, or production system.

## Runner architecture

`RunnerConfig` is the bounded objective contract: run ID, repository, exact source SHA, permitted paths, exact allowed argv commands, objective, timeouts, output/command limits, explicit environment, network request, and review-cycle limit. `WorkspaceManager` rejects a dirty source checkout, verifies the SHA, clones it without hardlinks, creates a dedicated branch in the temporary clone, and always cleans up. The source checkout is never used as the worker directory.

`CommandPolicy` requires an exact allowlisted argv tuple, rejects shell wrappers and shell composition tokens, and uses `shell=False`. `PathPolicy` compares the before/after workspace inventory and rejects traversal, symlink changes, and out-of-scope files. `EnvironmentPolicy` constructs a new allowlisted environment rather than inheriting the parent environment. `RunnerResult` contains bounded output summaries, exit status, changed files, attempted checks, timestamps, duration, workspace/branch identity, failure reason, and structured audit records.

The `CodexAdapter` protocol isolates the future CLI invocation detail. Slice B provides only `FakeCodexAdapter`. All command output is streamed with a hard aggregate cap; command and overall deadlines terminate the process group. An in-process, thread-safe `RunLeaseRegistry` rejects competing attempts for the same run ID. Durable leases and crash recovery belong to a later persistence/bridge slice.

## Security boundaries

Network access is denied by default. A request for network access is rejected because this portable Python runner cannot provide a hard network sandbox. Application policy is not a substitute for OS/container isolation. Before any production use, the execution environment must supply process, filesystem, network, resource, credential, and identity isolation.

The command and path policies are intentionally fail closed. They are policy checks around a development worker, not a security boundary against a compromised kernel, interpreter, dependency, or host. No unrestricted shell, shell wrapper, production credentials, or automatic merge capability is provided.

## Slice A integration

`RunnerCoordinator` emits `START`, runs the bounded adapter, then emits `IMPLEMENTED` on success or `RUNNER_FAILED` on failure. Successful runs stop at `REVIEWING`; failed runs enter Slice A's blocked state. Existing immutable events, idempotency, source-SHA protection, optimistic concurrency, durable RED gates, bounded reviews, human escalation, and deterministic replay remain unchanged. Slice C's independent reviewer can consume the reviewing state later; it is deliberately not implemented here.

## Simulator scenarios

`--scenario all` includes Slice A GREEN/AMBER/RED paths plus Slice B runner GREEN, command failure, timeout/process termination, and out-of-scope modification scenarios. Each runner scenario uses a throwaway local repository and cleans it up.

## Deliberate limitations

Slice B does not authorise production execution. It does not connect to LayMatched, OpenAI APIs, Codex services, Stripe, email, DNS, databases, webhooks, CI, or any external provider. It does not implement autonomous merging, sophisticated container resource isolation, durable leases, persisted audit logs, or a real Codex CLI adapter. Those require later slices and explicit OS/container controls.
