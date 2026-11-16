# Harness: live-vs-mock parity spot checks

The offline harness (#53) runs the mock server against the typed models in CI
(no Chrome, no Apple). That catches **shape** drift, but not **value/ordering**
drift (fixture rot the models can't see). This is the documented manual
procedure to spot-check mock fixtures against a real, logged-in session (#137).

> Run this only against your own account, on a machine with a working tonearm
> login. Never record or commit real library rows, tokens, or identifying data.

## Procedure

1. **Pick representative routes** (read-only): e.g.
   - `GET /api/library/songs?limit=5`
   - `GET /api/library/search?term=<a known artist>`
   - `GET /api/playback/now-playing`
   - `GET /api/catalog/search?term=<artist>&types=songs&limit=3`
   - `GET /api/library/recently-added?limit=5`
2. **Capture normalized shapes** from each mode:
   - Live: `curl -s "$BASE/api/library/songs?limit=5" | jq -S '.items[]|{id,type,name,artist,album}'`
   - Mock: `TONEARM_MOCK=1 python3 -m tonearm` on another port, same `jq` filter.
3. **Diff the structure, not the data:** compare key sets, types, nesting, and
   ordering semantics (e.g. pagination fields `count/next_offset`, list vs object
   envelopes). Values WILL differ; **field presence, names, and types must not**.
4. **Check ordering contracts explicitly:** list endpoints that promise an order
   (recently-added, queue) should preserve that contract in both modes.
5. **Record findings** as fixture updates (synthetic/anonymized only) or as code
   fixes — never paste live payloads into the repo.

## What this catches

- A field renamed/added in the live normalizer but missing from mock fixtures.
- An envelope difference (`{items:[]}` vs raw array) that the model still accepts
  loosely.
- Pagination/`next_offset` semantics drifting between mock and real walks.

## Automation boundary

CI enforces model conformance and the route harness over synthetic fixtures.
Live-vs-mock parity remains a **manual, documented** gate because it requires a
real Apple session and must not leak private data. Re-run after changes to
normalizers, mock fixtures, or route shapes.
