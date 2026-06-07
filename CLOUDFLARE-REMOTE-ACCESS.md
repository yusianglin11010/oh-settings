# OpenHands v1 behind Cloudflare (Tunnel + Access) — Runbook

How to expose a self-hosted **OpenHands v1 (agent-server architecture, e.g. `ghcr.io/openhands/openhands:1.5.0`)**
on a single Cloudflare hostname (`agents.nildev.net`) protected by Cloudflare Access (email policy),
so conversations actually work end-to-end.

> Why this is non-trivial: in OpenHands v1 the **browser connects directly to each
> conversation's agent-server**, and every sandbox publishes its **own dynamic host
> port**. That collides head-on with Cloudflare (single hostname, port 443 only, Access
> on everything). On top of that, several **sandbox→app** and **app→sandbox** callbacks
> are derived from one config value (`OH_WEB_URL` / `container_url_pattern`), and the
> "obvious" public-URL setting silently breaks them. This doc captures the working set.

---

## 0. 處理摘要 (what was done & final state)

**目標**：讓自架 OpenHands v1 透過 Cloudflare Tunnel + Access（Email 政策）在 `agents.nildev.net`
單一域名上完整可用，並把「agent = host root」的風險收掉。**全部已驗證通過 ✅**

**最終狀態（都實測過）**
- ✅ 經 CF Tunnel + Access 遠端存取主畫面（Email 鎖）
- ✅ 對話 socket 連得上、訊息收發正常
- ✅ MCP（default / github / playwright）正常，agent 會回應
- ✅ agent 能 build/run container，但跑在隔離的 dind，**碰不到 host**

**做的 5 項變更（→ 對應章節）**

| # | 變更 | 解決什麼 | 章節 |
|---|---|---|---|
| 1 | 加 **Caddy** sidecar，`/runtime/<port>/*` 路徑式 demux | 瀏覽器連不到動態 port 的 sandbox | §2(1), §4 |
| 2 | CF Access 對 `/runtime/*` 設 **Bypass** | `Sandbox failed to start within 120s`（app 健康檢查被 Access 擋） | §2(2), §5, §6 |
| 3 | `SANDBOX_HOST_PORT=3010` | webhook 回呼打錯 port（3000 vs 發佈的 3010） | §2(3) |
| 4 | `OH_WEB_URL=http://host.docker.internal:3010` + 注入 `OH_ALLOW_CORS_ORIGINS_1` | 訊息送不出去（default MCP 走公開網址撞 Access → `text/html` → 30s timeout） | §2(4/4b) |
| 5 | **Docker-in-Docker**（dind）取代裸 `docker.sock` 掛載 | agent 被打穿 = host root 的爆炸半徑 | §9 |

**目前跑著的服務**：`openhands-app`（:3010）、`oh-caddy`（:8080→tunnel）、`oh-dind`（:2375 綁 172.17.0.1）、
`github-mcp`（:8082）、`playwright-mcp`（:8931）、外部的 `cloudflared`（tunnel，inbound 唯一入口）。

**安全現況**
- 網路層：家用 NAT + tunnel-only，**沒有對外開 inbound**，繞過 CF 直打主機的路不存在。
- `/runtime/*`：因 Bypass 而對公網開放，但實際操作靠 sandbox 的 **session API key**（無 key 一律 401，只有 `/alive`、`/health` 裸奔）。
- 爆炸半徑：dind 隔離後，agent 即使被穿也只拿到巢狀 daemon 的 root，**不是 host root**。
- 待辦（可選、不急）：動態 IP 讓「IP-scoped bypass」不可行；若要把 `/runtime/*` 也鎖回 Email，走 **split-horizon + LE 憑證**（§6 末）。

---

## 1. Architecture / request flows

