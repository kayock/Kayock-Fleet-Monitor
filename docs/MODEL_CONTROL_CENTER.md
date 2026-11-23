# Model Control Center (Fleet Monitor v3.2)

Fleet-wide AI model management UI integrated into Kayock Fleet Monitor.

## What it is

A **Model Control** tab in the existing dashboard for discovering models across
Windows, Ubuntu Station, and Ubuntu Portable; loading/unloading where the
engine supports it; previewing parameter changes; and saving reusable profiles.

It extends the existing `AIManager` and fleet `allow_control` proxy. It does
**not** replace AI Lab (`:3010`), which remains chat/compare only and never
auto-loads models.

## Interface modes

| Mode | Audience | Shows |
|---|---|---|
| **Simple** (default) | Everyday use | Model, machine, engine, loaded state, context, Load/Unload/Settings |
| **Advanced** | Power users | Plus resources, endpoint, remote-control state, editable engine params |
| **Expert** | Operators | Plus raw configured launch args, engine notes, full parameter surface |

## Live vs configured vs pending

| Layer | Meaning |
|---|---|
| **Active** | Verified from the running server (e.g. reported context). Missing values show **Unknown — not reported by backend**. |
| **Configured** | Stored in `ai_servers.json` launch arguments. |
| **Pending** | User edits in Settings — not applied until confirmed. |

Apply Changes shows a diff. Startup settings that require restart are labeled
**Restart required**. Fleet Monitor does **not** silently rewrite launch
arguments; Expert/Configure remains the path for arg edits, then Restart.

## Load / unload behavior

| Engine | Load | Unload | Model switch |
|---|---|---|---|
| Ollama | Yes (`keep_alive`) | Yes | Runtime |
| LM Studio | Yes | Yes | Runtime |
| Lemonade | Yes | Yes | Runtime |
| llama-server | No | No | **Restart with model** (managed servers only) |
| vLLM / SGLang / generic OpenAI | No | No | Unsupported (monitor only) |

**Unload model** ≠ **Stop model server**.

Remote Load/Unload require **Allow remote control** on that fleet machine.
Unconfigured discovered processes are never started/stopped/loaded.

## Profiles

Saved in local `model_profiles.json` (gitignored).

- Create / edit / rename / duplicate / delete / export / import
- Built-in **templates** (Fast, Balanced, Deep Thinking, Coding, Creative, Benchmark, Custom) only fill pending fields — they never auto-apply
- Preview shows the change list before Apply

## Memory warnings

Before Load / Load-via-Restart, MCC may show an **estimate**-labeled warning
when available RAM and model size measurements suggest pressure. Estimates are
never presented as guaranteed. Other models are never auto-unloaded.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/mcc/inventory` | Fleet-wide model cards |
| GET | `/api/mcc/capabilities?engine=` | Engine capability matrix |
| POST | `/api/mcc/settings/preview` | Pending vs active/configured diff |
| POST | `/api/mcc/memory-warning` | Estimate warning helper |
| GET/POST/DELETE | `/api/mcc/profiles…` | Profile CRUD / preview / import / export |
| POST/GET | `/api/fleet/<id>/ai/…` | Existing remote proxy (now includes GET `/models`) |

## Safety

- No automatic downloads, loads, unloads, or restarts
- No authentication gates added
- Busy-guard on stop/restart preserved
- Existing contexts, timeouts, thinking settings, and GPU allocations are not changed by MCC itself
- AI Lab on port 3010 remains operational and separate

## Single-machine vs multi-machine

- **One computer:** open This machine + Model Control; manage local configured servers.
- **Fleet:** add peers in All Machines; enable Allow remote control only where desired; use Model Control filters per machine.
