"""The crossfade command line.

Exit codes: 0 success; 1 usage or validation error; 2 experiment completed with a rejected or inconclusive decision
(useful in CI); 3 integrity failure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from . import discovery, experiments, export, findings, revisions, telemetry
from .models import Decision, Evaluation, Experiment, Finding, Hypothesis, Outcome, Revision, Run, new_id
from .project import BUNDLE_DIR, STATE_DIR, Workspace, WorkspaceError
from .runner import load_outcome, load_spans, run_scenario

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Crossfade: run software under controlled conditions and verify whether changes improve it.")
scenario_app = typer.Typer(help="Scenarios in the locked evaluation bundle.")
world_app = typer.Typer(help="World plugins: stateful stand-ins for dependencies.")
exp_app = typer.Typer(help="Experiments: baseline vs candidate under controlled conditions.")
traces_app = typer.Typer(help="Traces captured from runs, or imported.")
app.add_typer(scenario_app, name="scenario")
app.add_typer(world_app, name="world")
app.add_typer(exp_app, name="experiment")
app.add_typer(traces_app, name="traces")

JSON = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]


def out(data: Any, as_json: bool, human: str = "") -> None:
    if as_json:
        typer.echo(json.dumps(data, indent=2, default=str))
    else:
        typer.echo(human or json.dumps(data, indent=2, default=str))


def ws() -> Workspace:
    try:
        return Workspace.open(".")
    except WorkspaceError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from e


def fail(msg: str, code: int = 1) -> NoReturn:
    typer.secho(msg, fg="red", err=True)
    raise typer.Exit(code)


TEMPLATE_PROJECT = """name: {name}
app:
  start: {start}
  health: /health
world:
  plugin: world.py:AppWorld
  fixture: default
"""
TEMPLATE_POLICY = """version: "1"
protected_paths: ["crossfade/", ".crossfade/"]
gates: []           # names of functions in checks.py that must pass
objectives:
  - {metric: latency_ms, direction: lower, min_improvement: 0.10, role: primary}
confidence: 0.9
min_trials: 5
max_cv: 0.35
timeout_seconds: 60
"""
TEMPLATE_WORLD = '''"""World plugin: stateful stand-ins for the dependencies this app calls. Edit to match reality."""
from crossfade.models import Fidelity
from crossfade.worlds.base import Request, Response, World, route


class AppWorld(World):
    name = "app-world"
    version = "0.1"
    fidelity = Fidelity(implements=[], unsupported=["everything not listed in implements"])

    def fixtures(self):
        return {"default": lambda w: w.state.setdefault("items", {})}

    @route("GET", "/items/{item_id}")
    def get_item(self, req: Request) -> Response:
        item = self.table("items").get(req.params["item_id"])
        return Response(200, item) if item else Response(404, {"error": "not found"})
'''
TEMPLATE_CHECKS = '''"""Gate functions. Each receives a VersionView and returns (passed, detail). Locked with the bundle."""


def no_server_errors(v):
    errs = [r for run, o in v.completed() for r in o["responses"] if r.get("status", 200) >= 500]
    return not errs, f"{len(errs)} responses were 5xx"
'''
TEMPLATE_SCENARIO = """name: smoke
description: The app starts and answers its health check.
steps:
  - {kind: http, method: GET, path: /health}
expect:
  - {target: "responses[0].status", op: eq, value: 200}
