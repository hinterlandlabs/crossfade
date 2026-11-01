# Plugins

`crossfade plugins` lists every plugin with its kind, capabilities, required permissions and health.

## In-process plugins

Python classes or modules registered in `crossfade.plugins.BUILTIN` with a `PluginInfo`:

```python
PluginInfo(name="world-runtime.http", version="1.0", kind="world-runtime", provides=["world.serve"],
           permissions=["bind_localhost"], health=lambda: (True, "ok"), description="...")
```

The core resolves capabilities (`resolve("execution.run")`) and skips plugins whose interface version doesn't match or
whose health check fails. Plugins receive only what they need; they never get the ledger, evaluator internals or secrets.

World plugins are the most common extension and live in a project's bundle (`crossfade/world.py`), so they are locked
and versioned with the rest of the evaluation.

## Out-of-process protocol (specified; not yet implemented)

For less-trusted or non-Python plugins: newline-delimited JSON-RPC 2.0 over stdin/stdout.

1. Crossfade starts the plugin with an empty environment plus the variables its manifest requests and the operator approves.
2. `initialize {interface_version, config}` → `{name, version, provides, permissions}`; mismatched versions are rejected.
3. Capability calls, e.g. `discovery.inspect {root}` → `{manifest}`, `world.handle {method, path, body}` → `{status, body}`.
4. `shutdown`.

Plugins written by agents stay disabled until a person approves them after their contract tests pass.
