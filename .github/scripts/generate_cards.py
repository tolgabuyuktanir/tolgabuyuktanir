#!/usr/bin/env python3
"""Render the profile README's stat cards as static SVGs.

Runs in GitHub Actions, so the README no longer depends on third-party card
services (which are often rate-limited or down). The heatmap and streak count
commits the user authored on each repository's default branch. The built-in
GITHUB_TOKEN sees public repositories only; set a PROFILE_TOKEN secret to
include private ones.
Without a token it writes empty placeholder cards, which is handy for
previewing the layout locally.

Outputs (in ./assets):
  stats.svg          stars, commits, PRs, issues, repos, contributed-to
  top-langs.svg      most used languages across owned, non-fork public repos
  streak.svg         total commits, current and longest commit streak
  commits.svg        commit heatmap for the last 53 weeks
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.request
from zoneinfo import ZoneInfo
from pathlib import Path
from xml.sax.saxutils import escape

LOGIN = os.environ.get("GH_USER", "tolgabuyuktanir")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
# Commits are bucketed into days in this time zone.
TZ = ZoneInfo(os.environ.get("CARDS_TZ", "Europe/Istanbul"))
OUT = Path(os.environ.get("CARDS_OUT", "assets"))
# Languages that would skew the chart (e.g. notebook outputs) can be listed here.
EXCLUDED_LANGS = {s.strip() for s in os.environ.get("EXCLUDED_LANGS", "").split(",") if s.strip()}

# Palette, shared with the README banner.
BG = "#0D1117"
BORDER = "#30363D"
TITLE = "#22D3EE"
TEXT = "#C9D1D9"
MUTED = "#8B949E"
STRONG = "#E6EDF3"
FIRE = "#F97316"
HEAT = ["#161B22", "#0E4F5C", "#0E7490", "#06B6D4", "#67E8F9"]
FONT = "'Segoe UI', -apple-system, BlinkMacSystemFont, Helvetica, Arial, sans-serif"


# --------------------------------------------------------------------------- data

def gql(query: str, variables: dict) -> dict:
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.load(resp)
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"], indent=2))
    return payload["data"]


PROFILE_Q = """
query($login: String!, $after: String) {
  user(login: $login) {
    id
    pullRequests { totalCount }
    issues { totalCount }
    repositoriesContributedTo(contributionTypes: [COMMIT, PULL_REQUEST, ISSUE, REPOSITORY]) { totalCount }
    repositories(ownerAffiliations: OWNER, isFork: false, privacy: PUBLIC, first: 100, after: $after) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        stargazerCount
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
    }
  }
}
"""

# Every non-fork repo the token can see that the user owns or works in. With the
# default GITHUB_TOKEN that is public repos only; a PROFILE_TOKEN secret adds
# private ones (only their commit counts and dates are used, never names).
COMMIT_REPOS_Q = """
query($login: String!, $after: String) {
  user(login: $login) {
    repositories(ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER], isFork: false,
                 first: 50, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { nameWithOwner defaultBranchRef { name } }
    }
  }
}
"""

HISTORY_Q = """
query($owner: String!, $name: String!, $branch: String!, $uid: ID!, $after: String) {
  repository(owner: $owner, name: $name) {
    ref(qualifiedName: $branch) {
      target {
        ... on Commit {
          history(author: {id: $uid}, first: 100, after: $after) {
            pageInfo { hasNextPage endCursor }
            nodes { committedDate }
          }
        }
      }
    }
  }
}
"""


def paginate(query: str, variables: dict, path: list[str]):
    after = None
    while True:
        node = gql(query, {**variables, "after": after})
        for key in path:
            node = (node or {}).get(key) or {}
        if not node:
            return
        yield from node.get("nodes", [])
        if not node["pageInfo"]["hasNextPage"]:
            return
        after = node["pageInfo"]["endCursor"]


def fetch() -> dict:
    repos, after, user = [], None, None
    while True:
        user = gql(PROFILE_Q, {"login": LOGIN, "after": after})["user"]
        page = user["repositories"]
        repos.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]

    langs: dict[str, dict] = {}
    for repo in repos:
        for edge in repo["languages"]["edges"]:
            name = edge["node"]["name"]
            if name in EXCLUDED_LANGS:
                continue
            entry = langs.setdefault(name, {"size": 0, "color": edge["node"]["color"] or MUTED})
            entry["size"] += edge["size"]

    # Commits authored by the user on each repo's default branch, bucketed by
    # local day. This is what the heatmap and streak count.
    days: dict[str, int] = {}
    commits = 0
    for repo in paginate(COMMIT_REPOS_Q, {"login": LOGIN}, ["user", "repositories"]):
        if not repo["defaultBranchRef"]:
            continue  # empty repository
        owner, name = repo["nameWithOwner"].split("/", 1)
        variables = {"owner": owner, "name": name, "uid": user["id"],
                     "branch": f"refs/heads/{repo['defaultBranchRef']['name']}"}
        for c in paginate(HISTORY_Q, variables, ["repository", "ref", "target", "history"]):
            when = dt.datetime.fromisoformat(c["committedDate"].replace("Z", "+00:00"))
            key = when.astimezone(TZ).date().isoformat()
            days[key] = days.get(key, 0) + 1
            commits += 1

    return {
        "stars": sum(r["stargazerCount"] for r in repos),
        "repos": user["repositories"]["totalCount"],
        "prs": user["pullRequests"]["totalCount"],
        "issues": user["issues"]["totalCount"],
        "contributed": user["repositoriesContributedTo"]["totalCount"],
        "commits": commits,
        "langs": langs,
        "days": days,
        "today": dt.datetime.now(TZ).date(),
    }


def streaks(days: dict[str, int], today: dt.date) -> tuple[int, int, int, str]:
    """Total commits, current streak and longest streak, in days."""
    total = sum(days.values())
    active = sorted(dt.date.fromisoformat(d) for d, n in days.items() if n > 0)
    longest = run = 0
    prev = None
    for day in active:
        run = run + 1 if prev and (day - prev).days == 1 else 1
        longest = max(longest, run)
        prev = day
    current, day = 0, today
    if days.get(day.isoformat(), 0) == 0:  # today is not over yet
        day -= dt.timedelta(days=1)
    while days.get(day.isoformat(), 0) > 0:
        current += 1
        day -= dt.timedelta(days=1)
    first = active[0].isoformat() if active else ""
    return total, current, longest, first


# --------------------------------------------------------------------------- svg

def fmt(n) -> str:
    if n is None:
        return "—"
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1_000:
        return f"{n / 1000:.1f}k"
    return str(n)


def card(w: int, h: int, title: str, body: str, label: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="{escape(label)}">
<title>{escape(label)}</title>
<style>
  text {{ font-family: {FONT}; }}
  .t {{ font-size: 17px; font-weight: 600; fill: {TITLE}; }}
  .l {{ font-size: 13px; fill: {MUTED}; }}
  .v {{ font-size: 22px; font-weight: 700; fill: {STRONG}; }}
  .s {{ font-size: 13px; fill: {TEXT}; }}
  .fade {{ opacity: 0; animation: in .6s ease-out forwards; }}
  @keyframes in {{ to {{ opacity: 1; }} }}
</style>
<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="10" fill="{BG}" stroke="{BORDER}"/>
<text x="24" y="36" class="t">{escape(title)}</text>
{body}
</svg>
"""