"""


@app.command()
def init(name: Annotated[str | None, typer.Option(help="Project name (defaults to the directory name).")] = None,
         start: Annotated[str, typer.Option(help="Command that starts the app.")] = "python app.py", as_json: JSON = False) -> None:
    """Register this git repository with Crossfade, inspect it, and scaffold the evaluation bundle if missing."""
    root = Path(".").resolve()
    if not (root / ".git").exists():
        fail("crossfade init must run at the root of a git repository")
    bundle = root / BUNDLE_DIR
    created = []
    if not (bundle / "project.yaml").exists():
        (bundle / "scenarios").mkdir(parents=True, exist_ok=True)
        for fname, content in (("project.yaml", TEMPLATE_PROJECT.format(name=name or root.name, start=start)), ("policy.yaml", TEMPLATE_POLICY),
                               ("world.py", TEMPLATE_WORLD), ("checks.py", TEMPLATE_CHECKS), ("scenarios/smoke.yaml", TEMPLATE_SCENARIO)):
            if not (bundle / fname).exists():
                (bundle / fname).write_text(content)
                created.append(f"{BUNDLE_DIR}/{fname}")
    gi = root / ".gitignore"
    lines = gi.read_text().splitlines() if gi.exists() else []
    if f"{STATE_DIR}/" not in lines:
        gi.write_text("\n".join([*lines, f"{STATE_DIR}/"]) + "\n")
    w = Workspace(root)
    import yaml

    proj = w.register(name or yaml.safe_load((bundle / "project.yaml").read_text()).get("name") or root.name)
    manifest = discovery.inspect(root)
    digest = w.artifacts.put_json(manifest)
    w.ledger.append("project.inspected", actor="crossfade.discovery", project_id=proj.id, subject=proj.id, manifest=digest)
    out({"project": proj.model_dump(), "created": created, "manifest": digest, "summary": manifest["summary"]}, as_json,
        f"Registered {proj.name} ({proj.id}).\n" + (f"Created {', '.join(created)}.\n" if created else "")
        + f"Manifest {digest[:19]}: {json.dumps(manifest['summary'])}\nNext: review {BUNDLE_DIR}/, then `crossfade lock`.")


@app.command()
def inspect(as_json: JSON = False) -> None:
    """Inspect the repository and print an evidence-labelled manifest."""
    m = discovery.inspect(Path(".").resolve())
    if as_json:
        return out(m, True)
    typer.echo(json.dumps(m["summary"], indent=2))
    for f in m["facts"]:
        typer.echo(f"  {f['key']:22} {str(f['value'])[:60]:60} [{f['evidence']}, {f['confidence']:.1f}]  {f['source'][:50]}")


@app.command()
def lock(as_json: JSON = False) -> None:
    """Snapshot the evaluation bundle (crossfade/) into the artifact store. Experiments use the locked copy."""
    w = ws()
    try:
        digest = w.lock()
    except Exception as e:  # noqa: BLE001
        fail(f"bundle is invalid: {e}")
    b = w.bundle(digest)
    out({"digest": digest, "scenarios": sorted(b.scenarios), "gates": b.policy.gates}, as_json,
        f"Locked {digest}\n  scenarios: {', '.join(sorted(b.scenarios))}\n  gates: {', '.join(b.policy.gates) or '(none)'}")


@app.command("revision")
def revision(ref: str, label: Annotated[str, typer.Option()] = "", dirty: Annotated[bool, typer.Option(help="Capture uncommitted changes.")] = False,
             as_json: JSON = False) -> None:
    """Register a git ref (or the dirty working tree) as an immutable revision."""
    w = ws()
    r = revisions.resolve(w, ref, label=label, capture_dirty=dirty)
    out(r.model_dump(), as_json, f"{r.id}  {r.commit[:12]}  {r.label}{'  (dirty capture)' if r.dirty else ''}")


@app.command()
def observe(ref: Annotated[str, typer.Argument(help="Git ref to run.")] = "HEAD", scenario: Annotated[list[str] | None, typer.Option()] = None,
            as_json: JSON = False) -> None:
    """Run scenarios once against a revision, capture traces, and record findings."""
    w = ws()
    b = w.bundle()
    rev = revisions.resolve(w, ref)
    runs = []
    for name in scenario or sorted(b.scenarios):
        res = run_scenario(w, b, rev, b.scenarios[name])
        runs.append(res.run)
        cov = telemetry.coverage(res.spans)
        if not as_json:
            typer.echo(f"{res.run.id}  {name:24} {res.run.status.value:12} {res.run.metrics.get('latency_ms', 0):8.1f} ms  "
                       f"{int(res.run.metrics.get('dependency_calls', 0))} dependency calls  {cov['app_spans']} app spans")
            if res.run.error:
                typer.echo(f"    error: {res.run.error}")
    found = findings.record(w, [r for r in runs if not b.scenarios[r.scenario].holdout])  # holdout details never reach agents
    if as_json:
        return out({"runs": [r.model_dump() for r in runs], "findings": [f.model_dump() for f in found]}, True)
    for f in found:
        typer.echo(f"finding {f.id}: {f.description}")


@app.command()
def baseline(ref: Annotated[str, typer.Argument()] = "HEAD", trials: Annotated[int, typer.Option()] = 3, as_json: JSON = False) -> None:
    """Establish reproducible baseline measurements for a revision."""
    w = ws()
    b = w.bundle()
    rev = revisions.resolve(w, ref)
    runs: list[Run] = []
    for t in range(trials):
        for name in sorted(b.scenarios):
            runs.append(run_scenario(w, b, rev, b.scenarios[name], trial=t).run)
    import statistics

    summary: dict[str, Any] = {}
    for metric in sorted({k for r in runs for k in r.metrics}):
        vals = [r.metrics[metric] for r in runs if metric in r.metrics]
        summary[metric] = {"mean": statistics.fmean(vals), "stdev": statistics.pstdev(vals), "n": len(vals)}
    w.ledger.append("baseline.established", actor="human", project_id=w.project.id, subject=rev.id, bundle=b.digest,
                    runs=[r.id for r in runs], summary=summary)
    failed = [r for r in runs if r.status.value != "completed"]
    out({"revision": rev.id, "runs": [r.id for r in runs], "summary": summary, "failed": len(failed)}, as_json,
        f"Baseline {rev.label} ({rev.commit[:12]}): {len(runs)} runs, {len(failed)} not completed\n"
        + "\n".join(f"  {k:22} mean {v['mean']:10.3f}  stdev {v['stdev']:8.3f}" for k, v in summary.items()))


def _print_result(w: Workspace, exp: Experiment, d: Decision, ev: Evaluation) -> None:
    color = {Outcome.ACCEPTED: "green", Outcome.REJECTED: "red"}.get(d.outcome, "yellow")
    typer.echo("")
    typer.secho(f"{d.outcome.value.upper()}  {exp.name}  ({exp.id})", fg=color, bold=True)
    for r in d.reasons:
        typer.echo(f"  - {r}")
    typer.echo("Checks:")
    for c in ev.checks:
        mark = "pass" if c.passed else ("FAIL" if c.passed is False else "    ")
        typer.echo(f"  {mark}  {c.name}" + (f"  {c.detail[:160]}" if c.detail else ""))
    typer.echo(f"Decision {d.id} by {d.authority}. Acceptance is not merge authorization.")


@exp_app.command("create")
def exp_create(baseline: Annotated[str, typer.Option()], candidate: Annotated[str, typer.Option()], name: Annotated[str, typer.Option()] = "",
               hypothesis: Annotated[str | None, typer.Option()] = None, trials: Annotated[int | None, typer.Option()] = None,
               scenario: Annotated[list[str] | None, typer.Option()] = None, as_json: JSON = False) -> None:
    """Create an experiment comparing two revisions."""
    w = ws()
    exp = experiments.create(w, name or f"{baseline} vs {candidate}", baseline, candidate, scenario, trials, hypothesis)
    problems = experiments.validate(w, exp)
    out({"experiment": exp.model_dump(), "problems": problems}, as_json, f"{exp.id}  {exp.name}" + ("" if not problems else "\n  problems: " + "; ".join(problems)))


@exp_app.command("run")
def exp_run(experiment_id: str, as_json: JSON = False) -> None:
    """Run an experiment: baseline and candidate trials, independent evaluation, decision."""
    w = ws()
    try:
        exp = w.ledger.get(experiment_id, Experiment)
    except KeyError:
        fail(f"unknown experiment {experiment_id}")
    res = experiments.run(w, exp, progress=(lambda s: None) if as_json else typer.echo)
    if as_json:
        out({"decision": res.decision.model_dump(), "evaluation": res.evaluation.model_dump()}, True)
    else:
        _print_result(w, exp, res.decision, res.evaluation)
    if res.decision.outcome != Outcome.ACCEPTED:
        raise typer.Exit(2)


@exp_app.command("show")
def exp_show(experiment_id: str, as_json: JSON = False) -> None:
    """Show an experiment's hypothesis, revisions, runs, evaluation and decision."""
    w = ws()
    exp = w.ledger.get(experiment_id, Experiment)
    d, ev = experiments.decision_for(w, exp.id), experiments.evaluation_for(w, exp.id)
    data = {"experiment": exp.model_dump(), "state": experiments.state(w, exp.id).value, "decision": d.model_dump() if d else None,
            "evaluation": ev.model_dump() if ev else None, "events": [e.model_dump() for e in w.ledger.events(subject=exp.id)]}
    if as_json or not (d and ev):
        return out(data, True)
    _print_result(w, exp, d, ev)


