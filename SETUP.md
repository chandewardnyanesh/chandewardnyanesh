# Setup

This repo renders the animated SVG cards shown in the profile README.

## 1. Create the profile repo

The repo must be named exactly like the account, or GitHub will not show the README
on the profile page:

```bash
gh repo create chandewardnyanesh/chandewardnyanesh --public --description "Profile README"
git init && git branch -M main
git remote add origin git@github.com:chandewardnyanesh/chandewardnyanesh.git
```

Drop the contents of this folder in, then commit.

## 2. Add the portrait

Put a square image at `assets/portrait.jpg` (800×800 or larger, JPG, PNG or WebP).
It is embedded into `hero.svg` as base64, so keep it under ~120 KB — run it through
`squoosh` or:

```bash
# ~60 KB, plenty for a 132px circle on a 2× display
magick portrait-original.jpg -resize 400x400^ -gravity center -extent 400x400 -quality 82 assets/portrait.jpg
```

Without that file the hero falls back to a `DC` monogram, which also looks fine —
the fallback is not a placeholder you have to replace.

## 3. Add the token

The contribution calendar is only exposed through GitHub's GraphQL API, and the
`GITHUB_TOKEN` that Actions injects cannot read it. Create a fine-grained PAT with
the **read:user** scope, then:

```
Repo → Settings → Secrets and variables → Actions → New repository secret
Name: GH_PAT
```

Without it, the daily job fails at the fetch step and the cards keep the last
committed data.

## 4. Render locally

```bash
pip install 'fonttools[woff]'

python scripts/fetch_contributions.py --demo     # synthetic year, no token needed
GH_PAT=ghp_xxx python scripts/fetch_contributions.py   # the real thing

python scripts/render_cards.py                   # animated, into cards/
STATIC=1 python scripts/render_cards.py --out cards-static   # final frame
```

Open `cards/preview.html` to watch it play, or `cards-static/preview.html` to
screenshot the finished state. Both are laid out at 880px, the width of GitHub's
README column.

## 5. The daily job

`.github/workflows/profile-cards.yml` runs at 02:47 UTC (08:17 IST): fetch data →
render animated → render static → commit if anything changed. It also runs on any
push that touches `config.json`, `scripts/` or `assets/`, and from the Actions tab
via **Run workflow**.

## Editing content

Everything the cards say lives in `config.json` — name, one-liner, tile links, repo
list and blurbs, colours, and the per-card animation head-start under `stagger`.
Stars, forks and languages are overwritten each run from `data/repos.json`; the
blurbs stay hand-written, because the GitHub descriptions are worse copy.

To change the order of the stack, edit `README.md` and the `stagger` values together
so the cascade still runs top to bottom.

## Things worth knowing

- **Why fonts are embedded.** An SVG shown through an `<img>` tag cannot fetch
  anything — no external CSS, no Google Fonts. Each card therefore carries base64
  WOFF subsets containing only the glyphs that card actually uses, which is why they
  are 12–45 KB rather than 300 KB.
- **Why there is no `:hover`.** Same reason: an `<img>` document gets no pointer
  events. The card sheen is a one-shot sweep on load instead.
- **Camo caching.** GitHub proxies README images and caches them. After a push the
  new cards usually appear within minutes; if one looks stale, append a query string
  to that image's `src` in the README (`cards/hero.svg?v=2`) to force a re-fetch.
- **Fonts are OFL.** Inter and Fira Code ship under the SIL Open Font License, kept
  alongside them in `assets/fonts/`. Subsetting and embedding is permitted; keep the
  licence files in the repo.
