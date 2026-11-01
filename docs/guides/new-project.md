# Adding Crossfade to a project

1. `crossfade init` at the repository root. It registers the project, records a discovery manifest (every fact labelled
   observed, static inference, declared or assumed) and scaffolds `crossfade/` if it doesn't exist.
2. `crossfade/project.yaml`: the command that starts your app (it receives `PORT`, `WORLD_URL`, `APP_DATA_DIR`) and its
   health path. Python commands starting with `python` run under OpenTelemetry automatically; other languages can export
   OTLP/HTTP JSON to `$WORLD_URL/v1/traces`.
3. `crossfade/world.py`: model the dependencies your app calls. Start with the routes your scenarios need; unsupported routes
   return 501 and are counted as coverage gaps. Fill in `fidelity` honestly.
4. `crossfade/checks.py`: gate functions for behavior that must never break. Write them from the specification, not from
   whatever the current code does.
5. `crossfade/scenarios/*.yaml`: steps (`http`, `conversation`, `clock`) and expectations. Mark some `holdout: true`.
   `crossfade scenario generate <run>` drafts a scenario from a recorded run; review every expectation before keeping it.
6. `crossfade/policy.yaml`: gates, objectives (primary and guard), confidence, trials, protected paths, limits.
7. `crossfade lock`, then `crossfade baseline main`, then compare candidates.

Point your app's dependency base URLs at `WORLD_URL` (for example via an environment variable) so experiments never touch
production.
