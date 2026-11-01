# Experiments

An experiment compares a baseline revision and a candidate revision on the same scenarios, worlds, seeds and limits.

**States**: draft → validated → preparing → baseline_running / candidate_running → evaluating → completed (or failed,
cancelled). States are events on the experiment; the decision is a separate object, so an experiment can complete with a
rejected candidate.

**Validation** checks that both revisions exist, the locked bundle is present, scenarios exist, and the policy denies network.

**Trials** run baseline and candidate alternately (order flips every trial) so drift on the machine affects both. Each
trial uses seed `experiment.seed + scenario.seed + trial`. The number of trials is at least `policy.min_trials`.

**Runs** record revision, scenario digest, world and snapshot digest, seed, trial, status (`completed`, `app_failed`,
`infra_failed`), metrics and artifact digests for the outcome, trace and log.

**Reproducing** an experiment: the decision references the evaluation, which references every run; each run references
the exact commit, scenario digest, world snapshot digest and seed. `crossfade evaluate <experiment>` re-runs the evaluator over
the stored runs and checks the decision reproduces. `crossfade compare <baseline> <candidate>` with the same refs re-executes it.

**Acceptance is not authorization.** `Decision.merge_authorized` is always false; merging and deploying are human decisions.
