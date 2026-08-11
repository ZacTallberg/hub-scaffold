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

This is a privileged local capability. Any page can attempt to navigate to a custom protocol, so
the signed grant, exact issuer match, and authoritative consume are all mandatory. The Hub's general
write token grants terminal board authority; keep its workstation file private to the current user.

## Prerequisites

- Windows PowerShell 5.1 or newer;
- Python available as `python` on the current user's `PATH`;
- HTTPS reachability to the issuing Hub (HTTP is accepted only for loopback);
- a local checkout containing `hub_core/launch_grant.py`;
- an operator-owned wrapper script/executable that actually starts the chosen worker tool.

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
The workstation's `-IssuerUrl` must match the grant's issuer exactly, including scheme, host, port,
path, and any `/hub` mount prefix.

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
  -IssuerUrl https://project.example.com/hub/api/launch-grant/consume `
  -TokenFile C:\path\to\hub-token.txt `
  -WorkerCommand C:\path\to\start-my-agent.ps1 `
  -Repo C:\path\to\the-project
```

Remove it with:

```powershell
powershell -ExecutionPolicy Bypass -File adapters\windows\register-worker-protocol.ps1 -Remove
```

The token value is never placed in the URL, page, registry, process arguments, or repository. Only
its file path is registered. A missing handler, missing token file, wrong issuer, expired/replayed
grant, changed task/count, or unreachable Hub fails closed before any worker process is created.

## Wrapper contract

The adapter starts the configured wrapper once per requested worker and supplies context through
environment variables, not command-line arguments:

| Variable | Meaning |
|---|---|
| `HUB_AGENT_ID` | Fresh worker id for this process |
| `HUB_TASK_ID` | Task selected in the Hub, or empty |
| `HUB_REPO` | Registered project checkout |
| `HUB_DIR` | That checkout's `PROJECT/.hub` path |
| `HUB_WORKER_PROMPT_FILE` | Temporary prompt/instructions file |

The wrapper should remain attached for the worker's lifetime and return its exit code. If it starts
a detached process and exits immediately, the adapter correctly closes its own window but can no
longer manage the detached child.

Before using the browser, verify the wrapper without launching a real worker:

```powershell
powershell -ExecutionPolicy Bypass -File adapters\windows\launch-worker.ps1 `
  -Count 1 -WorkerCommand C:\path\to\start-my-agent.ps1 `
  -Repo C:\path\to\the-project -DryRun
```

## Troubleshooting

| Symptom | Check |
|---|---|
| Hub shows no Launch Worker control | `HUB_WORKER_LAUNCH_ENABLED=True`, then restart the server and reload the page |
| Browser does nothing or asks how to open the link | Re-register the protocol for the current user and allow the browser's external-protocol prompt |
| “could not reach the Hub” / issuer unreachable | HTTPS/DNS/VPN, exact `-IssuerUrl`, proxy scheme settings, and whether the endpoint redirects |
| “missing/invalid X-Write-Token” | Token file contents differ from the server or the token was rotated |
| “grant issuer does not match” | Server and workstation issuer URLs differ textually; register again with the exact consume URL |
| “expired,” “already used,” or action/task/count mismatch | Reload/retry to obtain a fresh pre-armed grant; never reuse a copied URL |
| Window flashes and closes | Run the wrapper directly to inspect its error; confirm `python` and the worker command are on the user's PATH |
| Windows remain after workers finish | The wrapper is still running or waits for input; it must return when the worker is finished |

The Hub page never asks for a write token and has no unlock flow. If either appears, a stale frontend
or old deployment is being served; verify the live build SHA and clear the stale deployment/cache
rather than entering a token.
