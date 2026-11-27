#!/usr/bin/env bash
# Tests for scripts/check.sh, run against fake repos whose Template CI steps are quick shell
# commands (nothing is rendered): -h, a leftover argument, when and in what order the results
# print, a variant whose steps can't be generated, the docs build, and Ctrl-C. Needs uvx, like check.sh.
# Usage: scripts/test-check.sh   (about 10 s; the temp dir is kept only on failure)
set -uo pipefail

check="$(cd "$(dirname "$0")" && pwd)/check.sh"
t=$(mktemp -d "${TMPDIR:-/tmp}/checktest.XXXXXX")
failed=0
ok() { echo "ok: $1"; }
bad() { echo "FAIL: $1"; failed=1; }

# fake_repo DIR: a repo that check.sh accepts, at version 1.2.3, with the workflow read from stdin
fake_repo() {
  mkdir -p "$1/.github/workflows" "$1/template/.claude/skills/factory"
  cat > "$1/.github/workflows/template-ci.yml"
  echo 1.2.3 > "$1/template/.claude/skills/factory/VERSION"
  printf '# Changelog\n\n- v1.2.3 (2026-01-01): fake\n' > "$1/CHANGELOG.md"
}

# -h prints the whole leading comment block, whatever its length: here, two lines longer
awk 'NR == 3 {print "# added 1"; print "# added 2"} {print}' "$check" > "$t/check-h.sh"
end=$(grep -n -v '^#' "$t/check-h.sh" | head -1 | cut -d: -f1)
want=$(sed -n "2,$((end - 1))p" "$t/check-h.sh")
got=$("$BASH" "$t/check-h.sh" -h)
if [ "$got" = "$want" ] && grep -q '^# added 2$' <<<"$got" && grep -q '^# Usage: ' <<<"$got"; then
  ok "-h prints the header"
else
  bad "-h printed:"; echo "$got"
fi

# an option after the repo path is an error, not silently ignored
got=$("$BASH" "$check" "$t" -j 1 2>&1)
rc=$?
if [ "$rc" = 2 ] && grep -q '^unexpected argument: -j ' <<<"$got"; then
  ok "a leftover argument is rejected"
else
  bad "leftover argument: exit $rc, output: $got"
fi

# Each batch's results print as soon as it finishes, in the matrix order, including b, whose steps
# can't be generated (no matrix.word). a finishes after c, and d checks that c's result is already
# printed when d starts: true for -j 1 and for -j 2 (batches a-c and d-e).
fake_repo "$t/order" <<'YAML'
jobs:
  render:
    strategy:
      matrix:
        include:
          - {variant: a, word: x}
          - {variant: b}
          - {variant: c, word: x}
          - {variant: d, word: x}
          - {variant: e, word: x}
    steps:
      - name: Step
        run: |
          echo ${{ matrix.word }}
          case ${{ matrix.variant }} in
            a) sleep 1 ;;
            d) grep -q '^c: pass' "$CHECK_TEST_OUT" ;;
            e) false ;;
          esac
YAML
want='a: pass (1 steps)
b: cannot generate steps
c: pass (1 steps)
d: pass (1 steps)
e: FAIL
version: pass (1.2.3)'
for j in 1 2; do
  out="$t/order-$j.out"
  CHECK_TEST_OUT="$out" TMPDIR="$t" "$BASH" "$check" -j "$j" "$t/order" \
    > "$out" 2> "$t/order-$j.err"
  rc=$?
  got=$(grep -E '^[a-z]+: ' "$out")
  if [ "$rc" = 1 ] && [ "$got" = "$want" ] \
    && grep -A1 '^b: cannot generate steps' "$out" | grep -q KeyError \
    && grep -A1 '^e: FAIL' "$out" | grep -q 'STEP FAILED: Step'; then
    ok "-j $j: results print per batch, in the matrix order"
  else
    bad "-j $j: exit $rc, output:"; cat "$out" "$t/order-$j.err"
  fi
done

# The docs build: `mkdocs build --strict` through uvx with mkdocs-material, run from the repo
# root, into a temp dir outside the repo; a failure shows the log's last 20 lines and fails the run.
# A repo without mkdocs.yml (the fakes above) skips it. A fake uvx stands in for mkdocs only.
grep -q '^mkdocs build --strict: skipped (no mkdocs.yml)$' "$t/order-1.out" \
  && ok "no mkdocs.yml: the docs build is skipped" || bad "docs build not skipped without mkdocs.yml"
