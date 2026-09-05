# Optional PostgreSQL dependency

The additive compiler clarification store uses **psycopg 3.3.4** and
**psycopg-binary 3.3.4**, pinned by the `postgres` optional extra. Neither package
is vendored into this repository, and no existing deployment is switched to it.

The first-party 3.3.4 tag supplies the LGPL-3.0 license text:
`https://github.com/psycopg/psycopg/blob/3.3.4/LICENSE.txt`.
The official installation guide documents the separately installable binary
distribution, its bundled client libraries, and platform support:
`https://www.psycopg.org/psycopg3/docs/basic/install.html`.
Published package/version metadata is available at
`https://pypi.org/project/psycopg/3.3.4/` and
`https://pypi.org/project/psycopg-binary/3.3.4/`.

This is an unmodified, replaceable runtime dependency, not copied implementation
code. A commercial redistributor must retain the upstream notices/licenses,
provide the applicable corresponding-source/relinking information, review
licenses for bundled libpq/OpenSSL and platform libraries, and include the exact
installed wheel/platform inventory in its release SBOM. This change is not a
legal opinion or a completed enterprise supply-chain certification. The
pure-Python/system-libpq deployment option can be reviewed separately; it is not
silently substituted for the pinned CI dependency.
