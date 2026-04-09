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
MAX_REPOS = 5
REQUEST_TIMEOUT = 20
STATE_FILE = "data/trending_seen.json"
MAX_SEEN = 2000
# Pas de cap sur les stars totales — la vélocité (stars/jour) fait le tri naturellement

GH_HEADERS = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
AI_HEADERS = {"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json"}
AI_URL = "https://models.inference.ai.azure.com/chat/completions"

now = datetime.now(timezone.utc)
d1 = (now - timedelta(days=1)).strftime("%Y-%m-%d")   # hier
d3 = (now - timedelta(days=3)).strftime("%Y-%m-%d")   # 3 jours
d7 = (now - timedelta(days=7)).strftime("%Y-%m-%d")   # 1 semaine

# Requêtes centrées sur la NOUVEAUTÉ — created: empêche les vieux repos d'apparaître
# La vélocité (stars/jour) fait le tri entre pépites et repos déjà connus
QUERIES = [
    # Repos tout frais (1 jour) qui gagnent déjà des stars
    f"created:>={d1} stars:>20",
    # Repos de 3 jours avec bonne traction
    f"created:>={d3} stars:>80",
    # Repos AI/LLM de la semaine
    f"topic:llm created:>={d7} stars:>40",
    f"topic:ai-agent created:>={d7} stars:>20",
    f"topic:generative-ai created:>={d7} stars:>40",
    f"topic:rag created:>={d7} stars:>20",
    f"topic:large-language-model created:>={d7} stars:>40",
    # Général : créé cette semaine avec forte traction
    f"created:>={d7} stars:>150",
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


def ai_summarize(repo: Dict, age: str, stars_per_day: float) -> str:
    full_name = repo.get("full_name", "?")
    description = repo.get("description") or ""
    language = repo.get("language") or "inconnu"
    stars = repo.get("stargazers_count", 0)
    topics = ", ".join(repo.get("topics", [])[:8])
    owner, name = (full_name.split("/", 1) if "/" in full_name else ("?", full_name))
    readme = fetch_readme(owner, name)

    context = (
        f"Repo: {full_name}\n"
        f"Créé il y a: {age}\n"
        f"Stars: {stars:,} (+{stars_per_day:.0f}/jour)\n"
        f"Language: {language}\n"
        f"Description: {description}"
    )
    if topics:
        context += f"\nTopics: {topics}"
    if readme:
        context += f"\n\nREADME:\n{readme}"

    messages = [
        {
            "role": "system",
            "content": (
                "Tu es un chasseur de pépites GitHub. Tu trouves des repos prometteurs AVANT qu'ils deviennent célèbres.\n"
                "Ce repo est récent et gagne des stars rapidement — c'est une découverte à partager.\n"
                "Format Telegram HTML strict :\n"
                "- Ligne 1 : [emoji] <b>owner/repo</b> — titre accrocheur (max 10 mots)\n"
                "- Ligne 2 : • Ce que c'est (max 18 mots, factuel)\n"
                "- Ligne 3 : • Pourquoi c'est intéressant/unique (max 18 mots)\n"
                "- Ligne 4 : 🔗 <i>github.com/owner/repo</i>\n"
                "- Pas de markdown, HTML simple uniquement (<b>, <i>)\n"
                "- Style : enthousiaste, comme si tu partageais une découverte à un ami dev\n"
                "- Ne pas mentionner le nombre de stars\n"
            ),
        },
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
        f"⭐ <b>{html.escape(full_name)}</b>\n"
        f"• {html.escape(description or 'Pas de description.')}\n"
        f"• {language} — {stars:,} ⭐ — {age}\n"
        f"🔗 <i>github.com/{html.escape(full_name)}</i>"
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

all_repos: Dict[int, Dict] = {}
for query in QUERIES:
    data = safe_get("https://api.github.com/search/repositories", {
        "q": query, "sort": "stars", "order": "desc", "per_page": 20
    })
    if data and "items" in data:
        for item in data["items"]:
            rid = item.get("id")
            if rid and rid not in seen_ids and rid not in all_repos:
                all_repos[rid] = item
    time.sleep(1)  # rate limit GitHub Search API

if not all_repos:
    print("Aucune pépite trouvée.")
    sys.exit(0)

# Tri par vélocité (stars/jour) — pas par stars totales
sorted_repos = sorted(all_repos.values(), key=velocity_score, reverse=True)
selected = sorted_repos[:MAX_REPOS]

print(f"Pépites sélectionnées: {len(selected)}")
for r in selected:
    print(f"  {r['full_name']} — {r['stargazers_count']} ⭐ — vélocité: {velocity_score(r):.1f}/jour")

sent = 0
for repo in selected:
    full_name = repo.get("full_name", "?")
    age = days_old_label(repo)
    spd = velocity_score(repo)
    print(f"Traitement: {full_name} ({age}, +{spd:.0f}⭐/jour)")
    summary = ai_summarize(repo, age, spd)
    if send_telegram(summary):
        seen_ids.add(repo["id"])
        sent += 1
    time.sleep(1)

save_seen(seen_ids)
print(f"✅ {sent}/{len(selected)} pépites envoyées")
