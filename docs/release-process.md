# Release attempt control (#444)

The ordinary PR and main path is fractional. Use the `ci-full` label for the
complete platform matrix. A release needs successful full dispatch runs for
every required cell in `ci/release_full_ci_manifest.v1.json`, all at the exact
merged candidate SHA. Native hosted macOS Intel and Apple Silicon are allowed
for this opt-in full/release validation.

Before starting a candidate, update the control issue body with exactly one each
of `- Version-bump PR: #<number>`, `- Version-bump merge SHA: **<full SHA>**`,
`- Candidate PR: #<number>`, and `- Candidate merge SHA: **<full SHA>**`.
The bump PR must have merged to `main` and changed the package version from its
first parent. The candidate PR must also have merged to `main`; its commit must
descend from the bump and retain the requested source and lockfile version.
The candidate can move to a later reviewed merge while publication has not begun.
After the first external publication write, record a `fleet-release-freeze/v1`
issue comment containing the exact directive, `info.json` digest, five asset
SHA-256 values, and the packaged `.crate` SHA-256. Resume then requires that same identity, accepts an existing tag
only when it resolves to the frozen candidate, and rejects conflicting existing
GitHub asset hashes and crates.io checksum. Missing declared outputs may be filled on resume.

`ci/release.py` is the attempt front door. Issue #444 is the live control record
for the v1.0.1 pilot. After a reviewed version bump is merged to `main`, use a
clean checkout of that main commit:

```sh
python3 ci/release.py start --issue 444 --candidate-sha <40-character-SHA> --dry
python3 ci/release.py start --issue 444 --candidate-sha <40-character-SHA>
python3 ci/release.py status --issue 444
python3 ci/release.py resume --issue 444 --candidate-sha <same-SHA>
```

`--dry` on the front door only prints the plan. Without it, `start` or `resume`
appends the versioned directive to the issue and dispatches a **non-publishing**
`auto-release.yml` worker. That worker checks issue identity and the exact main
SHA before any build, builds the five declared archives, runs Cargo publication
dry-run, and uploads a preflight artifact containing `info.json` with sizes and
SHA-256 hashes. Its `dry_run=true` path creates no Git tag or GitHub Release.
Tag pushes cannot trigger this workflow.

The current real publication job remains disabled by an explicit failing gate.
Before opening that gate, implement and test the retryable destination state
machine: verify the full run IDs against every manifest cell; preserve the same
issue, SHA, tag, and hashes on resume; check crates.io package size and registry
state; publish the crate and a complete draft GitHub Release from one worker;
retry transient GitHub failures up to ten times; finalize only after both
destinations are verified; verify that `v1.0.1` resolves to the candidate SHA.
Crates.io and GitHub cannot be one atomic transaction, so a partial result must
stay recorded on the issue and resume without changing identity.

The remaining publisher state machine must create the freeze record before its
first tag, registry, or release write, preserve each archive's validated hashes
across retries, and verify final registry destinations. The real publisher is
still disabled by its explicit failing gate.

`python3 -m ci.release_destinations --issue 444 --candidate-sha <SHA> --dist
<five-archive-directory> --crate <packaged-crate-path>` runs read-only destination
preflight. It re-inspects the five archives against `info.json`, hashes the
packaged `.crate`, compares any existing issue freeze, tag, draft assets and
crates.io checksum, then prints the missing outputs and proposed freeze record.
The `.crate` must be outside the five-archive directory. This command makes no
issue comment, tag, release, upload, or registry write. The tested state-machine
interface in `ci/release_destinations.py` freezes before its first write,
requires authoritative issue readback of that freeze before a tag or upload,
resolves existing annotated tags and release tag references to the candidate,
rechecks destinations after ambiguous writes, and retries only explicitly transient
GitHub failures (ten attempts, exponential delays capped at 30 seconds).
There is deliberately no live write adapter or workflow call to it yet.

The real publisher is still **no-go**. Its live adapter must bind the crate
checksum to the exact successful Cargo package and dry run, enforce the
registry's upload-size and version rules, and verify the registry checksum on
resume before this workflow's explicit failing gate may be removed.

The archive inspection gate now checks the C ZIP against candidate vendor files,
checks binary archive `PROVENANCE.txt` commit/target fields, and checks the
installed Mach-O or PE library header. It records the validated target, commit,
member list, and binary hash in `info.json`. This does **not** establish that the
full test jobs executed the same library bytes: `auto-release.yml` builds its
installable assets in separate jobs from `macos-bundles.yml` and
`windows-bundles.yml`, whose test bundles are built again.

The worker collates and hashes its five archives in `preflight-assets`, then
downloads that exact preflight artifact in four native `smoke-shipped-assets`
jobs (hosted Intel Mac, Apple Silicon Mac, and two Windows DLL lanes). Every
dry or real attempt checks the exact candidate checkout, compares the outer
archive and packaged library SHA-256 with `info.json`, then calls `mi_malloc`
and `mi_free` through that packaged library. The `release` job depends on all
four smoke rows and re-verifies the same archived bytes before any possible
publication step. This allocation smoke is in addition to the release-local C
suite, which also precedes publication. The real publication path remains
fail-closed.

The release worker now builds its C test executables in the same tree as the
installed product libraries, with `MI_DHAT=OFF` retained. A release-local native
matrix compares the installed shared and static library bytes with the test
bundle's copies, then runs the bundle's C suite on hosted macOS Intel/ARM and
Windows GNU/MSVC. This matrix is a predecessor of `release`, so a failed or
missing native row prevents every publication step. The older full CI bundles
still exercise their own configurations, including DHAT, and are not claimed
to have byte-identical libraries. The real publisher stays behind the explicit
failing fleet gate until the remaining release state machine is implemented.
