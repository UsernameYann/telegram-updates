import base64
import html
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

import requests

GH_TOKEN = os.environ["GH_TOKEN"]
STARS_TOKEN = os.environ.get("STARS_GH_TOKEN", GH_TOKEN)
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
MODEL_NAME = os.getenv("DIGEST_MODEL", "gpt-4o")
STARS_WINDOW_HOURS = int(os.getenv("STARS_WINDOW_HOURS", "8"))
REQUEST_TIMEOUT = 20
MAX_STARS = 10
TELEGRAM_CHUNK = 3800

GH_HEADERS = {
    "Authorization": f"Bearer {STARS_TOKEN}",
    "Accept": "application/vnd.github.star+json",
}
GH_HEADERS_README = {
    "Authorization": f"Bearer {STARS_TOKEN}",
    "Accept": "application/vnd.github+json",
}
AI_HEADERS = {"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json"}
AI_URL = "https://models.inference.ai.azure.com/chat/completions"

cutoff = datetime.now(timezone.utc) - timedelta(hours=STARS_WINDOW_HOURS)
today = datetime.now().strftime("%d/%m/%Y")
print(f"Checking stars since: {cutoff.isoformat()}")


def safe_get(url: str, headers: Dict, default: Any = None) -> Any:
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
        print(f"GET {url} -> {resp.status_code} {resp.text[:120]}")
    except requests.RequestException as exc:
        print(f"GET {url} -> {exc}")
    return default


def fetch_readme(owner: str, repo: str) -> str:
    data = safe_get(f"https://api.github.com/repos/{owner}/{repo}/readme", GH_HEADERS_README)
    if not data or "content" not in data:
        return ""
    try:
        return base64.b64decode(data["content"]).decode("utf-8", errors="ignore")[:2500]
    except Exception:
        return ""


def summarize_repo(repo_data: Dict) -> str:
    full_name: str = repo_data.get("full_name", "?")
    description: str = repo_data.get("description") or ""
    language: str = repo_data.get("language") or "inconnu"
    stars: int = repo_data.get("stargazers_count", 0)
    topics: str = ", ".join(repo_data.get("topics", [])[:8])

    owner, repo = (full_name.split("/", 1) if "/" in full_name else ("?", full_name))
    readme = fetch_readme(owner, repo)

    context = f"Repo: {full_name}\nLanguage: {language}\nStars: {stars}\nDescription: {description}"
    if topics:
        context += f"\nTopics: {topics}"
    if readme:
        context += f"\n\nREADME (extrait):\n{readme}"

    messages = [
        {
            "role": "system",
            "content": (
                "Tu analyses les nouveaux favoris GitHub d'un développeur.\n"
                "Retourne un bloc Telegram HTML en français, compact mais utile, sans invention.\n"
                "Règles :\n"
                "- 1 seul bloc, pas de markdown, HTML simple uniquement (<b>, <i>)\n"
                "- Ligne titre : [emoji pertinent] <b>owner/repo</b> — titre court\n"
                "- Ligne description : commence par '• ' (phrase courte, max 18 mots, ce que c'est)\n"
                "- Ligne detail optionnelle : commence par '• ' (max 18 mots)\n"
                "- Interdit de faire un paragraphe continu\n"
                "- Si le README est insuffisant, rester factuel et ne pas inventer\n"
                "- Style : direct, explicatif, utile pour comprendre le projet\n"
            ),
        },
        {"role": "user", "content": context},
    ]

    payload = {"model": MODEL_NAME, "messages": messages, "max_tokens": 250, "temperature": 0.2}

    try:
        resp = requests.post(AI_URL, headers=AI_HEADERS, json=payload, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            text = resp.json()["choices"][0]["message"]["content"].strip()
            return re.sub(r"[ \t]+", " ", text).strip()
        print(f"AI {resp.status_code} for {full_name}")
    except Exception as exc:
        print(f"AI error for {full_name}: {exc}")

    return (
        f"⭐ <b>{html.escape(full_name)}</b>\n"
        f"• {html.escape(description or 'Pas de description.')}\n"
        f"• {language} — {stars} ⭐"
    )


def split_for_telegram(message: str, max_len: int = TELEGRAM_CHUNK) -> List[str]:
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0
    for line in message.split("\n"):
        add_len = len(line) + 1
        if current and current_len + add_len > max_len:
            chunks.append("\n".join(current))
            current = [line]
            current_len = add_len
        else:
            current.append(line)
            current_len += add_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def send_telegram(text: str) -> None:
    for chunk in split_for_telegram(text):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "HTML"},
                timeout=REQUEST_TIMEOUT,
            )
            print(f"Telegram: {r.status_code}")
        except requests.RequestException as exc:
            print(f"Telegram error: {exc}")


# --- Main ---
starred_raw = safe_get(
    "https://api.github.com/user/starred?sort=created&direction=desc&per_page=30",
    GH_HEADERS,
    [],
)

if not starred_raw:
    print("Aucun résultat de l'API starred.")
    sys.exit(0)

# Filtre par starred_at (comme SFL filtre par merged_at)
new_repos: List[Dict] = []
for item in starred_raw:
    starred_at_str = item.get("starred_at", "")
    repo_data = item.get("repo", item)
    try:
        starred_at = datetime.fromisoformat(starred_at_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        continue
    if starred_at > cutoff:
        new_repos.append(repo_data)

new_repos = new_repos[:MAX_STARS]
print(f"Favoris des {STARS_WINDOW_HOURS}h: {len(new_repos)}")

if not new_repos:
    print("Aucun nouveau favori — rien envoyé.")
    sys.exit(0)

summaries: List[str] = []
for repo_data in new_repos:
    name = repo_data.get("full_name", "?")
    print(f"Traitement: {name}")
    summaries.append(summarize_repo(repo_data))

count = len(new_repos)
header = f"📌 <b>Nouveaux favoris GitHub — {today}</b> ({count} repo{'s' if count > 1 else ''})\n"
message = header + "\n\n".join(summaries)

send_telegram(message)
print("✅ Done")
