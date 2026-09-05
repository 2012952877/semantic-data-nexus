# Authorized catalog query BFF

The BFF adds two opt-in versioned endpoints under the existing authenticated API:

| Endpoint | Body |
| --- | --- |
| `POST /api/v1/catalog/queries` | `catalog-compile/v1`: `request_id`, immutable `catalog` pin, `question` |
| `POST /api/v1/catalog/clarifications/{id}/answers` | `catalog-answer/v1`: same `catalog` pin, answer `revision`, `choice_id` |

Both require `compiler:query` from the current server-resolved workspace
membership. Browser requests use the existing `X-Workspace-Id` selector and
`X-Nexus-CSRF` token; the selector does not itself grant access. Catalog scope
must match the verified workspace. Local header-based development authentication
does not authorize these routes. Existing M0 routes and defaults are unchanged.

`HttpCatalogBackendClient` preserves the explicit snake-case versioned request
and response envelopes, uses the existing `ServiceContextHandler` to sign method,
path and body, and forwards the request cancellation token to `HttpClient`.
No caller identity DTO or unsigned backend context is introduced. The response
pin/request/status and typed public result are checked before returning it.
Known typed conflicts, authorization failures and terminal scalar failures retain
their controlled HTTP status. Unknown token usage stays null.

The backend handles compilation/clarification fencing and scoped persisted result
replay under the actual shared PostgreSQL authorization guard. Runtime version 1
does not broaden the compiler's mapped operator forms. The current file-based
server catalog source explicitly contains synthetic Arrow data; it is not a
customer resolver or general catalog-management service.

## Reproducible acceptance

The existing `compose.identity.yaml` test stack now runs the ordinary backend
with a **test-only loopback model socket**, while production startup remains
unchanged. `prepare_catalog_fixture.py` generates two synthetic ontology domains,
their physical bindings, expected results, server grants and socket responses
from the versioned contract examples. It never downloads customer data or
queries a live model.

`playwright.identity.config.ts` runs the existing identity tests and
`catalog-identity.spec.ts`. The catalog case performs actual code/PKCE login at
Keycloak, workspace selection, authorized resource grants, query/result,
clarification/resume, same-request replay, wrong-scope/CSRF rejection, browser
abort and post-revocation denial. Cancellation observation uses a writable
directory containing only value-free synthetic fixture events. It does not expose
credentials or production debug endpoints.

Local Windows evidence uses `tests/CatalogIdentityHost`, a test-only host that
starts the real ControlApi entry point. Its OIDC backchannel trusts only the
explicit disposable test CA and resolves only `identity.localhost:8443` to
loopback. Certificate names, signatures and issuer/audience verification remain
enabled. It neither modifies the host certificate store/hosts file nor ships in
the production ControlApi image. The local HTTPS reverse proxy must forward
client disconnects upstream (as nginx does by default); Vite's development proxy
needs explicit upstream destruction on a prematurely closed client response.
That exact local proxy is checked in as `apps/web/tests/localIdentityServer.mjs`.
It targets only `127.0.0.1:5088`, requires an explicit disposable fixture directory,
and destroys the upstream request when the browser aborts. It is not bundled in
the web application and changes no production authentication settings.

### Local run profile used for the recorded browser evidence

Use the existing identity preparation generator for disposable CA/realm/session
fixtures and `prepare_catalog_fixture.py --output <fixture-directory>` for catalog
fixtures. Keep the local PostgreSQL database and all generated keys in the test
session; do not register a Windows service or trust the CA in the host store.
The server environment values come from those generated fixture files, not live
credentials.

| Process | Local profile |
| --- | --- |
| PostgreSQL 16 | New synthetic database, random loopback port, existing identity migrations and explicit bootstrap for Alice in workspace-a/workspace-b |
| Keycloak 26.7.3 | `start-dev --import-realm --http-enabled=false --http-host=127.0.0.1 --hostname=https://identity.localhost:8443`, fixture TLS files and generated realm import |
| Backend | `catalog_test_host.py --fixtures <directory> --backend-port 8088 --model-port 8099`; service auth and separate `SEMANTIC_CATALOG_PROVIDER_*` loopback settings |
| BFF | Built `CatalogIdentityHost.dll`, Development, `ASPNETCORE_URLS=http://127.0.0.1:5088`, `NEXUS_IDENTITY_FIXTURE_CA=<directory>/ca.crt`, existing provider/signing config and shared DB, backend URI `http://127.0.0.1:8088/` |
| HTTPS web | `NEXUS_IDENTITY_FIXTURE_DIR=<directory>` then `node apps/web/tests/localIdentityServer.mjs` |
| Browser suite | Same fixture env, `pnpm --dir apps/web exec playwright test --config playwright.identity.config.ts` |

The complete three-test Keycloak suite passed in this local profile, including
`fetch.abort` propagation. The new `.github/workflows/identity.yml` wiring runs
the **same abort assertion** through the product nginx test stack when hosted
runners are available. That nginx/Compose run has **not** been executed for this
new layer because hosted jobs were blocked before startup. Local Vite-proxy
evidence must not be relabeled as an actual nginx run.

Focused BFF tests are `CatalogBackendClientTests`: versioned forwarding, both
query/answer cancellation paths, wrong pin/request, invalid integer response,
unknown usage and typed terminal error propagation. Full control-plane tests use
the existing disposable PostgreSQL test configuration. Hosted Actions remain
separate evidence from these local runs; no new hosted-green claim follows from
local success.
