# GitHub PR Review Poller + OpenCode Bridge — LM-2nd (60s, Zero AI Cost)

This document describes the lightweight polling service + OpenCode bridge that lets Whizzy finish work,
open a PR, stop consuming model time, and be woken automatically when an
independent review has posted a **strict exact-SHA marker** for the current PR head.

## End-to-End Flow

```
Ian starts Whizzy/OpenCode normally (opencode TUI on pts/1)
→ Whizzy develops branch → tests → commit → push → opens PR via gh
→ Whizzy records WAITING_FOR_REVIEW:
      orchestrator-pr-watch add --repo owner/repo --pr 193 --sha <HEAD> --opencode-session ses_...
→ Whizzy stops (no AI cost while waiting) — Ian can watch the same TUI session

polling (60s, gh CLI only, no LLM) ──────────────────────────────┐
  gh pr view --json state,headRefOid,mergeable                   │
  gh api comments / reviews → parse strict marker for exact HEAD  │
                                                                  │
  if CHANGES_REQUIRED for expected SHA → inject FIX into SAME     │
     OpenCode session (opencode run --session ses_... <prompt>)    │
  if APPROVED for expected SHA → inject MERGE verify+merge into   │
     SAME OpenCode session                                        │
  else → remain WAITING / STALE / ERROR                           │
                                                                  │
Whizzy resumes in same TUI (visible) → fixes or merges → pushes new HEAD B
→ poller observes HEAD changed A→B → automatically rebinds expected SHA to B → WAITING_FOR_REVIEW (no manual add)
→ SHA B requires new independent review
→ loop continues until APPROVED→MERGED or closed
```

