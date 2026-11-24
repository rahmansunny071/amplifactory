"""MkDocs hook: render the repo's CHANGELOG.md as docs/reference/changelog.md at build time,
so the changelog lives in one place (the root file) and the site never drifts from it."""

from pathlib import Path

FRONT = '---\ntitle: "Changelog"\ndescription: "Every release, what changed and why."\n---\n\n'


def on_pre_build(config, **kwargs):
    root = Path(config["config_file_path"]).parent
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    text = text.replace("# Changelog", "# Changelog", 1)
    target = Path(config["docs_dir"]) / "reference" / "changelog.md"
    new = FRONT + text
    if not target.exists() or target.read_text(encoding="utf-8") != new:
        target.write_text(new, encoding="utf-8")