```
                         ┌─────────────── Cloudflare edge (Access: email policy) ───────────────┐
  Browser ──https──▶ agents.nildev.net ──Tunnel──▶ cloudflared ──▶ Caddy (:80) ──▶ openhands-app :3000
                         │   /runtime/<port>/*  (Access: BYPASS)  ──▶ Caddy ──▶ host.docker.internal:<port>  (sandbox)
                         └──────────────────────────────────────────────────────────────────────┘

  app  →  sandbox  : health check  GET https://agents.nildev.net/runtime/<port>/alive   (hairpins through CF; needs BYPASS)
  app  →  sandbox  : internal mgmt via  http://host.docker.internal:<port>              (SANDBOX_LOCAL_RUNTIME_URL)
  sandbox → app    : event webhook   POST http://host.docker.internal:3010/api/v1/webhooks      (SANDBOX_HOST_PORT)
  sandbox → app    : default MCP     POST http://host.docker.internal:3010/mcp/mcp              (OH_WEB_URL, internal)
  sandbox → app    : webhook-secrets GET  http://host.docker.internal:3010/api/v1/webhooks/...  (OH_WEB_URL, internal)
```

Key insight: **two different "directions" need two different URLs.**
- **Browser → sandbox** must use the *public* Cloudflare URL (`https://agents.nildev.net/runtime/{port}`).
- **sandbox ↔ app** (MCP, webhooks) must use an *internal* URL (`http://host.docker.internal:3010`) — going through CF would hit Access and break.

---

## 2. The four fixes (each one is load-bearing)

| # | Problem (symptom) | Root cause | Fix |
|---|---|---|---|
| 1 | Browser can't reach per-conversation socket through CF | v1 browser connects directly to each sandbox's **dynamic host port**; CF can't proxy LAN IP / random ports | **Caddy sidecar** demuxes `/runtime/<port>/*` → `host.docker.internal:<port>`, collapsing all sandbox ports onto one hostname. `SANDBOX_CONTAINER_URL_PATTERN=https://agents.nildev.net/runtime/{port}` |
| 2 | `Sandbox failed to start within 120s` | App health-checks the sandbox at the **public** URL (`container_url_pattern`) → hairpins out → **CF Access 302** (app has no cookie) → never "alive" | **Cloudflare Access BYPASS** on path `runtime/*` (see §5) |
| 3 | Conversation appeared stuck; webhook never received | Sandbox→app event webhook went to `host.docker.internal:`**`3000`** (container port), but app is published on **3010** → connection refused | `SANDBOX_HOST_PORT=3010` (must equal the **host-published** port). *Env var is `SANDBOX_HOST_PORT`, NOT the `OH_SANDBOX_HOST_PORT` shown in the field docstring.* |
| 4 | Message "送不出去" → `MCPTimeoutError: ... timed out after 30s`, sandbox log `Unexpected content type: text/html` | The **default OpenHands MCP** url = `{OH_WEB_URL}/mcp/mcp`. With `OH_WEB_URL` = public CF host, the sandbox hairpins out → **CF Access returns login HTML** → MCP tool-listing hangs 30s → `send_message` throws | Set `OH_WEB_URL=http://host.docker.internal:3010` (internal) so MCP/webhooks go direct to the app. Then re-add the browser origin to CORS via `OH_AGENT_SERVER_ENV` (next row) |
| 4b | (side effect of 4) browser WebSocket rejected on CORS | `OH_ALLOW_CORS_ORIGINS_0` is auto-set to `OH_WEB_URL`, now internal — doesn't match browser `Origin: https://agents.nildev.net` | Inject a 2nd CORS entry into every sandbox: `OH_AGENT_SERVER_ENV={"OH_ALLOW_CORS_ORIGINS_1":"https://agents.nildev.net"}` |

---

## 3. `docker-compose.yml` — the critical env (openhands-app service)

