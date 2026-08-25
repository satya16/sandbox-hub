# sandbox-hub

A local control panel for spinning up disposable test resources -- REST APIs
and MCP servers, each available with no auth, static API-key auth, or OAuth2
-- as Docker containers, toggled from one page.

Useful when you're building a client (an app, an agent, an MCP client) and
need something real to point it at without standing up infrastructure or
depending on a live third-party service.

## What it gives you

| Resource | No auth | API key | OAuth2 |
|---|---|---|---|
| REST API | `rest-none` | `rest-apikey` | `rest-oauth` |
| MCP server (streamable-http) | `mcp-none` | `mcp-apikey` | `mcp-oauth` |

Plus a **Local OAuth2 Provider** -- a minimal authorization server
(`client_credentials` and `authorization_code`+PKCE grants, token
introspection) that starts automatically whenever an OAuth-mode resource is
enabled, and issues real credentials you can use to actually get a token.

Every resource is a real, runnable service: the REST APIs serve a small
`/items` collection, the MCP servers expose `echo` / `add` / `current_time`
tools over the streamable-http transport. Enough to point a real client at
and see auth actually enforced -- 401s, `WWW-Authenticate` challenges,
token introspection -- not a mock.

## Design

- **The hub** (`hub/`, FastAPI + Docker SDK) is the only thing you run
  directly. It talks to the Docker Engine API over `/var/run/docker.sock`
  and creates/starts/stops each resource as a sibling container on a
  dedicated `sandboxhub-net` bridge network, live -- no compose file
  regeneration, no restart-to-reconfigure.
- **The resource images** (`resources/rest-api`, `resources/mcp-server`,
  `resources/oauth-provider`) are plain, undockered-by-the-hub images. Each
  is parameterized by env vars (`AUTH_MODE=none|apikey|oauth`, ...) so one
  image backs all three auth variants of that resource type.
- **The UI** (`hub-ui/`, React + AntD) is built at image-build time and
  served directly by the hub, so the whole thing is one container and one
  URL: `http://localhost:8090`.
- All published ports bind to `127.0.0.1` by default (`SANDBOXHUB_BIND_HOST`
  to change) -- these are test/dummy auth servers, not things you want on
  your LAN.
- State lives in Docker itself, not a separate database: resource status is
  read live from `docker ps`, and API keys are read back from the running
  container's own env. OAuth client credentials are the one exception --
  they live in the hub's memory (and the provider's), since both are
  intentionally ephemeral disposable test infra. Restarting the hub or the
  provider resets them; just hit "Rotate credentials".

## Quickstart

Requires Docker.

```sh
./setup.sh
```

Then open **http://localhost:8090**, flip on whichever resources you need,
and copy the generated URL / key / curl snippet into whatever you're testing.

Manual equivalent:

```sh
docker compose --profile build-only build   # builds rest-api, mcp-server, oauth-provider images
docker compose up -d --build hub            # builds the UI into the hub image, starts it
```

### Developing the UI

```sh
cd hub-ui
npm install
npm run dev       # http://localhost:5173, proxies /api to the hub on :8090
```

## Ports

| Service | Host port |
|---|---|
| hub (UI + API) | 8090 |
| rest-none | 8101 |
| rest-apikey | 8102 |
| rest-oauth | 8103 |
| mcp-none | 8111 |
| mcp-apikey | 8112 |
| mcp-oauth | 8113 |
| oauth-provider | 8199 |

## Roadmap ideas

- Publish prebuilt images to Docker Hub (`satya16dev/sandbox-hub-*`) so
  `docker run` works without cloning.
- A realistic IdP option (Keycloak and/or Dex) alongside the built-in
  minimal OAuth provider, for testing against something closer to what
  you'd integrate with in production.

## Security note

This exists to make local testing convenient, not to be a secure identity
provider. Tokens, client secrets, and API keys generated here are for local
development only -- don't point real credentials or production traffic at
any of this.