Clarification: `opencode run --session <id> "<prompt>"` is a **short-lived second OpenCode CLI process** that performs the continuation against the **SAME** OpenCode session (`ses_...` in `~/.local/share/opencode/opencode.db`). It does **not** create a new Whizzy conversation/session. The TUI continues to show the same history; the injected message appears live (WAL). Ian keeps watching the same TUI.
```

## Critical Unknown — OpenCode Bridge (Proven)

**Investigated on LM-2nd 2026-09-04:**

- OpenCode launched as `opencode` TUI, PID 61914, on `/dev/pts/1`, PPID 61733, `OPENCODE=1`, `OPENCODE_PID=61914`
- No LISTEN port in TUI mode (`ss -tlnp` shows only ssh); `attach <url>` is for `serve/web`, not needed
- Proven working injection **without tmux/screen**:
  ```
  opencode run --session ses_f9384e574ffex0zbZLRMvSMMIy "reply with just: PONG" --format json
  ```
  ran **concurrently** with the TUI (second process `62461`) and the `PONG` appeared **live in the same TUI session**
  (`ses_f9384e574...` DB at `~/.local/share/opencode/opencode.db`, WAL mode). The TUI watches the same sqlite DB, so the message appears instantly.

**Preferred outcome:** Use the existing OpenCode session directly via `opencode run --session <id> "<prompt>"` — **no tmux required**.

Why tmux is NOT required here:
- TUI already has a first-class, DB-backed session continuation (`run --session`). Injecting via PTY (`TIOCSTI`/`tmux send-keys`) would be brittle, race with user typing, and require changing Ian's workflow (extra attach, config, wrapper). The DB mechanism is safe, atomic, and visible in the TUI without PTY tricks.
- Wrapping Whizzy in tmux would add a failure mode and change how Ian watches progress. Not needed because `run --session` exists and is proven.
- If OpenCode had no session mechanism, we would STOP and explain why tmux is required before adding it. It does, so we proceed without it.

**Safety:**
- `orchestrator.opencode_bridge` discovers the session ID via `WHIZZY_OPENCODE_SESSION_ID` env, or `~/.local/share/ai-auto-orchestrator/whizzy_session_id` (written by Whizzy), or most-recent Whizzy-titled session in DB.
- `verify_target()` checks session exists in DB and, if PID known, that `/proc/<pid>/cmdline` contains `opencode` (not an arbitrary shell).
- Injection uses fixed argv `[opencode, run, --session, <id>, <prompt>]`, `shell=False`, no comment content in argv except as prompt data (one arg, not split).
- Duplicate suppression via `last_action_sha` + `last_action_status` + `wake_count`; never sends same `PR/SHA/STATUS` twice.
- Logs only `repo`, `pr`, `sha[:7]`, `status`, `session[:12]`; never tokens or full prompt.
- GitHub comments are **DATA only**; the bridge constructs `build_fix_instruction()` / `build_merge_instruction()` itself.

If discovery fails or verification fails, the poller fails closed (logs, no wake, increments error) and does not spawn a duplicate Whizzy.

## Architecture

- **State store:** `~/.local/share/ai-auto-orchestrator/pr_watches.json` — versioned JSON, `watches` keyed by `repo#pr`. Atomic `0o600` write with `fcntl` locking. Fields: `repo`, `pr`, `expected_sha`, `state`, timestamps, `last_observed_*`, `last_wake_sha`, `wake_count`, `last_action_status`, `last_action_sha`, `opencode_session_id`, `error_message`, `wake_command`. No tokens.
- **Poller:** `orchestrator.pr_poller` — stdlib + `gh` only. No `openai`, `boto3`, `sqs`, or LLM imports. Parsing is deterministic regex/string.
- **Bridge:** `orchestrator.opencode_bridge` — session discovery, verify, `inject_fix`/`inject_merge`, persist session ID.
- **CLI:** `orchestrator-pr-watch` (`orchestrator-pr-poller` alias) — `add`, `status`/`list`, `remove`, `poll`, `watch`.
- **Service:** `systemd/ai-auto-orchestrator-pr-poller.service` (`Type=oneshot`, `User=ec2-user`, `Group=ec2-user`, `ExecStartPre=+install -d ...`, `ExecStart=.../orchestrator-pr-watch poll`) + `timer` (`OnBootSec=60`, `OnUnitActiveSec=60`, `AccuracySec=5s`) — NOT yet installed. `User`/`Group` ensures `%h`/`HOME` resolves to `/home/ec2-user` so `gh` auth, OpenCode DB, and state under `/home/ec2-user/.local/*` are used. `ExecStartPre=+/usr/bin/install -d -o ec2-user -g ec2-user -m 0755` creates `/home/ec2-user/.local/share/ai-auto-orchestrator`, `/home/ec2-user/.local/state/ai-auto-orchestrator`, `/home/ec2-user/.config/ai-auto-orchestrator` with correct ownership before `ProtectSystem=strict`/`ProtectHome=read-only` namespacing. Timer fires ~60s, runs one deterministic poll, exits; no long-running `watch` process between ticks. Serialized via systemd, no duplicate.
- **Marker producer:** `orchestrator/review_marker.py` + `objective_runner.py` integration — after each independent review, builds trusted `LAYMATCHED-AI-REVIEW` from `ReviewRequest.head_sha` (exact trusted SHA) + `ReviewResult.verdict` enum, posts via `GitHubPort.comment`. Model prose never grants merge.

## State Model

`WAITING_FOR_REVIEW` — polling, no marker yet.
`CHANGES_REQUIRED` — valid exact-SHA `CHANGES_REQUIRED` detected, fix wake sent.
`APPROVED` — valid exact-SHA `APPROVED` detected, merge wake pending.
`ACTION_SENT` — wake successfully delivered to OpenCode (terminal for that SHA/STATUS; prevents duplicate).
`STALE` — `HEAD` changed (fallback when not `OPEN` or invalid SHA); normally auto-rebinds to new HEAD and `WAITING` (see below).
`WAITING` after auto-rebind — `expected_sha` updated to current `headRefOid`, `last_action_*` cleared, old review not carried.
`MERGED` — PR merged (`state==MERGED` or `mergedAt`), stop.
`CLOSED` — PR closed without merge, stop.
`ERROR` — GitHub API failure, retain safe state, log, do not wake; recovers to `WAITING` on next success.

Persisted atomically with `fsync` + `fcntl LOCK_EX`; survives restart without duplicate wakes (`last_action_*` + `wake_count`).

## Review Marker Format (Strict, Fail-Closed)

