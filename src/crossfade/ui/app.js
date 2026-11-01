// Crossfade local UI. Every number shown comes from the API, which reads the ledger and artifact store.
const view = document.getElementById('view');
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const api = async p => { const r = await fetch('/v1' + p); if (!r.ok) throw new Error(`${p}: ${r.status}`); return r.json(); };
const when = t => new Date(t * 1000).toLocaleString();
const badge = o => `<span class="badge ${esc(o || 'none')}">${esc(o || 'pending')}</span>`;
const num = (x, d = 1) => x === undefined || x === null ? '' : Number(x).toLocaleString(undefined, { maximumFractionDigits: d });

async function home() {
  const { items } = await api('/experiments?limit=500');
  view.innerHTML = `<h1>Experiments</h1><p class="muted">Each experiment runs a baseline and a candidate under the same conditions. The evaluator decides; acceptance is not merge authorization.</p>
  ${items.length ? `<table><thead><tr><th>Decision</th><th>Experiment</th><th>Baseline → candidate</th><th>Reason</th><th>Created</th></tr></thead><tbody>
  ${items.map(e => `<tr class="click" data-href="#/exp/${e.id}"><td>${badge(e.outcome || e.state)}</td><td><b>${esc(e.name)}</b><div class="mono muted">${e.id}</div></td>
  <td class="mono">${esc(e.baseline)} → ${esc(e.candidate)}</td><td class="small">${esc((e.reasons || [])[0] || '')}</td><td class="small muted">${when(e.created_at)}</td></tr>`).join('')}
  </tbody></table>` : '<p>No experiments yet. Run <code>crossfade compare &lt;baseline&gt; &lt;candidate&gt;</code>.</p>'}`;
}

function diffHtml(p) {
  return esc(p).split('\n').map(l => l.startsWith('+') && !l.startsWith('+++') ? `<span class="add">${l}</span>` : l.startsWith('-') && !l.startsWith('---') ? `<span class="del">${l}</span>` : l).join('\n');
}

async function experiment(id) {
  const d = await api('/experiments/' + id);
  const ev = d.evaluation, dec = d.decision;
  const gates = ev ? ev.checks.filter(c => c.kind === 'gate') : [];
  const objs = ev ? ev.checks.filter(c => c.kind === 'objective') : [];
  const runs = d.runs;
  const metrics = [...new Set(runs.flatMap(r => Object.keys(r.metrics)))].filter(m => !m.startsWith('calls:')).slice(0, 6);
  view.innerHTML = `<p class="small"><a href="#/">Experiments</a></p>
  <h1>${esc(d.experiment.name)} ${badge(dec ? dec.outcome : d.state)}</h1>
  <p class="mono muted">${d.experiment.id} · ${d.experiment.trials} trials · scenarios ${d.experiment.scenarios.map(esc).join(', ')} · policy ${esc(d.experiment.policy_digest.slice(0, 19))}</p>
  ${dec ? `<ul class="reasons">${dec.reasons.map(r => `<li>${esc(r)}</li>`).join('')}</ul><p class="small muted">Decided by ${esc(dec.authority)}. Merge authorized: ${dec.merge_authorized ? 'yes' : 'no (a separate human decision)'}.</p>` : `<p>State: ${esc(d.state)}${d.job ? ' · ' + esc(d.job.status) : ''}</p>`}
  ${d.hypothesis ? `<h2>Hypothesis</h2><p>${esc(d.hypothesis.mechanism)}</p>` : ''}
  <div class="grid">
    <div class="card"><span class="muted small">Baseline</span><b class="mono" style="font-size:15px">${esc(d.baseline.label)}</b><span class="mono small">${d.baseline.commit.slice(0, 12)}</span></div>
    <div class="card"><span class="muted small">Candidate</span><b class="mono" style="font-size:15px">${esc(d.candidate.label)}</b><span class="mono small">${d.candidate.commit.slice(0, 12)} · ${esc(d.candidate_meta?.originator || '')}</span></div>
    ${objs.map(o => `<div class="card"><span class="muted small">${esc(o.name.replace('objective:', ''))}</span><b>${num(o.baseline, 4)} → ${num(o.candidate, 4)}</b><span class="small">${esc(o.detail.split(', improvement ')[1] || '')}</span></div>`).join('')}
  </div>
  ${gates.length ? `<h2>Required checks</h2><table><thead><tr><th>Check</th><th>Result</th><th>Detail</th></tr></thead><tbody>
  ${gates.map(c => `<tr><td class="mono">${esc(c.name)}</td><td class="${c.passed ? 'pass' : 'fail'}">${c.passed ? 'pass' : 'fail'}</td><td class="small">${esc(c.detail)}</td></tr>`).join('')}</tbody></table>` : ''}
  <h2>Runs</h2><table><thead><tr><th>Side</th><th>Scenario</th><th class="num">Trial</th><th>Status</th>${metrics.map(m => `<th class="num">${esc(m)}</th>`).join('')}</tr></thead><tbody>
  ${runs.map(r => `<tr class="click" data-href="#/run/${r.id}"><td>${esc(r.side)}</td><td>${esc(r.scenario)}</td><td class="num">${r.trial + 1}</td><td>${esc(r.status)}</td>${metrics.map(m => `<td class="num">${num(r.metrics[m], 4)}</td>`).join('')}</tr>`).join('')}</tbody></table>
  ${d.patch ? `<h2>Patch</h2><pre class="diff">${diffHtml(d.patch)}</pre>` : ''}
  <h2>Events</h2><table><tbody>${d.events.map(e => `<tr><td class="small muted">${when(e.at)}</td><td class="mono">${esc(e.type)}</td><td class="mono small">${esc(JSON.stringify(e.payload))}</td></tr>`).join('')}</tbody></table>`;
}

