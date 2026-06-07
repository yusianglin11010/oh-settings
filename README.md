# oh-settings — self-hosted OpenHands v1 infrastructure

Docker Compose stack + custom sandbox image + reverse-proxy/Cloudflare config to run
**OpenHands v1** (`ghcr.io/openhands/openhands:1.5.0`, agent-server architecture)
self-hosted, reachable remotely through a **single Cloudflare hostname** with
**Cloudflare Access** (email-gated), and with the agent's Docker access **isolated from
the host**.

Deep-dive runbook + troubleshooting: [`CLOUDFLARE-REMOTE-ACCESS.md`](./CLOUDFLARE-REMOTE-ACCESS.md).

---

## Components — what & why

| Path / service | What | Why |
|---|---|---|
| `docker-compose.yml` | The whole stack (app + caddy + dind + 2 MCP sidecars). | One file brings up everything; all the non-obvious env lives here with inline comments. |
| `openhands-app` (`:3010`) | The OpenHands v1 server. Mounts the **host** docker socket. | It orchestrates conversations and **spawns one sandbox container per conversation** (needs the host daemon for that). |
| `Dockerfile.sandbox` → `openhands-sandbox:latest` | Custom agent-server image = `agent-server:1.12.0-python` + **Go**. | The agent needs Go too; base image lacks it. Browser/GitHub tooling is **not** baked in — provided via MCP sidecars to keep the image slim. |
| `Dockerfile.sandbox-dotnet` | Optional .NET variant of the sandbox image. | For tasks that need the .NET SDK. |
| `build-and-test.sh` | Builds `openhands-sandbox:latest` and smoke-tests the runtimes. | Reproducible image build; verifies Python/Go/Node/tsc/docker are present. |
| `oh-dind` (`docker:27-dind`, `:2375`) | Isolated Docker-in-Docker daemon the agent uses. | So the agent can build/run containers **without root on the host** (see Security). |
| `oh-caddy` (`:8080`) | Reverse proxy. Demuxes `/runtime/<port>/*` → the right sandbox. | OpenHands v1 has the browser talk **directly to each sandbox's dynamic port**; Caddy collapses them onto one hostname so Cloudflare can serve them. |
| `github-mcp` (`:8082`), `playwright-mcp` (`:8931`) | MCP tool servers, run as HTTP sidecars. | In v1 the MCP client lives in the *sandbox*; running these over HTTP keeps the sandbox image slim (no GitHub/browser binaries baked in). |
| `set-mcp-servers.py` | Writes the two sidecars into `~/.openhands/settings.json` (`mcp_config.shttp_servers`). | Idempotent way to wire MCP; GitHub PAT is stored only in settings and sent as a bearer token. |
| `Caddyfile` | Caddy config for the demux above. | See §4 of the runbook. |
| `cloudflared` (external, you run it) | Cloudflare Tunnel — the **only** inbound path. | No ports are opened to the internet; ingress is an outbound tunnel + Cloudflare Access. |

---

## Architecture — how it fits together

```
Browser ──https──▶ agents.nildev.net ──(CF Access: email)── Tunnel ──▶ cloudflared ──▶ Caddy ──▶ openhands-app :3000
                     │  /runtime/<port>/*  (CF Access: BYPASS)        ──▶ Caddy ──▶ host.docker.internal:<port>  (sandbox)
                     └─────────────────────────────────────────────────────────────────────────────────────────┘

sandbox → app  : webhooks + default MCP  →  http://host.docker.internal:3010/...   (internal, NOT via CF)
app → sandbox  : health check            →  https://agents.nildev.net/runtime/<port>/alive  (via CF, BYPASS)
agent → docker : DOCKER_HOST             →  tcp://host.docker.internal:2375  (the isolated dind, not the host)
```

**The one rule that explains most of the config:** *browser → sandbox* must use the
**public** Cloudflare URL, but *sandbox ↔ app* (webhooks, MCP) must use an **internal**
URL — routing those through Cloudflare hits Access and breaks them.

---

## Setup — how to run

