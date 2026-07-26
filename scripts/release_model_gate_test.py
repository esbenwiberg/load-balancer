#!/usr/bin/env python3
# =============================================================================
# release_model_gate_test.py — offline unit tests for the goal-35 tripwire.
#
# Proves BOTH directions the completion condition demands:
#   - DECISION=open + a deploy-from-main workflow  -> gate FAILS (exit 1)
#   - DECISION=decided (or no deploy workflow)      -> gate PASSES (exit 0)
# plus the push-to-main parser's edge cases (the whole point is that CI's
# pull_request:[main] must NOT trip, but a real push:[main] deploy must).
# Pure stdlib; no network, no docker. Wired into scripts/check.sh fast tier.
# =============================================================================
import os
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import release_model_gate as g  # noqa: E402


def dedent(s: str) -> str:
    return textwrap.dedent(s).lstrip("\n")


class ParseDecision(unittest.TestCase):
    def test_open(self):
        self.assertEqual(g.parse_decision("DECISION=open"), "open")

    def test_decided_with_comments_and_ws(self):
        text = dedent("""
            # a comment
            #   DECISION=open   (an example that must NOT win)
              DECISION = decided
        """)
        self.assertEqual(g.parse_decision(text), "decided")

    def test_last_wins(self):
        self.assertEqual(g.parse_decision("DECISION=open\nDECISION=decided"), "decided")

    def test_case_insensitive_value(self):
        self.assertEqual(g.parse_decision("DECISION=Decided"), "decided")

    def test_missing_raises(self):
        with self.assertRaises(ValueError):
            g.parse_decision("# nothing here\n")

    def test_invalid_value_raises(self):
        with self.assertRaises(ValueError):
            g.parse_decision("DECISION=maybe")


class PushToMainDetection(unittest.TestCase):
    def yes(self, y):
        self.assertTrue(g.triggers_on_push_to_main(dedent(y)))

    def no(self, y):
        self.assertFalse(g.triggers_on_push_to_main(dedent(y)))

    def test_inline_push(self):
        self.yes("on: push\njobs: {}\n")

    def test_inline_list_with_push(self):
        self.yes("on: [pull_request, push]\njobs: {}\n")

    def test_inline_list_without_push(self):
        self.no("on: [pull_request, workflow_dispatch]\njobs: {}\n")

    def test_block_push_no_branches(self):
        self.yes("on:\n  push:\n  workflow_dispatch:\njobs: {}\n")

    def test_block_push_branches_inline_main(self):
        self.yes("on:\n  push:\n    branches: [main]\n")

    def test_block_push_branches_inline_master(self):
        self.yes("on:\n  push:\n    branches: ['master', 'foo']\n")

    def test_block_push_branches_list_main(self):
        self.yes("on:\n  push:\n    branches:\n      - dev\n      - main\n")

    def test_block_push_branches_concrete_non_main_excluded(self):
        self.no("on:\n  push:\n    branches: [dev, staging]\n")

    def test_block_push_branches_list_concrete_non_main_excluded(self):
        self.no("on:\n  push:\n    branches:\n      - dev\n      - staging\n")

    def test_block_push_branches_glob_fail_closed(self):
        # a glob might match main -> cannot prove exclusion -> True
        self.yes("on:\n  push:\n    branches: ['release/*']\n")

    def test_block_push_branches_doublestar(self):
        self.yes("on:\n  push:\n    branches: ['**']\n")

    def test_pull_request_only_is_not_push(self):
        # exactly the current CI shape — must NOT trip
        self.no("on:\n  pull_request:\n    branches: [main]\n")

    def test_pull_request_inline_only(self):
        self.no("on: pull_request\n")

    def test_push_tags_only_is_not_a_branch_push(self):
        # push: tags: (no branches:) fires ONLY on tag pushes, not on main branch
        # push -> a tagged deploy (a human act), out of the automatic tripwire.
        self.no("on:\n  push:\n    tags: ['v*']\n")

    def test_push_branches_and_tags_main(self):
        # branches: decisive even alongside tags:
        self.yes("on:\n  push:\n    branches: [main]\n    tags: ['v*']\n")

    def test_push_branches_ignore_main_excluded(self):
        self.no("on:\n  push:\n    branches-ignore: [main]\n")

    def test_push_branches_ignore_other_fires_on_main(self):
        self.yes("on:\n  push:\n    branches-ignore: [dev]\n")

    def test_nested_key_named_on_does_not_match(self):
        # an input literally named 'on' must not be read as a trigger
        self.no(
            dedent("""
            on:
              workflow_dispatch:
                inputs:
                  on:
                    description: not a trigger
            jobs: {}
        """)
        )


