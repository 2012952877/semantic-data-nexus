from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.clean_room_scan import Finding, _write_report, scan_blob


def _dashed_identifier(*parts: str) -> str:
    return "-".join(parts)


class CleanRoomScanTests(unittest.TestCase):
    def test_allows_reviewed_synthetic_data_and_placeholders(self) -> None:
        content = (
            b"DATABRICKS_WORKSPACE_HOST=https://example.invalid\n"
            b"DATABRICKS_TOKEN=${DATABRICKS_TOKEN:?required}\n"
        )

        self.assertEqual(
            scan_blob("data/synthetic/generated/regions.csv", content),
            [],
        )

    def test_reports_credential_assignment_without_exposing_value(self) -> None:
        key = "DATABRICKS_" + "TOKEN"
        sensitive_values = [
            "Q9_" * 12,
            "".join(chr(code) for code in range(ord("A"), ord("Z") + 1)) + "a7",
        ]

        for sensitive_value in sensitive_values:
            with self.subTest(value_length=len(sensitive_value)):
                findings = scan_blob(
                    "config.env",
                    f"{key}={sensitive_value}\n".encode(),
                )
                self.assertEqual(
                    [finding.rule for finding in findings],
                    ["credential-assignment"],
                )
                rendered = "\n".join(
                    f"{finding.path}:{finding.line}:{finding.rule}:{finding.message}"
                    for finding in findings
                )
                self.assertNotIn(sensitive_value, rendered)

    def test_reports_short_bare_credentials_in_configuration_files(self) -> None:
        key = "API_" + "KEY"
        short_literal = "shortKey7"
        findings = scan_blob(
            "service.env",
            f"{key}={short_literal}\n".encode(),
        )

        self.assertEqual(
            [finding.rule for finding in findings],
            ["credential-assignment"],
        )

    def test_reports_namespaced_credential_keys(self) -> None:
        namespaced_keys = [
            "AZURE_OPENAI_" + "API_KEY",
            "DATABASE_" + "PASSWORD",
            "ApplicationInsights__" + "ConnectionString",
            "azureOpen" + "AIApiKey",
            "my" + "APIKey",
            "tenant" + "SASToken",
        ]

        for key in namespaced_keys:
            with self.subTest(key=key):
                findings = scan_blob(
                    "service.env",
                    f"{key}=shortKey7\n".encode(),
                )
                self.assertEqual(
                    [finding.rule for finding in findings],
                    ["credential-assignment"],
                )

    def test_reports_credentials_in_nonterminal_namespace_segments(self) -> None:
        keys = [
            "ConnectionStrings__DefaultConnection",
            "Auth__ApiKey__Primary",
            "auth.apiKey.primary",
        ]

        for key in keys:
            with self.subTest(key=key):
                findings = scan_blob(
                    "service.env",
                    f"{key}=shortKey7\n".encode(),
                )
                self.assertEqual(
                    [finding.rule for finding in findings],
                    ["credential-assignment"],
                )

    def test_reports_typed_source_and_bicep_assignments(self) -> None:
        first_name = "AZURE_OPENAI_" + "API_KEY"
        second_name = "DATABASE_" + "PASSWORD"
        object_key = "postgresEntraAdministrator" + "ObjectId"
        object_value = _dashed_identifier(
            "12345678", "9abc", "4def", "8123", "456789abcdef"
        )
        cases = [
            (
                "settings.py",
                f'{first_name}: str = "shortKey7"',
                "credential-assignment",
            ),
            (
                "settings.ts",
                f'const {second_name}: string = "shortKey7";',
                "credential-assignment",
            ),
            (
                "main.bicep",
                f"param {object_key} string = '{object_value}'",
                "private-account-identifier",
            ),
            (
                "settings.py",
                f'{first_name}: str = r"shortKey7"',
                "credential-assignment",
            ),
            (
                "settings.py",
                f'{first_name}: str = """shortKey7"""',
                "credential-assignment",
            ),
            (
                "settings.py",
                f'{object_key}: str = ("{object_value}")',
                "private-account-identifier",
            ),
        ]

        for path, content, expected_rule in cases:
            with self.subTest(path=path):
                findings = scan_blob(path, content.encode())
                self.assertEqual(
                    [finding.rule for finding in findings],
                    [expected_rule],
                )

    def test_reports_multiline_json_and_typescript_credentials(self) -> None:
        key = "API_" + "KEY"
        cases = [
            ("settings.json", f'"{key}":\n"shortKey7"'),
            ("settings.ts", f'const {key}: string =\n"shortKey7";'),
        ]

        for path, content in cases:
            with self.subTest(path=path):
                findings = scan_blob(path, content.encode())
                self.assertEqual(
                    [finding.rule for finding in findings],
                    ["credential-assignment"],
                )

    def test_reports_powershell_credentials_and_identifiers(self) -> None:
        credential_name = "api" + "Key"
        object_key = "object" + "Id"
        principal_key = "principal" + "Id"
        object_value = _dashed_identifier(
            "12345678", "9abc", "4def", "8123", "456789abcdef"
        )
        content = "\n".join(
            [
                f'${credential_name} = "shortKey7"',
                f"${object_key} = '{object_value}'",
                f"${{{principal_key}}} = '{object_value}'",
            ]
        ).encode()

        findings = scan_blob("settings.ps1", content)

        self.assertEqual(
            [finding.rule for finding in findings],
            [
                "credential-assignment",
                "private-account-identifier",
                "private-account-identifier",
            ],
        )

    def test_allows_only_explicit_nonliteral_credential_references(self) -> None:
        key = "DATABRICKS_" + "TOKEN"
        safe_values = [
            "token_provider",
            "${DATABRICKS_TOKEN:?required}",
            "os.environ[TOKEN_NAME]",
            "configuration.Token",
        ]

        for safe_value in safe_values:
            with self.subTest(safe_value=safe_value):
                self.assertEqual(
                    scan_blob("settings.py", f"{key}={safe_value}\n".encode()),
                    [],
                )

    def test_quoted_credential_requires_an_explicit_placeholder(self) -> None:
        key = "DATABRICKS_" + "TOKEN"
        literals = [
            '"ordinary-looking-literal"',
            '"configuration.shortKey7"',
            '["shortKey7"]',
        ]

        for literal in literals:
            with self.subTest(literal=literal):
                findings = scan_blob(
                    "settings.json",
                    f'"{key}": {literal}\n'.encode(),
                )
                self.assertEqual(
                    [finding.rule for finding in findings],
                    ["credential-assignment"],
                )

    def test_reports_private_key_and_concrete_workspace_without_values(self) -> None:
        header_value = ("-" * 5) + "BEGIN PRIVATE KEY" + ("-" * 5)
        workspace = "https://adb-" + "123456789" + ".987654.azuredatabricks.net"
        content = f"{header_value}\nendpoint={workspace}\n".encode()

        findings = scan_blob("settings.txt", content)

        self.assertEqual(
            {finding.rule for finding in findings},
            {"private-demo-host", "private-key-material"},
        )
        rendered = json.dumps(
            [
                {
                    "path": finding.path,
                    "line": finding.line,
                    "rule": finding.rule,
                    "message": finding.message,
                }
                for finding in findings
            ]
        )
        self.assertNotIn(workspace, rendered)

    def test_reports_concrete_app_service_and_tenant_identity_without_values(
        self,
    ) -> None:
        app_hosts = [
            "https://m0-" + "customer" + "." + "azurewebsites" + ".net/path",
            "https://" + "ab" + "." + "azurewebsites" + ".net",
        ]
        tenant_identities = [
            "operator" + "@" + "private-tenant" + "." + "onmicrosoft" + ".com",
            "user" + "@" + "a" + "." + "onmicrosoft" + ".com",
            "external_user#EXT#@" + "ab" + "." + "onmicrosoft" + ".com",
        ]
        findings = scan_blob(
            "deployment.txt",
            (
                f"endpoint={app_hosts[0]}\n"
                f"shortEndpoint={app_hosts[1]}\n"
                f"owner={tenant_identities[0]}\n"
                f"shortTenantOwner={tenant_identities[1]}\n"
                f"guestOwner={tenant_identities[2]}\n"
            ).encode(),
        )

        self.assertEqual(
            [finding.rule for finding in findings],
            [
                "private-app-service-host",
                "private-app-service-host",
                "private-tenant-identity",
                "private-tenant-identity",
                "private-tenant-identity",
            ],
        )
        rendered = json.dumps(
            [
                {
                    "path": finding.path,
                    "line": finding.line,
                    "rule": finding.rule,
                    "message": finding.message,
                }
                for finding in findings
            ]
        )
        for app_host in app_hosts:
            self.assertNotIn(app_host, rendered)
        for tenant_identity in tenant_identities:
            self.assertNotIn(tenant_identity, rendered)

    def test_reports_private_hosts_and_identities_before_terminal_periods(self) -> None:
        private_ip = "http://" + "10" + ".1.2.3"
        private_dns = "https://" + "service" + ".internal"
        app_host = "https://" + "ab" + "." + "azurewebsites" + ".net"
        tenant_identity = "user#EXT#@" + "ab" + "." + "onmicrosoft" + ".com"
        content = "\n".join(
            f"{value}..."
            for value in (private_ip, private_dns, app_host, tenant_identity)
        ).encode()

        findings = scan_blob("notes.md", content)

        self.assertEqual(
            [finding.rule for finding in findings],
            [
                "private-network-host",
                "private-network-host",
                "private-app-service-host",
                "private-tenant-identity",
            ],
        )

    def test_reports_keyed_private_identity_and_warehouse_ids(self) -> None:
        object_key = "postgresEntraAdministrator" + "ObjectId"
        principal_key = "principal" + "Id"
        warehouse_key = "DATABRICKS_" + "WAREHOUSE_ID"
        object_value = _dashed_identifier(
            "12345678", "1234", "4234", "9234", "123456789abc"
        )
        principal_value = _dashed_identifier(
            "abcdefab", "cdef", "4abc", "8def", "abcdefabcdef"
        )
        warehouse_value = "ab12" * 4
        content = "\n".join(
            [
                f"{object_key}: {object_value}",
                f"{principal_key}={principal_value}",
                f"{warehouse_key}={warehouse_value}",
            ]
        ).encode()

        findings = scan_blob("deployment.yaml", content)

        self.assertEqual(
            [finding.rule for finding in findings],
            [
                "private-account-identifier",
                "private-account-identifier",
                "private-account-identifier",
            ],
        )
        rendered = json.dumps([finding.__dict__ for finding in findings])
        self.assertNotIn(object_value, rendered)
        self.assertNotIn(principal_value, rendered)
        self.assertNotIn(warehouse_value, rendered)

    def test_allows_zero_and_placeholder_identity_values(self) -> None:
        object_key = "postgresEntraAdministrator" + "ObjectId"
        warehouse_key = "DATABRICKS_" + "WAREHOUSE_ID"
        zero_uuid = _dashed_identifier(
            "00000000", "0000", "0000", "0000", "000000000000"
        )
        content = "\n".join(
            [
                f"{object_key}: {zero_uuid}",
                f"{warehouse_key}: ci-placeholder",
            ]
        ).encode()

        self.assertEqual(scan_blob("deployment.yaml", content), [])

    def test_reports_private_ids_in_prose_and_azure_resource_paths(self) -> None:
        tenant_label = "Tenant " + "ID"
        subscription_label = "subscription " + "id"
        workspace_label = "workspace " + "ID"
        tenant_value = _dashed_identifier(
            "12345678", "9abc", "4def", "8123", "456789abcdef"
        )
        subscription_value = _dashed_identifier(
            "87654321", "abcd", "4abc", "8abc", "abcdef123456"
        )
        workspace_value = "12345" + "6789012345"
        resource_id = (
            "/" + "subscriptions" + "/" + subscription_value + "/resourceGroups/demo"
        )
        content = "\n".join(
            [
                f"{tenant_label}: {tenant_value}",
                f"{subscription_label} {subscription_value}",
                f"{workspace_label}: {workspace_value}",
                resource_id,
            ]
        ).encode()

        findings = scan_blob("notes.md", content)

        self.assertEqual(
            [finding.rule for finding in findings],
            [
                "private-account-identifier",
                "private-account-identifier",
                "private-account-identifier",
                "private-account-identifier",
            ],
        )
        rendered = json.dumps([finding.__dict__ for finding in findings])
        self.assertNotIn(tenant_value, rendered)
        self.assertNotIn(subscription_value, rendered)
        self.assertNotIn(workspace_value, rendered)
        self.assertNotIn(resource_id, rendered)

    def test_allows_zero_and_placeholder_ids_in_prose_and_resource_paths(
        self,
    ) -> None:
        tenant_label = "Tenant " + "ID"
        workspace_label = "workspace " + "ID"
        zero_uuid = _dashed_identifier(
            "00000000", "0000", "0000", "0000", "000000000000"
        )
        resource_id = "/" + "subscriptions" + "/" + zero_uuid + "/resourceGroups/demo"
        content = "\n".join(
            [
                f"{tenant_label}: {zero_uuid}",
                f"{workspace_label}: demo-placeholder",
                resource_id,
            ]
        ).encode()

        self.assertEqual(scan_blob("notes.md", content), [])

    def test_reports_wrapped_and_dash_separated_prose_ids(self) -> None:
        tenant_label = "Tenant " + "ID"
        tenant_value = _dashed_identifier(
            "12345678", "9abc", "4def", "8123", "456789abcdef"
        )
        en_dash = chr(0x2013)
        em_dash = chr(0x2014)
        content = "\n".join(
            [
                f"{tenant_label} - {tenant_value}",
                f"{tenant_label} {en_dash} {tenant_value}",
                f"{tenant_label} {em_dash} {tenant_value}",
                f"{tenant_label} ({tenant_value})",
                f"{tenant_label}: <{tenant_value}>",
                f"{tenant_label}: `{tenant_value}`",
                f"{tenant_label}: {{{tenant_value}}}",
            ]
        ).encode()

        findings = scan_blob("notes.md", content)

        self.assertEqual(
            [finding.rule for finding in findings],
            ["private-account-identifier"] * 7,
        )
        rendered = json.dumps([finding.__dict__ for finding in findings])
        self.assertNotIn(tenant_value, rendered)

    def test_allows_invalid_and_explicit_cloud_placeholders(self) -> None:
        content = "\n".join(
            [
                "endpoint=https://example.invalid",
                "app=https://placeholder." + "azurewebsites" + ".net",
                "owner=sample@" + "example." + "onmicrosoft" + ".com",
            ]
        ).encode()

        self.assertEqual(scan_blob("deployment.example", content), [])

    def test_reports_private_hosts_before_query_and_fragment_delimiters(self) -> None:
        private_ip_url = "http://" + "10" + ".0.0.1" + "?probe=1"
        private_dns_url = "https://" + "service" + ".internal" + "#status"
        findings = scan_blob(
            "deployment.txt",
            f"first={private_ip_url}\nsecond={private_dns_url}\n".encode(),
        )

        self.assertEqual(
            [finding.rule for finding in findings],
            ["private-network-host", "private-network-host"],
        )
        rendered = json.dumps(
            [
                {
                    "path": finding.path,
                    "line": finding.line,
                    "rule": finding.rule,
                    "message": finding.message,
                }
                for finding in findings
            ]
        )
        self.assertNotIn(private_ip_url, rendered)
        self.assertNotIn(private_dns_url, rendered)

    def test_reports_quoted_and_punctuated_private_urls(self) -> None:
        json_url = "http://" + "192.168" + ".1.5"
        yaml_url = "https://" + "service" + ".corp"
        markdown_url = "http://" + "172.16" + ".0.9"
        content = "\n".join(
            [
                f'{{"origin": "{json_url}"}},',
                f"origin: '{yaml_url}',",
                f"<{markdown_url}>",
            ]
        ).encode()

        findings = scan_blob("deployment.txt", content)

        self.assertEqual(
            [finding.rule for finding in findings],
            [
                "private-network-host",
                "private-network-host",
                "private-network-host",
            ],
        )
        rendered = json.dumps([finding.__dict__ for finding in findings])
        self.assertNotIn(json_url, rendered)
        self.assertNotIn(yaml_url, rendered)
        self.assertNotIn(markdown_url, rendered)

    def test_scans_utf16_powershell_content_with_and_without_bom(self) -> None:
        key = "API_" + "KEY"
        private_url = "http://" + "192" + ".168.10.5"
        text = f'${key} = "shortKey7"\n$endpoint = "{private_url}"'

        for encoding in ("utf-16", "utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding):
                findings = scan_blob("settings.ps1", text.encode(encoding))
                self.assertEqual(
                    {finding.rule for finding in findings},
                    {"credential-assignment", "private-network-host"},
                )

    def test_fails_closed_for_unrecognized_nul_text(self) -> None:
        findings = scan_blob("settings.txt", b"a\x00b")

        self.assertEqual(
            [finding.rule for finding in findings],
            ["unsupported-text-encoding"],
        )

    def test_reports_unapproved_data_and_screenshot_paths(self) -> None:
        data_findings = scan_blob("exports/customer.csv", b"id,value\n")
        image_findings = scan_blob("docs/screenshots/demo.png", b"\x89PNG")

        self.assertEqual(
            {finding.rule for finding in data_findings},
            {"unapproved-raw-data"},
        )
        self.assertEqual(
            {finding.rule for finding in image_findings},
            {"private-demo-path", "proprietary-binary-asset"},
        )

    def test_json_report_contains_only_value_free_finding_fields(self) -> None:
        finding = Finding(
            path="config.env",
            line=3,
            rule="credential-assignment",
            message="a non-placeholder credential assignment is prohibited",
        )
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.json"
            _write_report(report_path, [finding], 4)
            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(report["findingCount"], 1)
        self.assertEqual(
            set(report["findings"][0]),
            {"line", "message", "path", "rule"},
        )


if __name__ == "__main__":
    unittest.main()
