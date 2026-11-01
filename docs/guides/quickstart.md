# Quickstart

```sh
git clone https://github.com/hinterlandlabs/crossfade && cd crossfade && uv sync
uv run crossfade example support-agent /tmp/support && cd /tmp/support
CF="uv run --project $OLDPWD crossfade"
$CF init && $CF lock
$CF observe main                       # one run per scenario, traces and findings
$CF compare main drop-prefetch         # accepted: cheaper, every gate passes
$CF compare main trim-prompt           # rejected: cheaper, refunds outside the return window
$CF history && $CF verify
$CF serve                              # http://127.0.0.1:8700
```

`compare` exits 0 when accepted and 2 otherwise, so it can gate CI.
