# Model Control Center (Fleet Monitor v3.2.2)

Fleet-wide AI model management UI integrated into Kayock Fleet Monitor.

## What it is

A **Model Control** tab in the existing dashboard for discovering models across
Windows, Ubuntu Station, and Ubuntu Portable; building persistent per-installation
profiles; loading/unloading where the engine supports it; previewing and applying
supported parameter changes; and guiding management setup.

It extends the existing `AIManager` and fleet `allow_control` proxy. It does
**not** replace AI Lab (`:3010`), which remains chat/compare only and never
auto-loads models.

## Interface modes

| Mode | Audience | Shows |
|---|---|---|
| **Simple** (default) | Everyday use | Friendly name, machine, engine, loaded state, management status, Load/Unload/Settings |
| **Advanced** | Power users | Plus resources, endpoint, remote-control state, editable engine params |
| **Expert** | Operators | Plus identity key, raw configured launch args, engine notes, technical apply labels |

## Configure Fleet Management (v3.2.2)

**[Configure Fleet Management]** registers each inference server once.

- Groups models under their servers (not one setup per model card)
- Preview lists machine, engine, endpoint, model count, registration state, remote control, and supported actions
- Distinguishes **model management** (load/unload) from **process management** (start/stop/restart)
- Apply writes Fleet Monitor management config only — never starts/stops/loads/unloads
- Remote registration uses existing `allow_control` peer proxy (`/api/ai/.../adopt`)
- Does **not** automatically enable `allow_control`

## Profiles and Sync

**[Sync Model Profiles]** creates or refreshes inventory profiles from discovery.

- Stable identity: `machine_id|server_key|model` (never display name alone)
- Creates missing profiles; updates discovery metadata only
- Preserves friendly names, notes, tags, protected flag, and user settings
- Marks temporarily unavailable models — never auto-deletes
- Repeated sync does not create duplicates

Profiles are stored in local `model_profiles.json` (gitignored).

### Friendly names and associations

- Friendly display names are editable; API calls always use the actual model id
- Possible application associations are labeled **Possible association — unverified**
  until the user confirms
- Optional **Protected** flag requires extra confirmation before unload/stop/restart/switch

Suggested tags: Application Models, Coding, General, Creative, Experimental.

## Live vs configured vs pending

| Layer | Meaning |
|---|---|
| **Active** | Verified from the running server (e.g. reported context). Missing values show **Unknown**. |
| **Configured** | Stored in `ai_servers.json` launch arguments. |
| **Pending** | User edits in Settings — not applied until confirmed. |
| **Request preferences** | Saved on the profile for clients that opt in — not global server state. |

### Apply buttons

| Button | Behavior |
|---|---|
| **Save Profile** | Persist prefs / tags / protected — no engine mutation |
| **Apply Supported Changes** | Save per-request prefs to the profile; leave restart-required settings pending |
| **Apply With Restart** | Merge **known** startup flags into local `ai_servers.json` (preserve unknown args), then restart after confirmation |

Remote startup-arg rewrite is intentionally not proxied — apply those on the peer that owns the config.

## Management status

| State | Meaning |
|---|---|
| Manageable | Authorized actions available |
| Monitoring only | Configured but no model-management actions |
| Setup required | Discovered but not adopted/configured |
| Remote control disabled | Peer needs Allow remote control |
| Backend unsupported | Engine has no management pathway here |
| Server unavailable | Configured server offline |

**[Set Up Management]** previews adoption / allow_control steps and only acts after explicit confirm.

Filters: **manageable** and **needs setup**.

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

## Resource labels

- **Model RAM / VRAM**: only when the model is loaded and a per-model figure exists; otherwise **Model RAM: Unknown**
- **Server RSS**: process memory for the inference server (Advanced+)
- **Machine avail RAM / GPU**: host-level metrics (Advanced+)
- Disk **size** when reported for installed weights

Unloaded cards never copy server-process RSS as if every model were consuming it.

## Memory warnings

Before Load / Load-via-Restart, MCC may show an **estimate**-labeled warning
when available RAM and model size measurements suggest pressure. Estimates are
never presented as guaranteed. Other models are never auto-unloaded.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/mcc/inventory` | Fleet-wide model cards (+ attached profiles) |
| POST | `/api/mcc/profiles/sync` | Non-destructive profile sync + summary |
| GET | `/api/mcc/capabilities?engine=` | Engine capability matrix |
| POST | `/api/mcc/settings/preview` | Pending vs active/configured diff + classification |
| POST | `/api/mcc/settings/apply` | `save_profile` / `supported` / `with_restart` |
| POST | `/api/mcc/setup/preview` | Guided management setup preview |
| POST | `/api/mcc/setup/apply` | Confirmed adopt / enable allow_control |
| POST | `/api/mcc/memory-warning` | Estimate warning helper |
| GET/POST/PATCH/DELETE | `/api/mcc/profiles…` | Profile CRUD / preview / import / export |
| POST/GET | `/api/fleet/<id>/ai/…` | Existing remote proxy (includes GET `/models`) |

## Safety

- No automatic downloads, loads, unloads, or restarts
- No authentication gates added
- Busy-guard on stop/restart preserved
- Existing contexts, timeouts, thinking settings, and GPU allocations are not changed by sync or Save Profile
- Apply With Restart only rewrites **recognized** startup flags and preserves unrelated args
- AI Lab on port 3010 remains operational and separate

## Single-machine vs multi-machine

- **One computer:** open This machine + Model Control; Sync Profiles; manage local configured servers.
- **Fleet:** add peers in All Machines; enable Allow remote control only where desired; use Model Control filters per machine.