@app.command()
def compare(baseline: str, candidate: str, name: Annotated[str, typer.Option()] = "", trials: Annotated[int | None, typer.Option()] = None,
            hypothesis: Annotated[str | None, typer.Option()] = None, as_json: JSON = False) -> None:
    """Create and run an experiment in one step."""
    w = ws()
    exp = experiments.create(w, name or f"{baseline} vs {candidate}", baseline, candidate, None, trials, hypothesis)
    res = experiments.run(w, exp, progress=(lambda s: None) if as_json else typer.echo)
    if as_json:
        out({"experiment": exp.id, "decision": res.decision.model_dump(), "evaluation": res.evaluation.model_dump()}, True)
    else:
        _print_result(w, exp, res.decision, res.evaluation)
    if res.decision.outcome != Outcome.ACCEPTED:
        raise typer.Exit(2)


@app.command()
def evaluate(experiment_id: str, as_json: JSON = False) -> None:
    """Re-run the evaluator over an experiment's recorded runs and check it reproduces the recorded decision."""
    from .evaluation import VersionView, judge

    w = ws()
    exp = w.ledger.get(experiment_id, Experiment)
    d, ev = experiments.decision_for(w, exp.id), experiments.evaluation_for(w, exp.id)
    if d is None or ev is None:
        fail("experiment has no decision yet")
        return
    b = w.bundle(exp.policy_digest)
    views = []
    for label, ids in (("baseline", ev.baseline_runs), ("candidate", ev.candidate_runs)):
        runs = [w.ledger.get(i, Run) for i in ids]
        views.append(VersionView(label, runs, {r.id: load_outcome(w, r) for r in runs}, b.scenarios))
    base, cand = w.ledger.get(exp.baseline_revision, Revision), w.ledger.get(exp.candidate_revision, Revision)
    touched = revisions.changed_paths(w, base, cand)
    j = judge(b, b.policy, views[0], views[1], touched, revisions.protected_violations(touched, b.policy.protected_paths),
              revisions.protected_violations(touched, b.policy.require_review_for) if b.policy.require_review_for else [])
    same = j.outcome == d.outcome
    out({"recorded": d.outcome.value, "recomputed": j.outcome.value, "reproduced": same}, as_json,
        f"recorded {d.outcome.value}, recomputed {j.outcome.value}: {'reproduced' if same else 'MISMATCH'}")
    if not same:
        raise typer.Exit(3)


