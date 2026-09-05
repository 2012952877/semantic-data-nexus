from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    rule: str
    message: str


@dataclass(frozen=True)
class TrackedBlob:
    path: str
    object_id: str
    content: bytes


_CREDENTIAL_FILE_SUFFIXES = {
    ".der",
    ".jks",
    ".key",
    ".p12",
    ".pem",
    ".pfx",
}
_PROPRIETARY_ASSET_SUFFIXES = {
    ".bmp",
    ".doc",
    ".docx",
    ".gif",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".tif",
    ".tiff",
    ".webp",
    ".xls",
    ".xlsm",
    ".xlsx",
}
_RAW_DATA_SUFFIXES = {
    ".avro",
    ".csv",
    ".db",
    ".orc",
    ".parquet",
    ".sql",
    ".sqlite",
    ".sqlite3",
    ".tsv",
}
_SOURCE_CODE_SUFFIXES = {
    ".bicep",
    ".cs",
    ".go",
    ".java",
    ".js",
    ".jsx",
    ".ps1",
    ".py",
    ".rs",
    ".ts",
    ".tsx",
}
_SUSPICIOUS_PATH_PARTS = {
    "customer-data",
    "customer_data",
    "private",
    "prompts",
    "proprietary",
    "screenshots",
}
_SAFE_RAW_DATA_PREFIXES = ("data/synthetic/generated/",)
_SAFE_VALUE_MARKERS = (
    "dummy",
    "example",
    "fake",
    "invalid",
    "placeholder",
    "redacted",
    "sample",
    "synthetic",
    "test",
    "your",
)
_SAFE_EXACT_VALUES = {
    "",
    "***",
    "<redacted>",
    "changeme",
    "none",
    "null",
    "password",
    "secret",
    "token",
}

