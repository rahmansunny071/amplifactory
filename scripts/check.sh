#!/usr/bin/env bash
# Run amplifactory's "checks pass" locally, the way Template CI does: render every variant of the
# Template CI matrix, run every step of its render job on each, then mkdocs build --strict.
# Steps run as `bash -eo pipefail <file>`, one file per step. Temp dir is kept only on failure.
# Usage: scripts/check.sh [repo-path]   (default: the repo root)
# The variants and steps are read from .github/workflows/template-ci.yml, so they can't drift.
# Renders with --vcs-ref HEAD, so COMMIT first: Copier renders the committed HEAD.
set -uo pipefail

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
summary=""
for v in $variants; do
  export REPO="$repo" RUNNER_TEMP="$work/$v"
  mkdir -p "$RUNNER_TEMP"
  uvx --from pyyaml python "$work/gen.py" "$wf" "$v" "$work" > "$work/steps-$v.sh" || { echo "$v: cannot generate steps"; fail=1; continue; }
  if (cd "$repo" && bash -eo pipefail "$work/steps-$v.sh") > "$work/log-$v.txt" 2>&1; then
    echo "$v: pass ($(grep -c '^== ' "$work/log-$v.txt") steps)"
  else
    echo "$v: FAIL"
    grep -E '::error|STEP FAILED|rror|failed' "$work/log-$v.txt" | head -20
    fail=1
  fi
done

if (cd "$repo" && uvx --with mkdocs-material mkdocs build --strict -d "$work/site") > "$work/mkdocs.log" 2>&1; then
  echo "mkdocs build --strict: pass"
else
  echo "mkdocs build --strict: FAIL"; tail -20 "$work/mkdocs.log"; fail=1
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
