# FOXAI AI Lab Command Center

Station-local dashboard for AI server discovery, Direct Chat, Compare Models, and the Kayock QR Toolkit.

AI Lab is a **companion application** to Kayock Fleet Monitor. It is not a replacement for Fleet Monitor telemetry. Discovery reuses each machine’s Fleet Monitor `/metrics` feed, then verifies candidate chat endpoints from the Station LAN before exposing them for inference.

## Relationship to Fleet Monitor

| Component | Role |
|---|---|
| Fleet Monitor (`:5000` / peer ports) | Machine metrics, AI server inventory, GPU/CPU samplers |
| AI Lab (`:3010`) | Operator UI for chat/compare/QR against verified endpoints |
| Source in this repo | `ai-lab/` (independent runtime; see Install) |
| Runtime on Ubuntu Station | `/home/kayock/Applications/FoxaiAiLab` via user systemd `foxai-ai-lab.service` |

Do not merge AI Lab into the Fleet Monitor Python process. Keep the separate port and service so chat timeouts cannot stall fleet sampling.

## Install (Ubuntu Station)

```bash
# From a checkout of this repository:
rsync -a --exclude='.venv' --exclude='__pycache__' ai-lab/ ~/Applications/FoxaiAiLab/
cd ~/Applications/FoxaiAiLab
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
chmod +x start.sh

# User systemd unit (example in ai-lab/deploy/foxai-ai-lab.service)
mkdir -p ~/.config/systemd/user
cp ai-lab/deploy/foxai-ai-lab.service ~/.config/systemd/user/
# Or keep the existing unit that already points at ~/Applications/FoxaiAiLab
systemctl --user daemon-reload
systemctl --user enable --now foxai-ai-lab.service
```

## Port 3010 configuration

| Variable | Default | Meaning |
|---|---|---|
| `AI_LAB_HOST` | `0.0.0.0` | Bind address |
| `AI_LAB_PORT` | `3010` | HTTP port |

Example: `http://10.0.0.202:3010/` · Compare deep-link: `http://10.0.0.202:3010/#compare`

## Application startup

```bash
systemctl --user start foxai-ai-lab.service
systemctl --user status foxai-ai-lab.service
curl -sS http://127.0.0.1:3010/api/health
```

`start.sh` runs `.venv/bin/python app.py` from the application directory.

## Model discovery

1. Poll Fleet Monitor `/metrics` on Ubuntu Station, Ubuntu Portable, and Windows.
2. Normalize loopback hosts to each machine’s LAN IP so Station can reach peers.
3. Probe each candidate (`/api/tags` + `/api/ps` for Ollama, `/v1/models` or `/props` for OpenAI-compatible / llama-server).
4. Deduplicate by `(machine, lan_host, port)`.
5. Build a per-server `models[]` list distinguishing:
   - **loaded** — verified currently usable on the LAN endpoint
   - **installed_not_loaded** — listed by the server but not loaded
   - **unreachable_from_station** — Fleet reported a model, but Station cannot reach the API

AI Lab **never** auto-loads, unloads, downloads, or starts inference services.

Refresh Discovery (UI) or `GET /api/servers` updates the inventory.

## Direct Chat

1. Select a LAN-reachable chat-compatible server.
2. Select a **loaded** model (installed-but-not-loaded options are listed and blocked with a clear message).
3. Submit a prompt to `POST /api/chat` with the exact `endpoint`, `engine`, and `model`.
4. Responses preserve answer text and Ollama thinking/reasoning when present.
5. Unavailable endpoints return visible errors; switching models does not require a restart.

Existing defaults preserved unless a defect requires change: chat `max_tokens` default 96 (UI), proxy timeout 90s (non-stream) / 120s (stream), thinking default **disabled** in the UI select.

## Comparison Lab

1. Select Model A and Model B (optional Model C) from discovered identifiers.
2. Labels use `model · engine @ host:port · machine · loaded|not loaded` so machine names are not confused with model IDs.
3. Run comparison — the UI fires **independent** `/api/chat` requests per slot so a slow or failed model cannot block others from rendering.
4. Backend `POST /api/compare` also runs targets in a thread pool and flags `duplicate_same_model` when selectors intentionally share the same endpoint+model.
5. Unloaded selections error without substituting another model.

## QR Toolkit

AI Lab mounts the existing Kayock QR Generator at `/qr/` (source path configured in `app.py`, default `/home/kayock/Projects/Kayock-QR-Generator/src`).

- Generate / scan pages are served read-only from that project.
- In-page “QR for application links” uses the bundled `qrcode.js` library; encoded payload is shown in the status line for verification.

## API surface

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Liveness |
| GET | `/api/servers` | Discovery payload |
| GET | `/api/models?endpoint=&engine=` | Loaded / available models for one endpoint |
| POST | `/api/chat` | Single completion (optional stream NDJSON) |
| POST | `/api/compare` | Parallel multi-target completion |
| GET | `/api/homepage-links` | Family Hub link scrape for QR convenience |
| GET | `/qr/…` | QR Toolkit static assets |

## Safety constraints

- No credentials committed; no model weights in git.
- No authentication gates added by AI Lab.
- No automatic model load/unload.
- Homepage / Family Hub URLs are consumed read-only for QR convenience.