```
LAYMATCHED-AI-REVIEW
STATUS: APPROVED | CHANGES_REQUIRED
PR: 193
HEAD: ab12... (40-64 hex)
REVIEWER: independent
FINDINGS:
F-001 ...
F-002 ...
```

- Header `LAYMATCHED-AI-REVIEW` must be a line by itself (ignores surrounding text, finds header).
- Within next 12 lines (up to 40 for `FINDINGS`), all four `STATUS`/`PR`/`HEAD`/`REVIEWER` must be present as `KEY: value` (case-insensitive keys, stripped).
- `STATUS` must be exactly `APPROVED` or `CHANGES_REQUIRED`.
- `PR` must equal watched PR (int).
- `HEAD` must be 40-64 hex and **exactly** equal `expected_sha` (case-insensitive).
- `REVIEWER` must be non-empty.
- Optional `REPO: owner/repo` if present must match watched repo.
- For `CHANGES_REQUIRED`, `FINDINGS:` block is parsed (up to 20 lines, 500 chars each) but not machine-validated beyond capture; findings are **data**, never executed.
- Missing/malformed/wrong field → **rejected**. Casual `LGTM`, `looks good`, `approved` without header are **ignored**.
- Parsing in `parse_review_marker()` / `parse_approval_marker()` — **no LLM**.

## Valid-Review Rules

- marker format valid
- `PR` matches watched PR
- `HEAD` matches watched `expected_sha` (lower-cased)
- `current PR HEAD` (from `gh pr view headRefOid`) still equals that `HEAD`
- **Both** GitHub retrievals (`pr view` state + `issues/comments` + `pulls/reviews`) must succeed with valid JSON; any failure or malformed response → `ERROR` (not `no review`), no wake, bounded log, retry next tick
- If `HEAD` changed (`current HEAD != expected`) and PR is `OPEN` with valid new SHA → **auto-rebind**: watcher atomically updates `expected_sha` to new `headRefOid`, clears old marker/status, `WAITING_FOR_REVIEW`. No manual `add` needed; old review for A cannot approve B (exact-SHA check). Persisted and survives restart.
- If not `OPEN` or invalid SHA → `STALE` fallback, no rebind, no wake.
- New `HEAD` requires completely new independent review.

## How to Add/Remove a Watch

```bash
# Whizzy opens PR, gets PR# and HEAD:
SHA=$(git rev-parse HEAD)
PR=$(gh pr view --json number --jq .number)

# Persist session for bridge (or set env)
echo "ses_f9384e574ffex0zbZLRMvSMMIy" > ~/.local/share/ai-auto-orchestrator/whizzy_session_id
# or
export WHIZZY_OPENCODE_SESSION_ID=ses_f9384e574ffex0zbZLRMvSMMIy

# Add watch
orchestrator-pr-watch add --repo ibettison/AI_Auto_Orchestrator --pr $PR --sha $SHA --opencode-session ses_f9384e574ffex0zbZLRMvSMMIy
# alternative wake command (generic, not opencode)
orchestrator-pr-watch add --repo owner/repo --pr 193 --sha ab12... --wake-command "whizzy-wake --repo {repo} --pr {pr} --sha {sha}"

# Status / list
orchestrator-pr-watch status --verbose
orchestrator-pr-watch list

# Single poll (debug, no LLM)
orchestrator-pr-watch poll
orchestrator-pr-watch poll --wake-command "echo would-wake {repo} {pr} {sha}"

# After fixing and pushing new commit — poller auto-rebinds, no manual add required:
# (manual `add` with new SHA still works if run, but is not required)
NEW_SHA=$(git rev-parse HEAD)
# optional: orchestrator-pr-watch add --repo owner/repo --pr 193 --sha $NEW_SHA

# Remove
orchestrator-pr-watch remove --repo owner/repo --pr 193
orchestrator-pr-watch remove  # all
```

Paths via flags or env: `--state-file`, `--log-file`, `AI_ORCHESTRATOR_PR_WATCH_STATE`, `AI_ORCHESTRATOR_PR_POLL_LOG`.

## Fix Loop (Automatic)

