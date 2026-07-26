#!/usr/bin/env python3
# =============================================================================
# release_model_gate.py — goal 35: the deploy-from-main tripwire, as a CONTROL.
#
# CLAUDE.md's auto-merge autonomy is valid ONLY while nothing deploys from
# `main`. The moment a workflow auto-deploys on push to main, an unattended run
# could squash-merge itself into a live deploy. That used to be prose; the
# 2026-07-23 go-real premortem's one lesson was "a rule we wrote down is not a
# control we enforce." This is the control.
#
# It FAILS (exit 1) when the committed RELEASE_MODEL marker says DECISION=open
# AND any GitHub Actions workflow would AUTOMATICALLY deploy as a consequence of
# code reaching `main` — i.e. it has BOTH:
#   (T) an automatic push-to-main trigger, AND
#   (D) a deploy-action token.
# exit 0 = safe, exit 1 = tripped, exit 2 = misconfigured (marker missing/bad).
#
# DESIGN — heuristic, fail-closed, NO free-text bypass:
#   To catch an UNDECLARED deploy workflow (the actual threat: a future run adds
#   one), detection must INFER deploy-ness — an explicit "I am a deploy" opt-in
#   would just be another prose rule that the same unattended actor could omit.
#   So we grep for deploy-shaped tokens + a main-push trigger, and we fail
#   CLOSED: when a push trigger's branch scope can't be PROVEN to exclude main,
#   we assume it includes main. The only way to trip this is to genuinely look
#   like an automatic deploy from main — which is exactly when a human must have
#   decided the release model first. There is deliberately no bypass marker: a
#   bypass an unattended run could set is not a control.
#
# Scope: AUTOMATIC deploy-on-push-to-main only. Manual (workflow_dispatch) / tag
# / release deploys are a human act and out of the automatic tripwire; per
# CLAUDE.md they still need this decision closed by a human. Pure stdlib,
# offline, deterministic — no YAML dependency, no network, no cloud calls.
# =============================================================================
from __future__ import annotations

import argparse
import glob
import os
import re
import sys

# Deploy-action tokens. Broad on purpose (fail-closed): the high-value catch is
# an Azure / container deploy (this repo's north star is a Foundry-fronting
# gateway hosted in Azure), plus generic CD actions and cluster applies. This is
# a BACKSTOP, not a proof of deploy-ness — the RELEASE_MODEL decision + human
# review remain the real gate. Matched case-insensitively as substrings.
DEPLOY_TOKENS = (
    "azure/login",
    "azure/webapps-deploy",
    "azure/arm-deploy",
    "azure/container-apps-deploy",
    "azure/aci-deploy",
    "az containerapp",
    "az webapp",
    "az deployment",
    "az acr",
    "az functionapp",
    "azure/functions-action",
    "docker push",
    "docker/build-push-action",
    "kubectl apply",
    "kubectl rollout",
    "helm upgrade",
    "terraform apply",
)


def parse_decision(text: str) -> str:
    """Return the DECISION value from RELEASE_MODEL text. Raise on missing/bad.

    Format is one `DECISION=<value>` line (comments start with #). We take the
    last non-comment DECISION= line so a stray commented example can't win.
    """
    value = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^DECISION\s*=\s*(\S+)\s*$", line)
        if m:
            value = m.group(1).lower()
    if value is None:
        raise ValueError("RELEASE_MODEL has no `DECISION=<value>` line")
    if value not in ("open", "decided"):
        raise ValueError(
            f"RELEASE_MODEL DECISION={value!r} is not one of: open, decided"
        )
    return value


def _branch_list_excludes_main(entries: list[str]) -> bool:
    """True only if we can PROVE this explicit branch list excludes main/master.

    Fail-closed: a glob (`*`/`?`/`[`) might match main, so any glob entry means
    we canNOT prove exclusion -> return False (assume main is reachable).
    """
    for e in entries:
        b = e.strip().strip("'\"").strip()
        if not b:
            continue
        if b in ("main", "master"):
            return False  # explicitly includes main/master
        if any(ch in b for ch in "*?["):
            return False  # a glob might match main — cannot prove exclusion
    return True  # a concrete list of non-main names — proven excluded


