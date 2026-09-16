# Teams Monitor Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans and independently scoped parallel tasks. Track completion below.

**Goal:** Deliver a portable Teams cache monitor with full bootstrap, reliable incremental batches, MCP access and Windows scheduling.

**Architecture:** One shared service behind CLI and stdio MCP. A versioned JSON state and immutable outbox separate collection from externally acknowledged OneNote delivery.

**Tech Stack:** Python 3.12 standard library, existing vendored IndexedDB/LevelDB/V8 parsers, unittest, GitHub Actions.

**Spec:** docs/superpowers/specs/2026-09-16-teams-monitor-design.md

## Global Constraints

- No mandatory installed third-party packages. Validate with python -S.
- Do not read real Teams caches or schedule a task on this development machine.
- Commit and push completed implementation to GuHuiX0/teams-zero-cli.
- All published insights carry account/conversation/message evidence identifiers.
- Cache disappearance is not proof of message deletion.

## Task 1: Reader correctness

Files: reader.py, vendored IndexedDB parser, test_reader.py.
Interface: preserve TeamsCacheReader messages/accounts/conversations/mentions;
add find_caches() -> list[str], diagnostics property. Existing Message fields stay.

- [x] Add synthetic version/update/tombstone/corrupt-newest and HTML tests.
- [x] Run `python -S -m unittest test_reader -v` and observe failures.
- [x] Resolve current raw records before decoding and use live_only=True throughout reader.
- [x] Add clear discovery/schema diagnostics; run reader and existing tests.

## Task 2: Durable ingestion and extraction

Files: state.py, ingest.py, extract.py, service.py, test_monitor.py.
Interface: Monitor(config_path).collect(mode), status(), list_batches(),
get_batch(batch_id, offset=0, limit=100), ack(batch_id, receipt), workflow().
TeamsService(config_path=None).call(name, arguments) -> dict; TOOLS list of MCP tool definitions.

- [x] Test bootstrap >50 messages, no-op retry, late arrivals, edited messages,
  output failure, state failure/retry, acknowledge idempotency, lock contention.
- [x] Run `python -S -m unittest test_monitor -v` to confirm missing behaviors.
- [x] Implement atomic JSON replacement, OS lock, config-relative paths, schema validation.
- [x] Implement deterministic fingerprint-based batches and separate delivery receipts.
- [x] Extract conservative bilingual category candidates, terms and cited workflow counts.
- [x] Verify all ingestion tests and shared service filtering/pagination.

## Task 3: MCP transport

Files: mcp_server.py, test_mcp.py.
Interface: serve(service, stdin=None, stdout=None) -> int, service.call(name, args),
service.tools list. Entrypoint supplied by CLI task.

- [x] Add stdio tests for initialization, discovery, calls, errors, notifications and Unicode.
- [x] Observe failing tests; implement supported lifecycle and schema validation.
- [x] Ensure malformed requests do not crash server and stdout is JSON-RPC only.

## Task 4: Scheduler

Files: scheduler.py, test_scheduler.py.
Interface: task_xml(python_path, script_path, config_path, interval_minutes=15,
task_name='TeamsCacheMonitor') -> str; install_task(...), remove_task(name).

- [x] Test XML parsed action/arguments/working directory with spaces, ampersands, Unicode.
- [x] Observe failing tests, implement XML and explicit schtasks invocation.
- [x] Verify no shell command interpolation and no replacement of existing tasks by default.

## Task 5: CLI, delivery guide and packaging

Files: cli.py, test_service.py, test_cli.py, README.md, docs/monitoring.md,
examples/, tools/release.py, .github/workflows/tests.yml, PROVENANCE.json.

- [x] Add CLI/service tests for name ambiguity, pagination, errors, monitor lifecycle.
- [x] Observe failures, implement all documented subcommands through shared operations.
- [x] Document bootstrap -> schedule collection -> MCP host reads pending batch ->
  OneNote write with batch deduplication -> acknowledge receipt.
- [x] Build ZIP from tracked source only; verify hashes; exclude all runtime data.
- [x] Run Python 3.12 suite and independent review; fix confirmed findings.
- [x] Commit, push, and verify remote SHA and CI outcome.

## Design refinements during implementation

- All-ID fingerprints replace timestamp-only filtering to catch older edits/late records.
- Two checkpoints separate durable collection from external publication acknowledgement.
- Dependency-free MCP uses a deliberately limited stdio tools capability set.
- Implement on a dedicated branch in the clean existing checkout, preserving local main.

## Verification record

- 57 synthetic tests passed locally with Python 3.12 and `-S`.
- Windows and Ubuntu CI passed at `b5c2053`, including source hash verification
  and creation of the portable source ZIP (GitHub Actions run 35044700957).
- The extracted 47-file ZIP passed provenance verification without a Git checkout
  and its monitor entry point launched successfully.
- Review fixes cover journal/acknowledgement interleaving, empty-body message edits,
  reserved path collisions and metadata-only account validation.
- Implementation is pushed on `codex/incremental-teams-mcp`, PR #1.
- No real Teams cache, scheduled task registration, or OneNote destination was
  exercised on the development computer; target-machine acceptance remains.
