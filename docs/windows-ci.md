# Windows CI — how the fleet is wired

## Shape

Nix does not run on Windows. The only Nix on a Windows box is inside WSL2, which
is Linux and produces ELF. So:

```
ubuntu-latest                                windows-latest
  nix build .#packages.x86_64-windows.*
  stage/                                     download artifact
  static gates (format, imports, .lgx)       re-count the PE manifest
  write pe-manifest.txt                 ──▶  run the SAME smoke script
  upload artifact                            ← the only real Windows coverage
        │
        └─▶ ubuntu-latest: same script under wine (fast red, not a substitute)
```

`logos-libp2p-module`'s existing `windows-wsl` job is the counter-example: it
burns a windows-latest runner, a WSL Ubuntu install and a cold Nix bootstrap on
every run to build and test `x86_64-linux` binaries. Delete or convert it.

## The staged tree layout is a contract

`stage/` holds **one directory per target and nothing else**. A single-target
repo whose target is `lgx` gets:

```
stage/lgx/bin/lgx.exe
stage/lgx/bin/*.dll
stage/pe-manifest.txt
```

There is no `stage/bin`. Smoke scripts run from `stage/`, so every path they
name **starts with the target name**:

```yaml
smoke: |
  run lgx/bin/lgx.exe --help     # correct
  run bin/lgx.exe --help         # refused by run's pre-flight; stage/bin does not exist
```

This is not a hypothetical. The first draft of the `logos-package` caller said
`run bin/lgx.exe --help`, and staging that repo's real output and running it on
a Windows 11 box reproduced `run: line 2: .../stage/bin/lgx.exe: No such file or
directory`, exit 127 — on **both** legs, for a build that was otherwise perfect.

That bare 127 is what the `run` wrapper exists to replace. Re-measured on a
Windows 11 box against a real staged tree, a wrong path now produces:

```
::error::run: 'bin/lgpm.exe' does not exist (cwd: .../tree).
::error::Smoke paths are relative to the staged tree ROOT, and the root holds
::error::one directory PER TARGET -- so a path starts with the target name:
::error::staged targets here: lgpm
```

and exit **1**, not 127 — the distinction matters, because 127 is also what a
program that fails to start reports under the same shell. The existence check
is what separates the two; it reports a fact ("no such file"), not a cause.

## What a repo adds

One file, `.github/workflows/windows.yml`, from `windows.yml.template`. Nothing
else — no Nix pinning, no cache config, no gate scripts.

```yaml
jobs:
  windows:
    uses: logos-co/logos-ci-actions/.github/workflows/windows-ci.yml@v1
    secrets: inherit
    with:
      targets: lgx
      smoke: |
        run lgx/bin/lgx.exe --help | tee help.txt
        grep -qi usage help.txt
```

Not `run … | grep -qi usage`: that is the SIGPIPE shape this repo's own lint
rule (3) forbids, and `lint-actions.sh` now checks `smoke:` blocks as well as
`run:` blocks, so it is caught in the template and in any caller that copies it.
`grep -q` also eats the wrapper's stdout — the diagnostics survive because they
go to stderr, but the program's own output does not.

### `smoke-file`, for anything longer than a few lines

```yaml
    with:
      targets: lgx
      smoke-file: .github/smoke/lgx-cli.sh
```

Same contract as `smoke` — same cwd, same `run` wrapper, same `$STAGE_ABS` —
and mutually exclusive with it; setting both is refused before the build starts,
because one of them would otherwise be silently ignored and a reader of the
caller could not tell which.

Two things a file buys that an inline block cannot:

* **It is linted.** An inline `smoke:` is invisible to shellcheck — `actionlint`
  substitutes a placeholder for `${{ }}` before handing a script over, which is
  the same blind spot that let `[ -z "${{ inputs.smoke }}" ]` ship. A committed
  script is linted by the caller's own CI like any other file.
* **It runs by hand.** `.github/smoke/lgx-cli.sh` is written so `LGX` defaults to
  the `.exe` but can be overridden, so the identical file runs against a native
  build during development. That is how you discover an assertion that never
  bites — `logos-package`'s script was first written against `master`'s `lgx`
  and asserted manifest schema `0.3.0`; the branch it landed on had bumped it to
  `0.4.0`, and only re-running it against that branch's own binary caught it.