class DeployAction(unittest.TestCase):
    def test_azure_login(self):
        self.assertTrue(g.has_deploy_action("uses: azure/login@v2"))

    def test_az_containerapp(self):
        self.assertTrue(g.has_deploy_action("run: az containerapp update ..."))

    def test_case_insensitive(self):
        self.assertTrue(g.has_deploy_action("USES: Azure/Login@v2"))

    def test_no_deploy_tokens(self):
        self.assertFalse(g.has_deploy_action("run: scripts/check.sh --full"))


class EvaluateEndToEnd(unittest.TestCase):
    def _repo(self, decision_text, workflows):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "RELEASE_MODEL"), "w") as fh:
            fh.write(decision_text)
        wf = os.path.join(d, ".github", "workflows")
        os.makedirs(wf)
        for name, content in workflows.items():
            with open(os.path.join(wf, name), "w") as fh:
                fh.write(dedent(content))
        return d

    DEPLOY_WF = """
        on:
          push:
            branches: [main]
        jobs:
          deploy:
            runs-on: ubuntu-latest
            steps:
              - uses: azure/login@v2
              - run: az containerapp update --name gw --resource-group rg
    """

    CI_WF = """
        on:
          pull_request:
            branches: [main]
        jobs:
          gate:
            runs-on: ubuntu-latest
            steps:
              - run: scripts/check.sh --full
    """

    def test_open_plus_deploy_fails(self):
        root = self._repo("DECISION=open\n", {"deploy.yaml": self.DEPLOY_WF})
        code, msgs = g.evaluate(root)
        self.assertEqual(code, 1)
        self.assertTrue(any("deploy.yaml" in m for m in msgs))

    def test_decided_plus_deploy_passes(self):
        root = self._repo("DECISION=decided\n", {"deploy.yaml": self.DEPLOY_WF})
        code, _ = g.evaluate(root)
        self.assertEqual(code, 0)

    def test_open_plus_ci_only_passes(self):
        # the CI-only shape (pull_request) is safe even while open
        root = self._repo("DECISION=open\n", {"ci.yaml": self.CI_WF})
        code, _ = g.evaluate(root)
        self.assertEqual(code, 0)

    def test_open_no_workflows_passes(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "RELEASE_MODEL"), "w") as fh:
            fh.write("DECISION=open\n")
        code, _ = g.evaluate(d)
        self.assertEqual(code, 0)

    def test_missing_marker_is_misconfig(self):
        d = tempfile.mkdtemp()
        code, _ = g.evaluate(d)
        self.assertEqual(code, 2)

    def test_bad_marker_is_misconfig(self):
        root = self._repo("DECISION=perhaps\n", {})
        code, _ = g.evaluate(root)
        self.assertEqual(code, 2)

    def test_push_deploy_but_non_main_branch_passes_while_open(self):
        wf = """
            on:
              push:
                branches: [staging]
            jobs:
              deploy:
                steps:
                  - run: az webapp deploy
        """
        root = self._repo("DECISION=open\n", {"stg.yaml": wf})
        code, _ = g.evaluate(root)
        self.assertEqual(code, 0)


class RealRepo(unittest.TestCase):
    """The gate must PASS on the actual repo as committed (open + no deploy)."""

    def test_current_repo_is_clean(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code, msgs = g.evaluate(root)
        self.assertEqual(code, 0, msgs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
