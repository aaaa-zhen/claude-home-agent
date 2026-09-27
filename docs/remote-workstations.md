# Remote Workstations

Remote workstations are computers that this Mac-based home agent can use as
execution endpoints.

## Important Boundary

A normal web page cannot give the agent shell access to the computer that opened
it. Browsers cannot run arbitrary local commands, read arbitrary local files, or
start SSH by themselves.

For real work on a company computer, one of these must be true:

- The company computer runs the downloadable connector script from the connect
  page. This is the default mode.
- The company computer already exposes SSH on a network address reachable by
  this Mac. This is the advanced fallback.
- The company computer is reachable through an approved VPN or private network.

The default connector mode uses outbound HTTPS polling, so it does not require
opening an inbound SSH port on the company computer.

## Connect Page

Generate the private connect URL:

```bash
./venv/bin/python remote_hosts.py connect-url
```

Open that URL on the remote computer. The page shows a short tutorial:

1. Download `home-agent-connector.py`.
2. Run it in a terminal with an alias, such as `office-mac`.
3. Leave that terminal open.
4. Click the status link on the page to confirm that the computer is online.

When the script starts, it registers itself in `runtime/remote-hosts.json`,
sends a notification, then polls `api-server.py` for jobs. When the user asks the
WeChat agent to use that alias, `remote_hosts.py run <alias> -- <command>`
queues a job and waits for the connector to execute it.

Direct SSH registration still exists under the advanced section of the page.

## CLI Usage

List hosts:

```bash
./venv/bin/python remote_hosts.py list --status
```

Run a command:

```bash
./venv/bin/python remote_hosts.py run office-mac -- pwd
```

For connector hosts, this queues a command and waits for the running connector
script to pick it up. For SSH hosts, it runs through SSH directly.

Run with JSON output:

```bash
./venv/bin/python remote_hosts.py run office-mac --json -- git status --short
```

Remove a host:

```bash
./venv/bin/python remote_hosts.py forget office-mac
```

## WeChat Agent Usage

When the user says something like "用公司电脑..." or names a remote alias, the
agent should:

1. Identify the target alias.
2. Run `remote_hosts.py list --status` or `remote_hosts.py test <alias>` when
   freshness matters.
3. Use `remote_hosts.py run <alias> -- <command>` for the requested action.
4. Reply with the result briefly.

Do not reveal stored passwords, connector tokens, or raw connection profiles.

## Safety

Only register computers the user is authorized to access. For company devices
and company data, obey company policy. Ask for confirmation before destructive
actions, data deletion, credential changes, or exfiltrating private/company
files out of the remote computer.

## Next Mode

If the polling connector needs richer capabilities, add file upload/download and
screen/browser helpers next. Reverse SSH can still be added later for low-latency
interactive terminal sessions.