_SECRET_ASSIGNMENT = re.compile(
    r"""
    (?<![A-Za-z0-9_${])
    (?P<key>
        ["']?
        (?:
            (?:[A-Za-z][A-Za-z0-9]*(?:__|[_-]))*
        )
        (?:
            databricks_token
            | access[_-]?token
            | api[_-]?key
            | client[_-]?secret
            | account[_-]?key
            | connection[_-]?string
            | password
            | private[_-]?key
            | sas[_-]?token
        )
        ["']?
    )
    \s*[:=]\s*
    (?P<value>
        "(?:[^"\\]|\\.)*"
        | '(?:[^'\\]|\\.)*'
        | [^,\s#]+
    )?
    """,
    re.IGNORECASE | re.VERBOSE,
)
_HOST_ASSIGNMENT = re.compile(
    r"""
    (?<![A-Za-z0-9_${])
    ["']?databricks_(?:workspace_)?host["']?
    \s*[:=]\s*
    (?P<value>
        "(?:[^"\\]|\\.)*"
        | '(?:[^'\\]|\\.)*'
        | [^,\s#]+
    )?
    """,
    re.IGNORECASE | re.VERBOSE,
)
_IDENTIFIER_ASSIGNMENT = re.compile(
    r"""
    (?<![A-Za-z0-9_${])
    ["']?
    (?P<key>[A-Za-z][A-Za-z0-9_.-]{1,100})
    ["']?
    \s*[:=]\s*
    (?P<value>
        "(?:[^"\\]|\\.)*"
        | '(?:[^'\\]|\\.)*'
        | [^,\s#]+
    )?
    """,
    re.IGNORECASE | re.VERBOSE,
)
_DECLARED_ASSIGNMENT = re.compile(
    r"""
    (?<![A-Za-z0-9_${])
    (?:(?:const|let|var|param)\s+)?
    (?P<key>[A-Za-z][A-Za-z0-9_.-]{1,100})
    (?:
        \s*:\s*[A-Za-z_][A-Za-z0-9_<>,.\[\]|? ]*
        | \s+[A-Za-z_][A-Za-z0-9_<>,.\[\]|?]*
    )?
    \s*=\s*
    (?P<value>
        "(?:[^"\\]|\\.)*"
        | '(?:[^'\\]|\\.)*'
        | [^,\s#;]+
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_POWERSHELL_ASSIGNMENT = re.compile(
    r"""
    (?<![A-Za-z0-9_$])
    \$
    (?:
        \{(?P<braced_key>[A-Za-z][A-Za-z0-9_-]{1,100})\}
        | (?P<plain_key>[A-Za-z][A-Za-z0-9_-]{1,100})
    )
    \s*=\s*
    (?P<value>
        "(?:[^"\\]|\\.)*"
        | '(?:[^'\\]|\\.)*'
        | [^,\s#;]+
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_PROSE_IDENTIFIER = re.compile(
    r"""
    (?<![A-Za-z0-9])
    ["']?
    (?P<key>
        (?:account|client|object|principal|subscription|tenant|warehouse|workspace)
        [\s_-]+id
    )
    ["']?
    \s*
    (?:(?:[:=]|\bis\b|[-\u2013\u2014])\s*)?
    (?:["'`(<\[{]\s*)?
    (?P<value>[A-Za-z0-9-]{10,})
    \s*
    (?:["'`)>\]}])?
    """,
    re.IGNORECASE | re.VERBOSE,
)
_AZURE_SUBSCRIPTION_RESOURCE_ID = re.compile(
    r"""
    /subscriptions/
    (?P<value>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})
    (?=/|$|[^A-Za-z0-9-])
    """,
    re.IGNORECASE | re.VERBOSE,
)
_PRIVATE_IDENTIFIER_KEY_SUFFIXES = (
    "accountid",
    "clientid",
    "objectid",
    "principalid",
    "subscriptionid",
    "tenantid",
    "warehouseid",
    "workspaceid",
)
_CREDENTIAL_KEY_SEQUENCES = (
    ("account", "key"),
    ("access", "token"),
    ("api", "key"),
    ("api", "keys"),
    ("client", "secret"),
    ("connection", "string"),
    ("connection", "strings"),
    ("databricks", "token"),
    ("private", "key"),
    ("sas", "token"),
)
_UUID_LITERAL = re.compile(
    r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_LONG_NUMERIC_LITERAL = re.compile(r"\d{10,}")
_WAREHOUSE_IDENTIFIER_LITERAL = re.compile(r"[A-Za-z0-9]{16,}")
_ZERO_UUID = "00000000-0000-0000-0000-000000000000"
_CONCRETE_DATABRICKS_HOST = re.compile(
    r"""
    https://
    (?:
        adb-\d{5,}\.\d+\.azuredatabricks\.net
        | dbc-[a-z0-9]{8,}\.cloud\.databricks\.com
    )
    (?=$|[^A-Za-z0-9.:-]|[.]+(?=$|[\s/"'`),;!?\]}>]))
    """,
    re.IGNORECASE | re.VERBOSE,
)
_PRIVATE_NETWORK_URL = re.compile(
    r"""
    https?://
    (?:
        10(?:\.\d{1,3}){3}
        | 192\.168(?:\.\d{1,3}){2}
        | 172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}
        | [a-z0-9.-]+\.(?:corp|internal|lan)
    )
    (?::\d+)?
    (?=$|[^A-Za-z0-9.:-]|[.]+(?=$|[\s/"'`),;!?\]}>]))
    """,
    re.IGNORECASE | re.VERBOSE,
)
_AZURE_APP_SERVICE_URL = re.compile(
    r"""
    https?://
    (?P<host>[a-z0-9][a-z0-9-]{0,58}[a-z0-9])
    [.]azurewebsites[.]net
    (?::\d+)?
    (?=$|[^A-Za-z0-9.:-]|[.]+(?=$|[\s/"'`),;!?\]}>]))
    """,
    re.IGNORECASE | re.VERBOSE,
)
_ONMICROSOFT_IDENTITY = re.compile(
    r"""
    (?<![A-Za-z0-9._%+-])
    (?P<local>[A-Za-z0-9._%+#-]+)
    @
    (?P<tenant>[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)
    [.]onmicrosoft[.]com
    (?=$|[^A-Za-z0-9.-]|[.]+(?=$|[\s/"'`),;!?\]}>]))
    """,
    re.IGNORECASE | re.VERBOSE,
)

_CONTENT_RULES = (
    (
        "private-key-material",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
        ),
        "private key material is prohibited",
    ),
    (
        "databricks-pat",
        re.compile(r"(?<![A-Za-z0-9])dapi[0-9a-f]{32,}(?![A-Za-z0-9])"),
        "a Databricks token-shaped value is prohibited",
    ),
    (
        "github-token",
        re.compile(
            r"(?<![A-Za-z0-9])(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{30,})(?![A-Za-z0-9])"
        ),
        "a GitHub token-shaped value is prohibited",
    ),
    (
        "aws-access-key",
        re.compile(r"(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])"),
        "an AWS access-key-shaped value is prohibited",
    ),
    (
        "slack-token",
        re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{20,}(?![A-Za-z0-9])"),
        "a Slack token-shaped value is prohibited",
    ),
    (
        "azure-storage-key",
        re.compile(
            r"(?i)DefaultEndpointsProtocol=https?;[^\r\n]{0,300}\bAccountKey[=][A-Za-z0-9+/=]{16,}"
        ),
        "an Azure Storage connection string with key material is prohibited",
    ),
    (
        "signed-url",
        re.compile(
            r"(?i)https?://[^\s\"']+[?&](?:sig|signature)=[A-Za-z0-9_%+/=-]{20,}"
        ),
        "a signed URL is prohibited",
    ),
)


class ScanError(RuntimeError):
    pass


def _run_git(
    repository: Path, arguments: list[str], *, input_bytes: bytes | None = None
) -> bytes:
    process = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        input=input_bytes,
        capture_output=True,
    )
    if process.returncode != 0:
        raise ScanError(
            f"git {arguments[0]} failed with exit code {process.returncode}"
        )
    return process.stdout


