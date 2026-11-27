// Show the newest release tag beside the star and fork counts in the repo widget (header and mobile
// drawer). Material fills the widget from the GitHub Releases API (version, stars, forks); a repo
// that only pushes tags gets no version, so add one from the tag the build stamped on the widget,
// linked to that release (or tag) page. Material appends its facts after the page loads, hence the
// observer. If the API fails (rate limit, offline) the tag is still shown, on its own.
document$.subscribe(function () {
  document.querySelectorAll(".md-source[data-release-tag]").forEach(function (source) {
    var repo = source.querySelector(".md-source__repository");
    // navigation.instant re-runs this on every page while the header persists.
    if (source.dataset.releaseDone) return;
    source.dataset.releaseDone = "1";
    var li = document.createElement("li");
    li.className = "md-source__fact md-source__fact--version";
    var a = document.createElement("a");
    a.href = source.dataset.releaseUrl;
    a.textContent = source.dataset.releaseTag;
    // The widget is itself a link to the repo: keep this click on the release.
    a.addEventListener("click", function (e) { e.stopPropagation(); });
    li.appendChild(a);
    var own = null;

    function place() {
      var theirs = repo.querySelector(".md-source__facts:not(.md-source__facts--own)");
      if (theirs) {
        if (own) { own.remove(); own = null; }
        if (!theirs.querySelector(".md-source__fact--version")) theirs.insertBefore(li, theirs.firstChild);
      } else if (!own) {
        own = document.createElement("ul");
        own.className = "md-source__facts md-source__facts--own";
        own.appendChild(li);
        repo.appendChild(own);
        repo.classList.add("md-source__repository--active");
      }
    }

    new MutationObserver(place).observe(repo, { childList: true });
    if (repo.querySelector(".md-source__facts")) place();
    setTimeout(place, 3000);
  });
});
