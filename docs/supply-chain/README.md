# Resolved supply-chain evidence

This foundation inventories artifacts built from an exact private repository
revision. It does **not** certify commercial readiness, change the source LICENSE,
deploy images, or replace human license/NOTICE review. A successful inventory job
and an accepted release are deliberately different results.

## Subjects and boundaries

| Subject | Evidence actually inspected | Distribution scope |
| --- | --- | --- |
| semantic-api runtime | Built image, installed Python METADATA/WHEEL/RECORD, verified public wheel bytes, Debian database | Actual installed environment, including tooling left in that image |
| semantic-backend runtime | The same, including local connector/compiler/runtime wheels and resolved transitives | Installed backend plus its enabled Databricks extra |
| control-api runtime | Published `.deps.json`, runtime/resource/native files, public/restore-cache `.nupkg` byte equality and nuspec/license files | Published NuGet closure and image OS/framework components |
| web build | Installed pnpm production tree, installed package files, exact lock integrity, public tarballs and generated `dist` hashes | Conservative bundled-production input closure; not all development dependencies |
| web runtime | nginx image packages and byte-for-byte copies of build-stage `dist` files | OS/nginx plus the separately linked production JavaScript inputs |
| each named build stage | Actual cached Docker build-stage image and package/file catalog | Build-only unless explicitly linked as a bundled-production input |

Node's absence from nginx does not mean Vue is absent from the product. The Web
relationship records the production closure, frozen lock and manifest, builder
image identity, and output-file hashes copied into nginx. This is conservative
input attribution, **not** proof that every symbol of every production package
survives tree shaking. Development packages remain build-scoped.
Platform alternatives seen only in the pnpm lock are `build-lock-candidate`, not
asserted to be installed in the builder or shipped in nginx.
Multiple installed peer contexts for the same npm name/version currently fail
closed rather than silently merging potentially different dependency edges.

The product Dockerfiles are used unchanged, using their existing installation and
build-script policy. No additional package lifecycle scripts run during scanning.
Syft enrichment/network and Go-tool execution are disabled. Saved image layers are
read as archives with OCI whiteout/opaque-directory semantics, not extracted onto
the host or executed. Container filesystem export is deliberately not used: Docker
mount handling changes `/etc/hosts` and `/etc/hostname` relative to image bytes.
The only post-build
container commands read interpreter facts and `pnpm list`; they have no network.
Public PyPI/npm archive retrieval executes no downloaded code.

## Run it

Use an isolated checkout and Linux/amd64 Docker for actual image collection.
Windows can run the tests, official generator and offline review but does not
replace Linux image evidence. Tools and generated output belong outside tracked
source. The following PowerShell example uses an explicitly supplied evidence
directory; collection refuses to overwrite an existing directory.

```powershell
python -m pip install --only-binary=:all: --require-hashes -r scripts\supply_chain\requirements.lock
python -m scripts.supply_chain.bootstrap --tools $env:SUPPLY_CHAIN_TOOLS
python -m pytest scripts\supply_chain\tests
$revision = git rev-parse HEAD
python -m scripts.supply_chain.pipeline collect --target web --tools $env:SUPPLY_CHAIN_TOOLS --output $env:EVIDENCE_DIR --revision $revision
python -m scripts.supply_chain.pipeline accept --tools $env:SUPPLY_CHAIN_TOOLS --output $env:EVIDENCE_DIR --revision $revision
```

Set `SUPPLY_CHAIN_TOOLS` to a dedicated cache and `EVIDENCE_DIR` to a new output
directory first. Prefer a virtual environment. Collection requires committed,
unmodified tracked source. Available targets are `web`, `control-api`,
`semantic-api`, and `semantic-backend`.

Acceptance exits **0** only for accepted evidence, **2** for a well-formed but
blocked review, and **1** for invalid/unavailable evidence or a failed operation.
Error diagnostics are fixed codes, not package-controlled commands or full
environment/token dumps. Do not run commands embedded in reports.

### Private CI