mkdir "$t/bin"
real_uvx=$(command -v uvx)
cat > "$t/bin/uvx" <<SH
#!/usr/bin/env bash
case " \$* " in
  *" mkdocs "*)
    echo "\$PWD|\$*" > "\$CHECK_TEST_DOCS"
    [ "\${CHECK_TEST_DOCS_FAIL:-}" = 1 ] || exit 0
    seq 1 30 | sed 's/^/docs log line /'; echo "WARNING - broken link"; exit 1 ;;
esac
exec "$real_uvx" "\$@"
SH
chmod +x "$t/bin/uvx"
fake_repo "$t/docs" <<'YAML'
jobs:
  render:
    strategy:
      matrix:
        include:
          - {variant: a}
    steps:
      - name: Step
        run: "true"
YAML
touch "$t/docs/mkdocs.yml"
PATH="$t/bin:$PATH" CHECK_TEST_DOCS="$t/docs.args" TMPDIR="$t" "$BASH" "$check" "$t/docs" \
  > "$t/docs.out" 2>&1
rc=$?
if [ "$rc" = 0 ] && grep -q '^mkdocs build --strict: pass$' "$t/docs.out" \
  && [ "$(cut -d'|' -f1 "$t/docs.args")" = "$(cd "$t/docs" && pwd)" ] \
  && grep -q -- '--with mkdocs-material mkdocs build --strict -d ' "$t/docs.args" \
  && grep -q -- "-d $t/amplicheck\\." "$t/docs.args"; then
  ok "the docs build runs strictly with mkdocs-material, outside the repo"
else
  bad "docs build: exit $rc, args '$(cat "$t/docs.args" 2>/dev/null)', output:"; cat "$t/docs.out"
fi
PATH="$t/bin:$PATH" CHECK_TEST_DOCS="$t/docs.args" CHECK_TEST_DOCS_FAIL=1 TMPDIR="$t" \
  "$BASH" "$check" "$t/docs" > "$t/docsfail.out" 2>&1
rc=$?
if [ "$rc" = 1 ] && grep -q '^mkdocs build --strict: FAIL$' "$t/docsfail.out" \
  && grep -q '^WARNING - broken link$' "$t/docsfail.out" \
  && ! grep -q '^docs log line 10$' "$t/docsfail.out" \
  && grep -q '^a: pass' "$t/docsfail.out" && grep -q '^version: pass' "$t/docsfail.out"; then
  ok "a failing docs build fails the run and shows the log's tail"
else
  bad "docs build failure: exit $rc, output:"; cat "$t/docsfail.out"
fi

# Ctrl-C stops the renders: SIGINT to check.sh's process group, as a terminal sends it, must also
# end the steps' children (background jobs ignore SIGINT)
fake_repo "$t/slow" <<'YAML'
jobs:
  render:
    strategy:
      matrix:
        include:
          - {variant: x}
          - {variant: y}
    steps:
      - name: Step
        run: |
          sleep 60 &
          echo $! > "$CHECK_TEST_PIDS/${{ matrix.variant }}"
          wait
YAML
mkdir "$t/pids"
set -m # a process group of its own, with SIGINT not ignored: like a command run from a terminal
CHECK_TEST_PIDS="$t/pids" TMPDIR="$t" "$BASH" "$check" -j 2 "$t/slow" \
  > "$t/int.out" 2> "$t/int.err" &
set +m
pid=$!
for _ in $(seq 100); do [ -s "$t/pids/x" ] && [ -s "$t/pids/y" ] && break; sleep 0.1; done
sleeps=$(cat "$t/pids/x" "$t/pids/y" 2>/dev/null)
kill -INT -- "-$pid"
for _ in $(seq 100); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
if kill -0 "$pid" 2>/dev/null; then
  kill -TERM -- "-$pid" 2>/dev/null
  bad "Ctrl-C: check.sh still runs after 10 s (is SIGINT ignored in this shell?)"
fi
wait "$pid"
rc=$?
left=""
for _ in $(seq 50); do
  left=""
  for p in $sleeps; do kill -0 "$p" 2>/dev/null && left="$left $p"; done
  [ -z "$left" ] && break
  sleep 0.1
done
if [ -n "$sleeps" ] && [ "$rc" = 130 ] && [ -z "$left" ] \
  && grep -q '^interrupted; logs kept in: ' "$t/int.err"; then
  ok "Ctrl-C stops the renders"
else
  bad "Ctrl-C: step pids '$sleeps', still running '$left', exit $rc, stderr:"; cat "$t/int.err"
  [ -z "$left" ] || kill $left 2>/dev/null
fi

if [ "$failed" = 0 ]; then
  rm -rf "$t"
  echo "all passed"
else
  echo "files kept in: $t"
fi
exit $failed
