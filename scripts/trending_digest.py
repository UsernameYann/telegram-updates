import base64
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Set

import requests

GH_TOKEN = os.environ["GH_TOKEN"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
MODEL_NAME = os.getenv("DIGEST_MODEL", "gpt-4o")
MAX_FRESH = 2   # Nouveaux repos à forte vélocité
MAX_VIRAL = 2   # GitHub Trending du jour (toutes dates)
MAX_GEMS = 1    # Pépite oubliée / topic varié
REQUEST_TIMEOUT = 20
STATE_FILE = "data/trending_seen.json"
MAX_SEEN = 2000

GH_HEADERS = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
AI_HEADERS = {"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json"}
AI_URL = "https://models.inference.ai.azure.com/chat/completions"

now = datetime.now(timezone.utc)
d1  = (now - timedelta(days=1)).strftime("%Y-%m-%d")
d3  = (now - timedelta(days=3)).strftime("%Y-%m-%d")
d7  = (now - timedelta(days=7)).strftime("%Y-%m-%d")
d30 = (now - timedelta(days=30)).strftime("%Y-%m-%d")

# Pool A — Nouveautés à fort momentum (triées par vélocité)
QUERIES_FRESH = [
    f"created:>={d1} stars:>20",
    f"created:>={d3} stars:>80",
    f"topic:llm created:>={d7} stars:>40",
    f"topic:ai-agent created:>={d7} stars:>20",
    f"topic:generative-ai created:>={d7} stars:>40",
    f"topic:rag created:>={d7} stars:>20",
    f"topic:large-language-model created:>={d7} stars:>40",
    f"created:>={d7} stars:>150",
]

# Pool B — Pépites oubliées / projets méconnus / sujets variés
# Cap à 60k stars pour éviter les incontournables (React, VS Code…)
# pushed:>=d30 → encore actifs
QUERIES_GEMS = [
    # Jeux & gamedev
    f"topic:game stars:300..60000 pushed:>={d30}",
    f"topic:game-engine stars:200..40000 pushed:>={d30}",
    f"topic:pixel-art stars:100..20000 pushed:>={d30}",
    # Terminal, CLI, TUI
    f"topic:cli stars:300..60000 pushed:>={d30}",
    f"topic:tui stars:200..40000 pushed:>={d30}",
    f"topic:terminal stars:300..50000 pushed:>={d30}",
    # Self-hosted / homelabs
    f"topic:self-hosted stars:500..60000 pushed:>={d30}",
    f"topic:homelab stars:200..30000 pushed:>={d30}",
    # Dev tools / productivité
    f"topic:developer-tools stars:300..60000 pushed:>={d30}",
    f"topic:productivity stars:300..50000 pushed:>={d30}",
    # Langages tendance
    f"topic:rust stars:500..60000 pushed:>={d30}",
    f"topic:golang stars:300..60000 pushed:>={d30}",
    f"topic:zig stars:100..20000 pushed:>={d30}",
    # Créatif / audio / art
    f"topic:creative-coding stars:200..30000 pushed:>={d30}",
    f"topic:music stars:200..30000 pushed:>={d30}",
    f"topic:animation stars:200..30000 pushed:>={d30}",
    # Sécurité / hacking éthique
    f"topic:security stars:500..60000 pushed:>={d30}",
    f"topic:hacking stars:300..40000 pushed:>={d30}",
    # Robotique / maker
    f"topic:robotics stars:200..30000 pushed:>={d30}",
    f"topic:arduino stars:200..30000 pushed:>={d30}",
]


def velocity_score(repo: Dict) -> float:
    """Stars par jour depuis la création — mesure la vélocité réelle."""
    stars = repo.get("stargazers_count", 0)
    created_at = repo.get("created_at", "")
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        days_old = max((now - created).total_seconds() / 86400, 0.5)
        return stars / days_old
    except Exception:
        return float(stars)


def days_old_label(repo: Dict) -> str:
    """Retourne '2 jours', '5 heures', etc."""
    created_at = repo.get("created_at", "")
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        delta = now - created
        hours = int(delta.total_seconds() / 3600)
        if hours < 24:
            return f"{hours}h"
        return f"{delta.days}j"
    except Exception:
        return "?"


def load_seen() -> Set[int]:
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen: Set[int]) -> None:
    os.makedirs("data", exist_ok=True)
    ids = list(seen)[-MAX_SEEN:]
    with open(STATE_FILE, "w") as f:
        json.dump(ids, f)