def triggers_on_push_to_main(text: str) -> bool:
    """True if the workflow's top-level `on:` runs on push to `main`.

    Handles the common shapes, fail-closed on ambiguity:
      on: push                         -> all branches -> includes main
      on: [push, pull_request]         -> push present -> includes main
      on:\n  push:                     -> no `branches:` filter -> all branches
      on:\n  push:\n    branches:[...]  -> main iff list can reach main
    `pull_request` triggers are NOT push and never count (CI uses that). The
    `on:` key is only honoured at column 0 (top-level) so a nested key named
    `on` (e.g. an input) cannot false-match.
    """
    lines = text.splitlines()

    # --- inline form at column 0: `on: push` / `on: [push, ...]` -------------
    for raw in lines:
        m = re.match(r"^on\s*:\s*(.+?)\s*$", raw)  # column 0, non-empty value
        if not m:
            continue
        rhs = m.group(1).split("#", 1)[0].strip()
        if rhs == "":
            break  # comment-only value -> block form (none) -> not push
        tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_-]*", rhs)
        return "push" in tokens
    # (no inline `on:` value -> fall through to block-form scan)

    # --- block form: top-level `on:` mapping, then a `push:` sub-key ---------
    n = len(lines)
    i = 0
    in_on = False
    while i < n:
        raw = lines[i]
        i += 1
        if raw.strip() == "" or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if not in_on:
            if indent == 0 and re.match(r"^on\s*:\s*(#.*)?$", raw.strip()):
                in_on = True
            continue
        # inside the on: block until a key dedents back to column 0
        if indent == 0:
            break
        if not re.match(r"^push\s*:\s*(#.*)?$", raw.strip()):
            continue
        # --- found `push:` — decide from its branches/tags filters ----------
        # GitHub push-trigger semantics:
        #   push: (empty)            -> all branches (and tags)   -> main fires
        #   push: branches:[...]     -> only those branches       -> main iff listed
        #   push: tags:[...] only    -> only tags, NO branch push -> main does NOT
        #   push: branches-ignore    -> all-except                -> main unless ignored
        # `branches:` is decisive; absent it, tags-only means no branch push.
        push_indent = indent
        saw_branches = branches_reach_main = False
        saw_tags = saw_branches_ignore = main_ignored = False
        while i < n:
            praw = lines[i]
            if praw.strip() == "" or praw.lstrip().startswith("#"):
                i += 1
                continue
            pind = len(praw) - len(praw.lstrip())
            if pind <= push_indent:
                break  # end of the push: sub-block
            key = praw.strip()
            mb = re.match(r"^(branches|branches-ignore|tags)\s*:\s*(.*?)\s*$", key)
            if not mb:
                i += 1
                continue
            name = mb.group(1)
            entries, i = _consume_list_value(lines, i, mb.group(2))
            if name == "branches":
                saw_branches = True
                branches_reach_main = not _branch_list_excludes_main(entries)
            elif name == "tags":
                saw_tags = True
            else:  # branches-ignore
                saw_branches_ignore = True
                main_ignored = any(
                    e.strip().strip("'\"") in ("main", "master") for e in entries
                )
        if saw_branches:
            return branches_reach_main
        if saw_branches_ignore:
            return not main_ignored  # fail-closed: main fires unless ignored
        if saw_tags:
            return False  # tags-only push -> never a branch push
        return True  # push: with no branch/tag filter -> all branches
    return False