def stats_svg(d: dict | None) -> str:
    g = (lambda k: d[k]) if d else (lambda k: None)
    items = [
        ("Total stars", g("stars")),
        ("Commits", g("commits")),
        ("Pull requests", g("prs")),
        ("Issues", g("issues")),
        ("Public repos", g("repos")),
        ("Contributed to", g("contributed")),
    ]
    cells = []
    for i, (label, value) in enumerate(items):
        col, row = i % 3, i // 3
        x, y = 24 + col * 150, 82 + row * 66
        cells.append(
            f'<g class="fade" style="animation-delay:{i * 80}ms">'
            f'<rect x="{x}" y="{y - 26}" width="3" height="44" rx="1.5" fill="{TITLE}"/>'
            f'<text x="{x + 14}" y="{y - 6}" class="v">{fmt(value)}</text>'
            f'<text x="{x + 14}" y="{y + 14}" class="l">{label}</text></g>'
        )
    return card(495, 195, "GitHub stats", "\n".join(cells), "GitHub stats")


def langs_svg(d: dict | None) -> str:
    langs = sorted((d or {}).get("langs", {}).items(), key=lambda kv: -kv[1]["size"])[:8]
    total = sum(v["size"] for _, v in langs) or 1
    parts = []
    if not langs:
        parts.append(f'<rect x="24" y="56" width="447" height="10" rx="5" fill="{BORDER}"/>')
        parts.append(f'<text x="24" y="100" class="l">Generated daily by GitHub Actions</text>')
    else:
        parts.append('<clipPath id="bar"><rect x="24" y="56" width="447" height="10" rx="5"/></clipPath><g clip-path="url(#bar)">')
        x = 24.0
        for name, v in langs:
            w = 447 * v["size"] / total
            parts.append(f'<rect x="{x:.2f}" y="56" width="{w + 0.5:.2f}" height="10" fill="{v["color"]}"/>')
            x += w
        parts.append("</g>")
        for i, (name, v) in enumerate(langs):
            col, row = i % 2, i // 2
            x, y = 24 + col * 225, 98 + row * 24
            pct = 100 * v["size"] / total
            parts.append(
                f'<g class="fade" style="animation-delay:{i * 60}ms">'
                f'<circle cx="{x + 5}" cy="{y - 4}" r="5" fill="{v["color"]}"/>'
                f'<text x="{x + 18}" y="{y}" class="s">{escape(name)}</text>'
                f'<text x="{x + 200}" y="{y}" class="l" text-anchor="end">{pct:.1f}%</text></g>'
            )
    return card(495, 195, "Most used languages", "\n".join(parts), "Most used languages")