def fetch_github_trending(since: str = "daily") -> List[str]:
    """Scrape github.com/trending et retourne une liste de 'owner/repo' triée par stars aujourd'hui."""
    url = f"https://github.com/trending?since={since}"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; trending-digest-bot/1.0)"}
    # Préfixes GitHub à exclure (pas des repos)
    excluded = {
        "trending", "sponsors", "apps", "features", "about", "login", "join",
        "marketplace", "topics", "collections", "orgs", "users", "settings",
        "resources", "solutions", "enterprise", "pricing", "contact", "security",
        "open-source", "readme", "discussions", "pulls", "issues", "codespaces",
        "packages", "actions", "projects", "wiki", "explore", "new", "search",
    }
    for attempt in range(2):
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                print(f"Trending page: {resp.status_code}")
                return []
            # Extrait tous les liens /owner/repo (format alphanum + tirets/points)
            raw = re.findall(r'href="/([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)"', resp.text)
            seen_slugs: set = set()
            results = []
            for slug in raw:
                owner = slug.split("/")[0].lower()
                if owner in excluded or slug in seen_slugs:
                    continue
                seen_slugs.add(slug)
                results.append(slug)
                if len(results) >= 25:
                    break
            print(f"Trending scraped: {len(results)} repos")
            return results
        except Exception as exc:
            if attempt == 0:
                time.sleep(2)
            else:
                print(f"Trending scrape abandon: {exc}")
    return []


def safe_get(url: str, params: Optional[Dict] = None, retries: int = 2) -> Any:
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=GH_HEADERS, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            print(f"GET {url} -> {resp.status_code} {resp.text[:120]}")
            return None
        except requests.RequestException as exc:
            if attempt < retries:
                print(f"Retry {attempt + 1}: {exc}")
                time.sleep(2)
            else:
                print(f"Abandon: {exc}")
    return None


def fetch_readme(owner: str, repo: str) -> str:
    data = safe_get(f"https://api.github.com/repos/{owner}/{repo}/readme")
    if not data or "content" not in data:
        return ""
    try:
        return base64.b64decode(data["content"]).decode("utf-8", errors="ignore")[:2000]
    except Exception:
        return ""


def ai_summarize(repo: Dict, age: str, stars_per_day: float, is_gem: bool = False) -> str:
    full_name = repo.get("full_name", "?")
    description = repo.get("description") or ""
    language = repo.get("language") or "inconnu"
    stars = repo.get("stargazers_count", 0)
    topics = ", ".join(repo.get("topics", [])[:8])
    owner, name = (full_name.split("/", 1) if "/" in full_name else ("?", full_name))
    readme = fetch_readme(owner, name)

    context = (
        f"Repo: {full_name}\n"
        f"Âge: {age}\n"
        f"Stars: {stars:,}\n"
        f"Language: {language}\n"
        f"Description: {description}"
    )
    if topics:
        context += f"\nTopics: {topics}"
    if readme:
        context += f"\n\nREADME:\n{readme}"

    system_prompt = (
        "Tu es un créateur de posts X pour un compte de curation GitHub en français.\n"
        "Format strict — 4 lignes :\n"
        "Ligne 1 : [emoji] owner/repo\n"
        "Ligne 2 : accroche forte (max 10-12 mots)\n"
        "Ligne 3 : Bénéfice concret en 1 phrase courte (max 15 mots)\n"
        "Ligne 4 : github.com/owner/repo #hashtag1 #hashtag2\n"
        "Règles : ton naturel et direct, pas corporate, 2 hashtags max pertinents en anglais, pas de balises HTML.\n"
        "Langue : TOUT le texte des lignes 1, 2 et 3 doit être en FRANÇAIS uniquement. Jamais d'anglais sauf le nom du repo et les hashtags.\n"
        "Interdit : zéro tiret '-' ou '—' dans les lignes 2 et 3.\n"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": context},
    ]
    payload = {"model": MODEL_NAME, "messages": messages, "max_tokens": 200, "temperature": 0.3}

    for attempt in range(2):
        try:
            resp = requests.post(AI_URL, headers=AI_HEADERS, json=payload, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                text = resp.json()["choices"][0]["message"]["content"].strip()
                return re.sub(r"[ \t]+", " ", text).strip()
            print(f"AI {resp.status_code} for {full_name}")
        except Exception as exc:
            if attempt == 0:
                time.sleep(2)
            else:
                print(f"AI abandon: {exc}")

    return (
        f"⭐ {html.escape(full_name)} — {html.escape(description[:80] if description else 'Pas de description.')}\n"
        f"github.com/{html.escape(full_name)}"
    )


def send_telegram(text: str) -> bool:
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
                timeout=REQUEST_TIMEOUT,
            )
            print(f"Telegram: {r.status_code}")
            return r.status_code == 200
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2)
            else:
                print(f"Telegram abandon: {exc}")
    return False