@app.command()
def history(as_json: JSON = False) -> None:
    """Every experiment, its decision and why."""
    w = ws()
    h = experiments.history(w)
    if as_json:
        return out(h, True)
    if not h:
        typer.echo("no experiments yet")
    for e in h:
        typer.echo(f"{e['id']}  {str(e['outcome'] or e['state']).upper():13} {e['name']}  ({e['baseline']} -> {e['candidate']})")
        for r in e["reasons"]:
            typer.echo(f"      {r[:150]}")


@app.command("findings")
def findings_cmd(as_json: JSON = False) -> None:
    """Findings recorded from traces."""
    w = ws()
    fs = w.ledger.list("finding", Finding, limit=200)
    out([f.model_dump() for f in fs], as_json, "\n".join(f"{f.id}  [{f.severity}] {f.description}" for f in fs) or "no findings yet")


@app.command()
def hypothesis(mechanism: str, finding: Annotated[list[str] | None, typer.Option()] = None, impact: Annotated[str, typer.Option()] = "",
               risks: Annotated[str, typer.Option()] = "", author: Annotated[str, typer.Option()] = "human", as_json: JSON = False) -> None:
    """Record a hypothesis: a proposed mechanism and its expected impact."""
    w = ws()
    h = Hypothesis(id=new_id("hyp"), project_id=w.project.id, findings=finding or [], mechanism=mechanism, expected_impact=impact, risks=risks, author=author)
    w.ledger.put(h, actor=author)
    out(h.model_dump(), as_json, f"{h.id}  {mechanism}")