def tracked_blobs(repository: Path) -> list[TrackedBlob]:
    index = _run_git(repository, ["ls-files", "--stage", "-z"])
    entries: list[tuple[str, str]] = []
    for raw_entry in index.split(b"\0"):
        if not raw_entry:
            continue
        try:
            metadata, raw_path = raw_entry.split(b"\t", 1)
            _mode, object_id, stage = metadata.decode("ascii").split()
        except ValueError as error:
            raise ScanError("git index returned an unexpected entry") from error
        if stage != "0":
            raise ScanError("git index contains unresolved merge entries")
        path = raw_path.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        entries.append((path, object_id))

    request = b"".join(object_id.encode("ascii") + b"\n" for _, object_id in entries)
    batch = _run_git(repository, ["cat-file", "--batch"], input_bytes=request)
    offset = 0
    blobs: list[TrackedBlob] = []
    for path, expected_object_id in entries:
        header_end = batch.find(b"\n", offset)
        if header_end < 0:
            raise ScanError("git object batch ended before its header")
        header = batch[offset:header_end].decode("ascii", errors="strict")
        offset = header_end + 1
        parts = header.split()
        if len(parts) != 3 or parts[0] != expected_object_id or parts[1] != "blob":
            raise ScanError("git object batch returned an unexpected object")
        size = int(parts[2])
        content = batch[offset : offset + size]
        if len(content) != size:
            raise ScanError("git object batch ended before its content")
        offset += size
        if batch[offset : offset + 1] != b"\n":
            raise ScanError("git object batch returned malformed framing")
        offset += 1
        blobs.append(
            TrackedBlob(path=path, object_id=expected_object_id, content=content)
        )
    return blobs