def streak_svg(d: dict | None) -> str:
    if d:
        total, current, longest, first = streaks(d["days"], d["today"])
        since = f"since {first[:4]}" if first else ""
    else:
        total = current = longest = None
        since = ""
    cols = [
        (fmt(total), "Total commits", since, STRONG),
        (fmt(current), "Current streak", "days in a row", FIRE),
        (fmt(longest), "Longest streak", "days", STRONG),
    ]
    body = []
    for i, (value, label, sub, color) in enumerate(cols):
        cx = 140 + i * 275
        body.append(
            f'<g class="fade" style="animation-delay:{i * 100}ms">'
            f'<text x="{cx}" y="84" text-anchor="middle" class="v" style="font-size:30px;fill:{color}">{value}</text>'
            f'<text x="{cx}" y="110" text-anchor="middle" class="s">{label}</text>'
            f'<text x="{cx}" y="128" text-anchor="middle" class="l" style="font-size:12px">{escape(sub)}</text></g>'
        )
        if i:
            body.append(f'<line x1="{cx - 137}" y1="56" x2="{cx - 137}" y2="130" stroke="{BORDER}"/>')
    return card(830, 150, "Commit streak", "\n".join(body), "Commit streak")


def heatmap_svg(d: dict | None) -> str:
    today = d["today"] if d else dt.date.today()
    days = d["days"] if d else {}
    # 53 columns ending with the current week (weeks start on Sunday, like GitHub).
    end = today + dt.timedelta(days=(5 - today.weekday()) % 7)
    start = end - dt.timedelta(days=53 * 7 - 1)
    peak = max([days.get((start + dt.timedelta(days=i)).isoformat(), 0) for i in range(53 * 7)] + [1])
    size, gap, x0, y0 = 11, 3, 52, 64
    cells, months, last_month = [], [], None
    for i in range(53 * 7):
        day = start + dt.timedelta(days=i)
        col, row = i // 7, i % 7
        if day > today:
            continue
        n = days.get(day.isoformat(), 0)
        level = 0 if n == 0 else min(4, 1 + int(3 * n / peak))
        x, y = x0 + col * (size + gap), y0 + row * (size + gap)
        cells.append(f'<rect x="{x}" y="{y}" width="{size}" height="{size}" rx="2.5" fill="{HEAT[level]}"><title>{day.isoformat()}: {n} commit{"" if n == 1 else "s"}</title></rect>')
        if row == 0 and day.month != last_month and col < 51:
            months.append(f'<text x="{x}" y="{y0 - 10}" class="l" style="font-size:11px">{day.strftime("%b")}</text>')
            last_month = day.month
    for row, label in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        months.append(f'<text x="{x0 - 10}" y="{y0 + row * (size + gap) + 10}" class="l" text-anchor="end" style="font-size:11px">{label}</text>')
    legend_x = 830 - 24 - 5 * 15 - 70
    legend = [f'<text x="{legend_x}" y="{y0 + 7 * (size + gap) + 22}" class="l" style="font-size:11px">Less</text>']
    for i, c in enumerate(HEAT):
        legend.append(f'<rect x="{legend_x + 32 + i * 15}" y="{y0 + 7 * (size + gap) + 12}" width="11" height="11" rx="2" fill="{c}"/>')
    legend.append(f'<text x="{legend_x + 32 + 5 * 15 + 4}" y="{y0 + 7 * (size + gap) + 22}" class="l" style="font-size:11px">More</text>')
    body = "\n".join(months + cells + legend)
    return card(830, 200, "Commits · last 12 months", body, "Commit heatmap for the last 12 months")


def main() -> int:
    data = None
    if TOKEN:
        data = fetch()
    else:
        print("GITHUB_TOKEN not set: writing placeholder cards", file=sys.stderr)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "stats.svg").write_text(stats_svg(data), encoding="utf-8")
    (OUT / "top-langs.svg").write_text(langs_svg(data), encoding="utf-8")
    (OUT / "streak.svg").write_text(streak_svg(data), encoding="utf-8")
    (OUT / "commits.svg").write_text(heatmap_svg(data), encoding="utf-8")
    print(f"Wrote cards to {OUT}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
