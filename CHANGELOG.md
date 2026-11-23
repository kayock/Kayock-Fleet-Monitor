# Changelog

All notable changes to Kayock Fleet Monitor are documented in this file.

## [v3.3.0-dev] — 2026-11-23

AI Diagnostics & Troubleshooting Center (development branch
`feature/ai-diagnostics-v3.3`). Read-only by default; does not alter inference
configurations or restart services.

### Added

- **Model Configuration Inspector** — Diagnostics button on MCC cards; live
  read-only inspection via Ollama `/api/show`+`/api/ps`, llama.cpp `/props`,
  LM Studio and Lemonade capability-aware probes
- **Inference request metadata** — stop-reason classification, token accounting,
  timing, bounded local history (`diagnostic_history.json`)
- **FOXAI AI Lab bridge** — chat/compare posts metadata-only records to
  `/api/diagnostics/ingest`; UI shows stop reason + Diagnostics link
- **Streaming diagnostics** — Ollama NDJSON and OpenAI SSE emit exactly one
  final record (including interrupted / cancelled / timed-out streams)
- **Rules-based troubleshooting** — deterministic findings (output limit,
  context, timeout, connection, model unavailable, slow generation)
- **Configuration conflict detection** — distinguishes intentional overrides vs
  potential conflicts across model/server/profile/request layers
- **Performance history** — completed/failed counts, avg tok/s, timeouts,
  output-limit occurrences (from captured requests only)
- **Diagnostic reports** — Copy text / Export JSON with privacy sanitization

### Fixed (AI Lab)

- Removed hard-coded Direct Chat `max_tokens: 96` and Compare `max_tokens: 64`
- Direct Chat / Compare: **Maximum Output Tokens** = model default or custom
- Model-default mode omits Ollama `num_predict` and OpenAI `max_tokens`
- Compact per-response diagnostic summary with Fleet Monitor deep link
- `request_id` de-duplication on diagnostics ingest

### Safety / non-goals

- Never invents missing values; unknown fields labeled explicitly
- Does not store prompts, responses, or credentials
- Does not load/unload models, change contexts, or restart inference services
- Not deployed to production without explicit approval

## [v3.2.2] — 2026-11-23

Stable Model Control Center release: fleet-wide server registration, accurate
management eligibility, persistent model profiles, and documented separation of
model management from inference-server process control.

### Added

- **Configure Fleet Management** wizard — discover servers across the fleet,
  preview readiness, select eligible servers, and register them with explicit
  confirmation
- **Server-level registration** — register each inference server once; associated
  model cards inherit management capabilities
- **Remote adoption** — register peers through the existing `allow_control`
  proxy (`/api/ai/.../adopt`) without a separate remote-execution stack
- **Management summary** — servers discovered/registered/needing setup vs
  models discovered/manageable/needing setup
- **Needs Setup** filter and server-oriented setup entry points on model cards
- **Profile inventory sync** — non-destructive create/update with stable
  multi-machine identities; offline profiles preserved
- Friendly names, tags, notes, protected-model confirmations, and unverified
  application-association labels
- Settings apply modes: Save Profile, Apply Supported Changes, Apply With Restart
  (known startup flags only; unknown launch args preserved)
- Corrected resource labels (model RAM vs server RSS vs machine memory/GPU)

### Changed

- Management eligibility prefers registration state before remote-control messaging
- New registrations leave `exe` blank so discovery noise cannot grant process control
- Dashboard static assets are cache-busted by app version; index uses `no-store`
- Documentation clarifies single-machine, multi-machine, and mixed OS fleets

### Safety / non-goals

- Registration does **not** start, stop, restart, load, unload, or download models
- Remote control remains opt-in per fleet machine
- No authentication gates added
- Request preferences are not silently applied to unrelated applications

### Known limitations

- llama-server has no unload-while-running; restart-based switching requires
  configured process control (executable path)
- vLLM / SGLang / generic OpenAI endpoints remain monitoring-focused for model
  management
- Apply With Restart that rewrites startup args applies on the machine that owns
  `ai_servers.json` (not proxied as a silent remote config rewrite)
- Peer Fleet Monitor instances must expose the adopt API for remote registration

## [v3.2.1] — 2026-11-22

Profile sync hardening, legacy profile merge, and UI cache-busting for the
Model Control Center.

## [v3.2.0] — 2026-11-22

Initial Model Control Center: fleet inventory cards, Simple/Advanced/Expert
modes, profile storage, settings preview, and remote load/unload when allowed.

## Earlier

See Git history for v3.0.x / v3.1.x system monitoring, AI discovery, and fleet
features.