Setting `smoke-file` is also what makes the smoke jobs **check the caller out**.
They otherwise only download the artifact — `$GITHUB_WORKSPACE` on those runners
holds nothing but `stage/` — so a caller-side path is unreachable without it.
The checkout lands in `caller/` and runs before the download, so it cannot
interact with the staged tree in either order.

The path is validated in `cross-build`, which already has the repo checked out:
a mistyped path fails there, in seconds, naming every `.sh` in the repo — rather
than ten minutes later on the Windows leg, where the message would be about
staged trees and read as a path-contract mistake. An **empty** file is refused
too, at both layers: it satisfies `-f`, runs, asserts nothing, exits 0, and is
otherwise indistinguishable from a smoke test that passed.

## `run`, and what it does and does not claim

Every PE is launched through a `run` wrapper on `PATH` — wine on the Linux leg,
a direct exec on the Windows leg, so one script serves both. What it adds over
`exec "$@"` is deliberately small:

* it **checks the PE exists before launching it**. Without that check a missing
  path and a program that failed to start arrive as the same status (`127` under
  bash, `53` under wine) and neither says which happened. The check reports a
  fact — `no such file: X` — and lists the staged target directories;
* on **any** non-zero exit it reports the exit code, the program's stdout
  verbatim, its stderr verbatim, and a listing of what actually shipped beside
  the binary. Nothing else. The listing is the one diagnostic here that has
  never been wrong, because it reports the tree rather than interpreting it;
* it treats **exit 0 with no output at all** as a failure. Silent success is
  this project's dominant defect class — an install rule that copied nothing, a
  package that labelled itself linux-amd64, a plugin shipped without its DLLs, a
  reply never sent; every one of them exited 0. `run -q` opts out, for a command
  that is legitimately silent. That is a contract with the caller, not a
  diagnosis;
* it writes **every one of its own diagnostics to stderr**, and reproduces the
  program's stdout there too on failure. The documented idiom pipes the wrapper
  (`run … | grep -qi usage`), and `grep -q` eats stdout; measured on Windows 11
  against a real failing PE, a report written to stdout reached the log as one
  raw line and nothing else. The program's own stdout still goes to stdout, so
  `| grep` keeps working.

### What it used to claim, and why that is gone

Earlier revisions read a cause out of the exit code and out of whichever DLL
name happened to appear on stderr: an exit-code case analysis, a named Windows
status condition, and a characterisation of which DLL each shell reports when a
load fails. **Three successive versions of that last claim were made and each
failed to reproduce when re-measured** — a first sweeping form, a corrected
narrower one, and a re-measured distribution. The behaviour is not stable enough
to characterise, and every attempt printed a confident false sentence into the
log of whoever hit it.

So the wrapper no longer says what a missing DLL is, which DLL the loader named,
or what any exit code means beyond what the program itself reported. The one
orienting sentence it still prints is that a non-zero exit with nothing on
stdout is *consistent with* the process having failed before `main()` — stated
without naming a cause, and explicitly without claiming it did.

The bar this is held to: on a real missing-DLL failure the log carries the exit
code, both streams, and the tree — enough for a maintainer to diagnose it —
without telling them anything that might be false.

## No `producer | grep -q` anywhere, and a lint that keeps it that way

Under `set -euo pipefail`, a producer that is still writing when its consumer
exits early is killed by SIGPIPE (141), and `pipefail` promotes that to the
pipeline's status. Both possible consequences shipped in this repo, on
**adjacent lines** of the `.lgx` gate. Measured on `ubuntu:24.04` / GNU tar 1.35
/ bash 5.2 — the builder's own platform — against `.lgx` fixtures built from
real cross-built PEs:

```
tar tzf "$lgxf" | grep -q '^variants/windows-x86_64/'
```
grep exits at the first match while tar is still listing → `PIPESTATUS 141 0` →
**false red on a perfectly correct `.lgx`**, at 25 entries and every size above
it. The `lgx-variant: true` configuration could not pass at all — the very
configuration the previous round added to unblock.