@app.command()
def propose(agent: Annotated[str, typer.Option(help="Command that runs a coding agent in the candidate worktree.")],
            base: Annotated[str, typer.Option()] = "HEAD", hypothesis: Annotated[str | None, typer.Option()] = None,
            name: Annotated[str | None, typer.Option()] = None,
            pass_env: Annotated[list[str] | None, typer.Option(help="Environment variable the agent may see (repeatable).")] = None,
            allow_write: Annotated[list[Path] | None, typer.Option(help="Extra directory the agent may write, e.g. its cache (repeatable).")] = None,
            require_sandbox: Annotated[bool, typer.Option(help="Refuse to run without an OS sandbox.")] = False, as_json: JSON = False) -> None:
    """Let a coding agent create a candidate from a staged copy of the base revision, without the evaluation bundle."""
    from .agents import propose as _propose

    w = ws()
    rev, cand = _propose(w, base, agent, hypothesis, name, pass_env=pass_env, require_sandbox=require_sandbox, allow_write=allow_write)
    out({"revision": rev.model_dump(), "candidate": cand.model_dump()}, as_json,
        f"candidate {cand.id} at {rev.commit[:12]} ({rev.label}), {len(cand.touched_paths)} files changed: {', '.join(cand.touched_paths)}\n"
        f"Next: crossfade compare {base} {rev.commit[:12]}")


@app.command()
def optimize(agent: Annotated[str, typer.Option(help="Command that runs a coding agent; it gets a staged copy of the current best revision.")],
             base: Annotated[str, typer.Option()] = "HEAD", rounds: Annotated[int, typer.Option(help="Most rounds to run.")] = 5,
             patience: Annotated[int, typer.Option(help="Stop after this many rounds in a row without an accepted change.")] = 3,
             max_minutes: Annotated[float, typer.Option(help="Time budget for the whole loop.")] = 60,
             trials: Annotated[int | None, typer.Option()] = None,
             pass_env: Annotated[list[str] | None, typer.Option(help="Environment variable the agent may see (repeatable).")] = None,
             allow_write: Annotated[list[Path] | None, typer.Option(help="Extra directory the agent may write (repeatable).")] = None,
             require_sandbox: Annotated[bool, typer.Option(help="Refuse to run without an OS sandbox.")] = False,
             into: Annotated[str | None, typer.Option(help="Fast-forward this branch to the best revision when the loop keeps something.")] = None,
             as_json: JSON = False) -> None:
    """Trace the app, let an agent change it, keep what tests better, and repeat from the kept version."""
    from .optimize import optimize as _optimize

    w = ws()
    res = _optimize(w, base, agent, rounds, patience, max_minutes, trials, pass_env, allow_write, require_sandbox, into,
                    progress=(lambda s: None) if as_json else typer.echo)
    data = {"id": res.id, "start": res.start.commit, "best": res.best.commit, "kept": res.kept, "stopped": res.stopped,
            "applied": res.applied, "rounds": [vars(r) for r in res.rounds]}
    end = f"; {res.applied}." if res.applied else ". Review it before merging."
    out(data, as_json, f"\n{res.id}: {res.kept} of {len(res.rounds)} rounds kept; stopped because {res.stopped}.\n"
        f"Best revision {res.best.commit[:12]}" + ("" if res.kept else " (unchanged)") + end)
    if not res.kept:
        raise typer.Exit(2)