def _path_findings(path: str) -> Iterable[Finding]:
    normalized = PurePosixPath(path)
    suffix = normalized.suffix.lower()
    lowered_parts = {part.lower() for part in normalized.parts}
    lowered_name = normalized.name.lower()

    if suffix in _CREDENTIAL_FILE_SUFFIXES:
        yield Finding(
            path,
            0,
            "credential-file",
            "tracked credential or private-key files are prohibited",
        )
    if suffix in _PROPRIETARY_ASSET_SUFFIXES:
        yield Finding(
            path,
            0,
            "proprietary-binary-asset",
            "tracked screenshots, Office documents, PDFs, and raster assets are prohibited",
        )
    if suffix in _RAW_DATA_SUFFIXES and not path.startswith(_SAFE_RAW_DATA_PREFIXES):
        yield Finding(
            path,
            0,
            "unapproved-raw-data",
            "raw data or SQL is allowed only in the reviewed synthetic data path",
        )
    if lowered_parts & _SUSPICIOUS_PATH_PARTS or lowered_name.startswith("screenshot"):
        yield Finding(
            path,
            0,
            "private-demo-path",
            "tracked private, prompt, screenshot, customer, or proprietary paths are prohibited",
        )


def _literal_value(raw_value: str | None) -> tuple[str, bool]:
    if raw_value is None:
        return "", False
    value = raw_value.strip().rstrip(",;")
    quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}
    return (value[1:-1] if quoted else value), quoted


def _has_placeholder_marker(value: str) -> bool:
    tokens = set(re.findall(r"[a-z0-9]+", value.lower()))
    return bool(tokens.intersection(_SAFE_VALUE_MARKERS))


def _is_safe_shell_expansion(value: str) -> bool:
    match = re.fullmatch(
        r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<operator>:[-+=?]|[-+=?])(?P<operand>[^{}]*))?\}",
        value,
    )
    if match is None:
        return False
    operator = match.group("operator")
    if operator is None or operator in {"?", ":?"}:
        return True
    operand = match.group("operand").strip()
    return not operand or _has_placeholder_marker(operand) or operand.startswith("$")


def _is_explicit_nonliteral_reference(value: str, path: str) -> bool:
    if re.fullmatch(r"\$[A-Za-z_][A-Za-z0-9_]*", value):
        return True
    if _is_safe_shell_expansion(value):
        return True
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in _SOURCE_CODE_SUFFIXES | {".md", ".yaml", ".yml"} and re.fullmatch(
        r"""\$\(python(?:3)?\s+-c\s+(['"])import\s+secrets;\s*print\(secrets[.]token_urlsafe\(\d{1,3}\)\)\1\)""",
        value,
    ):
        return True
    if suffix in {".md", ".ps1"} and re.fullmatch(
        r"""\[System[.]Net[.]NetworkCredential\]::new\(\s*["']{2}\s*,\s*\$[A-Za-z_][A-Za-z0-9_]*\s*\)[.]Password""",
        value,
    ):
        return True
    return bool(
        suffix in _SOURCE_CODE_SUFFIXES
        and re.fullmatch(
            r"""(?:builder[.])?Configuration\[(?:["'][A-Za-z0-9:_.-]+["'])\]""",
            value,
            re.IGNORECASE,
        )
    )