```
if tar tzf "$lgxf" | grep -qE '^variants/(linux|darwin)'; then
```
The same 141, but here it is an `if` **condition**, so "killed" reads as "no
match". A `.lgx` carrying `variants/linux-amd64` **passed silently** through the
check whose own comment calls it a contract. Measured with the linux entry at
position 2 of 207 → `141 0` → passed; the *same archive* written linux-last →
`0 0` → correctly refused. The gate's verdict depended on tar member **order**,
and `lgx add` writes variants in the order they were added.

Two things this makes concrete:

* **It is not enough to know the output is small.** The whole listing is ~1 KB.
  macOS `bsdtar` block-buffers it and passes both lines; GNU tar flushes per
  entry and fails both. A gate's correctness must not rest on a libc buffer size
  — and a fix validated only on a Mac would have looked fine.
* **Fixing the obvious site alone makes things worse.** In the shipped step the
  false red at the first line *masks* the silent pass at the second — it exits
  before reaching it. Repairing only the false red (the natural single-site fix,
  and the one that unblocks the `lgx` configuration) was measured to turn a
  Linux-carrying `.lgx` from REFUSE into **PASS**:

  | `.lgx` gate | correct windows-only `.lgx` | `.lgx` carrying `variants/linux-amd64` |
  |---|---|---|
  | as shipped | **REFUSE** (wrong) | REFUSE (right answer, wrong line) |
  | only the false red repaired | PASS | **PASS** (wrong) |
  | swept, both sites | PASS | REFUSE |

The fix everywhere is **capture, then test** — `names=$(tar tzf "$f")`, then
`grep -q … <<<"$names"`. It runs `tar` once instead of four times per archive,
and it is strictly stronger: the capture form fails under `set -e` when tar
itself fails, whereas `if tar … | grep` reads an **unreadable** archive as "no
non-Windows variant" and waves it through. Truncated diagnostics use
`sed -n '1,Np'`, which reads to EOF, rather than `head -N`, which exits early and
aborts the step at 141 *before* the `exit 1` that was going to explain the real
problem.

`.github/lint-actions.sh` rule (3) refuses the pattern outright. Run against the
pre-sweep tree it independently finds all **15** sites in 6 `run:` blocks, at
exact line numbers.

## `actionlint clean` covered less than it sounded like

Two gaps, both measured with actionlint 1.7.12:

* **Scope.** Bare `actionlint` lints `.github/workflows` and nothing else. Point
  it at any of the three composite actions and it rejects the file outright —
  `"jobs" section is missing in workflow [syntax-check]`, exit 1. So the three
  `action.yml` files — 8 of the 20 `run:` blocks, and *every gate* — had never
  been linted by anything.
* **Interpolation.** actionlint substitutes a placeholder for `${{ }}` before
  handing a script to shellcheck, so it is *structurally* incapable of catching
  interpolation-into-shell — the class that put `[ -z "${{ inputs.smoke }}" ]`
  into an earlier revision of this design, lint-clean. **A clean actionlint run
  is not evidence about that class.**

`.github/lint-actions.sh` (wired up by `.github/workflows/lint-ci.yml`) covers
both, over every workflow *and* every composite action:

1. shellcheck on each `run:` block;
2. **no `${{ }}` inside any `run:` block** — the rule forbids the construct
   rather than trying to parse it, which is why it covers what actionlint
   cannot. Caller data reaches a shell through `env:` or not at all;
3. no producer piped into an early-exiting consumer (see above).

It runs its own `--self-test` first, on every run: a linter that has silently
stopped matching anything reports exactly what a clean tree reports. Whole-line
comments are blanked before rules 2 and 3, so the comments that *explain* these
patterns do not trip them. A file `yq` cannot parse is an **error**, never "0
run blocks" — the first draft had `|| echo 0` there and reported "clean" on a
tree containing a workflow with a YAML syntax error, which actionlint caught.

## The artifact round trip is asserted, not assumed

The builder writes `pe-manifest.txt` into the tree root immediately before
upload; each smoke leg re-counts and diffs it. The upload/download hop is the
one link in this chain nobody has ever observed end to end, and a PE that
disappears there would otherwise reappear as an inscrutable launch failure.