```bash
# 1. Build the custom sandbox image
./build-and-test.sh                      # -> openhands-sandbox:latest

# 2. Wire the MCP sidecars into OpenHands settings (PAT stored only here)
sudo python3 set-mcp-servers.py ghp_yourtoken    # re-runnable; PAT reused if omitted

# 3. Bring up the stack
docker compose up -d                     # app + caddy + dind + mcp sidecars

# 4. Cloudflare (one-time, in the CF dashboard)
#    - Tunnel ingress:  agents.nildev.net  ->  http://localhost:8080  (or http://caddy:80)
#    - Access app 1:  agents.nildev.net          -> Allow  (Emails: you)
#    - Access app 2:  agents.nildev.net/runtime/*-> Bypass (Everyone)   # see Security
```

Apply config changes: `docker compose up -d` (recreates changed services).
Recreating `openhands-app` clears running sandboxes — each conversation re-spawns one.

---

## Configuration decisions — what / how / why

These are the load-bearing settings on `openhands-app` (full comments in `docker-compose.yml`):

| Setting (how) | What it does | Why it must be this |
|---|---|---|
| `SANDBOX_CONTAINER_URL_PATTERN=https://agents.nildev.net/runtime/{port}` | URL the **browser** uses to reach each sandbox. | Collapses dynamic per-sandbox ports onto one CF hostname (Caddy demuxes by path). |
| `OH_WEB_URL=http://host.docker.internal:3010` | Base URL for **sandbox→app** callbacks (default MCP, webhook-secrets, CORS_0). | Must be **internal** — a public CF URL hairpins into Access, returns login HTML, and hangs MCP tool-listing (message send fails). |
| `OH_AGENT_SERVER_ENV={"OH_ALLOW_CORS_ORIGINS_1":"https://agents.nildev.net","DOCKER_HOST":"tcp://host.docker.internal:2375"}` | Injects extra env into every sandbox. | `OH_ALLOW_CORS_ORIGINS_1` re-adds the browser origin (since CORS_0 is now the internal URL); `DOCKER_HOST` points the agent at the isolated dind. |
| `SANDBOX_HOST_PORT=3010` | Port sandboxes use for the event webhook back to the app. | Must equal the **host-published** port (`3010`), not the container's internal `3000`. (Env name is `SANDBOX_HOST_PORT`, despite the docstring saying `OH_SANDBOX_HOST_PORT`.) |
| `ports: "3010:3000"` | Publishes the app. | Host side `3010` must match `SANDBOX_HOST_PORT`. |
| *(no `SANDBOX_VOLUMES` docker.sock)* | The agent is **not** given the host docker socket. | That socket = root on the host. The agent uses the dind daemon instead. |

Caddy: strips `/runtime/<port>` and proxies to `host.docker.internal:<port>`
(auto-upgrades WebSockets); everything else → `openhands:3000`. TLS is terminated at the
Cloudflare edge, so Caddy serves plain HTTP.

---

## Security posture

- **Inbound:** none opened. The only path in is the Cloudflare Tunnel (outbound `cloudflared`).
  Behind home NAT there is no way to reach the host's ports directly / bypass Cloudflare.
- **Main UI (`/`):** Cloudflare Access, email-gated.
- **`/runtime/*`:** **Bypass** (public through CF) — this is required because the app's own
  health check hairpins through CF without a cookie. Real operations are still gated by the
  per-conversation **session API key** (no key → `401`; only `/alive` & `/health` are open).
- **Agent Docker = dind, not host:** the agent can build/run containers, but they are nested
  inside `oh-dind` and **cannot reach the host**. dind's `2375` is bound to `172.17.0.1`
  (the bridge gateway) only — never the LAN/internet.
- **Blast radius:** a compromised agent gets root **inside dind**, not the host.
- **Optional hardening (not done):** to email-gate `/runtime/*` too, use split-horizon DNS so
  the app's health check reaches Caddy directly + a trusted cert (the IP-scoped Access bypass
  is not viable here because the WAN IP is dynamic). See `CLOUDFLARE-REMOTE-ACCESS.md` §6.

> ⚠️ Behavior note: because the agent's Docker is a separate daemon, `docker run -v <path>:...`
> binds against **dind's** filesystem, not the sandbox working dir. `docker build` and running
> services/tests are unaffected; use a shared volume if a task must mount the agent's own files.
