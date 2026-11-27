"""MkDocs hook: expose the newest release tag to the theme as config.extra.release, so the repo
widget in the header can show it beside the star and fork counts. Material reads the tag from the
GitHub *Releases* API, which has nothing to say for a repo that only pushes tags; reading git here
works either way. Without tags (a shallow clone, say) it sets nothing and the widget is unchanged."""

import subprocess
from pathlib import Path


def on_config(config, **kwargs):
    root = Path(config["config_file_path"]).parent
    try:
        out = subprocess.run(
            ["git", "tag", "--list", "v[0-9]*", "--sort=-v:refname"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        return config
    if out:
        repo = config["repo_url"].rstrip("/")
        config.setdefault("extra", {})["release"] = {
            "tag": out[0],
            "url": f"{repo}/releases/tag/{out[0]}",
        }
    return config
