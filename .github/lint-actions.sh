#!/usr/bin/env bash
# Lint the shell that actually runs in CI.
#
# WHY THIS EXISTS: "actionlint clean" covers less than it sounds like.
#
#   * actionlint's default scope is `.github/workflows` ONLY. Measured with
#     actionlint 1.7.12 in this repo: bare `actionlint` exits 0 having linted
#     three workflow files; pointed at any of the three composite actions it
#     rejects them outright -- `"jobs" section is missing in workflow
#     [syntax-check]`, exit 1. So the three `action.yml` files, which is where
#     most of the shell in this design lives (8 of the 20 `run:` blocks, and
#     every gate), had NEVER been linted at all.
#
#   * actionlint substitutes a placeholder for `${{ }}` before handing a script
#     to shellcheck, so it is STRUCTURALLY incapable of catching
#     interpolation-into-shell -- the class that put an interpolated `inputs`
#     value into an earlier revision of this design, lint-clean, where the first
#     caller whose smoke script contained a double quote turned a good build
#     red. A clean actionlint run is not evidence about that class. Rule (2)
#     below is, because it forbids the construct instead of trying to parse it.
#
# WHAT IS LINTED
#   Three kinds of shell reach a CI runner in this repo, and only the first was
#   covered before:
#     * `run:` blocks, in workflows AND composite actions;
#     * `smoke:` blocks -- a caller's script, executed by the smoke action under
#       `bash -euo pipefail`. windows.yml.template carries one, and it is the
#       file every future repo copies;
#     * standalone scripts under .github/ -- `actions/windows-smoke/run`, the
#       wrapper BOTH smoke legs execute for every PE, and this file itself.
#   The first two are found by extension (*.yml, *.yaml, *.template); the third
#   by shebang, so a script added later is covered without editing this list.
#
# THE RULES
#   1. shellcheck.
#   2. NO `${{ }}` inside a run: or smoke: block. Caller data reaches a shell
#      through `env:` or not at all. NOT applied to standalone scripts: GitHub
#      never expands a file on disk, so `${{ }}` there is inert text, and the
#      rule would fire on this script's own error message.
#   3. NO producer piped into an early-exiting consumer (`grep -q`, `grep -m`,
#      `head`, `read`). Under `set -euo pipefail` the producer is killed by
#      SIGPIPE and `pipefail` promotes 141 to the pipeline's status -- which is
#      a false red in a plain command and a SILENT PASS in an `if` condition.
#      Both shipped here, on adjacent lines of the .lgx gate; see the header of
#      .github/actions/windows-gates/action.yml for the measurements.
#
# Usage: .github/lint-actions.sh [--self-test]
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$PWD
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
rc=0

fail() { echo "::error file=$1,line=$2::$3"; rc=1; }

# Everything parsed as YAML. `.template` is included because windows.yml.template
# is copied verbatim into every consuming repo -- an unlinted file that becomes
# N linted files is the worst place to keep a mistake.
files() {
  find "$ROOT/.github" -type f \
    \( -name '*.yml' -o -name '*.yaml' -o -name '*.template' \) \
    | LC_ALL=C sort
}

# Standalone shell, by shebang rather than by name. Fixtures under
# .github/lint-fixtures/ are deliberately broken and are excluded by extension.
shell_files() {
  find "$ROOT/.github" -type f \
    ! -name '*.yml' ! -name '*.yaml' ! -name '*.template' ! -name '*.txt' \
    | LC_ALL=C sort \
    | while read -r f; do
        case $(sed -n '1p' "$f") in
          '#!'*sh|'#!'*sh\ *) echo "$f" ;;
        esac
      done
}