def _is_safe_literal(raw_value: str | None, path: str) -> bool:
    value, quoted = _literal_value(raw_value)
    lowered = value.strip().lower()
    if lowered in _SAFE_EXACT_VALUES:
        return True
    if _has_placeholder_marker(lowered):
        return True
    if _is_explicit_nonliteral_reference(value.strip(), path):
        return True
    if quoted:
        return False
    candidate = value.strip()
    while len(candidate) >= 2 and (candidate[0], candidate[-1]) in {
        ("(", ")"),
        ("{", "}"),
        ("<", ">"),
    }:
        candidate = candidate[1:-1].strip()
    if re.fullmatch(
        r"(?is)(?:[rubf]{1,3})?(?:\"\"\".*\"\"\"|'''.*'''|\".*\"|'.*')",
        candidate,
    ):
        return False
    if "'" in candidate or '"' in candidate:
        return False
    lowered = candidate.lower()
    if lowered.startswith(
        (
            "<",
            "{{",
            "%",
            "@microsoft.keyvault",
            "config.",
            "configuration.",
            "environment.",
            "os.environ",
            "options.",
            "settings.",
            "vault.",
        )
    ):
        return True
    return PurePosixPath(path).suffix.lower() in _SOURCE_CODE_SUFFIXES and bool(
        re.fullmatch(
            r"[A-Za-z_$][A-Za-z0-9_$.\[\]():-]*",
            candidate,
        )
    )


def _is_private_identifier_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized.endswith(_PRIVATE_IDENTIFIER_KEY_SUFFIXES)


def _key_tokens(key: str) -> tuple[str, ...]:
    separated = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", key)
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", separated)
    return tuple(
        token for token in re.split(r"[^A-Za-z0-9]+", separated.lower()) if token
    )


def _is_credential_key(key: str) -> bool:
    tokens = _key_tokens(key)
    if "password" in tokens and not any(
        tokens[index : index + 2] == ("password", "auth")
        for index in range(len(tokens) - 1)
    ):
        return True
    return any(
        tokens[index : index + len(sequence)] == sequence
        for sequence in _CREDENTIAL_KEY_SEQUENCES
        for index in range(len(tokens) - len(sequence) + 1)
    )


def _is_private_identifier_literal(key: str, raw_value: str | None) -> bool:
    value, _quoted = _literal_value(raw_value)
    normalized = value.strip()
    while len(normalized) >= 2 and (normalized[0], normalized[-1]) in {
        ("(", ")"),
        ("{", "}"),
        ("<", ">"),
        ("[", "]"),
    }:
        normalized = normalized[1:-1].strip()
    unquoted, quoted = _literal_value(normalized)
    if quoted:
        normalized = unquoted.strip()
    normalized = normalized.strip("`")
    if not normalized or _has_placeholder_marker(normalized):
        return False
    if normalized.lower() == _ZERO_UUID:
        return False
    if _UUID_LITERAL.fullmatch(normalized) or _LONG_NUMERIC_LITERAL.fullmatch(
        normalized
    ):
        return True
    normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized_key.endswith("warehouseid") and bool(
        _WAREHOUSE_IDENTIFIER_LITERAL.fullmatch(normalized)
    )


def _strip_trailing_comment(value: str) -> str:
    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote is not None:
            escaped = True
            continue
        if quote is not None:
            if character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
            continue
        prefix = value[:index].rstrip()
        comment_boundary = (
            index == 0
            or value[index - 1].isspace()
            or value[index - 1] in {"'", '"', ")", "]", "}", ">", ";", ","}
            or bool(_UUID_LITERAL.fullmatch(prefix))
            or bool(_LONG_NUMERIC_LITERAL.fullmatch(prefix))
        )
        if character == "#" and comment_boundary:
            return prefix
        if (
            character == "/"
            and index + 1 < len(value)
            and value[index + 1] == "/"
            and comment_boundary
        ):
            return prefix
    return value.rstrip()


def _assignment_rhs(line: str, match: re.Match[str]) -> str:
    value_start = match.start("value")
    search_end = value_start if value_start >= 0 else match.end()
    equals_index = line.find("=", match.start(), search_end + 1)
    if equals_index < 0:
        return match.group("value")
    return _strip_trailing_comment(line[equals_index + 1 :].strip()).rstrip(";")