@app.command()
def verify(as_json: JSON = False) -> None:
    """Verify the ledger hash chain and every artifact's digest."""
    w = ws()
    n_ev, bad_ev = w.ledger.verify_chain()
    n_art, bad_art = w.artifacts.verify_all()
    ok = not bad_ev and not bad_art
    out({"events": n_ev, "bad_events": bad_ev, "artifacts": n_art, "bad_artifacts": bad_art, "ok": ok}, as_json,
        f"{n_ev} events, chain {'intact' if not bad_ev else 'BROKEN at ' + str(bad_ev[:5])}; {n_art} artifacts, "
        + ("all digests match" if not bad_art else f"{len(bad_art)} CORRUPT"))
    if not ok:
        raise typer.Exit(3)


@app.command()
def plugins(as_json: JSON = False) -> None:
    """Installed plugins, what they provide, what they need, and whether they're healthy."""
    from .plugins import status

    s = status()
    out(s, as_json, "\n".join(f"{'ok ' if p['healthy'] else '-- '} {p['name']:32} {p['kind']:15} {p['status']}" for p in s))


@scenario_app.command("list")
def scenario_list(as_json: JSON = False) -> None:
    w = ws()
    b = w.bundle()
    data = [{"name": s.name, "steps": len(s.steps), "expectations": len(s.expect), "holdout": s.holdout, "description": s.description} for s in b.scenarios.values()]
    out(data, as_json, "\n".join(f"{d['name']:24} {d['steps']:3} steps {d['expectations']:3} expectations{'  (holdout)' if d['holdout'] else ''}  {d['description']}" for d in data))


@scenario_app.command("generate")
def scenario_generate(run_id: str, name: Annotated[str, typer.Option()] = "", as_json: JSON = False) -> None:
    """Draft a scenario from a recorded run. Observed outputs become *draft* expectations to review, not truth."""
    import yaml

    w = ws()
    r = w.ledger.get(run_id, Run)
    b = w.bundle()
    src = b.scenarios[r.scenario]
    o = load_outcome(w, r)
    draft = {"name": name or f"{src.name}-from-{run_id[-6:]}", "description": f"Drafted from run {run_id}. Review every expectation before locking.",
             "seed": r.seed, "steps": [s.model_dump(exclude_defaults=True) for s in src.steps],
             "expect": [{"target": f"responses[{x['step']}].status", "op": "eq", "value": x["status"], "description": "observed, unreviewed"}
                        for x in o.get("responses", []) if "status" in x]}
    path = Path(BUNDLE_DIR) / "scenarios" / f"{draft['name']}.draft.yaml"
    path.write_text(yaml.safe_dump(draft, sort_keys=False))
    out({"path": str(path), "scenario": draft}, as_json, f"wrote {path}. Rename to .yaml after review, then `crossfade lock`.")


@world_app.command("list")
def world_list(as_json: JSON = False) -> None:
    w = ws()
    b = w.bundle()
    cls = b.world_cls
    world = cls(fixture=b.config.world.fixture)
    data = {"name": cls.name, "version": cls.version, "fixtures": sorted(world.fixtures()), "routes": [p for _, _, p, _, _ in world._routes],
            "entities": {k: len(v) if isinstance(v, dict) else 1 for k, v in world.state.items()}, "fidelity": cls.fidelity.model_dump()}
    out(data, as_json, json.dumps(data, indent=2))


