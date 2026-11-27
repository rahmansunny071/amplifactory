#!/usr/bin/env bash
# Run amplifactory's "checks pass" locally, the way Template CI does: render every variant of the
# Template CI matrix, run every step of its render job on each, then build the docs site with
# `mkdocs build --strict` (skipped in a repo without mkdocs.yml) and run the version check.
# Steps run as `bash -eo pipefail <file>`, one file per step. Temp dir is kept only on failure.
# Usage: scripts/check.sh [-j N] [repo-path]   (default: the repo root)
#   The variants run in parallel: one job per variant, capped by the CPU count. -j N (or CHECK_JOBS=N)
#   runs them in batches of N; -j 1 runs them one at a time. Each batch's results print as soon as
#   it finishes, in the matrix order. Ctrl-C stops the running renders.
# The variants and steps are read from .github/workflows/template-ci.yml, the file GitHub runs, so
# the two can't drift.
# Renders with --vcs-ref HEAD, so COMMIT first: Copier renders the committed HEAD.
set -uo pipefail

jobs=${CHECK_JOBS:-}
while [ $# -gt 0 ]; do
  case "$1" in
    -j) [ $# -ge 2 ] || { echo "-j needs a number" >&2; exit 2; }; jobs=$2; shift 2 ;;
    -h|--help) awk 'NR == 1 {next} /^#/ {print; next} {exit}' "$0"; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) break ;;
  esac
done
[ $# -le 1 ] || { echo "unexpected argument: $2 (usage: check.sh [-j N] [repo-path])" >&2; exit 2; }
case "$jobs" in
  '') ;;
  *[!0-9]*|0) echo "-j / CHECK_JOBS must be a positive number, not '$jobs'" >&2; exit 2 ;;
esac

here=$(cd "$(dirname "$0")/.." && pwd)
repo=$(cd "${1:-$here}" && pwd) || { echo "no such repo: ${1:-}" >&2; exit 2; }
wf="$repo/.github/workflows/template-ci.yml"
[ -f "$wf" ] || { echo "missing $wf" >&2; exit 2; }
command -v uvx >/dev/null 2>&1 || { echo "uvx (uv) is required" >&2; exit 2; }

if [ -n "$(git -C "$repo" status --porcelain 2>/dev/null)" ]; then
  echo "warning: working tree is dirty; renders use the committed HEAD, so commit first" >&2
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/amplicheck.XXXXXX")

# Prints variant names (no args), or, given a variant name, a bash script that does what the
# workflow does for it: matrix and runner.temp substituted, step env and working-directory applied.
cat > "$work/gen.py" <<'PY'
import re, sys, yaml

wf = yaml.safe_load(open(sys.argv[1]))
job = wf["jobs"]["render"]
variants = job["strategy"]["matrix"]["include"]
if len(sys.argv) < 3:
    for v in variants:
        print(v["variant"])
    sys.exit(0)
matrix = next(v for v in variants if v["variant"] == sys.argv[2])


def sub(text):
    text = re.sub(r"\$\{\{\s*matrix\.(\w+)\s*\}\}", lambda m: str(matrix[m.group(1)]).strip(), text)
    return re.sub(r"\$\{\{\s*runner\.temp\s*\}\}", "$RUNNER_TEMP", text)


outdir = sys.argv[3]
n = 0
for st in job["steps"]:
    run = st.get("run")
    if not run:
        continue
    name = st.get("name", "step")
    run = sub(run)
    if name == "Render":
        run = run.replace("copier copy ", "copier copy --quiet ", 1)
    wd = sub(st.get("working-directory", "$REPO"))
    env = "".join("export %s=%s\n" % (k, "'" + sub(str(v)).replace("'", "'\\''") + "'")
                  for k, v in (st.get("env") or {}).items())
    label = name.replace("'", "")
    n += 1
    stepfile = "%s/step-%s-%d.sh" % (outdir, matrix["variant"], n)
    open(stepfile, "w").write(run + "\n")
    # each step runs in its own bash -eo pipefail, so a failing command stops it (as in Actions)
    print("echo '== %s'" % label)
    print("( cd \"%s\" || exit 1\n%sbash -eo pipefail \"%s\"\n) || { echo 'STEP FAILED: %s'; exit 1; }"
          % (wd, env, stepfile, label))
PY

variants=$(uvx --from pyyaml python "$work/gen.py" "$wf") || { echo "cannot read $wf" >&2; exit 2; }