On exact-SHA `CHANGES_REQUIRED`:
1. Poll verifies `current HEAD == reviewed HEAD == expected_sha` and `PR` is `OPEN`.
2. Poll parses `FINDINGS` as data.
3. `opencode_bridge.inject_fix()` builds deterministic prompt (repo/pr/sha truncated + findings, no shell from GH) and runs **short-lived `opencode run --session <id> <prompt>`** against same session — visible in same TUI.
4. State → `CHANGES_REQUIRED` + `ACTION_SENT` (`last_action_status=CHANGES_REQUIRED`, `last_action_sha`, `wake_count++`); never repeats for same `PR/SHA/STATUS`.
5. Whizzy resumes (Ian watches), applies minimal fixes, runs exact allowlisted checks, commits, pushes new SHA B.
6. Poller on next tick sees `HEAD A→B`, **automatically rebinds** `expected_sha` from A to B, `WAITING_FOR_REVIEW` (no manual `add` required; atomic `save_watches`, survives restart). Old review for A not carried; B requires new review.
7. Do NOT merge.

## Merge Loop (Verify and Stop — Human Approval Required)

On exact-SHA `APPROVED`:
1. `inject_merge()` builds a verify-and-stop status report that tells Whizzy to **re-verify immediately and STOP**:
   - `gh pr view --json state,headRefOid,closed,mergedAt` → `OPEN`, `headRefOid==approved SHA`, `closed==false`, `mergedAt==null`
   - re-parse `LAYMATCHED-AI-REVIEW` for same `PR`/`HEAD`/`APPROVED` still applies
   - no newer `CHANGES_REQUIRED` for current HEAD
2. If all pass: report `HUMAN_MERGE_APPROVAL_REQUIRED` for the exact HEAD and `STOP` — wait for explicit human approval. **DO NOT MERGE.**
3. If any differs: report `ACTION_REQUIRED` with reason, fail closed, leave PR open.
4. `DO NOT DEPLOY`. No merge command is ever issued or instructed through any route.
5. State → `APPROVED` → `ACTION_SENT` after the status report is delivered; deduped per `SHA`.

## Exact-SHA Safety

- Bound at `add` time; every poll compares `lower()` to `headRefOid`.
- `HEAD A→B` while `OPEN` → **auto-rebind** to B, `WAITING`, old marker not carried; `STALE` only fallback when not `OPEN`/invalid SHA.
- `MERGED`/`CLOSED` → terminal, no wake.
- `gh` failure (PR state **or** comments/reviews API) → `ERROR`, no wake, bounded diagnostic, recovers to `WAITING` on next success. Partial marker source retrieval is not sufficient for permission-bearing decisions (fail closed).
- Wake only when `STATUS` + `PR` + `HEAD` match `expected_sha` **and** `current HEAD == expected_sha` **and** both GitHub retrievals succeeded.
- Whizzy verifies the APPROVED status and reports; it never merges (human merge approval required).

## How LM-2nd Should Run It (Polling, £0)

```bash
# After Whizzy opens PR:
orchestrator-pr-watch add --repo owner/repo --pr 193 --sha <expected> --opencode-session <ses_...>

# Timer/service will poll via gh every 60s:
#   gh pr view --json number,state,headRefOid,mergeable,mergeStateStatus,closed,mergedAt,isDraft
#   gh api repos/<repo>/issues/<pr>/comments --paginate
#   gh api repos/<repo>/pulls/<pr>/reviews --paginate
```

