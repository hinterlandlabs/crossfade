# Security policy

Please report vulnerabilities privately through GitHub security advisories on this repository ("Report a vulnerability")
rather than in a public issue. Include steps to reproduce and the impact.

We are especially interested in:
- ways a candidate revision can influence its own evaluation, read holdout scenarios, or write a decision;
- ways to rewrite ledger history or artifacts without `crossfade verify` noticing (beyond having raw write access to the files);
- secrets reaching the artifact store, logs or the API.

The local runner is documented as not being a hostile-code sandbox (docs/architecture/security.md); escapes from it are
known limitations, not vulnerabilities, unless they affect the trusted core.
