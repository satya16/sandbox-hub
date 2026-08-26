# sandbox-hub

A local control panel for spinning up disposable test resources -- REST
APIs, MCP servers, mock endpoints, webhook receivers, a chaos/rate-limit
sandbox, and a minimal API tester -- as Docker containers, each with its own
auth setup, from one page. Click **New**, pick what to run and how it
should be secured, hit **Start**. Run as many at once as you like, in any
mix of configs.

Useful when you're building a client (an app, an agent, an MCP client) and
need something real to point it at without standing up infrastructure or
depending on a live third-party service.

## What it gives you

Six kinds of resource, each configurable per instance:

- **REST API** -- a small sample API (`/items`). Also serves its own
  **OpenAPI spec** (`/openapi.json`, version 3.0 or 3.1 -- your choice) plus
  **Swagger UI** (`/docs`) and **ReDoc** (`/redoc`), with the spec/docs
  optionally gated behind their own static token, independent of whatever
  protects `/items`.
- **MCP server** (streamable-http) -- `echo` / `add` / `current_time` tools.
- **Mock API** -- define your own routes after starting it: method + path
  (or `*` for "any"/catch-all) mapped to a status code and a **templated
  JSON response** (`{{request.body.x}}`, `{{request.query.x}}`, `{{uuid}}`,
  `{{now}}`, ...). Routes can require certain fields be present in the
  request body (400 if missing), so you can test a client against a schema
  you define instead of a fixed sample one.
- **Webhook Receiver** -- accepts any request at any path and always
  responds 200. Every payload shows up live in the instance's log stream
  and in a "Received requests" panel on the card -- point a webhook sender
  at it and watch it arrive.
- **Rate Limit / Chaos API** -- a single `/test` endpoint you reconfigure
  live from the card: **normal** (always 200), **rate limit** (N requests
  per window, then 429 + `Retry-After`), or **chaos** (pick the exact
  status code, JSON body, injected latency, and a random-failure rate).
- **API Tester** -- a minimal, ephemeral REST client (like a tiny
  Postman/Bruno). Send one-off requests to any URL, or set it polling one on
  an interval and checking each response against an expected status /
  body-contains rule -- pass/fail results stream live to the card's log
  panel. Runs entirely server-side inside its own container (so polling
  keeps going whether or not the page is open, and there's no CORS
  dependency on the target), and gets its own full-page UI -- the card just
  links to it. Nothing is saved; it forgets everything on restart. To reach
  another sandbox-hub instance from here, use that instance's **Internal
  URL** (shown on its card) rather than its `localhost:PORT` one -- from
  inside a container, `localhost` means itself, not your machine.