# ---------------------------------------------------------------------------
# One block of shell, wherever it came from.
#   $1 rel path   $2 line offset for messages   $3 file holding the body
#   $4 'yaml' (rules 1,2,3) or 'script' (rules 1,3 -- see rule 2's note)
# ---------------------------------------------------------------------------
lint_block() {
  local rel=$1 line=$2 body=$3 kind=$4 off ln rest out
  # Rules (2) and (3) are about CODE. A whole-line comment is blanked (not
  # deleted, so line numbers still line up) -- otherwise this linter fires on
  # the comments that explain the very patterns it forbids, which is how a rule
  # gets switched off for being noisy.
  sed 's/^[[:space:]]*#.*$//' "$body" > "$TMP/code.sh"
  off=$line

  if [ "$kind" = yaml ]; then
    # (2) interpolation-into-shell. Checked BEFORE shellcheck, because this is
    # precisely the class shellcheck cannot be shown.
    if grep -nE '\$\{\{' "$TMP/code.sh" > "$TMP/hits"; then
      fail "$rel" "$line" "GitHub interpolation inside a run:/smoke: block. It is substituted
into the script TEXT before bash sees it, so a value containing a quote breaks
the surrounding command and a value containing \$(...) executes on the runner.
Pass it through env: and reference it as a shell variable."
      while IFS=: read -r ln rest; do
        echo "::error::    $rel:$((off + ln)): $rest"
      done < "$TMP/hits"
    fi
  fi

  # (3) the SIGPIPE class.
  if grep -nE '\|[[:space:]]*(grep([[:space:]]+[^|]*)?[[:space:]]+-[a-zA-Z]*q|grep[^|]*-m[[:space:]]*[0-9]|head([[:space:]]|$)|read([[:space:]]|$))' \
       "$TMP/code.sh" > "$TMP/hits"; then
    fail "$rel" "$line" "a producer is piped into a consumer that exits early.
Under \`set -euo pipefail\` the producer is killed by SIGPIPE (141) and pipefail
promotes that to the pipeline's status: a false red in a plain command, and a
SILENT PASS when the pipeline is an \`if\` condition. Capture, then test --
  names=\$(tar tzf \"\$f\"); grep -q PATTERN <<<\"\$names\"
For a truncated diagnostic use \`sed -n '1,Np'\`, which reads to EOF."
    while IFS=: read -r ln rest; do
      echo "::error::    $rel:$((off + ln)): $rest"
    done < "$TMP/hits"
  fi

  # (1) shellcheck, on the real body rather than the comment-blanked copy.
  # A YAML block has no shebang; a standalone script already has its own, and a
  # second one would shift every reported line number by 1.
  if [ "$(sed -n '1p' "$body" | cut -c1-2)" = '#!' ]; then
    cp "$body" "$TMP/sc.sh"
  else
    { echo '#!/usr/bin/env bash'; cat "$body"; } > "$TMP/sc.sh"
  fi
  if ! out=$(shellcheck -s bash --color=never "$TMP/sc.sh" 2>&1); then
    fail "$rel" "$line" "shellcheck findings"
    printf '%s\n' "$out" | sed 's/^/::error::    /'
  fi
}

# Every `run:` and every string `smoke:` in the repo, wherever it lives.
# Workflows keep run blocks under .jobs[].steps[]; composite actions under
# .runs.steps[]; a caller keeps smoke under .jobs[].<id>.with.
lint_file() {
  local f rel key sel n i line shell
  f=$1; rel=${f#"$ROOT"/}
  # A file yq CANNOT PARSE is an error, never "0 blocks". The first draft of
  # this script had `2>/dev/null || echo 0` here, and it reported "clean" on a
  # tree containing a workflow with a YAML syntax error -- a linter skipping the
  # file it cannot read, which is the exact defect class this whole design
  # refuses. actionlint caught it; this line is why it had to.
  if ! yq '.' "$f" > /dev/null 2>"$TMP/yqerr"; then
    fail "$rel" 1 "this file could not be parsed as YAML, so NOTHING in it was linted:"
    sed 's/^/::error::    /' "$TMP/yqerr"
    return 0
  fi
  for key in run smoke; do
    # `..|select(has(KEY))` finds blocks under any layout without this script
    # having to know which kind of file it is looking at. The string guard
    # matters for `smoke`: windows-ci.yml declares an INPUT named smoke, whose
    # value is a map of description/required/type, not a script.
    sel="[.. | select(type == \"!!map\" and has(\"$key\") and (.$key | type == \"!!str\"))]"
    n=$(yq "$sel | length" "$f")
    [ "$n" -gt 0 ] || continue
    for ((i = 0; i < n; i++)); do
      shell=$(yq "$sel | .[$i].shell // \"bash\"" "$f")
      line=$(yq "$sel | .[$i].$key | line" "$f")
      yq "$sel | .[$i].$key" "$f" > "$TMP/blk.sh"
      # pwsh/python/etc are not our business. A smoke block has no `shell:` of
      # its own and is always run by the smoke action under bash.
      if [ "$key" = smoke ]; then
        shell=bash
        # A `smoke:` whose whole value is ONE GitHub expression is a workflow
        # pass-through -- windows-ci.yml forwarding its own `inputs.smoke` down
        # to the composite action -- not a script. GitHub evaluates it before
        # any shell exists. Linting it as shell reports the forwarding line as
        # both an interpolation violation and four shellcheck errors, which is
        # the linter inventing work; the script it eventually carries is linted
        # where it is WRITTEN, in the caller's own workflow.
        if grep -qE '^[[:space:]]*\$\{\{[^{}]*\}\}[[:space:]]*$' "$TMP/blk.sh" \
           && [ "$(grep -c '' "$TMP/blk.sh")" -le 1 ]; then
          continue
        fi
      fi
      case "$shell" in
        sh*|bash*) lint_block "$rel" "$line" "$TMP/blk.sh" yaml ;;
        *) ;;
      esac
    done
  done
}

lint_shell_file() {
  local f rel
  f=$1; rel=${f#"$ROOT"/}
  lint_block "$rel" 0 "$f" script
}

# ---------------------------------------------------------------------------
# --self-test proves the rules FIRE, EACH ONE BY NAME. A lint nobody has watched
# fail is a lint nobody knows is wired up -- which is how the three action.yml
# files went unlinted while "actionlint is clean" was true. The previous version
# of this self-test asserted only that the overall status was non-zero on a file
# that breaks all three rules, so it would have reported "all three rules fired"
# with two of them dead.
# ---------------------------------------------------------------------------
if [ "${1:-}" = --self-test ]; then
  FIX=$ROOT/.github/lint-fixtures
  mkdir -p "$TMP/.github/actions/bad" "$TMP/.github/scripts"
  cp "$FIX/bad-action.yml.txt" "$TMP/.github/actions/bad/action.yml"
  cp "$FIX/bad-script.txt"     "$TMP/.github/scripts/bad"
  ROOT=$TMP

  st=0
  saw() {   # $1 = human name, $2 = fixed string the rule's message must contain
    if grep -Fq -- "$2" "$TMP/out"; then
      echo "  rule $1: FIRED"
    else
      echo "::error::self-test: rule $1 did NOT fire — it is dead, and every"
      echo "::error::  file this linter has 'passed' was passed without it."
      st=1
    fi
  }

  echo "--- self-test A: a run: block breaking each of the three rules ---"
  rc=0
  lint_file "$TMP/.github/actions/bad/action.yml" > "$TMP/out" 2>&1 || true
  sed 's/^/  /' "$TMP/out"
  saw "(1) shellcheck"       "shellcheck findings"
  saw "(2) interpolation"    "GitHub interpolation inside"
  saw "(3) SIGPIPE"          "piped into a consumer that exits early"
  [ "$rc" -ne 0 ] || { echo "::error::self-test: overall status was 0"; st=1; }

  echo "--- self-test B: a standalone shell file (rules 1 and 3) ---"
  rc=0
  lint_shell_file "$TMP/.github/scripts/bad" > "$TMP/out" 2>&1 || true
  sed 's/^/  /' "$TMP/out"
  saw "(1) shellcheck, on a standalone script"  "shellcheck findings"
  saw "(3) SIGPIPE, on a standalone script"     "piped into a consumer that exits early"
  [ "$rc" -ne 0 ] || { echo "::error::self-test: overall status was 0"; st=1; }

  echo "--- self-test C: the shebang collector actually finds the wrapper ---"
  ROOT=$PWD
  # Captured, not `shell_files | grep -Fq`: that is rule (3), and a linter that
  # breaks its own rule is the argument against the rule.
  shell_files > "$TMP/collected"
  if grep -Fq 'actions/windows-smoke/run' "$TMP/collected"; then
    echo "  collector: FOUND actions/windows-smoke/run"
  else
    echo "::error::self-test: shell_files() does not collect the run wrapper, so"
    echo "::error::  the script both smoke legs execute would go unlinted."
    st=1
  fi

  [ "$st" -eq 0 ] && echo "--- self-test PASSED: every rule fired, on both kinds of input ---"
  exit "$st"
fi

while read -r f; do lint_file "$f"; done < <(files)
while read -r f; do lint_shell_file "$f"; done < <(shell_files)
[ "$rc" -eq 0 ] && echo "lint-actions: clean ($(files | wc -l | tr -d ' ') YAML, $(shell_files | wc -l | tr -d ' ') shell)"
exit "$rc"