@world_app.command("serve")
def world_serve(fixture: Annotated[str, typer.Option()] = "default", seed: Annotated[int, typer.Option()] = 0, port: Annotated[int, typer.Option()] = 8710) -> None:
    """Serve the locked world locally for development. Control endpoints need the printed token."""
    import time

    from .worlds.base import WorldServer

    w = ws()
    b = w.bundle()
    srv = WorldServer(b.world_cls(fixture=fixture, seed=seed), port=port)
    (w.state / "world-serve.json").write_text(json.dumps({"url": srv.url, "token": srv.token}))
    with srv:
        typer.echo(f"{b.world_cls.name} on {srv.url}  (control token in {STATE_DIR}/world-serve.json). Ctrl-C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


@world_app.command("reset")
def world_reset() -> None:
    """Reset a world started with `crossfade world serve` to its fixture state."""
    import httpx

    w = ws()
    info = json.loads((w.state / "world-serve.json").read_text())
    r = httpx.post(info["url"] + "/_world/reset", headers={"X-World-Token": info["token"]}, trust_env=False)
    typer.echo("reset" if r.status_code == 200 else f"failed: {r.status_code}")


@traces_app.command("show")
def traces_show(run_id: str, as_json: JSON = False) -> None:
    """Show a run's spans as a tree, with timing and status. Dependency calls Crossfade measured sit under the span that made them."""
    w = ws()
    r = w.ledger.get(run_id, Run)
    spans = load_spans(w, r)
    if as_json:
        return out({"tree": telemetry.tree(spans), "coverage": telemetry.coverage(spans)}, True)

    def walk(nodes: list[dict[str, Any]], depth: int = 0) -> None:
        for n in nodes:
            s = n["span"]
            mark = "  (measured)" if s["source"] == "world" else "  (orphan)" if n.get("orphan") else ""
            typer.echo(f"{'  ' * depth}{s['name'][:60]:60} {(s['end'] - s['start']) * 1000:8.2f} ms  {s['status']}{mark}")
            walk(n["children"], depth + 1)

    walk(telemetry.tree(spans))
    cov = telemetry.coverage(spans)
    typer.echo(f"-- {cov['dependency_spans']} dependency calls measured by Crossfade, {cov['dependency_spans_linked']} traced to the code that made them; "
               f"{cov['model_spans']} model calls ({cov['model_spans_with_tokens']} with token counts); {cov['orphans']} orphan spans")


@traces_app.command("diff")
def traces_diff(
    a: Annotated[str | None, typer.Argument(help="Run id of the original (or use --experiment).")] = None,
    b: Annotated[str | None, typer.Argument(help="Run id of the change.")] = None,
    experiment: Annotated[str | None, typer.Option("--experiment", "-e", help="Compare every completed run of both sides.")] = None,
    scenario: Annotated[str | None, typer.Option(help="Only this scenario (with --experiment).")] = None,
    limit: Annotated[int, typer.Option(help="Rows to print.")] = 20,
    as_json: JSON = False,
) -> None:
    """What the change does differently: call paths that appear, disappear, run more or fewer times, or take longer."""
    w = ws()
    try:
        if experiment:
            d = export.behaviour_diff(w, experiment, scenario)
        elif a and b:
            d = telemetry.diff([load_spans(w, w.ledger.get(a, Run))], [load_spans(w, w.ledger.get(b, Run))])
        else:
            fail("give two run ids, or --experiment")
    except KeyError as e:
        fail(f"not found: {e.args[0]}")
    if as_json:
        return out(d, True)
    t = d["totals"]
    typer.echo(f"original: {d['runs_a']} run(s), change: {d['runs_b']} run(s); per run: spans {t['count_a']:g} -> {t['count_b']:g}, "
               f"errors {t['errors_a']:g} -> {t['errors_b']:g}, input tokens {t['input_tokens_a']:g} -> {t['input_tokens_b']:g}")
    if not d["rows"]:
        typer.echo("no differences in call paths")
    for r in d["rows"][:limit]:
        typer.echo(f"{r['change']:8} {r['count_a']:6g} -> {r['count_b']:<6g} {r['ms_a']:9.1f} -> {r['ms_b']:<9.1f} ms  {r['path'][-110:]}")
    if len(d["rows"]) > limit:
        typer.echo(f"... {len(d['rows']) - limit} more (--limit, or --json)")


@traces_app.command("export")
def traces_export(
    run_ids: Annotated[list[str] | None, typer.Argument(help="Runs to export (or use --experiment).")] = None,
    experiment: Annotated[str | None, typer.Option("--experiment", "-e", help="Export every run of an experiment.")] = None,
    endpoint: Annotated[str | None, typer.Option("--otlp", envvar="OTEL_EXPORTER_OTLP_ENDPOINT",
                                                 help="OTLP/HTTP endpoint, e.g. http://localhost:4318. Headers come from OTEL_EXPORTER_OTLP_HEADERS.")] = None,
    to_file: Annotated[Path | None, typer.Option("--out", help="Write OTLP/JSON to a file instead of sending it.")] = None,
    include_holdouts: Annotated[bool, typer.Option("--include-holdouts", help="Also export holdout runs (left out by default).")] = False,
    as_json: JSON = False,
) -> None:
    """Send runs to any OpenTelemetry backend, tagged with their commit, scenario, experiment and decision."""
    import os

    w = ws()
    try:
        if experiment:
            _, base, cand = export.runs_by_side(w, experiment)
            runs = base + cand
        elif run_ids:
            runs = [w.ledger.get(i, Run) for i in run_ids]
        else:
            fail("give run ids, or --experiment")
    except KeyError as e:
        fail(f"not found: {e.args[0]}")
    if not endpoint and not to_file:
        fail("give --otlp URL (or set OTEL_EXPORTER_OTLP_ENDPOINT), or --out FILE")
    payload, skipped = export.otlp(w, runs, include_holdouts)
    n = sum(len(ss["spans"]) for rs in payload["resourceSpans"] for ss in rs["scopeSpans"])
    if to_file:
        to_file.write_text(json.dumps(payload))
        where = str(to_file)
    else:
        assert endpoint
        headers = dict(kv.split("=", 1) for kv in os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "").split(",") if "=" in kv)
        try:
            export.send(endpoint, payload, headers)
        except Exception as e:  # noqa: BLE001
            fail(f"export failed: {e}")
        where = endpoint
    w.ledger.append("trace.exported", actor="human", project_id=w.project.id, subject=experiment or None, runs=len(runs) - len(skipped),
                    spans=n, destination="file" if to_file else "otlp")
    out({"runs": len(runs) - len(skipped), "spans": n, "skipped": skipped, "destination": where}, as_json,
        f"exported {n} spans from {len(runs) - len(skipped)} run(s) to {where}" + (f"; left out {len(skipped)} (holdout or no trace)" if skipped else ""))


