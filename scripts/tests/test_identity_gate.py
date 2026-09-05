from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class IdentityGateTests(unittest.TestCase):
    def test_identity_is_unconditional_reusable_gate(self) -> None:
        aggregate = (ROOT / ".github/workflows/m0-release-gate.yml").read_text()
        identity = (ROOT / ".github/workflows/identity.yml").read_text()
        self.assertRegex(identity, r"(?m)^  workflow_call:\s*$")
        match = re.search(
            r"(?ms)^  enterprise_identity:\n(?P<body>.*?)(?=^  [a-z_]+:)",
            aggregate,
        )
        self.assertIsNotNone(match)
        body = match.group("body") if match is not None else ""
        self.assertIn("uses: ./.github/workflows/identity.yml", body)
        self.assertNotRegex(body, r"(?m)^\s+if:")
        self.assertNotRegex(aggregate, r"(?m)^    paths(?:-ignore)?:")

    def test_every_job_reaches_the_fail_closed_aggregate(self) -> None:
        aggregate = (ROOT / ".github/workflows/m0-release-gate.yml").read_text()
        jobs = set(re.findall(r"(?m)^  ([a-z_]+):$", aggregate.split("jobs:\n", 1)[1]))
        jobs.remove("release_gate")
        release = aggregate.split("\n  release_gate:\n", 1)[1]
        needs = set(re.findall(r"(?m)^      - ([a-z_]+)$", release))
        environments = dict(re.findall(
            r"(?m)^          ([A-Z_]+): \$\{\{ needs\.([a-z_]+)\.result \}\}$",
            release,
        ))
        checked = dict(re.findall(r'(?m)^            "([a-z_]+)=\$([A-Z_]+)"', release))
        self.assertEqual(jobs, needs)
        self.assertEqual(needs, set(environments.values()))
        self.assertEqual(needs, set(checked))
        for job, environment in checked.items():
            self.assertEqual(job, environments[environment])
        self.assertIn("enterprise_identity", needs)
        self.assertIn("    name: M0 release gate\n    if: always()", release)
        self.assertIn('if [ "$result" != "success" ]; then', release)
        self.assertIn('exit "$status"', release)
        self.assertIn("status=1", release)


if __name__ == "__main__":
    unittest.main()
