# Claude Code

The home agent can call Claude Code directly when a task benefits from a strong
code agent.

The WeChat runtime should not start an interactive Claude Code session. Instead
it uses `claude_task.py` as a thin non-interactive launcher:

- start Claude Code with `--print`
- pass one task
- wait for the result
- return the result to the home agent

## Usage

Check availability without making a model request:

```bash
./venv/bin/python claude_task.py doctor
```

Run Claude Code:

```bash
./venv/bin/python claude_task.py run --cwd /path/to/project -- "inspect this repo and summarize the main entrypoints"
```

JSON output:

```bash
./venv/bin/python claude_task.py run --json --cwd /path/to/project -- "fix the failing test"
```

## When To Use Claude Code

Use Claude Code for:

- UI, webpage, mini-app, and interactive prototype generation
- visual polish where layout, CSS, and component decisions matter
- repo inspection
- multi-file code edits
- tests and build fixes
- project/file generation
- tasks where a dedicated code agent can work independently

Do not use it for ordinary home control, quick status checks, reminders, or
tasks already covered by a narrow local script.

## Routing Rule

Default to the narrowest capable executor:

- Main home agent: chat replies, reminders, HA device control, quick queries,
  small local script calls.
- Claude Code: coding, UI/web previews, project edits, test/build loops, or
  anything that benefits from a dedicated code agent.
- Remote workstation connector: tasks that must run on a specific computer.
- Remote workstation plus Claude Code: UI/code work that must happen on that
  specific computer and Claude Code is available there.

## Permissions

`claude_task.py` defaults to `bypassPermissions` because WeChat has no
interactive permission prompt. This is powerful. The main agent must still
follow `AGENTS.md` and ask before destructive actions or sensitive data moves.

Override with:

```bash
CLAUDE_TASK_PERMISSION_MODE=auto ./venv/bin/python claude_task.py run --cwd . -- "..."
```

## Cost And Timeout

Defaults:

- timeout: 900 seconds
- effort: high
- output cap: 30000 characters

Optional budget guard:

```bash
./venv/bin/python claude_task.py run --max-budget-usd 1 --cwd . -- "..."
```
