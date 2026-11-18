#!/usr/bin/env python3
"""
fetch_contributions.py — refreshes the JSON that the cards are rendered from.

Writes:
    data/contributions.json   one entry per day for the last 12 months
    data/repos.json           live stars / forks / language for the repos in config.json

The contribution calendar is only available through GitHub's GraphQL API, and the
default GITHUB_TOKEN inside Actions cannot read another user's contributions. Set a
fine-grained PAT with the read:user scope as the repository secret GH_PAT.

Usage:
    GH_PAT=ghp_xxx python scripts/fetch_contributions.py
    python scripts/fetch_contributions.py --demo      # synthetic data, no token needed
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRAPHQL = "https://api.github.com/graphql"

CALENDAR_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount contributionLevel } }
      }
    }
  }
}
"""

REPO_QUERY = """
query($login: String!) {
  user(login: $login) {
    repositories(first: 100, ownerAffiliations: OWNER, orderBy: {field: PUSHED_AT, direction: DESC}) {
      nodes { name stargazerCount forkCount description primaryLanguage { name } }
    }
  }
}
"""

LEVELS = {"NONE": 0, "FIRST_QUARTILE": 1, "SECOND_QUARTILE": 2,
          "THIRD_QUARTILE": 3, "FOURTH_QUARTILE": 4}


def graphql(query: str, variables: dict, token: str) -> dict:
    payload = json.dumps({"query": query, "variables": variables}).encode()
    request = urllib.request.Request(
        GRAPHQL, data=payload,
        headers={"Authorization": f"bearer {token}",
                 "Content-Type": "application/json",
                 "User-Agent": "profile-cards-renderer"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        sys.exit(f"GitHub API returned {exc.code}: {exc.read().decode()[:400]}")
    except urllib.error.URLError as exc:
        sys.exit(f"could not reach the GitHub API: {exc.reason}")
    if "errors" in body:
        sys.exit(f"GraphQL error: {json.dumps(body['errors'])[:400]}")
    return body["data"]


def fetch_calendar(login: str, token: str) -> dict:
    to = datetime.now(timezone.utc)
    frm = to - timedelta(days=365)
    data = graphql(CALENDAR_QUERY, {"login": login,
                                    "from": frm.isoformat(),
                                    "to": to.isoformat()}, token)
    user = data.get("user")
    if not user:
        sys.exit(f"no such GitHub user: {login}")
    calendar = user["contributionsCollection"]["contributionCalendar"]
    days = [
        {"date": day["date"],
         "count": day["contributionCount"],
         "level": LEVELS.get(day["contributionLevel"], 0)}
        for week in calendar["weeks"] for day in week["contributionDays"]
    ]
    return {"user": login,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "github-graphql",
            "total": calendar["totalContributions"],
            "range": {"from": days[0]["date"], "to": days[-1]["date"]},
            "days": days}


def fetch_repos(login: str, token: str, wanted: list[str]) -> dict:
    data = graphql(REPO_QUERY, {"login": login}, token)
    nodes = data["user"]["repositories"]["nodes"]
    found = {n["name"]: n for n in nodes}
    out = {}
    for name in wanted:
        node = found.get(name)
        if not node:
            print(f"  note: {name} not returned by the API (private, renamed, or deleted) — "
                  f"config.json values kept", file=sys.stderr)
            continue
        out[name] = {"stars": node["stargazerCount"],
                     "forks": node["forkCount"],
                     "language": (node["primaryLanguage"] or {}).get("name"),
                     "description": node["description"]}
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "repos": out}


def demo_calendar(login: str) -> dict:
    """Plausible synthetic year so the cards can be rendered before the PAT exists."""
    rng = random.Random(7)
    today = date.today()
    start = today - timedelta(days=364)
    days, total = [], 0
    for offset in range(365):
        day = start + timedelta(days=offset)
        weekend = day.weekday() >= 5
        # Busier recently, quieter at weekends, with a few dead stretches.
        momentum = 0.45 + 0.55 * (offset / 364)
        if rng.random() < (0.42 if weekend else 0.12) / momentum:
            count = 0
        else:
            count = max(0, int(rng.gauss(7 * momentum, 4)))
        if 150 < offset < 168:
            count = 0
        total += count
        level = 0 if not count else 1 if count <= 3 else 2 if count <= 7 else 3 if count <= 13 else 4
        days.append({"date": day.isoformat(), "count": count, "level": level})
    return {"user": login,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "demo",
            "total": total,
            "range": {"from": days[0]["date"], "to": days[-1]["date"]},
            "days": days}


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh contribution and repo data.")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--out", default="data")
    parser.add_argument("--demo", action="store_true",
                        help="write synthetic data instead of calling the API")
    args = parser.parse_args()

    cfg = json.loads((ROOT / args.config).read_text())
    login = cfg["identity"]["handle"]
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.demo:
        calendar = demo_calendar(login)
        repos = None
    else:
        token = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN")
        if not token:
            sys.exit("set GH_PAT (a fine-grained PAT with read:user), or pass --demo")
        calendar = fetch_calendar(login, token)
        repos = fetch_repos(login, token, [r["name"] for r in cfg["repos"]])

    (out_dir / "contributions.json").write_text(json.dumps(calendar, indent=1) + "\n")
    print(f"  data/contributions.json  {calendar['total']:,} contributions · "
          f"{len(calendar['days'])} days · source={calendar['source']}")
    if repos:
        (out_dir / "repos.json").write_text(json.dumps(repos, indent=1) + "\n")
        print(f"  data/repos.json          {len(repos['repos'])} repos refreshed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