function flatten(nodes, depth = 0, out = []) { for (const n of nodes) { out.push({ ...n.span, depth, orphan: n.orphan }); flatten(n.children, depth + 1, out); } return out; }

async function run(id) {
  const [d, t] = await Promise.all([api('/runs/' + id), api('/runs/' + id + '/trace')]);
  const spans = flatten(t.tree);
  const t0 = Math.min(...spans.map(s => s.start), ...t.dependencies.map(s => s.start)), t1 = Math.max(...spans.map(s => s.end), ...t.dependencies.map(s => s.end));
  const W = Math.max(t1 - t0, 1e-6);
  const resp = d.outcome.responses || [];
  view.innerHTML = `<p class="small"><a href="#/">Experiments</a></p><h1>Run ${esc(d.run.scenario)} <span class="badge ${d.run.status === 'completed' ? 'accepted' : 'rejected'}">${esc(d.run.status)}</span></h1>
  <p class="mono muted">${d.run.id} · revision ${esc(d.run.revision_id)} · trial ${d.run.trial + 1} · seed ${d.run.seed} · world ${esc(d.run.world)} ${esc(d.run.world_snapshot_digest.slice(0, 19))}</p>
  ${d.run.error ? `<p class="error">${esc(d.run.error)}</p>` : ''}
  <div class="grid">${Object.entries(d.run.metrics).filter(([k]) => !k.startsWith('calls:')).map(([k, v]) => `<div class="card"><span class="muted small">${esc(k)}</span><b>${num(v, 4)}</b></div>`).join('')}</div>
  <h2>Trace <span class="small muted">${spans.length} application spans, ${t.dependencies.length} dependency calls measured by the world. Select a span for its attributes.</span></h2>
  <div class="tree" id="tree">${spans.map((s, i) => `<div class="span ${s.status === 'error' ? 'err' : ''}" data-i="${i}"><span class="nm" style="padding-left:${s.depth * 14}px">${esc(s.name)}${s.orphan ? ' <span class="muted">(parent not captured)</span>' : ''}</span>
  <span class="bar"><i style="left:${(s.start - t0) / W * 100}%;width:${(s.end - s.start) / W * 100}%"></i></span><span class="ms">${num((s.end - s.start) * 1000, 2)} ms</span></div>`).join('')}</div>
  <h2>Responses</h2><table><thead><tr><th class="num">Step</th><th>Status</th><th class="num">ms</th><th>Body</th></tr></thead><tbody>
  ${resp.map(r => `<tr><td class="num">${r.step}</td><td>${esc(r.status ?? r.kind)}</td><td class="num">${num(r.ms, 1)}</td><td class="mono small">${esc(JSON.stringify(r.body ?? r.error ?? '')).slice(0, 300)}</td></tr>`).join('')}</tbody></table>
  <h2>Application log</h2><pre>${esc(d.log_tail || '(empty)')}</pre>`;
  document.getElementById('tree').addEventListener('click', e => {
    const row = e.target.closest('.span'); if (!row) return;
    document.querySelectorAll('.attrs').forEach(a => a.remove()); document.querySelectorAll('.span.sel').forEach(a => a.classList.remove('sel'));
    row.classList.add('sel');
    const s = spans[+row.dataset.i];
    row.insertAdjacentHTML('afterend', `<div class="attrs"><div class="kv">${Object.entries({ span_id: s.span_id, parent_id: s.parent_id, status: s.status, service: s.service, ...s.attributes }).map(([k, v]) => `<span class="muted">${esc(k)}</span><span>${esc(typeof v === 'object' ? JSON.stringify(v) : v)}</span>`).join('')}</div></div>`);
  });
}