# --- Main ---
seen_ids = load_seen()
print(f"IDs déjà vus: {len(seen_ids)}")

import random

# Pool A — Nouveautés à forte vélocité
fresh_repos: Dict[int, Dict] = {}
for query in QUERIES_FRESH:
    data = safe_get("https://api.github.com/search/repositories", {
        "q": query, "sort": "stars", "order": "desc", "per_page": 20
    })
    if data and "items" in data:
        for item in data["items"]:
            rid = item.get("id")
            if rid and rid not in seen_ids and rid not in fresh_repos:
                fresh_repos[rid] = item
    time.sleep(1)

# Pool B — GitHub Trending du jour (toutes dates confondues)
viral_repos: Dict[int, Dict] = {}
trending_slugs = fetch_github_trending(since="daily")
for slug in trending_slugs:
    if len(viral_repos) >= MAX_VIRAL * 5:
        break
    repo_data = safe_get(f"https://api.github.com/repos/{slug}")
    if repo_data:
        rid = repo_data.get("id")
        if rid and rid not in seen_ids and rid not in fresh_repos and rid not in viral_repos:
            viral_repos[rid] = repo_data
    time.sleep(0.5)

# Pool C — Pépites oubliées / sujets variés
gem_repos: Dict[int, Dict] = {}
gem_queries_shuffled = QUERIES_GEMS.copy()
random.shuffle(gem_queries_shuffled)
for query in gem_queries_shuffled:
    data = safe_get("https://api.github.com/search/repositories", {
        "q": query, "sort": "stars", "order": "desc", "per_page": 10
    })
    if data and "items" in data:
        for item in data["items"]:
            rid = item.get("id")
            if rid and rid not in seen_ids and rid not in fresh_repos and rid not in viral_repos and rid not in gem_repos:
                gem_repos[rid] = item
    time.sleep(1)
    if len(gem_repos) >= MAX_GEMS * 10:
        break

# Sélection finale
sorted_fresh = sorted(fresh_repos.values(), key=velocity_score, reverse=True)[:MAX_FRESH]
# Viral : GitHub Trending trie déjà par stars du jour → on garde l'ordre
sorted_viral = list(viral_repos.values())[:MAX_VIRAL]
# Gems : shuffle pour varier les topics
sorted_gems_pool = sorted(gem_repos.values(), key=lambda r: r.get("stargazers_count", 0))[:MAX_GEMS * 5]
random.shuffle(sorted_gems_pool)
selected_gems = sorted_gems_pool[:MAX_GEMS]

print(f"Fresh: {len(fresh_repos)} trouvés → {len(sorted_fresh)} sélectionnés")
print(f"Viral: {len(viral_repos)} trouvés → {len(sorted_viral)} sélectionnés")
print(f"Gems:  {len(gem_repos)} trouvés → {len(selected_gems)} sélectionnés")

if not sorted_fresh and not sorted_viral and not selected_gems:
    print("Aucune pépite trouvée.")
    sys.exit(0)

# Interleave : Fresh, Viral, Fresh, Viral, Gem
CATEGORY_LABEL = {0: "🆕 Fresh", 1: "🔥 Viral", 2: "💎 Gem"}
pools = [
    [(r, "fresh") for r in sorted_fresh],
    [(r, "viral") for r in sorted_viral],
    [(r, "gem")   for r in selected_gems],
]
order = [0, 1, 0, 1, 2]  # Fresh, Viral, Fresh, Viral, Gem
counters = [0, 0, 0]
selected: List[tuple] = []
for pool_idx in order:
    pool = pools[pool_idx]
    c = counters[pool_idx]
    if c < len(pool):
        selected.append(pool[c])
        counters[pool_idx] += 1

# Fallback : si moins de 5, compléter avec Fresh puis Gems
total = MAX_FRESH + MAX_VIRAL + MAX_GEMS
for pool_idx in [0, 2, 0, 2]:
    if len(selected) >= total:
        break
    pool = pools[pool_idx]
    c = counters[pool_idx]
    if c < len(pool):
        selected.append(pool[c])
        counters[pool_idx] += 1

sent = 0
for repo, category in selected:
    full_name = repo.get("full_name", "?")
    age = days_old_label(repo)
    spd = velocity_score(repo)
    is_gem = category == "gem"
    print(f"[{category.upper()}] {full_name} ({age}, {repo.get('stargazers_count', 0)}⭐)")
    summary = ai_summarize(repo, age, spd, is_gem=is_gem)
    if send_telegram(summary):
        seen_ids.add(repo["id"])
        sent += 1
    time.sleep(1)

save_seen(seen_ids)
print(f"✅ {sent}/{len(selected)} repos envoyés")