```yaml
    environment:
      # [Fix 1] Browser → sandbox: public CF path, demuxed by Caddy.
      - SANDBOX_CONTAINER_URL_PATTERN=https://agents.nildev.net/runtime/{port}

      # [Fix 4] sandbox → app callbacks (default MCP, webhook-secrets, CORS_0 base)
      #         MUST be internal — public CF host would hit Access and hang MCP.
      - OH_WEB_URL=http://host.docker.internal:3010

      # [Fix 4b] re-add the real browser origin to the sandbox CORS allow-list
      #          (+ DOCKER_HOST → the isolated dind daemon, see §9)
      - 'OH_AGENT_SERVER_ENV={"OH_ALLOW_CORS_ORIGINS_1":"https://agents.nildev.net","DOCKER_HOST":"tcp://host.docker.internal:2375"}'

      # [Fix 3] event webhook callback port = the HOST-published port (3010), not 3000.
      - SANDBOX_HOST_PORT=3010

      # unchanged supporting settings
      - SANDBOX_STARTUP_GRACE_SECONDS=120
      - SANDBOX_BASE_CONTAINER_IMAGE=openhands-sandbox:latest
      - AGENT_SERVER_IMAGE_REPOSITORY=openhands-sandbox
      - AGENT_SERVER_IMAGE_TAG=latest
      # NB: do NOT mount the host docker socket into sandboxes — that = host root for the
      # agent. Use the dind daemon instead (see §9). Only openhands-app keeps a socket
      # mount (in `volumes:`, to spawn sandboxes).
    ports:
      - "3010:3000"     # host:container — the 3010 must match SANDBOX_HOST_PORT
```

Caddy sidecar service:

```yaml
  caddy:
    image: caddy:2-alpine
    container_name: oh-caddy
    extra_hosts:
      - "host.docker.internal:host-gateway"
    ports:
      - "8080:80"                      # cloudflared ingress points here
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    restart: unless-stopped
```

(top-level `volumes: { caddy_data:, caddy_config: }`)

---

## 4. `Caddyfile`

```caddy
{
	auto_https off          # TLS terminated at Cloudflare edge; cloudflared talks plain HTTP to us
	admin off
}

:80 {
	# /runtime/<hostport>/...  ->  host.docker.internal:<hostport>  (strip the prefix)
	@runtime path_regexp rt ^/runtime/(\d+)(?:/(.*))?$
	handle @runtime {
		rewrite * /{re.rt.2}
		reverse_proxy host.docker.internal:{re.rt.1}     # reverse_proxy auto-upgrades WebSockets
	}
	# everything else -> the app UI + its own socket
	handle {
		reverse_proxy openhands:3000
	}
}
```

> NOTE: `admin off` means config changes require `docker compose restart caddy` (not `caddy reload`).

---

## 5. Cloudflare setup

### Tunnel ingress
Point the hostname at Caddy (you do this in the tunnel config / dashboard):
```
agents.nildev.net  →  http://caddy:80     (if cloudflared is on this compose network)
                   →  http://localhost:8080 (if cloudflared runs on the host)
```

### Access applications (order matters — most-specific path wins)
1. **Main UI** — application `agents.nildev.net` (path empty = everything):
   - Policy: **Allow**, Include = `Emails: <you>`  → email-gated.
2. **Runtime** — application `agents.nildev.net` path `runtime/*`:
   - Policy: **Bypass**, Include = `Everyone`.
   - Required because the app's own health check (`/runtime/<port>/alive`) hairpins
     through CF with **no cookie** and would otherwise get a 302. The browser socket
     rides the same path.

> ⚠ `Bypass` ≠ `Allow`. `Allow + Everyone` still forces authentication (cookieless app
> requests get 302). Must be **Bypass**. See §6 for the security trade-off and a tighter
> alternative.

---

## 6. Security — the `/runtime/*` Bypass and the docker.sock

`Bypass` makes `https://agents.nildev.net/runtime/*` **public** (no email gate). What still protects it:
- the agent-server requires a per-conversation **session API key** (`OH_SESSION_API_KEYS`) for real ops;
- the host port is **dynamic** (random, embedded in the path).

What's exposed / the risks:
- **`/runtime/<port>/alive` and `/health` are unauthenticated** → an attacker can port-scan
  `/runtime/<1024-65535>/alive` to **enumerate live sandboxes**.
