# AI Diagnostics & Troubleshooting Center (v3.3)

Read-only diagnostics for model configuration and inference troubleshooting.
Built on Fleet Monitor v3.2.2 Model Control Center.

## Goals

- Inspect each model’s **actual** configuration without opening Cursor or grepping logs
- Capture **metadata-only** inference records from FOXAI AI Lab
- Explain failures with a deterministic rules engine (no automatic fixes)

## Using Diagnostics

1. Open **Model Control Center**
2. Click **Diagnostics** on a model card
3. Review tabs: Overview · Configuration · Recent Requests · Troubleshooting · Reports
4. **Copy Diagnostic Report** or **Export JSON** as needed

Simple / Advanced / Expert modes still apply (raw backend metadata in Expert).

## API (no authentication)

| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/api/diagnostics/inspect` | Read-only config inspector |
| GET | `/api/diagnostics/history` | Bounded request history |
| GET | `/api/diagnostics/performance` | Aggregated performance |
| POST | `/api/diagnostics/ingest` | Accept metadata from AI Lab |
| POST | `/api/diagnostics/report` | Build sanitized report |
| GET | `/api/diagnostics/record/<id>` | Single record |

## AI Lab integration

AI Lab `chat_proxy.chat_completion` and streaming helpers emit records to
`FLEET_DIAGNOSTICS_URL` (default `http://127.0.0.1:5000/api/diagnostics/ingest`).

Stored fields include model, engine, endpoint, token counts, timings,
`done_reason` / `finish_reason`, request `max_tokens` / `max_tokens_mode`,
`stream_complete`, and stop classification. Prompts and responses are never stored.

Duplicate prevention: ingesting the same `request_id` twice returns the first record.

Set `FLEET_DIAGNOSTICS_DISABLE=1` to skip emission.

### Output-token limits (AI Lab v1.3+)

Direct Chat and Compare Models expose **Maximum Output Tokens**:

- **Use model/client default** — omits `num_predict` / `max_tokens` (no silent 96/64/128 cap)
- **Custom value** — positive integer; Ollama → `options.num_predict`; OpenAI-compatible → `max_tokens`

Preference is stored in the browser (`localStorage`). Fleet Monitor profiles are not modified.

### Streaming diagnostics

Ollama NDJSON and OpenAI SSE streams emit exactly one final diagnostic record
(including interrupted streams, timeouts, and client cancellation).

### Token-count note

`completion_tokens` is the backend-reported count. Character length of the
displayed reply is not a token count — a short token count with a longer-looking
string is expected when tokens ≠ characters, or when thinking/content are split.

## Privacy

Excluded by default: prompts, responses, thinking text, API keys, auth headers,
home-directory paths in exports.

## Deployment / rollback (do not auto-deploy)

Fleet Monitor v3.3 and AI Lab v1.3 can be deployed independently.

- **Fleet Monitor rollback:** check out tag `v3.2.2` / commit `f9201f7` and restart the monitor service only after approval
- **AI Lab rollback:** restore the previous `ai-lab/` tree from `2ec462f` (pre output-token fix) or an earlier known-good revision
- Runtime files (`ai_servers.json`, `model_profiles.json`, `diagnostic_history.json`) are local and not required in git

## Non-goals

- Do not change model contexts, output limits, thinking, or GPU allocation
- Do not restart Fleet Monitor, AI Lab, or inference services automatically
- Do not invent diagnostic values
