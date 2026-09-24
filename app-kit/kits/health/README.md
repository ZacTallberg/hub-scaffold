# health — readiness a deploy gate can trust

`health.py` provides `health_live` and `health_ready`. Wire both and point the deploy gate at
READY (liveness passes with the database down):

```python
from health import health_live, health_ready
urlpatterns += [path("health/live/", health_live), path("health/ready/", health_ready)]
```

The readiness payload is the contract in `patterns/deploy-contract.md` → "Gate hygiene":
`checks` is a list of `{name, status, detail}`, `"ok"` is the only passing status (a mode such as
"forwarder unarmed" is `ok` with the mode in `detail`), an empty `checks` fails, and the database
check touches a real table and refuses pending migrations. Append your own checks to `CHECKS`.
A 503 here during warm-up is recorded by the error kit as a warning, not a fault.