fail=0
# Generate every variant's steps first, then run the variants as background jobs, at most $jobs at
# a time (in batches: `wait -n` needs bash 4.3, and macOS ships 3.2). Each job has its own
# RUNNER_TEMP and log, and leaves its exit code in rc-<variant>. A variant whose steps can't be
# generated gets no steps-<variant>.sh. Each batch's results print once it finishes, in the matrix
# order.
runnable=""
for v in $variants; do
  mkdir -p "$work/$v"
  if uvx --from pyyaml python "$work/gen.py" "$wf" "$v" "$work" \
    > "$work/steps-$v.sh" 2> "$work/gen-$v.txt"; then
    runnable="$runnable $v"
  else
    rm -f "$work/steps-$v.sh"
  fi
done
if [ -z "$jobs" ]; then
  cpus=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)
  jobs=$(echo $runnable | wc -w | tr -d ' ')
  [ "$jobs" -le "$cpus" ] || jobs=$cpus
  [ "$jobs" -ge 1 ] || jobs=1
fi
run_variant() {
  ( export REPO="$repo" RUNNER_TEMP="$work/$1"; cd "$repo" && bash -eo pipefail "$work/steps-$1.sh" ) \
    > "$work/log-$1.txt" 2>&1
  echo $? > "$work/rc-$1"
}
excerpt() { grep -E '::error|STEP FAILED|rror|failed' "$1" | head -20; }
result() {
  if [ ! -f "$work/steps-$1.sh" ]; then
    echo "$1: cannot generate steps"; excerpt "$work/gen-$1.txt"; fail=1
  elif [ "$(cat "$work/rc-$1" 2>/dev/null)" = 0 ]; then
    echo "$1: pass ($(grep -c '^== ' "$work/log-$1.txt") steps)"
  else
    echo "$1: FAIL"; excerpt "$work/log-$1.txt"; fail=1
  fi
}
# Ctrl-C (or TERM, HUP) stops the renders too. Each job runs in a process group of its own (set -m;
# without it, background jobs ignore SIGINT), so stop() can send TERM to a job and all its children.
stop() {
  set +m
  trap - INT TERM HUP
  for p in $(jobs -p); do kill -TERM -- "-$p" 2>/dev/null; kill -CONT -- "-$p" 2>/dev/null; done
  wait 2>/dev/null
  echo "interrupted; logs kept in: $work" >&2
  kill -"$1" $$
}
trap 'stop INT' INT
trap 'stop TERM' TERM
trap 'stop HUP' HUP
batch=""
n=0
flush() {  # wait for the batch's jobs, then print its results in the matrix order
  wait
  for v in $batch; do result "$v"; done
  batch=""
  n=0
}
for v in $variants; do
  batch="$batch $v"
  if [ -f "$work/steps-$v.sh" ]; then
    set -m
    run_variant "$v" < /dev/null &
    set +m
    n=$((n + 1))
  fi
  # print now if nothing is running (only variants that couldn't be generated) or the batch is full
  if [ "$n" -eq 0 ] || [ "$n" -ge "$jobs" ]; then flush; fi
done
flush

# the docs site, built strictly (as the Docs workflow does) into a temp dir, never the checkout. A
# repo without mkdocs.yml skips it.
if [ -f "$repo/mkdocs.yml" ]; then
  if (cd "$repo" && uvx --with mkdocs-material mkdocs build --strict -d "$work/site") \
      > "$work/log-docs.txt" 2>&1; then
    echo "mkdocs build --strict: pass"
  else
    echo "mkdocs build --strict: FAIL"; tail -n 20 "$work/log-docs.txt"; fail=1
  fi
else
  echo "mkdocs build --strict: skipped (no mkdocs.yml)"
fi

# the Template CI "version" job: VERSION equals the first released changelog bullet
want=$(grep -m1 -E '^- v' "$repo/CHANGELOG.md" | sed -nE 's/^- v([0-9]+\.[0-9]+\.[0-9]+)([^0-9.].*)?$/\1/p')
have=$(tr -d '[:space:]' < "$repo/template/.claude/skills/factory/VERSION")
if [ -n "$want" ] && [ "$want" = "$have" ]; then
  echo "version: pass ($have)"
else
  echo "version: FAIL (CHANGELOG says '$want', VERSION says '$have')"; fail=1
fi

if [ "$fail" = 0 ]; then
  rm -rf "$work"
else
  echo "logs kept in: $work"
fi
exit $fail