`.github/workflows/supply-chain.yml` builds all four targets in isolated GitHub
Actions jobs. PR checkout uses the **head SHA**, not an implicit merge commit.
Artifacts include that full SHA in their names; the job summary records the
GitHub artifact ID and archive digest. Images are neither pushed nor deployed.
Retention is 14 days, so archive evidence through an approved private process if
the release requires longer retention.

The workflow also supports `workflow_call` and `workflow_dispatch`.
`require_acceptance: true` makes blocked acceptance a failing job **after** evidence
upload. Its default is false: existing unresolved baseline reviews do not break
unrelated release gates. The root aggregate workflow has intentionally not been
changed; later integration belongs to the coordinating workstream.

| File | Purpose |
| --- | --- |
| `*.cdx.json` | Official Syft CycloneDX 1.6, enriched with artifact/evidence bindings and normalized |
| `*.generator.cdx.json` | Unmodified standard generator output for lossless diagnosis/replay |
| `*.syft.json` | Package, file and relationship facts; image configuration and host-specific scanner configuration omitted |
| `*.inventory.json` | Resolved oracle, component identities, hashes, license provenance, scope, subject relationships, gaps |
| `blobs/<sha256>` | Content-addressed package metadata and legally necessary license/NOTICE evidence, not arbitrary source archives |
| `source.json` | Exact commit/tree and SHA-256 of all tracked inputs |
| `toolchain.json` | Pinned tool versions, upstream metadata, installed tool license evidence |
| `bundle.json` | Subject list and hashes binding all inventory files/blobs |
| `acceptance.json` | Separate policy decision with per-component states and unresolved gaps |

Source/image configuration is not an SBOM license fact. The published Syft source
projection retains image identity, manifest/layer digests, architecture and OS,
but not Docker environment/configuration payloads or absolute host archive paths.
Neither raw image archives nor full root filesystems are uploaded.

## What reproducibility means here

The standard is generated by pinned Syft, not a home-grown CycloneDX serializer.
Validation uses the exact official CycloneDX 1.6 schema revision and checksums in
`tools.json` (the maintained 1.6 schema and SPDX definitions in the official
specification 1.7.1 source release). This avoids relabeling newer valid SPDX IDs
merely because the original 1.6 tag had an older enum. Bootstrap retrieves the schemas once; validation registers all
references locally and does not fetch remote schemas.

Normalization removes only the generated document timestamp and random serial,
sorts unordered component/property/hash/reference/dependency sets, and derives a
UUID from the resulting content. Versions, licenses, hashes, relationships and
artifact identity are retained. Identical captured evidence and provenance yield
identical normalized documents. Observation dates remain in review evidence for
freshness enforcement; a new observation is not silently backdated.
Offline `accept` also regenerates the normalized document from its retained
generator output and bound inventory and requires exact byte equality.

This is **not** a promise of bit-identical independent product rebuilds. Existing
Python ranges and floating base-image tags are not rewritten by this workstream.
Each run records what it actually resolved and its exact image/config/archive
hashes. Python public wheels are retrieved at the installed version/tag and
matched against every hashed archive entry and installed file; this proves
byte-matching reconstruction, not that a registry supplied those original bytes
at build time. Local first-party compressed wheels and transient PEP 517 build
environments are not retained by the existing Dockerfiles. Those limitations are
explicit release-acceptance gaps, not fabricated hashes.

Unhashed `.pyc` entries generated by installation may be absent (official Python
images remove bytecode); their presence is recorded separately, without treating
them as missing wheel payload. Hashed files and un-hashed non-bytecode records
still have to exist, and every recorded payload hash is checked.

NuGet evidence compares restore-cache archives byte-for-byte with the exact
version's public NuGet archive and compares claimed runtime assets to the
published bytes. NuGet restore `contentHash` is retained separately: for signed
packages it is not the raw ZIP SHA-512. pnpm evidence
checks registry tarballs against the lock integrity and installed package bytes.
The original lock algorithm is retained (including legacy SHA-1); additional
SHA-256 records do not imply that a weak upstream lock digest became strong.

## License and NOTICE decisions

