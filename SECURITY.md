# Security guidance

Kayock Fleet Monitor is intended for trusted local networks. It is not designed to be publicly internet-facing and does not provide a general user-authentication boundary for its management endpoints.

- Bind to `127.0.0.1` for local-only use. Enable LAN binding only on a trusted network.
- Restrict inbound HTTP to trusted hosts or the local subnet through your firewall. Do not forward the dashboard port through your router.
- The UI's remote-control setting is a fleet feature permission, **not** authentication for every direct HTTP endpoint.
- Never commit `fleet.json`, `ai_servers.json`, `monitor_config.json` or real machine credentials. 
- Report security concerns privately to the repository maintainer rather than publishing exploit details in an issue.