def _line_findings(path: str, line_number: int, line: str) -> Iterable[Finding]:
    for rule, pattern, message in _CONTENT_RULES:
        if pattern.search(line):
            yield Finding(path, line_number, rule, message)

    for match in _SECRET_ASSIGNMENT.finditer(line):
        if not _is_safe_literal(_assignment_rhs(line, match), path):
            yield Finding(
                path,
                line_number,
                "credential-assignment",
                "a non-placeholder credential assignment is prohibited",
            )

    for match in _HOST_ASSIGNMENT.finditer(line):
        if not _is_safe_literal(_assignment_rhs(line, match), path):
            yield Finding(
                path,
                line_number,
                "private-demo-host",
                "a concrete workspace host assignment is prohibited",
            )

    for match in _IDENTIFIER_ASSIGNMENT.finditer(line):
        key = match.group("key")
        value = _assignment_rhs(line, match)
        if _is_credential_key(key) and not _is_safe_literal(value, path):
            yield Finding(
                path,
                line_number,
                "credential-assignment",
                "a non-placeholder credential assignment is prohibited",
            )
        if _is_private_identifier_key(key) and _is_private_identifier_literal(
            key,
            value,
        ):
            yield Finding(
                path,
                line_number,
                "private-account-identifier",
                "a concrete account, tenant, subscription, workspace, warehouse, client, principal, or object ID is prohibited",
            )

    for match in _POWERSHELL_ASSIGNMENT.finditer(line):
        key = match.group("braced_key") or match.group("plain_key")
        value = _assignment_rhs(line, match)
        if _is_credential_key(key) and not _is_safe_literal(value, path):
            yield Finding(
                path,
                line_number,
                "credential-assignment",
                "a non-placeholder credential assignment is prohibited",
            )
        if _is_private_identifier_key(key) and _is_private_identifier_literal(
            key,
            value,
        ):
            yield Finding(
                path,
                line_number,
                "private-account-identifier",
                "a concrete account, tenant, subscription, workspace, warehouse, client, principal, or object ID is prohibited",
            )

    for match in _DECLARED_ASSIGNMENT.finditer(line):
        key = match.group("key")
        value = _assignment_rhs(line, match)
        if _is_credential_key(key) and not _is_safe_literal(value, path):
            yield Finding(
                path,
                line_number,
                "credential-assignment",
                "a non-placeholder credential assignment is prohibited",
            )
        if _is_private_identifier_key(key) and _is_private_identifier_literal(
            key,
            value,
        ):
            yield Finding(
                path,
                line_number,
                "private-account-identifier",
                "a concrete account, tenant, subscription, workspace, warehouse, client, principal, or object ID is prohibited",
            )

    for match in _PROSE_IDENTIFIER.finditer(line):
        if _is_private_identifier_literal(
            match.group("key"),
            match.group("value"),
        ):
            yield Finding(
                path,
                line_number,
                "private-account-identifier",
                "a concrete account, tenant, subscription, workspace, warehouse, client, principal, or object ID is prohibited",
            )

    for match in _AZURE_SUBSCRIPTION_RESOURCE_ID.finditer(line):
        if _is_private_identifier_literal("subscriptionId", match.group("value")):
            yield Finding(
                path,
                line_number,
                "private-account-identifier",
                "a concrete account, tenant, subscription, workspace, warehouse, client, principal, or object ID is prohibited",
            )

    if _CONCRETE_DATABRICKS_HOST.search(line):
        yield Finding(
            path,
            line_number,
            "private-demo-host",
            "a concrete Databricks workspace host is prohibited",
        )
    if _PRIVATE_NETWORK_URL.search(line):
        yield Finding(
            path,
            line_number,
            "private-network-host",
            "a private network URL is prohibited",
        )
    for match in _AZURE_APP_SERVICE_URL.finditer(line):
        if not _has_placeholder_marker(match.group("host")):
            yield Finding(
                path,
                line_number,
                "private-app-service-host",
                "a concrete Azure App Service URL is prohibited",
            )
    for match in _ONMICROSOFT_IDENTITY.finditer(line):
        if not _has_placeholder_marker(
            f"{match.group('local')} {match.group('tenant')}"
        ):
            yield Finding(
                path,
                line_number,
                "private-tenant-identity",
                "a concrete onmicrosoft.com identity is prohibited",
            )


