import base64
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Set

import requests

GH_TOKEN = os.environ["GH_TOKEN"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
MODEL_NAME = os.getenv("DIGEST_MODEL", "gpt-4o")
MAX_FRESH = 3   # Nouveaux repos à forte vélocité
MAX_GEMS = 2    # Pépites oubliées / topics variés
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


def safe_get(url: str, params: Dict = None, retries: int = 2) -> Any:
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
        "Tu es un curateur GitHub qui partage des découvertes tech.\n"
        "Format Telegram HTML strict — 3 lignes uniquement :\n"
        "- Ligne 1 : [emoji] titre accrocheur (max 5 mots, PAS le nom du repo)\n"
        "- Ligne 2 : Ce que c'est (max 15 mots, factuel, pas de ponctuation finale)\n"
        "- Ligne 3 : <i>github.com/owner/repo</i>\n"
        "- Uniquement <i> autorisé. Pas de <b>, pas de •, pas d'autres balises.\n"
        "- Ne pas mentionner les stars ni l'âge du repo.\n"
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
        f"⭐ {html.escape(description[:60] if description else full_name)}\n"
        f"<i>github.com/{html.escape(full_name)}</i>"
    )


def send_telegram(text: str) -> bool:
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
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

# Pool B — Pépites oubliées / sujets variés
import random
gem_repos: Dict[int, Dict] = {}
gem_queries_shuffled = QUERIES_GEMS.copy()
random.shuffle(gem_queries_shuffled)  # shuffle pour varier les topics à chaque run
for query in gem_queries_shuffled:
    data = safe_get("https://api.github.com/search/repositories", {
        "q": query, "sort": "stars", "order": "desc", "per_page": 10
    })
    if data and "items" in data:
        for item in data["items"]:
            rid = item.get("id")
            if rid and rid not in seen_ids and rid not in fresh_repos and rid not in gem_repos:
                gem_repos[rid] = item
    time.sleep(1)
    if len(gem_repos) >= MAX_GEMS * 10:
        break  # assez de candidats

# Sélection finale
sorted_fresh = sorted(fresh_repos.values(), key=velocity_score, reverse=True)[:MAX_FRESH]
# Gems : tri par stars pour diversité, léger shuffle pour éviter toujours les mêmes topics
sorted_gems = sorted(gem_repos.values(), key=lambda r: r.get("stargazers_count", 0))[:MAX_GEMS * 5]
random.shuffle(sorted_gems)
selected_gems = sorted_gems[:MAX_GEMS]

print(f"Fresh trouvés: {len(fresh_repos)} → sélectionnés: {len(sorted_fresh)}")
print(f"Gems trouvés: {len(gem_repos)} → sélectionnés: {len(selected_gems)}")

if not sorted_fresh and not selected_gems:
    print("Aucune pépite trouvée.")
    sys.exit(0)

# Interleave : Fresh, Gem, Fresh, Gem, Fresh
selected: List[tuple] = []
fi, gi = 0, 0
while len(selected) < MAX_FRESH + MAX_GEMS:
    if fi < len(sorted_fresh):
        selected.append((sorted_fresh[fi], False))
        fi += 1
    if gi < len(selected_gems) and len(selected) < MAX_FRESH + MAX_GEMS:
        selected.append((selected_gems[gi], True))
        gi += 1
    if fi >= len(sorted_fresh) and gi >= len(selected_gems):
        break

sent = 0
for repo, is_gem in selected:
    full_name = repo.get("full_name", "?")
    age = days_old_label(repo)
    spd = velocity_score(repo)
    label = "💎 Gem" if is_gem else "🆕 Fresh"
    print(f"[{label}] {full_name} ({age}, {repo.get('stargazers_count', 0)}⭐)")
    summary = ai_summarize(repo, age, spd, is_gem=is_gem)
    if send_telegram(summary):
        seen_ids.add(repo["id"])
        sent += 1
    time.sleep(1)

save_seen(seen_ids)
print(f"✅ {sent}/{len(selected)} repos envoyés")
