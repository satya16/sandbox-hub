# sandbox-hub

A local control panel for spinning up disposable test resources -- REST
APIs, MCP servers, mock REST/GraphQL endpoints, webhook receivers, a
chaos/rate-limit sandbox, and a minimal API tester -- as Docker containers,
each with its own auth setup, from one page. Click **New**, pick what to
run and how it should be secured, hit **Start**. Run as many at once as
you like, in any mix of configs.

Useful when you're building a client (an app, an agent, an MCP client) and
need something real to point it at without standing up infrastructure or
depending on a live third-party service.

## What it gives you

Seven kinds of resource, each configurable per instance:

- **REST API** -- a small sample API (`/items`). Also serves its own
  **OpenAPI spec** (`/openapi.json`, version 3.0 or 3.1 -- your choice) plus
  **Swagger UI** (`/docs`) and **ReDoc** (`/redoc`), with the spec/docs
  optionally gated behind their own static token, independent of whatever
  protects `/items`. Optionally exposes an **async job endpoint**
  (`POST /jobs` -> `202` + a job id, `GET /jobs/{id}` polling
  `"pending"` -> `"done"` after a configurable delay) for testing client
  code against the submit-then-poll pattern real async APIs use --
  gated by whatever auth mode the instance is using, same as `/items`.
- **MCP server** (streamable-http) -- `echo` / `add` / `current_time` tools.
- **Mock API** -- define your own routes after starting it: method + path
  (or `*` for "any"/catch-all, or a segment like `{id}` for a **path
  parameter**, readable in templates as `{{request.params.id}}`) mapped to
  a status code and a **templated JSON response** (`{{request.body.x}}`,
  `{{request.query.x}}`, `{{request.params.x}}`, `{{uuid}}`, `{{now}}`,
  ...). Routes can require certain fields be present in the request body
  (400 if missing), so you can test a client against a schema you define
  instead of a fixed sample one. Any route can also inject **latency** and
  a **random failure rate** (a config'd fraction of requests get a random
  500/502/503/504), same idea as the Chaos API kind but per route. A route
  can instead be a **CRUD collection** (`/users` -> list/create,
  `/users/{id}` -> get/replace/merge/delete) backed by an in-memory store
  you seed when you create it -- real create/update/delete semantics
  (409 on a duplicate id, 404 on a missing one, auto-assigned ids) instead
  of a single canned response, for testing a client's full lifecycle
  against a resource rather than one fixed reply. Routes can also be
  **generated from an OpenAPI 3.x spec** (paste it, or point at a URL) --
  one static route per operation, answering with its documented example or
  a sample built from its response schema, so you can stand up a rough
  mock of a real API in one step instead of defining every route by hand.
- **GraphQL API** -- same idea as Mock API, but for GraphQL: define your own
  schema as SDL and, per Query/Mutation field, a templated mock response
  (`{{request.args.x}}`, `{{request.variables.x}}`, `{{uuid}}`, `{{now}}`,
  ...). Built on a real `graphql-core` schema instead of name-matching, so
  introspection and standard GraphQL error shapes work like a genuine
  endpoint. A resolver can also be set to always raise a GraphQL error
  instead, for testing a client's error handling. Ships with a small seeded
  Item/Query/Mutation schema so a fresh instance is queryable immediately,
  and comes with GraphiQL (`/graphiql`) for poking at it by hand.
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

All auth-capable kinds (REST API, MCP server, Mock API, GraphQL API, Webhook
Receiver) support the same auth modes: **none**, **static API key**, **HTTP
Basic**, a **self-contained JWT** (signed + verified locally with no
external calls -- tests a client's own token handling rather than a
lookup), **cookie / session login** (`POST /login`, then a cookie gates
everything else), and **OAuth2** (client_credentials or
authorization_code+PKCE against the local
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
  no compose file regeneration, no fixed port table to run out of. The hub
  joins that same network itself on first use (whichever container you
  started it as -- no `--network` flag needed on the `docker run` in the
  quickstart below), since it reaches each instance's own admin API
  (mock/GraphQL/chaos config, OAuth client registration) by container name,
  which only resolves between containers on the same network. The port
  is also editable per instance after creation (recreates the container on
  the new port, credentials/config untouched -- including live-edited
  Mock API routes, GraphQL schema/resolvers, and Chaos settings, which are
  carried across the recreate, as they are on "Rotate credentials"); the
  Update button on the
  card stays disabled for the whole recreate cycle and only re-enables once
  the instance is confirmed live again.
- **The resource images** (`resources/rest-api`, `resources/mcp-server`,
  `resources/mock-api`, `resources/graphql-api`, `resources/webhook-receiver`,
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
  satya16dev/sandboxhub-hub
```

That's the only image you pull yourself. The hub creates every resource
(`rest-api`, `mcp-server`, `mock-api`, `graphql-api`, `webhook-receiver`,
`chaos-api`, `api-tester`, `oauth-provider`) as a sibling container on
demand, and Docker auto-pulls each one from `satya16dev/sandboxhub-<kind>`
the first time you actually use that kind -- you never pull the other eight
by hand.

Note the Docker socket mount gives the container root-equivalent access to
your machine -- that's inherent to how the hub creates sibling containers,
not incidental. Resource ports bind to your machine's own `127.0.0.1` by
default, same as running it from source; add `-e SANDBOXHUB_BIND_HOST=0.0.0.0`
only if you want them reachable from other machines on your LAN.

Then open **http://localhost:8090**, click **New**, and configure whatever
you need. Copy the generated URL / key / curl snippet into whatever you're
testing.

Already have a `sandboxhub-hub` container from a previous run? `docker run
--name` fails if that name exists, even stopped. Either resume it
(`docker start sandboxhub-hub`) or remove it first
(`docker rm -f sandboxhub-hub`) before rerunning the command above.

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
  mode.
- Export/import a scenario (an instance's kind, auth config, and
  live-edited state -- routes, schema, chaos settings) as one JSON file, so
  a setup can be saved, shared, or checked into a repo instead of rebuilt
  by hand every time.

## Security note

This exists to make local testing convenient, not to be a secure identity
provider. Tokens, client secrets, passwords, and API keys generated here
are for local development only -- don't point real credentials or
production traffic at any of this.
