"""Copilot `permissionRequest` hook: approve exactly the factory's merge command, nothing else.

Opt-in only; the template never turns it on. Copy this file to ~/.copilot/hooks/
($COPILOT_HOME/hooks/ if you set COPILOT_HOME) and add the personal hook that README →
"Letting the factory merge for you" shows. Never run it as a repository hook: that would run the
working tree's copy, which any file edit could change.

Reads the permission request as JSON on stdin. In a `permissionRequest`, Copilot CLI 1.0.116 sends
the tool as `toolName` and its arguments as `toolInput`, though its hooks help names them
`toolArgs`; both are read, and so are Claude-style `tool_name`/`tool_input`. Prints
{"behavior": "allow"} only for a shell command that is, in full,
`gh pr merge <N> --merge --delete-branch` or `gh pr merge <N> --merge --admin --delete-branch`.
Anything else, and any error, prints nothing, so Copilot's normal permission flow decides. It never
exits 2, which Copilot treats as a deny. Stdlib only.

    python3 .claude/skills/factory/scripts/merge_hook.py < request.json
    python3 .claude/skills/factory/scripts/merge_hook.py --self-test
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

SHELL_TOOLS = {"bash", "powershell"}
MERGE = re.compile(r"gh pr merge [1-9][0-9]* --merge (--admin )?--delete-branch")
ALLOW = {"behavior": "allow"}


def command_of(request: dict) -> str | None:
    """The shell command a request asks to run, or None for any other tool or a malformed request."""
    tool = request.get("toolName", request.get("tool_name"))
    if not isinstance(tool, str) or tool.lower() not in SHELL_TOOLS:
        return None
    keys = ("toolInput", "toolArgs", "tool_input")
    args = next((request[k] for k in keys if k in request), None)
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None
    if not isinstance(args, dict):
        return None
    command = args.get("command")
    return command if isinstance(command, str) else None


def decide(request: object) -> dict | None:
    """ALLOW for the factory's merge command; None (no decision) for anything else."""
    if not isinstance(request, dict):
        return None
    command = command_of(request)
    if command is not None and MERGE.fullmatch(command):  # exact: no strip, no lookalike whitespace
        return ALLOW
    return None


def _self_test() -> None:
    def req(command: str, tool: str = "bash", as_string: bool = True) -> dict:
        args = {"command": command, "description": "merge the PR"}
        return {"toolName": tool, "toolArgs": json.dumps(args) if as_string else args}

    for command in (
        "gh pr merge 22 --merge --delete-branch",
        "gh pr merge 7 --merge --admin --delete-branch",
    ):
        assert decide(req(command)) == ALLOW, command
        assert decide(req(command, as_string=False)) == ALLOW, command
        assert decide(req(command, tool="powershell")) == ALLOW, command
    claude_style = {
        "tool_name": "Bash",
        "tool_input": {"command": "gh pr merge 3 --merge --delete-branch"},
    }
    assert decide(claude_style) == ALLOW

    # #95: the payload Copilot CLI 1.0.116 sends a permissionRequest hook (synthetic values)
    def copilot(command: str) -> dict:
        return {
            "hookName": "permissionRequest",
            "sessionId": "s",
            "timestamp": 1,
            "cwd": "/tmp/repo",
            "permissionSuggestions": [],
            "toolName": "bash",
            "toolInput": {"command": command},
        }

    assert decide(copilot("gh pr merge 22 --merge --delete-branch")) == ALLOW
    assert decide(copilot("gh pr merge 22 --merge --admin --delete-branch")) == ALLOW
    assert decide(copilot("gh pr close 22")) is None
    assert decide(copilot("gh pr merge 22 --merge --delete-branch && echo x")) is None
    refused = [
        "gh pr merge 22 --merge --delete-branch -R owner/repo",
        "gh pr merge 22 -R owner/repo --merge --delete-branch",
        "gh pr merge 22 --squash --delete-branch",
        "gh pr merge 22 --merge",
        "gh pr merge --merge --delete-branch",
        "gh pr merge 22 --merge --delete-branch && rm -rf ~",
        "gh pr merge 22 --merge --delete-branch; echo done",
        "gh pr merge 22 --merge --delete-branch\necho done",
        "gh pr merge 22 --merge --delete-branch | tee merge.log",
        "gh pr merge $(echo 22) --merge --delete-branch",
        "gh pr close 22",
        "gh api -X PUT repos/o/r/pulls/22/merge",
        "echo gh pr merge 22 --merge --delete-branch",
        " gh pr merge 22 --merge --delete-branch",
        "gh pr merge 22 --merge --delete-branch ",
        "gh pr merge 22 --merge --delete-branch\n",
        "gh pr merge 22 --merge --delete-branch\r",
        "gh pr merge 22 --merge --delete-branch\x1f",
        "\x0bgh pr merge 22 --merge --delete-branch",
        "gh pr merge 22 --merge --delete-branch\u2028",
        "gh pr merge 22 --merge\t--delete-branch",
        "gh pr merge 22\u00a0--merge --delete-branch",
        "gh pr merge \u0662\u0662 --merge --delete-branch",
    ]
    for command in refused:
        assert decide(req(command)) is None, command
    assert decide(req("gh pr merge 22 --merge --delete-branch", tool="edit")) is None
    assert decide({"toolName": "bash", "toolArgs": "{not json"}) is None
    assert decide({"toolName": "bash", "toolArgs": {"command": 5}}) is None
    assert decide({"toolName": "bash"}) is None
    assert decide(["not", "a", "request"]) is None
    # The command line: an allow prints the decision; anything else prints nothing; always exit 0.
    for stdin, out in (
        (json.dumps(req("gh pr merge 22 --merge --delete-branch")), json.dumps(ALLOW)),
        (json.dumps(req("gh pr close 22")), ""),
        ("not json", ""),
        ("", ""),
    ):
        run = subprocess.run(
            [sys.executable, __file__], input=stdin, capture_output=True, text=True, check=False
        )
        assert run.returncode == 0 and run.stdout.strip() == out, (stdin, run)
    print("merge_hook.py self-test: ok")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--self-test" in argv:
        _self_test()
        return 0
    try:
        decision = decide(json.loads(sys.stdin.read() or "null"))
    except (ValueError, OSError, RecursionError):  # no decision: the normal permission flow runs
        decision = None
    if decision:
        print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