def _decode_blob(content: bytes) -> str | None:
    try:
        if content.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = content.decode("utf-16")
        elif content.startswith(b"\xef\xbb\xbf"):
            text = content.decode("utf-8-sig")
        elif b"\x00" in content:
            if len(content) % 2:
                return None
            even_zeros = content[0::2].count(0)
            odd_zeros = content[1::2].count(0)
            threshold = max(1, len(content) // 8)
            if odd_zeros >= threshold and odd_zeros > even_zeros * 2:
                text = content.decode("utf-16-le")
            elif even_zeros >= threshold and even_zeros > odd_zeros * 2:
                text = content.decode("utf-16-be")
            else:
                return None
        else:
            text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return None if "\x00" in text else text.lstrip("\ufeff")


def _logical_assignment_lines(path: str, lines: list[str]) -> Iterable[tuple[int, str]]:
    source_code = PurePosixPath(path).suffix.lower() in _SOURCE_CODE_SUFFIXES
    for index, line in enumerate(lines):
        stripped_line = line.rstrip()
        if not (
            stripped_line.endswith(":") or (source_code and stripped_line.endswith("="))
        ):
            continue
        for continuation in lines[index + 1 : index + 5]:
            stripped = continuation.strip()
            if not stripped or stripped.startswith(("#", "//")):
                continue
            yield index + 1, f"{line.rstrip()} {stripped}"
            break


def scan_blob(path: str, content: bytes) -> list[Finding]:
    findings = list(_path_findings(path))
    text = _decode_blob(content)
    if text is None:
        if not findings:
            findings.append(
                Finding(
                    path,
                    0,
                    "unsupported-text-encoding",
                    "tracked text must use UTF-8 or recognizable UTF-16 encoding",
                )
            )
        return sorted(set(findings))

    lines = text.splitlines()
    for line_number, line in enumerate(lines, start=1):
        findings.extend(_line_findings(path, line_number, line))
    for line_number, logical_line in _logical_assignment_lines(path, lines):
        findings.extend(_line_findings(path, line_number, logical_line))
    return sorted(set(findings))


def scan_repository(repository: Path) -> tuple[list[Finding], int]:
    blobs = tracked_blobs(repository)
    findings = [
        finding for blob in blobs for finding in scan_blob(blob.path, blob.content)
    ]
    return sorted(set(findings)), len(blobs)


def _write_report(path: Path, findings: list[Finding], tracked_file_count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "schemaVersion": 1,
        "trackedFileCount": tracked_file_count,
        "findingCount": len(findings),
        "findings": [asdict(finding) for finding in findings],
    }
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scan tracked repository content without printing matched values.",
    )
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Git repository to scan (defaults to the repository containing this script).",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Optional path for a value-free JSON report.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    repository = args.repository.resolve()
    try:
        findings, tracked_file_count = scan_repository(repository)
    except (OSError, ScanError) as error:
        print(f"clean-room scan could not complete: {error}", file=sys.stderr)
        return 2

    if args.report is not None:
        _write_report(args.report.resolve(), findings, tracked_file_count)

    if findings:
        print(
            f"M0 clean-room scan found {len(findings)} finding(s) "
            f"across {tracked_file_count} tracked files.",
            file=sys.stderr,
        )
        for finding in findings:
            location = (
                finding.path if finding.line == 0 else f"{finding.path}:{finding.line}"
            )
            print(
                f"{location} [{finding.rule}] {finding.message}",
                file=sys.stderr,
            )
        return 1

    print(f"M0 clean-room scan passed for {tracked_file_count} tracked files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
