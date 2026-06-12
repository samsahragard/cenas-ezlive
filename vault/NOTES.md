# Gate 2 adversarial review - results (ck, 2026-06-12)

Reviewed and tested personally, independent of the build agents' own checks.

## Static
- py_compile: both cenas_vault_cloud.py and vault_sync.py PASS
- hmac.compare_digest used for BOTH username and password (3 uses)
- home-dir fallback removed; the one remaining expanduser() acts on the
  env-configured root only (no fallback path exists)
- /api/open + /api/reveal return 400 "local-only"; no subprocess/webbrowser
  calls remain in the cloud fork
- .gitignore covers .env, *.db, *.sqlite, __pycache__/, *.pyc
- no secrets anywhere in vault/ (render.yaml: VAULT_TOKEN sync:false only)

## Live probe (local boot, fake env, port 8899) - 26 checks
- 401 wall: no-auth on /, wrong password, unknown route, unauthenticated
  /sync/file POST - all 401 with WWW-Authenticate and {"ok": false} only
- Authed GETs: / /api/state /api/list(?path=) /api/search /api/recent
  /api/tagged all 200; unknown route 404
  (note: bare /api/list without ?path= is 400 in the ORIGINAL local vault
  too - faithful fork, not a regression)
- /api/open + /api/reveal: 400 local-only
- Sync: push verified by sha before write; manifest lists it; download by
  hash matches sha; bad sha -> 400; traversal (../, ..\\, drive letter) ->
  400; blocklisted relpaths (memory/, *secrets*, .git/, *conversations*) ->
  400; tombstone marks deleted=1 and quarantines

## Round-trip with the REAL worker against the local fork
- pass 1: pushed 29, skipped 41 junctions / 1 blocklist (08 Archive), 0 errors
- pass 2: all zeros (idempotent)
- cloud manifest after: 29 live files, 13,611,370 bytes - matches local payload
- pull direction: cloud-seeded file landed at
  C:\Cenas\99 Inbox\from-cloud\notes\cloud_only_note.txt, sha-verified

## Defect found and fixed at this gate
- MANIFEST SHAPE MISMATCH: the cloud node returns files as a LIST of rows;
  the worker's contract sketch expected a dict keyed by relpath. Each agent
  passed its own tests; the cross-test failed (exit 4). Fixed in
  vault_sync.py fetch_cloud_manifest(): both shapes accepted, normalized to
  the dict. Re-tested green.

## Deploy notes
- Worker test state (vault_sync_local.db) deleted after testing - first prod
  run starts fresh. vault_sync.allow ("C:\Cenas") and vault_sync.skip
  ("08 Archive", "99 Inbox\from-cloud") committed as the spec'd defaults.
- Payload at deploy time: 29 files / ~13 MB -> 1 GB disk is ample.