The source-controlled `review-policy.json` starts with **no approvals**. An SPDX
identifier alone never approves a component. Inventory retains metadata-declared
license values separately from Syft detections and original evidence. Unknown and
conflicting values remain visible; text inequality is a conservative review
signal, not a legal interpretation of dual licenses.

Each review must bind an exact versioned purl and `evidence_sha256`, with a
reviewer, rationale, expiry, license conclusion and explicit NOTICE decision.
If NOTICE is required, its content-addressed blobs must belong to that component.
Unknown/conflicting metadata requires an explicit `exception` decision, not an
ordinary approval. Exceptions are still exact-version/evidence-bound and expiring;
they cannot bypass missing artifacts, hashes, source identity or schema validity.
Wildcard approvals, duplicate conflicting entries and expired reviews fail.

Use the fields enforced by `validate_policy` in `scripts/supply_chain/review.py`.
The synthetic tests demonstrate policy shape, but **must not be copied as real
approvals**. A human reviewer must inspect the exact upstream version/source,
package bytes, notices, attribution and distribution obligations, record the
decision, then rerun offline acceptance with that policy.

Current baseline limitations include missing upstream declarations/license files
in some artifacts, potentially conflicting declarations/detections, unreviewed
components, unretained local wheels and ephemeral build environments. Inventory
success must not be described as commercially cleared. Broader distribution
obligations, source offers, trademark/patent terms, vulnerability analysis,
signing, reproducible independent builds and long-term evidence retention remain
outside this foundation.

New psycopg/libpq/OpenSSL and Keycloak distributions are **future integration
cases**, not shipped dependencies inferred from memory. When such artifacts land
on the selected revision, collect their actual runtime and binary/distribution
metadata, extend targeted coverage if necessary, and obtain exact evidence-bound
reviews. This work does not approve those licenses in advance.

## Tool provenance and maintenance

Syft 1.51.1 is checksum-pinned to upstream release archives. Its Apache-2.0 license
is separately pinned and retained. CycloneDX schemas come from the official
specification repository at a fixed revision. Python tooling is resolved from
exact pins into `requirements.lock` using public PyPI SHA-256 wheel digests;
`python-tools.json` retains version-specific upstream metadata.

The inspected tool declarations are MIT for jsonschema, attrs,
jsonschema-specifications, referencing, rpds-py, pytest, iniconfig and pluggy;
PSF-2.0 for typing-extensions; Apache-2.0 OR BSD-2-Clause for packaging; and
BSD-2-Clause for Pygments. Colorama's metadata lacks a license expression; its
installed LICENSE is retained rather than assigning one automatically. These are
CI-only tools, not additions to product dependencies or a legal-clearance claim.
`toolchain.json` records installed license-file hashes for inspection.
The three GitHub Actions are commit-pinned and their actual upstream MIT license
files are checksum-pinned in `tools.json` and retained with the tool evidence.

To update tools, change dedicated pins, run
`python -m scripts.supply_chain.lock_tools`, inspect upstream hashes/licenses and
the generated diff, then rerun the focused tests and real CI inventory. Do not
replace pins with `latest` or pipe remote installer scripts into a shell.

## First-party references

1. Syft versioned README and license:
   `https://github.com/anchore/syft/tree/v1.51.1`
2. Syft output format/version support:
   `https://oss.anchore.com/docs/guides/sbom/formats/`
3. Syft configuration and offline cataloging options:
   `https://oss.anchore.com/docs/reference/syft/configuration/`
4. Official schema revision:
   `https://github.com/CycloneDX/specification/tree/b29bae660048e0ad2fbc5f2972927b442ce951c4/schema`
5. Exact Python tool upstream metadata URLs and wheel digests:
   `scripts/supply_chain/python-tools.json`
6. OCI image-layer replacement, hardlink and whiteout semantics:
   `https://github.com/opencontainers/image-spec/blob/v1.1.1/layer.md`

The focused pytest suite exercises actual pinned generator output, offline
official-schema validation, deterministic normalization, named transitive
coverage, installed-file/archive drift, malformed/stale evidence, and exact
expiring review decisions. Actual Linux container results are supplied by CI,
not inferred from the Windows development host.
