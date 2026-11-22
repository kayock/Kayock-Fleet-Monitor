# Updating the Windows installation

## Discovered install path (this lab)

From the running Windows monitor’s `ai_servers.json` path:

`C:\Users\kayoc\Downloads\Kayock-Fleet-Monitor-v3\Kayock-Fleet-Monitor\`

Service/listen: `http://10.0.0.104:5000/` (app version reported as `3.0.0`
before this upgrade).

## Station → Windows SSH status (blocker)

- OpenSSH on `10.0.0.104:22` accepts connections.
- Auth methods offered: `publickey,password,keyboard-interactive`.
- Station’s `~/.ssh/id_ed25519` is offered and **rejected** (key not in the
  Windows account’s `authorized_keys`).
- No password/interactive auth is used from this automation.
- No new credentials, firewall rules, or security policy changes were made.

### Exact permission needed for unattended deploy

On the Windows account that owns the Fleet Monitor install (likely
`kayoc` / `Eric`), append Station’s public key to:

`C:\Users\<account>\.ssh\authorized_keys`

Station public key (`~/.ssh/id_ed25519.pub` on Ubuntu Station):

```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHhbXAf9T9JRd49eAMpfIEN+g1DL9EyJdbZmMMQEs679 kayock@eric-UbuntuStation
```

Print the live key on Station with:

```bash
cat ~/.ssh/id_ed25519.pub
```

After authorization, from Station:

```bash
ssh -o BatchMode=yes YOUR_WINDOWS_USER@10.0.0.104 hostname
```

## Manual update (no SSH required)

1. On Windows, stop Fleet Monitor (close the console or stop the task).
2. Backup `fleet.json`, `ai_servers.json`, `monitor_config.json` in the
   install folder.
3. Either:
   - `git pull` if that folder is a git checkout of
     `https://github.com/kayock/Kayock-Fleet-Monitor` on branch
     `fix/ai-lab-reliability`, or
   - Download/copy the branch source over the install, preserving the three
     JSON files and `.venv`.
4. Start with `start_monitor.bat`.
5. Verify:
   - `http://127.0.0.1:5000/healthz` shows `"version":"3.1.0"`
   - Process list no longer ranks **System Idle Process**
   - Storage/network rows include `kind` classification in `/metrics`

## Why this update matters on Windows

- Idle Process exclusion from CPU rankings
- Clearer CPU labeling (per-core vs aggregate)
- AI discovery InvalidURL / Docker-port hardening (parity with Linux)
- Page title uses the machine label instead of a hardcoded Windows string
