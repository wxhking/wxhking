#!/usr/bin/env python3
"""Update the merged-PR block in a GitHub profile README.

No external Python dependencies. Intended to run inside GitHub Actions.
GitHub Search API returns at most 1,000 search results for a query. Set
MAX_PAGES appropriately, and do not claim exhaustiveness above that limit.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

START = "<!-- CONTRIBUTIONS:START -->"
END = "<!-- CONTRIBUTIONS:END -->"
ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
USER = os.getenv("GITHUB_REPOSITORY_OWNER", "").strip()
TOKEN = os.getenv("GH_TOKEN", "").strip()
MIN_STARS = int(os.getenv("MIN_STARS", "0"))
TOP_N = max(1, int(os.getenv("TOP_N", "5")))
MAX_PAGES = max(1, min(10, int(os.getenv("MAX_PAGES", "10"))))
PROJECT_ICONS = {
    "bytedance/deer-flow": "https://avatars.githubusercontent.com/u/4158466?v=4",
    "tencentcloud/octop": "https://avatars.githubusercontent.com/u/20101770?v=4",
}


def api(path: str, params: dict | None = None) -> dict:
    suffix = "?" + urlencode(params) if params else ""
    url = f"https://api.github.com{path}{suffix}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "github-profile-merged-prs-template",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=25) as res:
            return json.load(res)
    except (HTTPError, URLError) as e:
        # Fail safe: keep the old README rather than erasing public data on errors.
        raise RuntimeError(f"GitHub API request failed: {url}: {e}") from e


def clean(text: str | None, limit: int = 100) -> str:
    text = " ".join((text or "").split()).replace("|", "\\|")
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return html.escape(text, quote=False)


def compact(num: int) -> str:
    if num < 1000:
        return str(num)
    return f"{num / 1000:.1f}k".replace(".0k", "k")


def project_link(full_name: str) -> str:
    display = f"[`{clean(full_name)}`](https://github.com/{full_name})"
    icon = PROJECT_ICONS.get(full_name.lower())
    if icon:
        display = f'<img src="{icon}" width="28" height="28" alt="{html.escape(full_name, quote=True)} icon" /> ' + display
    return display


def fetch_merged_prs() -> tuple[dict[str, list[str]], bool]:
    repo_counts: dict[str, list[str]] = {}
    total_count = 0
    truncated = False
    for page in range(1, MAX_PAGES + 1):
        data = api("/search/issues", {
            "q": f"is:pr is:merged is:public author:{USER} -user:{USER}",
            "per_page": 100,
            "page": page,
            "sort": "created",
            "order": "desc",
        })
        if data.get("incomplete_results"):
            raise RuntimeError("GitHub returned incomplete search results; preserving the existing README")
        items = data.get("items", [])
        total_count = int(data.get("total_count", 0))
        for item in items:
            # html_url typically has the form /OWNER/REPO/pull/NUMBER
            parts = item.get("html_url", "").split("/")
            if len(parts) < 7 or parts[-2] != "pull":
                continue
            repo_name = f"{parts[-4]}/{parts[-3]}"
            if parts[-4].lower() == USER.lower():
                continue  # Count contributions to other people's projects.
            urls = repo_counts.setdefault(repo_name, [])
            if item["html_url"] not in urls:
                urls.append(item["html_url"])
        if not items or len(items) < 100:
            break
        if page == MAX_PAGES:
            truncated = total_count > MAX_PAGES * 100
    return repo_counts, truncated


def render() -> str:
    counts, truncated = fetch_merged_prs()
    projects = []
    for full_name, urls in counts.items():
        repo = api("/repos/" + full_name)
        if repo.get("private"):
            continue
        stars = int(repo.get("stargazers_count", 0))
        if stars < MIN_STARS:
            continue
        projects.append({
            "name": repo.get("full_name", full_name),
            "count": len(urls),
            "urls": urls,
            "stars": stars,
            "language": repo.get("language") or "—",
            "description": repo.get("description") or "",
        })
    projects.sort(key=lambda r: (-r["stars"], r["name"].lower()))

    lines: list[str] = []
    if projects:
        lines.append(f"**{sum(r['count'] for r in projects)} merged PRs · {len(projects)} upstream projects**")
        lines.append("")
        lines += [
            "| Project | ★ | Language | Merged |",
            "| --- | ---: | --- | ---: |",
        ]
        for r in projects[:TOP_N]:
            desc = clean(r["description"])
            display = project_link(r["name"])
            if desc:
                display += f"<br><sub>{desc}</sub>"
            lines.append(
                f"| {display} | {compact(r['stars'])} | {clean(r['language'])} | {r['count']} |"
            )
        if len(projects) > TOP_N:
            lines += [
                "",
                "<details>",
                f"<summary>… and {len(projects) - TOP_N} more projects</summary>",
                "",
                "| Project | ★ | Merged |",
                "| --- | ---: | ---: |",
            ]
            for r in projects[TOP_N:]:
                lines.append(f"| {project_link(r['name'])} | {compact(r['stars'])} | {r['count']} |")
            lines += ["", "</details>"]
    else:
        lines.append(
            f"暂无符合条件的公开上游已合并 PR（项目 Star ≥ {MIN_STARS}）。"
            "新的贡献合并后会自动出现在这里。"
        )

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines += [
        "",
        f"<sub>Only merged PRs in external repositories with at least {MIN_STARS:,} stars. "
        f"Updated automatically: {now}."
        + (" Search results capped; older PRs may be omitted." if truncated else "")
        + "</sub>",
    ]
    return "\n".join(lines)


def main() -> None:
    if not USER:
        raise SystemExit("Missing GITHUB_REPOSITORY_OWNER")
    if not TOKEN:
        raise SystemExit("Missing GH_TOKEN")
    original = README.read_text(encoding="utf-8")
    if START not in original or END not in original:
        raise SystemExit("README.md is missing the required contribution markers")
    before, remainder = original.split(START, 1)
    _, after = remainder.split(END, 1)
    result = f"{before}{START}\n{render()}\n{END}{after}"
    if result != original:
        README.write_text(result, encoding="utf-8")
        print("Updated contribution table.")
    else:
        print("No changes.")


if __name__ == "__main__":
    main()
