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
WINDOW_HOURS = int(os.getenv("TRENDING_WINDOW_HOURS", "4"))
MAX_REPOS = 5
REQUEST_TIMEOUT = 20
STATE_FILE = "data/trending_seen.json"
MAX_SEEN = 500  # max IDs à garder en mémoire

GH_HEADERS = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
AI_HEADERS = {"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json"}
AI_URL = "https://models.inference.ai.azure.com/chat/completions"

today = datetime.now().strftime("%d/%m/%Y")
cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

# Topics à surveiller (AI/LLM/agents + général trending)
QUERIES = [
    f"topic:llm stars:>50 pushed:>={cutoff}",
    f"topic:ai-agent stars:>30 pushed:>={cutoff}",
    f"topic:generative-ai stars:>50 pushed:>={cutoff}",
    f"topic:large-language-model stars:>50 pushed:>={cutoff}",
    f"topic:rag stars:>30 pushed:>={cutoff}",
    f"topic:machine-learning created:>={cutoff} stars:>100",
    f"stars:>200 created:>={(datetime.now(timezone.utc) - timedelta(days=3)).strftime('%Y-%m-%d')}",
]


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
                print(f"GET {url} -> {exc} (retry {attempt + 1})")
                time.sleep(2)
            else:
                print(f"GET {url} -> abandon")
    return None


def fetch_readme(owner: str, repo: str) -> str:
    data = safe_get(f"https://api.github.com/repos/{owner}/{repo}/readme")
    if not data or "content" not in data:
        return ""
    try:
        return base64.b64decode(data["content"]).decode("utf-8", errors="ignore")[:2000]
    except Exception:
        return ""


def ai_summarize(repo: Dict) -> str:
    full_name = repo.get("full_name", "?")
    description = repo.get("description") or ""
    language = repo.get("language") or "inconnu"
    stars = repo.get("stargazers_count", 0)
    topics = ", ".join(repo.get("topics", [])[:8])
    owner, name = (full_name.split("/", 1) if "/" in full_name else ("?", full_name))
    readme = fetch_readme(owner, name)

    context = f"Repo: {full_name}\nLanguage: {language}\nStars: {stars:,}\nDescription: {description}"
    if topics:
        context += f"\nTopics: {topics}"
    if readme:
        context += f"\n\nREADME:\n{readme}"

    messages = [
        {
            "role": "system",
            "content": (
                "Tu es un curateur de repos GitHub populaires. Tu dois résumer un repo en français "
                "de façon concise, accrocheuse, prête à poster sur X (Twitter).\n"
                "Format Telegram HTML strict :\n"
                "- Ligne 1 : [emoji] <b>owner/repo</b> — titre accrocheur (max 10 mots)\n"
                "- Ligne 2 : • Ce que c'est en une phrase (max 18 mots)\n"
                "- Ligne 3 : • Ce qui le rend unique/intéressant (max 18 mots)\n"
                "- Ligne 4 : 🔗 <i>github.com/owner/repo</i>\n"
                "- Pas de markdown, HTML simple uniquement (<b>, <i>)\n"
                "- Style : direct, enthousiaste mais factuel\n"
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
                print(f"AI error {exc}, retry...")
                time.sleep(2)
            else:
                print(f"AI abandon: {exc}")

    return (
        f"⭐ <b>{html.escape(full_name)}</b>\n"
        f"• {html.escape(description or 'Pas de description.')}\n"
        f"• {language} — {stars:,} ⭐\n"
        f"🔗 <i>github.com/{html.escape(full_name)}</i>"
    )


def send_telegram(text: str) -> bool:
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": False},
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

# Collecte tous les repos depuis les différentes queries
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
        time.sleep(1)  # respecter le rate limit GitHub Search

if not all_repos:
    print("Aucun nouveau repo trouvé.")
    sys.exit(0)

# Tri par stars décroissant, on prend les MAX_REPOS meilleurs
sorted_repos = sorted(all_repos.values(), key=lambda r: r.get("stargazers_count", 0), reverse=True)
selected = sorted_repos[:MAX_REPOS]
print(f"Repos sélectionnés: {len(selected)}")

# Envoi : 1 message par repo
sent = 0
for repo in selected:
    full_name = repo.get("full_name", "?")
    print(f"Traitement: {full_name}")
    summary = ai_summarize(repo)
    if send_telegram(summary):
        seen_ids.add(repo["id"])
        sent += 1
    time.sleep(1)

save_seen(seen_ids)
print(f"✅ {sent}/{len(selected)} envoyés")