- **Defense-in-depth dropped** on the runtime: two locks (CF email + session key) → one (session key).
- **🔴 Blast radius:** every sandbox mounts `/var/run/docker.sock` (`SANDBOX_VOLUMES`) = **root on the
  host**. So *any* unauthenticated hole in the agent-server, now reachable from the public internet,
  is potentially internet→host-root.

### Recommended hardening (keeps it working, restores the email gate)
Replace the `runtime/*` **Bypass / Everyone** policy with **two** policies on the same app:
- **Allow** — Include `Emails: <you>`   → browsers (they carry the CF SSO cookie) stay email-gated.
- **Bypass** — Include `IP Ranges: 180.218.221.26/32`  → only the server's own hairpin health check.

Result: browser = email-gated, app health check = allowed by source IP, **random public = blocked**.
(Verify the host's egress IP is stable: `curl -s ifconfig.me` → currently `180.218.221.26`.)

### Gold standard (no public exposure at all)
Split-horizon: make the **app container** resolve `agents.nildev.net` → Caddy directly (so its health
check never leaves the host), and have Caddy serve that hostname over HTTPS with a cert the app trusts
(Let's Encrypt via the Cloudflare DNS-01 challenge). Then `/runtime/*` can be pure **Allow (email)** with
no bypass. Heavier setup (custom Caddy build + CF API token); do this only if you need the runtime
itself email-gated.

### Orthogonal but important
The `docker.sock` mount is the single biggest risk multiplier. Since the agent **does** need
to launch containers, we don't just remove it — we replace it with an isolated **Docker-in-Docker**
daemon so the agent can still run containers but can't reach the host. See **§9**.

---

## 7. Troubleshooting cheat-sheet

| Symptom | Check | Likely fix |
|---|---|---|
| Page loads, conversation socket never connects | browser console / is origin the CF host? | Access on whole host but app health blocked — confirm §5 Bypass on `runtime/*` |
| `Sandbox failed to start within 120s` | `docker exec openhands-app curl -s -o /dev/null -w '%{http_code}' https://agents.nildev.net/runtime/<port>/alive` → if **302** | CF Access blocking app health check → add/repair `runtime/*` Bypass |
| `...failed to start` but `/alive` returns 200 internally | `docker logs <sandbox>` shows it booted, but app never gets webhook | `SANDBOX_HOST_PORT` ≠ host-published port → set it = ports host side (3010) |
| Message won't send; `MCPTimeoutError`, sandbox log `Unexpected content type: text/html` | is `OH_WEB_URL` the public CF host? | set `OH_WEB_URL=http://host.docker.internal:3010` (+ `OH_ALLOW_CORS_ORIGINS_1` injection) |
| Message won't send; sandbox log CORS/Origin rejected | `docker inspect <sandbox> | grep ALLOW_CORS` | ensure `OH_ALLOW_CORS_ORIGINS_1=https://agents.nildev.net` is injected via `OH_AGENT_SERVER_ENV` |
| Caddy 502 `connection refused` on a `/runtime/<port>` | sandbox still booting (PyInstaller ~17s) | transient; resolves once agent-server listens. If persistent, that port's sandbox died |
| Sandboxes pile up / `resume` churn | `docker ps --filter name=oh-agent-server | wc -l` vs `max_num_sandboxes` (default 5) | clean up: `docker ps -aq --filter name=oh-agent-server | xargs -r docker rm -f` |

### Handy commands
```bash
# how many sandboxes / their ports
for c in $(docker ps --filter name=oh-agent-server --format '{{.Names}}'); do \
  echo "$c -> $(docker port "$c" 8000/tcp | grep 0.0.0.0 | sed 's/.*://')"; done

# confirm Access bypass works for the app's health path (expect 200, not 302)
docker exec openhands-app sh -c 'curl -s -o /dev/null -w "%{http_code}\n" https://agents.nildev.net/runtime/<port>/alive'

# verify the live fix env on the app
docker inspect openhands-app --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep -E 'OH_WEB_URL|OH_AGENT_SERVER_ENV|SANDBOX_HOST_PORT|SANDBOX_CONTAINER_URL_PATTERN'

# watch a message flow (no MCP timeout, agent responds)
docker logs --since 120s <sandbox> 2>&1 | grep -iE 'Received message|text/html|MCPTimeout'
```

---

## 8. Apply / restart

```bash
cd /home/debian/oh-settings
docker compose config >/dev/null && echo OK     # validate
docker ps -aq --filter name=oh-agent-server | xargs -r docker rm -f   # clean stale sandboxes
docker compose up -d                              # recreates app+caddy, picks up env
```

Recreating `openhands-app` clears all running sandboxes (each conversation re-spawns one on demand).

---

## 9. Agent Docker isolation — Docker-in-Docker (dind)

**Problem:** every sandbox used to bind-mount `/var/run/docker.sock` (the *host* daemon).
That gave the agent **effective root on the host** — it could `docker run --privileged -v /:/host`
and own the box. Combined with the public `/runtime/*` Bypass (§6), a single agent-server hole =
internet → host root.

**Why not docker-socket-proxy:** a socket proxy (Tecnativa) filters by API *endpoint*, not by request
*body*. The agent needs `POST /containers/create`, and the proxy can't stop that create from asking for
`Privileged:true` / host binds. So with container-create enabled it provides ~no protection against
host-root. (It's only useful for read-only `POST=0` monitoring tools.)

**Fix:** give the agent its own **isolated Docker daemon**. The agent's containers are nested inside
dind and cannot reach the host. One shared daemon for all sandboxes (cheapest; host-isolation doesn't
need a per-sandbox daemon).

### compose

```yaml
  dind:
    image: docker:27-dind
    container_name: oh-dind
    privileged: true                 # required to run a nested daemon
    environment:
      - DOCKER_TLS_CERTDIR=          # empty -> plain tcp 2375 (no TLS)
    ports:
      - "172.17.0.1:2375:2375"       # ⚠ bridge-gateway ONLY, never 0.0.0.0
    volumes:
      - dind_storage:/var/lib/docker # nested image cache, shared by sandboxes
    restart: unless-stopped
# top-level: volumes: { dind_storage: }
```

- **`172.17.0.1` bind is the security control.** That's the docker bridge gateway = what sandboxes
  reach via `host.docker.internal`. Binding there means LAN/internet can't reach the (unauthenticated)
  daemon; only containers on this host can. Binding `0.0.0.0:2375` would hand nested-root to anyone on
  the LAN — don't.
- Sandboxes are told to use it via `OH_AGENT_SERVER_ENV` → `DOCKER_HOST=tcp://host.docker.internal:2375`
  (§3). The sandbox image already has the docker CLI; with `DOCKER_HOST` set and no socket mounted, it
  transparently targets dind.
- `openhands-app` keeps its OWN `docker.sock` mount (it needs the host daemon to spawn sandboxes).

### What this buys / costs
- ✅ Compromised agent gets root **inside dind**, not the host (needs a container-escape to reach host).
- ✅ Agent can still `docker build` / `docker run` / pull images.
- 💰 Cost: ~400MB image (once) + ~150MB RAM for the one daemon; **~0 per extra sandbox** (shared).
- ⚠️ **Behavior change:** `docker run -v <path>:...` now binds against **dind's** filesystem, not the
  sandbox's working dir. `docker build` (context sent over the API) and running services/tests are fine;
  bind-mounting the agent's own files into a nested container won't see them (use a shared volume if a
  task ever needs that).

### Verify isolation
```bash
# 2375 must be bound to 172.17.0.1 ONLY (not 0.0.0.0)
ss -tlnp | grep ':2375'

# from a sandbox-like container: connects to dind, sees an EMPTY ps (no host containers), can run one
docker run --rm --add-host host.docker.internal:host-gateway \
  -e DOCKER_HOST=tcp://host.docker.internal:2375 docker:cli sh -c \
  'docker info --format "{{.ServerVersion}}"; docker ps; docker run --rm hello-world'
```
`docker ps` returning empty (vs the host's real container list) is the proof the agent is walled off.
