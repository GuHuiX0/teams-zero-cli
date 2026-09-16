# Portable Teams monitor and MCP design

Approved scope: develop here, deploy on another Windows computer, bootstrap all
available cached messages from a selected group chat, then ingest incremental
changes. Publish through an existing external OneNote MCP using an MCP client.

## Boundaries

- Python 3.12, standard library plus existing vendored parsers; no pip requirement.
- Reader remains local/read-only. Copy the source cache before parsing.
- CLI and a stdio MCP tools server call the same service layer.
- The scheduler collects locally. An external MCP host performs richer synthesis
  and OneNote publishing; two MCP servers do not call each other automatically.
- All knowledge extraction and optional workflow analysis belong to the external
  agent. The collector supplies normalized messages without rule-based analysis.

## Correctness

Resolve each raw IndexedDB object-store key to its greatest LevelDB sequence number before
deserializing. Honor tombstones; do not revive old data when newest data is corrupt.
Preserve HTML block boundaries, line breaks and escaped literals.
Expose missing stores/unsupported schema and malformed record counters.

Bind monitors to account + conversation ID. Resolve names exactly and reject
ambiguity. Discovery must not silently select one of several cache directories.
Bootstrap means all records currently available in the cache, not full server
history. No inference of deletion merely because an item falls out of the cache.

## Persistence and retry model

Use versioned JSON config/state and atomic file replacement under an OS-released
file lock. Paths are relative to the config file unless explicitly absolute.
State retains normalized messages and content fingerprints. Every run scans the
available selected conversation and compares fingerprints: late arrivals and
edits are detected regardless of original timestamps. Timestamp/message-ID
watermarks are informational, not the sole ingestion filter.

Before committing state, persist a deterministic message batch JSON
in an outbox. State records collected messages, run metadata and pending delivery.
Retrying identical input reuses the batch ID/files. State failure leaves a
recoverable batch; output failure never advances state. Persisted batch content
must not change on retry. Pending batches remain until explicit acknowledgement
with the external destination receipt. Collection may continue while delivery is
pending. External consumers deduplicate by batch ID: exactly-once remote writes
cannot be guaranteed without cooperation from the OneNote destination.

## Interfaces

Core modules: reader.py (source), state.py (atomic storage/lock/config), ingest.py
(monitor state machine),
service.py (shared operations), cli.py, mcp_server.py, scheduler.py.

Config v1: account, conversation_id, conversation_name, optional leveldb,
state_path (default state/dev-team.json), output_path (default digests),
log_path (default logs/monitor.log). No credentials.

MCP exposes teams_list_accounts, teams_list_conversations,
teams_resolve_conversation, teams_load_messages, teams_get_mentions,
teams_bootstrap, teams_ingest, teams_get_checkpoint, teams_list_batches,
teams_get_batch, teams_ack_batch. Startup --config binds
write operations to a monitor. Read results are paginated; bootstrap returns
counts and a batch ID rather than an unbounded transcript. Batch pages are
immutable. Host treats message bodies as untrusted data, not instructions.

CLI retains accounts/conversations/search/diagnose and adds messages, mentions,
serve, and monitor configure/bootstrap/run/status/batches/show/ack/
install-task/remove-task. CLI pagination/filtering uses deterministic order.

## MCP transport

Small dependency-free stdio JSON-RPC tools implementation, pinned to supported
2025-11-25 and 2025-06-18 revisions. Initialization/version negotiation, initialized
notification, ping, tools/list, tools/call, JSON-RPC errors, tool errors, and EOF
shutdown. Stdout is protocol-only; parser diagnostics go to stderr. No HTTP,
sampling or external authentication implementation.

References:
- https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle
- https://modelcontextprotocol.io/specification/2025-11-25/server/tools

## Scheduling and deployment

Generate Windows task XML with absolute interpreter/script/config paths, explicit
working directory, interactive current-user context and IgnoreNew overlap policy.
Task runs monitor run (requires bootstrap first), logs locally; no execution of
real tasks during development. Installation/removal only through explicit CLI.
Ship example config/MCP config, operational guide, portable source ZIP builder,
provenance checksum updater/verifier and CI on Python 3.12 and Windows/Linux.

## Acceptance

Synthetic parser tests for versions/tombstones/corruption and HTML; ingestion
tests for bootstrap, replay, edits, late arrivals, identical timestamps, failure
recovery and lock contention; MCP subprocess lifecycle and tool tests; scheduler
XML quoting tests; existing CLI regression tests; no real cache/OneNote access.