The file is deliberately **not** dot-prefixed: `upload-artifact@v4` excludes
hidden files unless `include-hidden-files` is set, so a `.pe-manifest.txt` would
be dropped silently — the exact failure class it exists to detect.

The comparison also has a **floor**, because two files that agree on being empty
agree about nothing: an empty tree carrying an empty manifest used to print
"artifact round trip verified: 0 PEs" and exit 0, and the smoke script then ran
against nothing. An artifact must carry at least one PE, or at least one `.lgx`
(the one shape whose PEs legitimately live inside an archive).

### …and it never fails without naming a file

The comparison and its diagnosis used to disagree about what a line is.
Reproduced on Windows 11 / MSYS bash 5.3.15 / GNU grep 3.0 with a **CRLF**
`pe-manifest.txt` carrying the same three PEs: `cat` is byte-exact so the `!=`
fired, but MSYS grep strips the CR before matching, so both `grep -Fxv -f` calls
found every line present and printed **nothing**. The step failed having named
no file at all — the least actionable possible red, for a tree that had lost
nothing.

A CR is not a missing PE. The sets are now compared **CR-normalised**; a
line-ending-only difference is a `::warning::`, not a failure. And if the
normalised sets really do differ but neither `grep` can localise it, both
listings are printed in full rather than the step exiting on an empty
explanation. Measured on Windows, original vs current:

| case | shipped | current |
|---|---|---|
| CRLF manifest, same 3 PEs | **fails, names 0 files** | passes, warns about the line endings |
| one PE genuinely lost in transit | fails, names it | fails, names it |
| manifest and listing in different **order**, same PEs | **fails, names 0 files** | fails, prints both listings whole |
| intact tree | passes | passes |

## The gates' reader is pinned, and proved

The import-closure gate is the one that decides whether a tree is complete, and
it is worth exactly what its `objdump` is worth. It comes from **this repo's own
flake.lock** — `path:$GITHUB_ACTION_PATH/../../..#legacyPackages.x86_64-linux.pkgsWindows.buildPackages.binutils`
— not from the runner's flake registry.

It is **not** the same pin that produced the PEs, and an earlier version of this
paragraph said it was. The PEs are built by `nix build .#packages.x86_64-windows.<t>`
against the *caller's* checkout, which resolves `logos-nix` from the caller's own
`flake.lock`; the objdump comes from this repo at `@v1`. That skew predates the
extraction of these actions out of logos-nix — it was already true when they
shared a repo, because the caller was always a different repo. It is tolerable
because the reader only has to parse `pei-x86-64`, which the action proves with
an `objdump --info` positive control before trusting it. If that ever stops
being enough, the fix is a check comparing the two revs, not a shared flake. Those
are different binaries: measured from one machine a minute apart,
`nixpkgs#pkgsCross.mingwW64.buildPackages.binutils` resolved to `2ygvxdjd…`
while the pin resolved to `a54mx5f1…`. The pinned path is in `cache.nixos.org`,
so this costs a substitution, not a build.

Before it is trusted, the action asserts `objdump --info` lists `pei-x86-64`,
and `import_closure.py` requires each file to report `file format pei-x86-64`.
Neither check is decoration: a reader that cannot parse a PE reports zero
imports, and zero imports is also what a perfectly bundled tree reports.

`$OBJDUMP` is exported to `$GITHUB_ENV`, so a caller's `extra-gates` script uses
that same verified binary instead of resolving a second one.

The `--info` check is itself run on a **captured** string rather than
`objdump --info | grep -qw`, because that pipeline is the SIGPIPE shape above in
the worst possible place: `if ! <pipeline>` inverts, so a 141 would **refuse a
perfectly good objdump** and take the whole gates action down with a message
accusing the reader of not supporting PE. Measured on `ubuntu:24.04` (Ubuntu's
own binutils, as a size proxy — the pinned 2.46 store path was not reachable on
this host): `x86_64-w64-mingw32-objdump --info` is 2042 B / 81 lines and squeaks
under the stdio buffer (`0 0`, passes today), while a multi-target
`/usr/bin/objdump --info` is 31153 B / 742 lines and gives `141 0`. A
mingw-only binutils lists only its own targets; the gate's correctness should
not rest on that table staying under ~4 KB.

