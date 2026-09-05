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
        literal = "ordinary-looking-literal"
        findings = scan_blob("settings.json", f'"{key}": "{literal}"\n'.encode())

        self.assertEqual(
            [finding.rule for finding in findings], ["credential-assignment"]
        )

    def test_reports_private_key_and_concrete_workspace_without_values(self) -> None:
        private_key_header = ("-" * 5) + "BEGIN PRIVATE KEY" + ("-" * 5)
        workspace = "https://adb-" + "123456789" + ".987654.azuredatabricks.net"
        content = f"{private_key_header}\nendpoint={workspace}\n".encode()

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
        tenant_identity = (
            "operator" + "@" + "private-tenant" + "." + "onmicrosoft" + ".com"
        )
        findings = scan_blob(
            "deployment.txt",
            (
                f"endpoint={app_hosts[0]}\n"
                f"shortEndpoint={app_hosts[1]}\n"
                f"owner={tenant_identity}\n"
            ).encode(),
        )

        self.assertEqual(
            [finding.rule for finding in findings],
            [
                "private-app-service-host",
                "private-app-service-host",
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
        self.assertNotIn(tenant_identity, rendered)

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
