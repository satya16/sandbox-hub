# sandbox-hub

A local control panel for spinning up disposable test resources -- REST APIs
and MCP servers -- as Docker containers, each with its own auth setup, from
one page. Click **New**, pick what to run and how it should be secured, hit
**Start**. Run as many at once as you like, in any mix of configs.

Useful when you're building a client (an app, an agent, an MCP client) and
need something real to point it at without standing up infrastructure or
depending on a live third-party service.

## What it gives you

Two kinds of resource, each configurable per instance:

- **REST API** -- a small sample API (`/items`). Auth: none, static API key,
  or OAuth2. Also serves its own **OpenAPI spec** (`/openapi.json`, version
  3.0 or 3.1 -- your choice) plus **Swagger UI** (`/docs`) and **ReDoc**
  (`/redoc`), with the spec/docs optionally gated behind their own static
  token, independent of whatever protects `/items`.
- **MCP server** (streamable-http) -- `echo` / `add` / `current_time` tools.
  Auth: none, static API key, or OAuth2 (spec-compliant Bearer +
  `WWW-Authenticate` challenge, discoverable via protected-resource
  metadata).

Nothing is a fixed toggle -- every instance is created with the settings you
pick in the New Resource dialog, gets its own container and port, and lives
until you delete it. Start three REST APIs with three different auth setups
side by side if that's what you need.

Behind the scenes, a **Local OAuth2 Provider** -- a minimal authorization
server (`client_credentials` and `authorization_code`+PKCE grants, token
introspection) -- starts automatically the moment any instance needs OAuth,
and stops itself once none do. It issues real credentials you can use to
actually get a token, not a stub.

Every resource is a real, runnable service -- enough to point a real client
at and see auth actually enforced (401s, `WWW-Authenticate` challenges,
token introspection, version-correct OpenAPI documents), not a mock.

## Design

- **The hub** (`hub/`, FastAPI + Docker SDK) is the only thing you run
  directly. It talks to the Docker Engine API over `/var/run/docker.sock`
  and creates each instance as a sibling container on a dedicated
  `sandboxhub-net` bridge network, live, on a freshly-allocated host port --
  no compose file regeneration, no fixed port table to run out of.
- **The resource images** (`resources/rest-api`, `resources/mcp-server`,
  `resources/oauth-provider`) are plain, hub-agnostic images. Each is
  parameterized entirely by env vars (`AUTH_MODE`, `OPENAPI_VERSION`, ...) so
  one image backs every instance of that kind, however it's configured.
- **The UI** (`hub-ui/`, React + AntD) is built at image-build time and
  served directly by the hub, so the whole thing is one container and one
  URL: `http://localhost:8090`.
- All published ports bind to `127.0.0.1` by default (`SANDBOXHUB_BIND_HOST`
  to change) -- these are test/dummy auth servers, not things you want on
  your LAN.
- Docker is the source of truth for what's running -- no separate database.
  Each instance is a container carrying its config as labels; status is read
  live from `docker ps`, and API keys / spec tokens are read back from the
  running container's own env. OAuth client credentials are the one
  exception -- they live in the hub's memory (and the provider's), since
  both are intentionally ephemeral disposable test infra. Restarting the hub
  or the provider resets them; just hit "Rotate credentials".

## Quickstart

Requires Docker.

```sh
./setup.sh
```

Then open **http://localhost:8090**, click **New**, and configure whatever
you need. Copy the generated URL / key / curl snippet into whatever you're
testing.

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
