# service-runner — what your host must supply

## Runtime packages (only the runtime you declare is imported, and only to RUN)

- `waitress` -- the WSGI server for `"runtime": "wsgi"`.
- `uvicorn` -- the ASGI server for `"runtime": "asgi"`.

## Optional Windows service host adapter (`--svc-run` only)

- `servicemanager` -- pywin32.
- `win32event` -- pywin32.
- `win32service` -- pywin32.
- `win32serviceutil` -- pywin32.

## Files

- `deploy/services.json` -- the manifest (or pass `--manifest`).
