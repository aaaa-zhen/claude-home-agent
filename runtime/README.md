# Runtime State

This directory is for private runtime state that should not be committed.

Expected files include:

- `remote-hosts.json` - SSH profiles for remote workstations.
- `remote-connect-token` - private token for the remote workstation connect page.
- queued connector jobs and connector tokens inside `remote-hosts.json`.

Keep this directory in private encrypted backups if remote workstation access
should survive migration.