async function findings() {
  const { items } = await api('/findings?limit=500');
  view.innerHTML = `<h1>Findings</h1><p class="muted">Observations derived from traces. Each links to the runs behind it.</p>
  <table><thead><tr><th>Severity</th><th>Finding</th><th>Detector</th><th class="num">Confidence</th><th>Evidence</th></tr></thead><tbody>
  ${items.map(f => `<tr><td>${esc(f.severity)}</td><td>${esc(f.description)}</td><td class="mono">${esc(f.detector)}</td><td class="num">${num(f.confidence, 2)}</td><td>${f.supporting_runs.slice(0, 3).map(r => `<a class="mono small" href="#/run/${r}">${r.slice(-8)}</a>`).join(' ')}</td></tr>`).join('') || '<tr><td colspan="5">None yet. Run <code>crossfade observe</code>.</td></tr>'}</tbody></table>`;
}

async function scenarios() {
  const s = await api('/scenarios');
  view.innerHTML = `<h1>Scenarios</h1><p class="muted">From the locked evaluation bundle. Holdout scenarios are run in every experiment but their contents are not shown.</p>
  <table><thead><tr><th>Name</th><th>Description</th><th class="num">Steps</th><th class="num">Expectations</th><th></th></tr></thead><tbody>
  ${s.map(x => `<tr><td class="mono">${esc(x.name)}</td><td>${esc(x.description)}</td><td class="num">${x.steps}</td><td class="num">${x.expectations}</td><td>${x.holdout ? '<span class="tag">holdout</span>' : ''}</td></tr>`).join('')}</tbody></table>`;
}

