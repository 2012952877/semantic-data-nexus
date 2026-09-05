# Enterprise identity and workspace isolation

This is the #32 identity boundary, not a replacement for runtime durability (#34),
catalog publication (#33/#35), or commercial billing/audit streams (#38).
Existing Azure-hosted BasicAuth demos and app registrations are unchanged.
No live Entra tenant verification is claimed.

## Authority and browser flow

The Web uses a same-origin backend-for-frontend (BFF), not browser token storage.
ASP.NET Core 8's maintained OpenID Connect handler performs confidential
authorization code + PKCE (S256), state/nonce validation, discovery/JWKS retrieval,
signature/algorithm, exact issuer, audience and lifetime checks. Tokens are not
returned to Web or saved in localStorage. A Secure, HttpOnly, host-only session
cookie references an encrypted PostgreSQL ticket; deletion on application logout
invalidates reuse of that cookie. Cookies do not slide beyond the ID-token
lifetime or the 30-minute application session limit. Logout signs out **this
application**, not every application at the IdP.

Cookie-authenticated mutations require the ASP.NET anti-forgery cookie/request
token pair (`X-Nexus-CSRF`). The session response is `no-store`. API bearer
authentication is separate: APIs validate **access tokens**, never use an ID token
as an API credential, and require an audience distinct from the browser client.
Entra bearer tokens must be v2 delegated tokens with `access_as_user`.

Only configured `Identity:Providers` can authenticate. There is no common,
organizations, consumer, arbitrary discovery URL or email-based auto-enrollment.
For Entra, register the exact tenant GUID and tenant-specific v2 authority, and
inject the confidential browser client's credential. `tid` must match that tenant;
the maintained Microsoft IdentityModel AAD signing-key issuer validator is enabled.
The application principal key is `(issuer, subject, identity_tenant)`. The generic
OIDC case has no `tid`; unexpected `tid` is rejected rather than silently discarded.
The multi-provider implementation uses the standard ASP.NET handlers, following
Microsoft's recommendation to avoid global option collisions among provider clients.

An authenticated subject is not automatically a member. PostgreSQL resolves its
active principal, workspace, direct membership and active group roles on **every
request**. Token roles/groups are not provisioning instructions. `X-Workspace-Id`
is only a selector; body/query tenant fields, `X-Dev-*`, `X-Tenant-Id`, and serialized
trusted-context JSON confer no authority. Server permissions include `run.reader`,
`run.contributor`, `run.admin`, `workspace:admin`, `resource:read`, `compiler:query`.
Reader cannot create/cancel/change feedback; contributor cannot administer identity.

## Shared control/backend authorization

The BFF signs a short-lived service assertion with its injected RSA private key.
The backend has only the pinned public key. This is service delegation, **not**
a homegrown end-user IdP. The assertion requires:

| Field | Meaning |
| --- | --- |
| `typ`, algorithm | `nexus-service+jwt`, RS256 only, RSA at least 2048 bits |
| `iss`, `aud` | Fixed `https://control.example.test` identity and `semantic-backend`; issuer is an identifier, not a fetched endpoint |
| `iat`, `nbf`, `exp` | At most 30 seconds and no later than the verified user's expiry |
| `jti` | Single-use ID, claimed transactionally in PostgreSQL, including across backend restarts |
| `htm`, `htu`, `bh` | Exact HTTP method, path/query and SHA-256 of transmitted body bytes |
| `ctx` | `trusted-context/v1` from the BFF verifier and membership store |

Unsigned/new headers, an IdP token directly sent to backend, wrong signature,
expired/replayed assertion and request substitution are denied. Backend checks
the same `identity_access` view and exact principal/scope/membership revision;
it does not maintain a second independent membership authority. Data is checked
again before releasing a backend response. Membership/role changes increment the
workspace authorization revision, conservatively invalidating older contexts.
Unavailable authorization storage returns a safe 503, never a success-shaped
empty result or anonymous fallback.

Control repository operations reauthorize under a shared transaction advisory
lock; supported identity mutations acquire the exclusive lock first. Statistics
read runs and feedback in one statement. Admin writers must use the provided API
or maintenance command, not out-of-band unlocked DML. Database credentials belong
only to trusted services/operators; expose neither PostgreSQL nor backend directly
to browsers. Production requires verified TLS for database and service transport.

## Existing and future surfaces

| Existing surface | Enforcement |
| --- | --- |
| BFF `/api/v1/me` | Server principal and current workspace permissions |
| BFF run create/list/detail/metadata/semantic-status/cancel/feedback, statistics | Live permissions, tenant/workspace-qualified storage queries and idempotency; foreign run IDs behave as absent |
| Backend run create/status/cancel/detail/result/lineage | Authenticated request-bound context and scoped record access |
| Result, manifest, diagnostics and lineage embedded in detail | Same scoped detail authorization; no independent raw blob download route |
| `/auth/session`, `/auth/login/{provider}`, `/auth/logout` | Public login discovery, validated session and anti-forgery-protected application logout |
| `/api/v1/workspace/memberships`, `/groups`, `/grants` PUT; `/access` GET | Current workspace admin only; scoped membership foreign keys, transactions and idempotency |
| Health probes | Anonymous readiness/liveness, no customer data |

There are currently **no** HTTP SSE/event stream, pagination cursor, export/blob
download, global user-directory management, catalog CRUD or catalog publishing
endpoints. Run lists support a bounded `limit`, applied **after** scope filtering.
Future streams must reauthorize before each event, exports before every page or
download, and resource content before read/use. Do not cite these future paths as
delivered endpoints. Web's existing synthetic ontology/status views are not
production catalog management.

For #33, `semantic_backend.auth_context.TrustedContext` is a frozen nested-attribute
model matching the product contract. `get_trusted_context()` fails without verified
request context. `PostgresAuthorization.require(context, pin, permission)` is
async and returns `CatalogAccess` with `frozenset[str]` attributes `entity_ids`,
`field_ids`, `metric_ids`, `relation_ids`, `member_ids`. A compiler needing its own
Pydantic DTO must construct it from these attributes; the returned dataclass does
not provide `model_dump`. The pin must include the complete resource-version/v1
identity, exact scope, kind, ID, positive revision and content digest. No wildcard
or current-label substitution is accepted. Grants do not establish that a resource
exists or that its bytes match its digest; the catalog store must prove both.
Compile/resume must recheck grants before storing/releasing output. Unknown
capabilities and absent grants fail closed.

## Runnable local identity, without Azure

Keycloak **26.7.3**, Apache-2.0, is pinned for this local alternative.
The dedicated `compose.identity.yaml` does not modify shared demo ingress or
Azure resources. It binds only loopback TLS ports 8443/8444; PostgreSQL and backend
have no published ports. It uses synthetic data and generated ephemeral credentials.
Keycloak's `start-dev` and file-backed local database are intentionally **not**
a production Keycloak deployment.

Prerequisites: Docker Compose, Python with the backend's cryptography dependency,
and pnpm for optional browser checks. From the repository root:

```powershell
python ops\identity\prepare.py
docker compose --env-file ops\identity\.generated\compose.env -f compose.identity.yaml up --build -d
Get-Content ops\identity\.generated\bootstrap-workspace-a.json | docker compose --env-file ops\identity\.generated\compose.env -f compose.identity.yaml exec -T control-api dotnet ControlApi.dll --identity-maintenance
Get-Content ops\identity\.generated\bootstrap-workspace-b.json | docker compose --env-file ops\identity\.generated\compose.env -f compose.identity.yaml exec -T control-api dotnet ControlApi.dll --identity-maintenance
```

Use `https://localhost:8444`. Local DNS must resolve `identity.localhost` to loopback
(add it to the local hosts file if needed). Trust **only this generated local CA**
for the local exercise; do not disable TLS validation in application code.
Alice's synthetic username is `alice`; the generated local login credential is
in `.generated/browser.json` for the operator/test runner, never committed or
printed by the preparation script. Bob is a valid IdP user with no membership.
Select an authorized workspace before using runs. Changing workspace navigates
to a new page, discarding run caches and pending retry state.

`prepare.py` refuses to overwrite an existing fixture directory. The generated
material and permissive keyring mount are disposable, local-only: never reuse
them in production. Stop and remove this dedicated Compose project's volumes
before generating a new realm/credential set. The CI workflow performs this
cleanup; do not retain its generated files as artifacts.

`Enterprise identity` CI exercises the real browser redirect, PKCE, login/session,
CSRF rejection, Web workspace selection, BFF/backend execution, scoped reads and
application logout. Unit JWT fixtures are additional negative evidence, not
claimed end-to-end SSO. Entra still requires separately authorized tenant-specific
validation before a production rollout.

## Provisioning, grants, revocation and migration

Provision registered OIDC identities using workspace-admin APIs. Membership and
group change bodies require a caller-generated `requestId`, target ID, declared
role/active state, and exact selected-workspace references. `/grants` additionally
requires resource kind/ID/revision/digest and explicit allowed catalog IDs.
Retrial with the same request ID/content is idempotent; a changed payload conflicts.
The operational `identity_changes` ledger records redacted metadata and a payload
digest in the same transaction. It is not audit-envelope/v1 or usage-envelope/v1:
identity administration and bootstrap may have no versioned resource, and must
not fabricate a resource identity or usage charge.

First workspace creation is an **operator-only** maintenance command, never
automatic first-login admin assignment. It takes explicit JSON on stdin using
the same registered provider configuration and database secret reference. See
the generated bootstrap JSON for its exact case-sensitive fields. `bootstrap`
creates one explicit workspace and admin membership; exact retries are logged
once. There is no anonymous bootstrap HTTP route.

Operator operation `revoke-principal` disables an account across its memberships;
workspace admins can revoke only memberships/groups in their selected workspace.
Existing cookies, bearer tokens and captured service contexts do not override a
revoked principal/membership. Local application logout also deletes its server ticket.

Migration 001's checksum is unchanged. Migration 002 is DDL only; it adds nullable
run scope and a null-safe scoped unique request key without any data adoption.
Unscoped legacy runs remain quarantined. Operator operation `adopt-legacy` requires
an exact `RunId`, expected `LegacySubject`, target principal/provider/subject and
authorized tenant/workspace. It updates metadata ownership atomically and records
the explicit decision; conflicts roll back. It does not migrate ephemeral backend
results, reinterpret old feedback author strings as OIDC identities, or recover
runtime work. Back up the database before adoption; there is no destructive
automatic down migration.

Production secret injection must supply the database connection string, OIDC
client credential and service signing key through the deployment's secret
reference mechanism. The backend gets only the corresponding public key plus
its least-privilege membership/replay database access. Use a persistent encrypted
volume with restricted ownership for `Identity:DataProtectionKeyRing`; replicate
it consistently across BFF instances. Key rotation, backups and operator DB
permissions are installation responsibilities. No secrets belong in source,
shell history, API responses, diagnostic artifacts or browser storage.

M0 compatibility is explicit: BFF `LocalDevelopmentAuth:Enabled=true` only in
`Development`; backend `SEMANTIC_NEXUS_AUTH_MODE=legacy-development` only with
`SEMANTIC_NEXUS_ENVIRONMENT=Development`. The existing local Compose and offline
CI exercises opt in. Enterprise defaults never fall back to that mode.

## Sources and limits

- [Microsoft: ASP.NET Core OIDC confidential code + PKCE / multi-provider BFF](https://learn.microsoft.com/aspnet/core/security/authentication/configure-oidc-web-authentication?view=aspnetcore-8.0)
- [Microsoft: API access-token validation and issuer/tenant/key scope](https://learn.microsoft.com/entra/identity-platform/access-tokens)
- [Microsoft: AAD signing-key issuer validation](https://learn.microsoft.com/dotnet/api/microsoft.identitymodel.validators.aadtokenvalidationparametersextension.enableaadsigningkeyissuervalidation)
- [Keycloak container guide](https://www.keycloak.org/server/containers), [26.7.3 release](https://github.com/keycloak/keycloak/releases/tag/26.7.3), [Apache-2.0 license](https://github.com/keycloak/keycloak/blob/26.7.3/LICENSE.txt).

Microsoft Learn documentation and code samples were retrieved for this work.
The Azure best-practices tool timed out; no Azure operations were performed.
The capability registry remains unchanged: this implementation does not promote
unimplemented catalog, runtime durability or commercial workflows.