Service (NOT yet installed) — runs explicitly as `ec2-user` to use its `gh` auth, OpenCode DB, and state:
```bash
sudo cp systemd/ai-auto-orchestrator-pr-poller.service /etc/systemd/system/
sudo cp systemd/ai-auto-orchestrator-pr-poller.timer /etc/systemd/system/
sudo systemctl daemon-reload
# Verify unit resolves to ec2-user before enabling:
systemctl cat ai-auto-orchestrator-pr-poller.service | grep -E "^(User|Group|ReadWritePaths|ExecStartPre|ExecStart)="
systemd-analyze verify ai-auto-orchestrator-pr-poller.service
# Directory setup is automatic via ExecStartPre=+/usr/bin/install -d
# (creates /home/ec2-user/.local/share/ai-auto-orchestrator,
#  /home/ec2-user/.local/state/ai-auto-orchestrator,
#  /home/ec2-user/.config/ai-auto-orchestrator as ec2-user:ec2-user 0755).
# If creating manually, ensure ownership:
#   sudo -u ec2-user install -d -m 0755 /home/ec2-user/.local/share/ai-auto-orchestrator \
#     /home/ec2-user/.local/state/ai-auto-orchestrator \
#     /home/ec2-user/.config/ai-auto-orchestrator
# Do NOT create root-owned directories under /root — the service must use /home/ec2-user.
sudo systemctl enable --now ai-auto-orchestrator-pr-poller.timer
systemctl list-timers ai-auto-orchestrator-pr-poller.timer
journalctl -u ai-auto-orchestrator-pr-poller -f
cat ~/.local/share/ai-auto-orchestrator/pr_watches.json
cat ~/.local/state/ai-auto-orchestrator/pr_poller.log
# Verify runtime user/home and no root-owned files:
#   systemctl show ai-auto-orchestrator-pr-poller.service -p User -p Group -p ReadWritePaths
#   journalctl -u ai-auto-orchestrator-pr-poller.service -n 20 --no-pager
#   ls -ld /home/ec2-user/.local/share/ai-auto-orchestrator /home/ec2-user/.local/state/ai-auto-orchestrator /home/ec2-user/.config/ai-auto-orchestrator
#   ls -ld /root/.local/share/ai-auto-orchestrator /root/.local/state/ai-auto-orchestrator 2>&1 | head
```
Service is `Type=oneshot`, `User=ec2-user`, `Group=ec2-user`, `ExecStartPre=+/usr/bin/install -d -o ec2-user ...` (directory creation, privileged), `ExecStart=.../orchestrator-pr-watch poll` — runs one poll and exits; timer (`OnBootSec=60`, `OnUnitActiveSec=60`, `AccuracySec=5s`, `Persistent=true`) fires ~60s, serializes via systemd, no duplicate watch process between ticks. `ReadWritePaths` uses absolute `/home/ec2-user/...` so mount namespacing never touches `/root`. No `watch --interval` long-running process.

## Cost Safety

See **COST SAFETY REVIEW** below — `WAITING/POLLING COST = £0` incremental AI/API/model cost. Polling is `gh` only (free, rate-limited). Model is invoked **only** on `opencode run --session` after a valid exact-SHA marker.

## Troubleshooting

```bash
orchestrator-pr-watch status --verbose
orchestrator-pr-watch poll --wake-command "echo would-wake {repo} {pr} {sha}"
cat ~/.local/state/ai-auto-orchestrator/pr_poller.log
gh pr view 193 --repo owner/repo --json number,state,headRefOid,mergeable,mergeStateStatus
gh api repos/owner/repo/issues/193/comments --paginate | jq '.[].body'
gh api repos/owner/repo/pulls/193/reviews --paginate | jq '.[].body'
orchestrator-pr-watch remove --repo owner/repo --pr 193
# Debug: run one poll in foreground (or use watch for local manual loop)
orchestrator-pr-watch poll --wake-command "echo would-wake {repo} {pr} {sha}"
orchestrator-pr-watch watch --interval 10 --max-iterations 3  # local loop, not used by timer
# Check opencode session:
cat ~/.local/share/ai-auto-orchestrator/whizzy_session_id
echo $WHIZZY_OPENCODE_SESSION_ID
ps -o pid,cmd,stat,tty -p $(cat /proc/sys/kernel/ns_last_pid)
sqlite3 ~/.local/share/opencode/opencode.db "select id,title,directory from session order by time_updated desc limit 5;"
```

## Migration to Webhook

Polling is V1. For webhook V2: keep `WatchRecord` + `parse_review_marker`, replace `poll_loop` with `POST /webhook` handling `issue_comment`/`pull_request_review`, call `evaluate_watch` on push. State, SHA binding, fail-closed, dedup, Whizzy double-check unchanged. `poll_once` remains for reconciliation. Timer can be disabled; no marker format change.

## Security Notes

