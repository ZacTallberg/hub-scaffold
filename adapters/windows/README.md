# Optional Windows worker-launch adapter

This adapter makes the Hub's **Launch Worker** control open a local agent wrapper without turning
the browser into a general writer or an arbitrary process launcher.

The trust chain is intentionally narrow:

1. The public Hub page obtains a same-origin CSRF token and pre-arms the control with a signed,
   short-lived grant bound to `action + task + count + issuer + nonce`.
2. The actual click follows `hub-worker://` synchronously while the browser still has the user's
   activation. There is no popup and no asynchronous fetch inside the ready click.
3. The registered workstation handler compares the grant's issuer to its configured Hub, presents
   the write token from a local file over HTTPS, and atomically consumes the nonce there.
4. Only an accepted consume can start the operator-supplied worker wrapper. The child PowerShell
   process does not use `-NoExit`, so its window closes when the worker finishes.

## Server settings

Enable this only on a Hub whose users have installed the workstation adapter:

```python
HUB_WORKER_LAUNCH_ENABLED = True
HUB_WORKER_PROTOCOL = "hub-worker"
HUB_WORKER_LAUNCH_ISSUER_URL = "{{LIVE_URL}}/hub/api/launch-grant/consume"
HUB_WORKER_GRANT_TTL_S = 120
```

An explicit issuer URL is recommended in production. If omitted, the server derives it from the
request host; normal Django `ALLOWED_HOSTS` and proxy-scheme configuration must then be correct.

## Workstation registration

Create a local token file readable only by your user and a vendor-specific worker wrapper. The
wrapper takes no required arguments; it reads:

- `HUB_AGENT_ID`
- `HUB_TASK_ID`
- `HUB_REPO`
- `HUB_DIR`
- `HUB_WORKER_PROMPT_FILE`

Then register once (no administrator rights; this writes under HKCU):

```powershell
powershell -ExecutionPolicy Bypass -File adapters\windows\register-worker-protocol.ps1 `
  -TokenFile C:\path\to\hub-token.txt `
  -WorkerCommand C:\path\to\start-my-agent.ps1
```

Remove it with:

```powershell
powershell -ExecutionPolicy Bypass -File adapters\windows\register-worker-protocol.ps1 -Remove
```

The token value is never placed in the URL, page, registry, process arguments, or repository. Only
its file path is registered. A missing handler, missing token file, wrong issuer, expired/replayed
grant, changed task/count, or unreachable Hub fails closed before any worker process is created.