async function world() {
  const w = await api('/worlds');
  const f = w.fidelity;
  const list = (t, xs) => xs && xs.length ? `<h2>${t}</h2><ul>${xs.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : '';
  view.innerHTML = `<h1>World: ${esc(w.name)} <span class="muted small">v${esc(w.version)}</span></h1><p class="mono muted">fixtures ${w.fixtures.map(esc).join(', ')} · default snapshot ${esc(w.snapshot_digest.slice(0, 19))}</p>
  <div class="grid">${Object.entries(w.entities).map(([k, v]) => `<div class="card"><span class="muted small">${esc(k)}</span><b>${v}</b></div>`).join('')}</div>
  <h2>Routes</h2><p>${w.routes.map(r => `<span class="tag mono">${esc(r)}</span>`).join('')}</p>
  ${list('Implements', f.implements)}${list('From contracts', f.from_contracts)}${list('Derived from traces', f.derived_from_traces)}${list('Inferred', f.inferred)}${list('Simplified', f.simplified)}${list('Not supported', f.unsupported)}${list('Divergence risks', f.divergence_risks)}`;
}

async function project() {
  const [p, plugins] = await Promise.all([api('/project'), api('/plugins')]);
  const m = p.manifest;
  view.innerHTML = `<h1>${esc(p.project.name)}</h1><p class="mono muted">${esc(p.project.root)} · bundle ${esc((p.bundle || 'not locked').slice(0, 19))}</p>
  <div class="grid">${Object.entries(p.counts).map(([k, v]) => `<div class="card"><span class="muted small">${esc(k)}s</span><b>${v}</b></div>`).join('')}</div>
  ${m ? `<h2>Manifest</h2><table><thead><tr><th>Fact</th><th>Value</th><th>Evidence</th><th class="num">Confidence</th><th>Source</th></tr></thead><tbody>
  ${m.facts.map(f => `<tr><td class="mono">${esc(f.key)}</td><td>${esc(typeof f.value === 'object' ? JSON.stringify(f.value) : f.value)}</td><td>${esc(f.evidence)}</td><td class="num">${num(f.confidence, 2)}</td><td class="small muted">${esc(f.source)}</td></tr>`).join('')}</tbody></table>` : ''}
  <h2>Plugins</h2><table><thead><tr><th>Plugin</th><th>Kind</th><th>Provides</th><th>Needs</th><th>Status</th></tr></thead><tbody>
  ${plugins.map(x => `<tr><td class="mono">${esc(x.name)}</td><td>${esc(x.kind)}</td><td class="small">${x.provides.map(esc).join(', ')}</td><td class="small">${x.permissions.map(esc).join(', ')}</td><td class="${x.healthy ? 'pass' : 'muted'}">${esc(x.status)}</td></tr>`).join('')}</tbody></table>`;
}

async function integrity() {
  const v = await api('/verify');
  view.innerHTML = `<h1>Integrity</h1><div class="grid"><div class="card"><span class="muted small">Ledger events</span><b>${v.events}</b><span class="${v.bad_events.length ? 'fail' : 'pass'}">${v.bad_events.length ? 'hash chain broken at ' + v.bad_events.slice(0, 5).join(', ') : 'hash chain intact'}</span></div>
  <div class="card"><span class="muted small">Artifacts</span><b>${v.artifacts}</b><span class="${v.bad_artifacts.length ? 'fail' : 'pass'}">${v.bad_artifacts.length ? v.bad_artifacts.length + ' do not match their digest' : 'every digest matches'}</span></div></div>
  <p class="small muted">The chain detects edits and deletions in the local ledger. It is not tamper-proof against someone with write access to the file; sign or anchor exported bundles for that.</p>`;
}

async function route() {
  const [, page, id] = (location.hash || '#/').split('/');
  document.querySelectorAll('nav a').forEach(a => a.classList.toggle('on', a.getAttribute('href') === '#/' + (page || '')));
  try {
    if (!page) await home(); else if (page === 'exp') await experiment(id); else if (page === 'run') await run(id);
    else if (page === 'findings') await findings(); else if (page === 'scenarios') await scenarios(); else if (page === 'world') await world();
    else if (page === 'project') await project(); else if (page === 'integrity') await integrity(); else view.innerHTML = '<p>Not found.</p>';
  } catch (e) { view.innerHTML = `<p class="error">${esc(e.message)}</p>`; }
  view.focus();
}
document.addEventListener('click', e => { const tr = e.target.closest('tr[data-href]'); if (tr) location.hash = tr.dataset.href; });
window.addEventListener('hashchange', route);
api('/project').then(p => document.getElementById('proj').textContent = p.project.name).catch(() => {});
route();
