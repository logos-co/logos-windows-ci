# logos-ci-actions

Reusable GitHub Actions workflows and composite actions for the Logos fleet.

Today that is one workflow — **Windows CI**, which cross-builds a repo's
`x86_64-windows` target on an ordinary Linux runner and then *runs* the result
on a real `windows-latest` runner. The repo is not named for it on purpose:
`nix-setup` and the lint harness are platform-neutral, and GitHub versions a
repo as a whole, so a `-windows-` name would force a second shared-CI repo the
first time anyone wants a Linux or macOS job.

## Using it

One file in your repo, from
[`.github/workflows/windows.yml.template`](.github/workflows/windows.yml.template):

```yaml
jobs:
  windows:
    uses: logos-co/logos-ci-actions/.github/workflows/windows-ci.yml@v1
    secrets: inherit
    with:
      targets: lgx
      smoke-file: .github/smoke/lgx-cli.sh
```

That is the entire per-repo cost. The Nix install and cache, the
target-existence guard, the cold-cache refusal, the PE-format and
import-closure gates, the artifact hand-off and its round-trip check, the wine
pre-filter and the real-Windows execution all live here.

**Read [`docs/windows-ci.md`](docs/windows-ci.md) before adding a caller.**
It covers the staged-tree path contract (every smoke path starts with a target
name — `lgx/bin/lgx.exe`, never `bin/lgx.exe`), what `run` does and does not
claim, and why a repo with no `packages.x86_64-windows` must not get this file.

## What is here

| path | what it is |
|---|---|
| `.github/workflows/windows-ci.yml` | the reusable workflow (`workflow_call`) |
| `.github/actions/nix-setup` | Nix + the Logos cache + disk reclaim |
| `.github/actions/windows-gates` | PE-format and import-closure gates |
| `.github/actions/windows-smoke` | runs a caller's smoke script on wine and on Windows |
| `.github/lint-actions.sh` | the lint `actionlint` cannot do — see below |
| `flake.nix` | pins the two tools the actions read (mingw objdump, wine) |

`.github/actions/<name>/` is a **required** depth, not a style choice: two of
the actions resolve their tool from `$GITHUB_ACTION_PATH/../../..`, which is
this repo's checkout root only at that nesting.

## Versioning

`v1` is one train: the workflow and all three actions are tagged from the same
commit and move together. Callers use the tag. Pinning a caller to a SHA while
the workflow's internal refs still say `@v1` silently mixes two commits.

## Linting

`.github/lint-actions.sh` exists because "actionlint is clean" covers less than
it sounds like — actionlint's default scope is `.github/workflows` only, so
`action.yml` files go unlinted entirely, and it substitutes a placeholder for
every expression interpolation before handing a script to shellcheck, so it is
*structurally* incapable of catching interpolation-into-shell. Both linters run
in `lint-ci.yml`; neither subsumes the other.

```bash
nix develop -c bash .github/lint-actions.sh              # lint
nix develop -c bash .github/lint-actions.sh --self-test  # prove each rule fires
```

## Relationship to logos-nix

[`logos-co/logos-nix`](https://github.com/logos-co/logos-nix) owns the Windows
**cross-compilation overlay** and the fleet's nixpkgs/Qt pin, and is a flake
input of ~36 repos. This repo owns the **CI harness** and is a flake input of
nothing.

They were one repo and are separate because they need opposite change cadences
on the same ref: nothing Nix-side reads a tag, so every consumer resolves
logos-nix's default branch, and a CI-YAML commit there changes derivation
hashes for anything that records the input's rev. The harness should be free to
move on every fix.

`logos-nix` keeps `windows-cache-prime.yml`, which is genuinely bound to the
overlay — its trigger watches `flake.lock` and `nix/windows/**`, which no
external repo can express.

The `flake.nix` here is deliberately **not** built on logos-nix. It reproduces
the two attributes the actions read — the mingw objdump and wine — from stock
nixpkgs, verified byte-identical by `drvPath`. That equivalence holds under
`buildPackages` and for stock native attributes and does **not** generalise:
overlay-free `pkgsWindows.qt6.qtbase` does not even evaluate. If a gate ever
needs a real cross-compiled attribute, this flake stops being sufficient.

## Status

**Nothing here has ever run on a runner.** It was extracted from an unmerged,
unpushed branch of logos-nix, with the individual pieces exercised locally.
Treat the first caller as the acceptance test, and enable exactly one.