@traces_app.command("import")
def traces_import(path: Path, as_json: JSON = False) -> None:
    """Import an OTLP/JSON trace export as evidence (no run is attached)."""
    w = ws()
    spans = telemetry.from_otlp_json(json.loads(path.read_text()), source="import")
    digest = w.artifacts.put_bytes("\n".join(s.model_dump_json() for s in spans).encode())
    w.ledger.index_spans(f"import-{digest[7:19]}", spans)
    w.ledger.append("trace.ingested", actor="human", project_id=w.project.id, subject=digest, spans=len(spans), source=str(path))
    out({"artifact": digest, "spans": len(spans), "coverage": telemetry.coverage(spans)}, as_json, f"imported {len(spans)} spans as {digest}")


@app.command()
def serve(host: Annotated[str, typer.Option()] = "127.0.0.1", port: Annotated[int, typer.Option()] = 8700) -> None:
    """Start the local API and UI (localhost only by default)."""
    import uvicorn

    from .api import create_app

    if host not in ("127.0.0.1", "localhost", "::1"):
        typer.secho(f"warning: binding to {host} exposes the experiment ledger beyond this machine", fg="yellow", err=True)
    uvicorn.run(create_app(ws()), host=host, port=port, log_level="warning")


@app.command()
def mcp() -> None:
    """Run the MCP server on stdio for coding agents."""
    from .mcp_server import main

    main(ws())


@app.command()
def example(name: str, dest: Path) -> None:
    """Copy an example app into a new git repo, with a baseline commit and one branch per candidate."""
    from .examples import materialize

    try:
        branches = materialize(name, dest)
    except ValueError as e:
        fail(str(e))
    typer.echo(f"{name} -> {dest}\n  branches: {', '.join(branches)}\nNext: cd {dest} && crossfade init && crossfade lock")


def main() -> None:
    try:
        app()
    except WorkspaceError as e:
        typer.secho(str(e), fg="red", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