### wine is pinned the same way, for the same reason

The Linux leg's launcher used to come from `nixpkgs#wine64Packages.minimal` —
the runner's **floating flake registry**, exactly the source the objdump finding
rejected one layer up, and the most likely first failure nobody could diagnose
from the log. wine is what decides whether every PE in a repo "starts", so an
unpinned wine is an unpinned verdict. It now resolves from this repo's own
`flake.lock` via `path:$GITHUB_ACTION_PATH/../../..#legacyPackages.x86_64-linux.wine64Packages.minimal`
(verified against this repo's pin: `wine64-10.0`), and the action asserts wine
can report its own version **before any smoke runs** — a launcher that cannot
start makes every binary in the tree fail identically, and the failure would be
reported against the artifact.

The launcher inside that store path is `wine64`, **not** `wine`. An earlier
revision hardcoded `$out/bin/wine`, which does not exist there, so the wine leg
could not pass for any caller. Listed from the real path this repo's pin
resolves to (`…-wine64-10.0/bin`): `wine64`, `wine64-preloader`, `wineserver`,
`winedbg`, … and no `wine`. The action now probes for `wine64` then `wine` and
fails naming the directory contents if neither is there, because a hardcoded
name is what broke it the first time.

## What a repo with no Windows target does

Nothing. Do not add the file. `windows-ci.yml` fails loudly on a repo with no
`packages.x86_64-windows` rather than passing with nothing to build — 23 of the
45 repos that have Linux+macOS CI are in this bucket, so that is the most likely
way this design gets misapplied.

For a **module** repo the port is usually a `logos-module-builder` bump and no
source change at all: `lib/common.nix` in module-builder adds `x86_64-windows`
to `systems` and routes it through `logos-nix.lib.mkWindowsPkgs`. Measured — with
module-builder overridden to master, `logos-accounts-module` goes from
"attribute missing" to 17 Windows attributes, `logos-delivery-module` to 20.
It does **not** work for a module repo that rebuilds `packages` over its own
hardcoded `systems` list (logos-storage-module, logos-chat-module,
logos-test-modules, the Rust/Nim ones) — those need real work.

A **fleet audit** — a scheduled job that fails when a repo gains a Windows
target and nobody adds the caller — is deliberately NOT in this change. It was
written, and taken back out, because it cannot be sized honestly yet:

* the org has **202 unarchived repos**, not the ~60 the design assumed. 106 of
  them carry a flake, each needing a `nix eval` with a 180 s ceiling — the job
  would be cancelled mid-loop by its own 60-minute timeout, producing no
  summary, no buckets and no error.
* corroborating a 404 against the repo's contents root — the fix for a token
  that can list repos but not read them — files an **empty repository** as
  unreadable. Measured: 7 of the org's no-flake repos 404 on contents root
  because they have no commits, so the audit would sit permanently red on a
  false statement.

Both are solvable; neither is solvable by guessing. It belongs in its own change,
sized against 202 repos, once the per-repo callers exist and there is something
to drift *from*.

### 404 is not an authoritative answer

The last silent-skip path was the one that looked most like a real answer.
GitHub returns **404 for a resource the token may not see**, so "this repo has
no `flake.nix`" and "this token cannot see this repo's contents" arrive as the
same status code — and 404 was filed straight into *no flake — nothing to
evaluate*, a bucket that does not fail the job. Reproduced by driving the
shipped step with a `gh` that 404s every contents probe: **25 repos, all 25
filed "no flake", exit 0.** The bucket-accounting check cannot see it, because
the repos genuinely *are* filed.

So the classifier is now calibrated against known answers before it is trusted,
the same way the import gate calibrates its objdump: probe a file that must
exist and a path that must not, **at both ends of the loop** (a token that
degrades halfway through turns every remaining repo into a silent "no flake").
Plus one blunt corroborating rule: logos-co is a Nix workspace, so a result in
which *nothing* has a flake is a finding about the probe, not about the org.

| contents probe | shipped | current |
|---|---|---|
| 404s everything | **exit 0**, 25/25 "no flake" | exit 1 — canary: known-present read 404 |
| canary repo visible, every other repo 404 | **exit 0**, 25/25 "no flake" | exit 1 — canary passes, "all 25 repos classified no flake" fires |
| healthy | exit 0, 25 out of scope | exit 0, 25 out of scope, canary clean at both ends |

## Versioning: what `@v1` covers

`windows-ci.yml` is a **cross-repo** reusable workflow, so it cannot reference
its sibling actions with a relative path. A `uses: ./.github/actions/...` is
resolved against a **directory in the workspace** — and in a called reusable
workflow that workspace belongs to the caller, not to this repo, so it fails.

Two precisions, because an earlier version of this paragraph got the mechanism
close but not right. The rule is about the *directory*, not about "the caller's
repo": in the `wine-smoke` and `native-smoke` jobs the caller is checked out to
`caller/` when `smoke-file` is set, and not at all otherwise, so `./` would fail
there for a different reason than "wrong repo". And GitHub GA'd a `$/`
self-repository form on 2026-07-30 that resolves against the repo of the file it
appears in, which would make the internal refs both correct and
repo-name-independent. **It is not used here**, deliberately: nobody has run it
in a cross-repo reusable workflow, and this harness has never executed on a
runner at all. Absolute refs are the form that is understood. Revisit `$/` once
there is a green run to regress against.

The rule: **`v1` is one train.** The workflow and all three actions
(`nix-setup`, `windows-gates`, `windows-smoke`) are tagged from the same commit
and move together. Every `uses:` **inside `windows-ci.yml`** says `@v1` — the
same string the callers use.

That rule is about the *reusable workflow* only, and the distinction is easy to
"fix" in the wrong direction. `lint-ci.yml` runs in **this** repo's own checkout,
where `uses: ./.github/actions/...` resolves correctly and pins nothing to a tag
that may not exist yet. It is right as it is. Only a workflow that is `uses:`-ed
*by another repo* has to spell its siblings absolutely.

Note also that "every `uses:` inside `windows-ci.yml` says `@v1`" is **not**
true, and it is the sentence someone will grep against when re-tagging: of its
`uses:` lines, the four that name this repo say `@v1`, and the rest are
third-party actions at their own tags (`actions/checkout@v4`,
`actions/upload-artifact@v4`, `actions/download-artifact@v4`). The train is the
four.

What breaks if the tag moves:

* Moving `v1` to a commit changes the behaviour of every caller at once, with no
  PR anywhere. That is the point of a train, and the cost of it. Only move `v1`
  to a commit where the workflow and the actions are mutually consistent.
* A caller that pins a **SHA** or a **branch** (`windows-ci.yml@abcd123`) still
  gets its actions from `v1`, because the internal refs are absolute. That
  combination silently mixes two commits: an old workflow body calling new
  actions. Callers use the tag. If you must pin a caller to a SHA for a
  bisect, pin the internal refs in that same SHA too, and do not merge it.
* Nothing runs before the tag exists. This repo starts with no tags; `v1` must
  be created on a `master` commit containing `windows-ci.yml` **and** all three
  action directories, before the first caller can be enabled.

## Cost

Measured, `x86_64-linux`, 6 cores, `logos-package`'s `lgx`:

| | wall | note |
|---|---|---|
| cold, empty store | **18m52s** | dominated by mingw `icu4c`; 4 derivations |
| warm store, this repo's derivation only | **~30s** | 1 derivation, 15 MiB staged |
| unchanged PR | **1.2s** build + ~2 min job overhead | |

The mingw **toolchain** substitutes from `cache.nixos.org`. The mingw **Qt**
stack does not, in either cache — `qtbase`, `qtdeclarative`, `qtsvg`,
`qtremoteobjects`, `icu4c`, `boost` were all probed 404 on both. Nobody upstream
will ever fill it: nixpkgs' Hydra does not build cross Qt.

**So priming is a prerequisite, not an optimisation.** Run
`windows-cache-prime.yml` to green before enabling any Qt-dependent caller. Until
then, `windows-ci.yml` refuses to start a build over `cold-derivation-budget`
(default 30 derivations) and tells you to prime — a 60-second red instead of a
four-hour one.
