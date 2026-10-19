"""Credit shown in the app footer and the README. Fill these in once; forks can change them."""
from . import __version__

VERSION = __version__
AUTHOR = "Michael Chein"
AUTHOR_URL = "https://michaelchein.com/"        # your website; leave empty to show the name without a link
REPO_URL = "https://github.com/michaelChein/chatter-radar"   # forks: change this (links are hidden if it contains OWNER)


def credit() -> dict:
    repo = "" if "OWNER" in REPO_URL else REPO_URL.rstrip("/")
    return {"version": VERSION, "author": AUTHOR, "author_url": AUTHOR_URL,
            "repo_url": repo, "issues_url": f"{repo}/issues" if repo else ""}
