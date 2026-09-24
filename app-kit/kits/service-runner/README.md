# service-runner — run, supervise and verify from one strict manifest

```
python service_runner.py --manifest deploy/services.json --validate-manifest
python service_runner.py --manifest deploy/services.json --console budget-app
python service_runner.py --manifest deploy/services.json --verify budget-app
```

`services.json` is strict: unknown or missing fields are refused, duplicate JSON keys are refused,
secret-like keys may not ride in `env`, and ports must fall inside the bands the manifest declares
(`"port_bands": [[8000, 8099]]`). Declaring the bands matters: a validator hard-coding ONE band
refuses every app that kept a port from before the band existed, and each then patches its own copy
of the runner by hand.

**A requested stop is not a crash.** The supervisor watches the server thread and raises when it
dies — but a stop is exactly what makes it die. Every stop path raises the `stopping` flag FIRST,
then closes the server; the watchdog treats a dead worker as a fault only when no stop was
requested. Raise the flag after closing and every deploy's own restart files a phantom
"server stopped unexpectedly".

**`--verify` reads the posture from the manifest** (`"gate": "required"` or `"public"`): health
must answer 200, and a gated app's anonymous root must redirect or refuse — `AUTH POSTURE FAILED`
otherwise. Run it as the last step of a deploy.

`--svc-run` is an optional adapter for a Windows service host (pywin32); the same supervision rule
applies there.