def _consume_list_value(lines: list[str], i: int, inline_rest: str):
    """Read a YAML sequence value that starts on line `i` (the `key: rest` line).

    Returns (entries, next_i). Handles inline `[a, b]` on the same line and the
    block `- a` / `- b` form on following lines. `i` points AT the key line.
    """
    rest = inline_rest.split("#", 1)[0].strip()
    if rest.startswith("["):
        return re.findall(r"[^,\[\]\s'\"]+", rest), i + 1
    # block-list form: gather following `- <item>` lines at one indent
    i += 1
    entries: list[str] = []
    item_indent = None
    n = len(lines)
    while i < n:
        raw = lines[i]
        if raw.strip() == "" or raw.lstrip().startswith("#"):
            i += 1
            continue
        if not raw.lstrip().startswith("-"):
            break
        ind = len(raw) - len(raw.lstrip())
        if item_indent is None:
            item_indent = ind
        if ind != item_indent:
            break
        entries.append(raw.strip()[1:].strip())
        i += 1
    return entries, i


def has_deploy_action(text: str) -> bool:
    low = text.lower()
    return any(tok in low for tok in DEPLOY_TOKENS)


def scan_workflow(text: str) -> bool:
    """True if this workflow auto-deploys from main (push-to-main AND deploy)."""
    return triggers_on_push_to_main(text) and has_deploy_action(text)


def find_workflows(root: str) -> list[str]:
    wf_dir = os.path.join(root, ".github", "workflows")
    hits: list[str] = []
    for pat in ("*.yml", "*.yaml"):
        hits.extend(glob.glob(os.path.join(wf_dir, pat)))
    return sorted(hits)


def evaluate(root: str) -> tuple[int, list[str]]:
    """Return (exit_code, message_lines)."""
    marker = os.path.join(root, "RELEASE_MODEL")
    msgs: list[str] = []
    if not os.path.isfile(marker):
        return 2, [f"RELEASE_MODEL marker missing at {marker}"]
    try:
        with open(marker, encoding="utf-8") as fh:
            decision = parse_decision(fh.read())
    except (OSError, ValueError) as e:
        return 2, [f"RELEASE_MODEL unreadable/invalid: {e}"]

    offenders: list[str] = []
    for wf in find_workflows(root):
        try:
            with open(wf, encoding="utf-8") as fh:
                if scan_workflow(fh.read()):
                    offenders.append(os.path.relpath(wf, root))
        except OSError as e:
            # A workflow we can't read is treated as an offender (fail-closed).
            return 2, [f"workflow unreadable: {wf}: {e}"]

    if decision == "open" and offenders:
        msgs.append(
            "RELEASE_MODEL DECISION=open, but these workflows auto-deploy from main:"
        )
        msgs.extend(f"  - {o}" for o in offenders)
        msgs.append(
            "A prose tripwire is not a control. Either revert the deploy-from-main "
            "workflow, OR have a human decide the release model and set "
            "DECISION=decided (see RELEASE_MODEL + GOALS.md). An unattended run "
            "must NOT auto-merge into a world where main deploys."
        )
        return 1, msgs

    if decision == "decided" and offenders:
        msgs.append(
            "RELEASE_MODEL DECISION=decided — deploy-from-main allowed. "
            f"Workflows deploying from main: {', '.join(offenders)}"
        )
        return 0, msgs

    msgs.append(
        f"RELEASE_MODEL DECISION={decision}; no deploy-from-main workflow found."
    )
    return 0, msgs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Goal-35 deploy-from-main tripwire.")
    ap.add_argument(
        "--root",
        default=None,
        help="repo root to scan (default: git toplevel, else cwd)",
    )
    args = ap.parse_args(argv)
    root = args.root
    if root is None:
        root = os.environ.get("RELEASE_MODEL_ROOT") or os.getcwd()
    root = os.path.abspath(root)
    code, msgs = evaluate(root)
    stream = sys.stderr if code != 0 else sys.stdout
    for m in msgs:
        print(m, file=stream)
    return code


if __name__ == "__main__":
    sys.exit(main())