All auth-capable kinds (REST API, MCP server, Mock API, Webhook Receiver)
support the same auth modes: **none**, **static API key**, **HTTP Basic**,
a **self-contained JWT** (signed + verified locally with no external calls
-- tests a client's own token handling rather than a lookup), **cookie /
session login** (`POST /login`, then a cookie gates everything else), and
**OAuth2** (client_credentials or authorization_code+PKCE against the local
provider, with proper `WWW-Authenticate` challenges and, for MCP, discovery
via protected-resource metadata).

Nothing is a fixed toggle -- every instance is created with the settings you
pick in the New Resource dialog, gets its own container and port, and lives
until you delete it. Start three REST APIs with three different auth setups
side by side if that's what you need.

Behind the scenes, a **Local OAuth2 Provider** -- a minimal authorization
server (`client_credentials`, `authorization_code`+PKCE, and a rotating
`refresh_token` grant -- access tokens default to a short 2-minute lifetime
specifically so refresh actually gets exercised -- plus token introspection)
starts automatically the moment any instance needs OAuth, and stops itself
once none do. It issues real credentials you can use to actually get a
token, not a stub.

Every resource is a real, runnable service -- enough to point a real client
at and see auth actually enforced (401s, `WWW-Authenticate` challenges,
token/JWT expiry, rate limits, injected failures), not a mock.

## Design

- **The hub** (`hub/`, FastAPI + Docker SDK) is the only thing you run
  directly. It talks to the Docker Engine API over `/var/run/docker.sock`
  and creates each instance as a sibling container on a dedicated
  `sandboxhub-net` bridge network, live, on a freshly-allocated host port --
  no compose file regeneration, no fixed port table to run out of. The port
  is also editable per instance after creation (recreates the container on
  the new port, credentials/config untouched); the Update button on the
  card stays disabled for the whole recreate cycle and only re-enables once
  the instance is confirmed live again.
- **The resource images** (`resources/rest-api`, `resources/mcp-server`,
  `resources/mock-api`, `resources/webhook-receiver`,
  `resources/chaos-api`, `resources/api-tester`, `resources/oauth-provider`)
  are plain, hub-agnostic images. Each is parameterized entirely by env vars
  (`AUTH_MODE`, `OPENAPI_VERSION`, ...) so one image backs every instance of
  that kind, however it's configured. Mock API and Chaos API additionally
  expose a small private admin API (`/_routes`, `/_config`) the hub calls
  on your behalf to reconfigure them live, with no restart. API Tester is
  the one kind with its own full page (linked from its card) instead of
  being driven through the hub's UI -- it's a self-contained tool, not
  something the hub needs to configure.
- **The UI** (`hub-ui/`, React + Material UI) is built at image-build time and
  served directly by the hub, so the whole thing is one container and one
  URL: `http://localhost:8090`.
- All published ports bind to `127.0.0.1` by default (`SANDBOXHUB_BIND_HOST`
  to change) -- these are test/dummy auth servers, not things you want on
  your LAN.
- Docker is the source of truth for what's running -- no separate database.
  Each instance is a container carrying its config as labels; status is read
  live from `docker ps`, and API keys / passwords / spec tokens are read
  back from the running container's own env. OAuth client credentials are
  the one exception -- they live in the hub's memory (and the provider's),
  since both are intentionally ephemeral disposable test infra. Restarting
  the hub or the provider resets them; just hit "Rotate credentials".

## Quickstart

Requires Docker.

### Prebuilt image (no clone needed)

```sh
docker run -d --name sandboxhub-hub \
  -p 8090:8090 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e SANDBOXHUB_BIND_HOST=0.0.0.0 \
  satya16dev/sandboxhub-hub
```

That's the only image you pull yourself. The hub creates every resource
(`rest-api`, `mcp-server`, `mock-api`, `webhook-receiver`, `chaos-api`,
`api-tester`, `oauth-provider`) as a sibling container on demand, and Docker
auto-pulls each one from `satya16dev/sandboxhub-<kind>` the first time you
actually use that kind -- you never pull the other seven by hand.

`SANDBOXHUB_BIND_HOST=0.0.0.0` is required here: the default (`127.0.0.1`)
binds resource ports to the hub *container's* own loopback, which is
unreachable from your host. And note the Docker socket mount gives the
container root-equivalent access to your machine -- that's inherent to how
the hub creates sibling containers, not incidental.

Then open **http://localhost:8090**, click **New**, and configure whatever
you need. Copy the generated URL / key / curl snippet into whatever you're
testing.

### From source

```sh
./setup.sh
```

Manual equivalent:

```sh
docker compose --profile build-only build   # builds every resource image
docker compose up -d --build hub            # builds the UI into the hub image, starts it
```

### Developing the UI

```sh
cd hub-ui
npm install
npm run dev       # http://localhost:5173, proxies /api to the hub on :8090
```

## Roadmap ideas

- A realistic IdP option (Keycloak and/or Dex) alongside the built-in
  minimal OAuth provider, for testing against something closer to what
  you'd integrate with in production.
- HMAC-signed request auth (Stripe/GitHub-webhook style) as another auth
  mode, and path-parameter routes (`/users/{id}`) for Mock API.

## Security note

This exists to make local testing convenient, not to be a secure identity
provider. Tokens, client secrets, passwords, and API keys generated here
are for local development only -- don't point real credentials or
production traffic at any of this.
