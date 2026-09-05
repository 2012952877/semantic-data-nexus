from __future__ import annotations

import os
import unittest
import urllib.request
from unittest.mock import patch

from scripts import full_stack_smoke


class HostedSmokeAuthenticationTests(unittest.TestCase):
    def test_local_mode_keeps_the_existing_unauthenticated_client(self) -> None:
        request = urllib.request.Request("http://127.0.0.1:8080/api/v1/me")
        with (
            patch.dict(
                os.environ,
                {"NEXUS_SMOKE_USERNAME": "", "NEXUS_SMOKE_PASSWORD": ""},
            ),
            patch.object(urllib.request, "urlopen") as open_url,
        ):
            result = full_stack_smoke._open_request(
                request, "http://127.0.0.1:8080", 15
            )
        self.assertIs(result, open_url.return_value)
        open_url.assert_called_once_with(request, timeout=15)

    def test_hosted_auth_is_scoped_to_its_https_origin(self) -> None:
        request = urllib.request.Request("https://test.example.invalid/api/v1/me")
        with (
            patch.dict(
                os.environ,
                {
                    "NEXUS_SMOKE_USERNAME": "synthetic-user",
                    "NEXUS_SMOKE_PASSWORD": "synthetic-test-placeholder",
                },
            ),
            patch.object(urllib.request, "HTTPPasswordMgrWithPriorAuth") as manager,
            patch.object(urllib.request, "build_opener") as build_opener,
            patch.object(urllib.request, "urlopen") as anonymous_open,
        ):
            full_stack_smoke._open_request(
                request, "https://test.example.invalid", 15
            )
        manager.return_value.add_password.assert_called_once_with(
            None,
            "https://test.example.invalid/",
            "synthetic-user",
            "synthetic-test-placeholder",
            is_authenticated=True,
        )
        build_opener.return_value.open.assert_called_once_with(request, timeout=15)
        anonymous_open.assert_not_called()

    def test_auth_is_never_sent_over_plain_http(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "NEXUS_SMOKE_USERNAME": "synthetic-user",
                    "NEXUS_SMOKE_PASSWORD": "synthetic-test-placeholder",
                },
            ),
            patch.object(urllib.request, "urlopen") as open_url,
            patch.object(urllib.request, "build_opener") as build_opener,
            self.assertRaisesRegex(full_stack_smoke.SmokeFailure, "HTTPS"),
        ):
            full_stack_smoke._open_request(
                urllib.request.Request("http://test.example.invalid/"),
                "http://test.example.invalid",
                15,
            )
        open_url.assert_not_called()
        build_opener.assert_not_called()

    def test_partial_credentials_fail_closed(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"NEXUS_SMOKE_USERNAME": "synthetic-user", "NEXUS_SMOKE_PASSWORD": ""},
            ),
            self.assertRaisesRegex(full_stack_smoke.SmokeFailure, "Both"),
        ):
            full_stack_smoke._open_request(
                urllib.request.Request("https://test.example.invalid/"),
                "https://test.example.invalid",
                15,
            )

    def test_authenticated_redirect_is_rejected_before_forwarding_credentials(self) -> None:
        with self.assertRaisesRegex(full_stack_smoke.SmokeFailure, "redirects"):
            full_stack_smoke._NoAuthenticatedRedirect().redirect_request(
                None, None, 302, "Found", {}, "http://unrelated.example/"
            )


if __name__ == "__main__":
    unittest.main()
