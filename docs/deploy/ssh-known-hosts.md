# SSH known_hosts bootstrap for ENM deploys

Deploy and rollback scripts verify SSH host keys by default:

- `SSH_STRICT_HOST_KEY_CHECKING=yes`
- `SSH_KNOWN_HOSTS_FILE=$HOME/.ssh/known_hosts`

Before the first Jenkins or manual deploy, bootstrap the target host key from a trusted network path and review the fingerprint out-of-band:

```bash
mkdir -p ~/.ssh
ssh-keyscan -p "$ENM_PORT" "$ENM_HOST" >> ~/.ssh/known_hosts
ssh-keygen -l -f ~/.ssh/known_hosts
```

Use `SSH_STRICT_HOST_KEY_CHECKING=accept-new` only for an explicitly approved first-use dev bootstrap. Do not set it to `no` in CI or production deploy jobs.
