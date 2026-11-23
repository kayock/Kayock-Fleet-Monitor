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

AI Lab `chat_proxy.chat_completion` emits records to
`FLEET_DIAGNOSTICS_URL` (default `http://127.0.0.1:5000/api/diagnostics/ingest`).

Stored fields include model, engine, endpoint, token counts, timings,
`done_reason` / `finish_reason`, request `max_tokens`, and stop classification.
Prompts and responses are never stored.

Set `FLEET_DIAGNOSTICS_DISABLE=1` to skip emission.

## Privacy

Excluded by default: prompts, responses, thinking text, API keys, auth headers,
home-directory paths in exports.

## Non-goals

- Do not change model contexts, output limits, thinking, or GPU allocation
- Do not restart Fleet Monitor, AI Lab, or inference services automatically
- Do not invent diagnostic values
