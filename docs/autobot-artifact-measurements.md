# Issue artifact history measurements

Measured 2026-10-30 on macOS through `gh 2.103.3`, with a scoped App installation
token on [unenrolled test issue #8](https://github.com/cylewitruk/stacks-core-autobot/issues/8).
Merging and scheduled execution stayed disabled; no models were invoked.
Test artifacts were removed, the issue closed, and temporary tokens revoked.

The test created `generic` and `plan` artifacts with synthetic bodies of 24,000
and 8,000 UTF-8 bytes. It changed each body through revision 101, spacing writes
at least two seconds apart. At each milestone it made three sequential reads via
`gh api` with a 90-second timeout, including subprocess startup and JSON decoding.
Bytes below are uncompressed JSON stdout, not network-transfer or billing bytes.

| Type | Revision | Versions returned | Oldest returned | Response bytes | Median seconds |
| ---- | ---- | ---- | ---- | ---- | ---- |
| generic | 1 | 1 | 1 | 96,704 | 0.573 |
| generic | 10 | 10 | 1 | 530,145 | 0.782 |
| generic | 50 | 30 | 21 | 1,493,374 | 1.065 |
| generic | 101 | 30 | 72 | 1,493,376 | 1.136 |
| plan | 1 | 1 | 1 | 32,718 | 0.565 |
| plan | 10 | 10 | 1 | 178,240 | 0.604 |
| plan | 50 | 30 | 21 | 501,649 | 0.710 |
| plan | 101 | 30 | 72 | 501,651 | 0.936 |

Only the newest 30 versions were returned. `per_page=100&page=1`,
`per_page=10&page=2` and `per_page=100&page=2` did not change the returned window.
This was observed with both the default API version (`2022-11-28`) and explicit
`2026-03-10`. There was no pagination metadata. This establishes response behavior
through 101 versions, not a guarantee about server retention or future limits.
The [API documentation](https://docs.github.com/en/rest/issues/artifacts?apiVersion=2026-03-10)
describes newest-first history but provides no version-pagination parameters.

The measurements justify two changes:

- Persist verified plan identity in the protected audit envelope. A live test
  successfully repaired a renamed/replaced body at revision 33 when only revisions
  4–33 were returned, retaining artifact identity, task state and approval fields.
  Unbound plans whose origin cannot be established still stop safely.
- Skip repeated reads of unchanged plan views within an invocation, saving one
  request per save (about 0.50 MB and 0.94 seconds for the late plan fixture).
  Each new invocation still verifies the view. The live unchanged-save check
  needed only one audit GET (0.541 seconds), with no plan read or write. Audit
  concurrency checks and write readback remain mandatory.

The observed response size plateaus after 30 versions. JSON decoding took at most
3.6 ms in these samples; broader caching or a new storage backend is not justified
by this measurement. Timings are local observations with three samples, not an SLA.

Regression tests cover safe diagnostics, truncated history, old unbound plans,
interrupted binding writes, unchanged-view reads, plan deletion and interleaved
edits. A saved audit reference outside the returned window remains a safe stop;
the controller does not infer or reset missing authoritative history.

Measured pre-fix `checkpoints.py` SHA-256: `50ade5655509bfcdbd9768637de7ed058968804b4e19417f4c8e05d5ea70ff8a`.