- Never logs `OPENAI_API_KEY`, `gh auth`, tokens; state `0o600`, `PrivateTmp=yes`, `NoNewPrivileges=yes`.
- Wake from trusted local config/env/file, never from PR body; `subprocess.run` with `shlex.split`, `shell=False`.
- Findings are data, truncated (20×500 chars), newlines stripped.
- `ProtectSystem=strict`, `ProtectHome=read-only` with `ReadWritePaths` for state/logs.

## Limitations

- 60s polling, not instant; cheap and reliable.
- Paginates all comment pages each poll (acceptable for low PR volume).
- State is local filesystem; re-add after disk loss.
- Wake must be idempotent; poller does not merge itself.
- After fix push, poller auto-rebinds `expected_sha` A→B and returns to `WAITING` (manual `add` with new SHA still works but is no longer required).
- Review markers are produced locally by `orchestrator/review_marker.py` from `ReviewResult.verdict` (not prose); exact SHA from trusted GitHub state; duplicate markers for same SHA/STATUS are suppressed; `gh` post failure fails closed.

## Architecture Diagram

```
[Whizzy TUI pts/1] --opencode run --session--> [sqlite DB] <--> [Poller gh poll 60s]
        |                                                |
   gh pr create + add watch                    re-verify + merge
        |                                                |
        +----------> [GitHub PR #193 HEAD=ab12...] <-----+
                     ^ comments with LAYMATCHED-AI-REVIEW
                     | independent reviewer
```

## Cost Safety Review

**Services contacted every 60s:**
- `gh pr view` (1 API call) — free GitHub API via `gh` credentials
- `gh api issues/comments --paginate` (0-n calls, paginated) — free
- `gh api pulls/reviews --paginate` (0-n calls) — free
- Local sqlite read/write for `pr_watches.json` and `pr_poller.log` — free
- `opencode` binary **only** when waking (valid marker) — free local, but model invocation costs only after marker (see below)

**Chargeable?** No. `gh` calls are against existing free GitHub PAT rate limits (non-billable). No `openai`, `openrouter`, `codex`, `aws`, `boto3`, `sqs`, or paid queue is contacted during polling. Verified by `grep` of `pr_poller.py` and `opencode_bridge.py` — no such imports.

**Model invoked during polling?** No. `pr_poller.poll_once`/`poll_loop` only run `gh` CLI via `subprocess.run(["gh", ...])` and parse markers locally. Tests `test_polling_path_invokes_no_model` and `test_no_paid_fallback` assert no `import openai` / `responses.create` / `boto3`.

**AWS/cloud used?** No. State is local file; service is `systemd` timer/service on LM-2nd host.

**Expected incremental cost while WAITING:** **£0**. Only GH free tier; no LLM tokens; timer wakes `gh` every 60s.

**What exact event causes model work to resume?** A valid `LAYMATCHED-AI-REVIEW` block for **exact** `expected_sha` + `PR` + `HEAD` + `REVIEWER` that matches `current PR HEAD` (still `OPEN`). Then `evaluate_watch` returns `should_wake=True`, `poll_once` calls `opencode_bridge.inject_fix` or `inject_merge` → `opencode run --session <id> <prompt>` which triggers **one** model turn in the existing OpenCode session (billable per existing Whizzy token usage, not a poll cost). No other event wakes the model.

**How duplicate wakes prevented?** `WatchRecord` stores `last_action_sha`, `last_action_status`, `last_wake_sha`, `wake_count`. `evaluate_watch` checks `last_action_sha == expected_lower && last_action_status == review_status` — if true, suppresses. `poll_once` also transitions to `ACTION_SENT` after successful `trigger_wake`/`inject_*`. `save_watches` is atomic with `fcntl LOCK_EX`, so restart reloads same state and sees `wake_count>0`. Tests `test_wake_fires_once_only`, `test_duplicate_wake_suppressed_for_changes`, `test_duplicate_merge_suppressed_after_action_sent` verify.

**Confirmation NO PAID MODEL FALLBACK exists:** Polling code path has no fallback to OpenAI; any `gh` failure → `ERROR` state, no wake. Bridge only runs after valid marker and still requires session verification; on verification failure it fails closed (no spawn). No `try: openai else: fallback` exists.

If any waiting/polling component required a charge, we would STOP and report — none does, so we proceed.
