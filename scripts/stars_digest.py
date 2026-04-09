import base64
import html
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests

GH_TOKEN = os.environ["GH_TOKEN"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GH_USERNAME = os.environ.get("GH_USERNAME", "yanncarer")
STATE_FILE = os.environ.get("STARS_STATE_FILE", "data/stars_state.json")
WINDOW_HOURS = int(os.getenv("STARS_WINDOW_HOURS", "8"))
MODEL_NAME = os.getenv("DIGEST_MODEL", "gpt-4o")
REQUEST_TIMEOUT = 20
MAX_STARS = 10
TELEGRAM_CHUNK = 3800

GH_HEADERS_STAR = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github.star+json",
}
GH_HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
}
AI_HEADERS = {"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json"}
AI_URL = "https://models.inference.ai.azure.com/chat/completions"

today = datetime.now().strftime("%d/%m/%Y")


def safe_get(url: str, headers: Dict, default: Any = None) -> Any:
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
        print(f"GET {url} -> {resp.status_code} {resp.text[:120]}")
    except requests.RequestException as exc:
        print(f"GET {url} -> {exc}")
    return default


def load_state() -> Optional[datetime]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
                return datetime.fromisoformat(data["last_seen"])
        except Exception:
            pass
    return None


def save_state(dt: datetime) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump({"last_seen": dt.isoformat()}, f)


def fetch_readme(owner: str, repo: str) -> str:
    data = safe_get(f"https://api.github.com/repos/{owner}/{repo}/readme", GH_HEADERS)
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
    url: str = repo_data.get("html_url", f"https://github.com/{full_name}")

    owner, repo = (full_name.split("/", 1) if "/" in full_name else ("?", full_name))
    readme = fetch_readme(owner, repo)

    context = (
        f"Repo: {full_name}\nLanguage: {language}\nStars: {stars}\n"
        f"Description: {description}"
    )
    if topics:
        context += f"\nTopics: {topics}"
    if readme:
        context += f"\n\nREADME (extrait):\n{readme}"

    messages = [
        {
            "role": "system",
            "content": (
                "Tu aides un développeur à trier ses favoris GitHub. "
                "L'utilisateur est développeur sur Sunflower Land (jeu Web3 Play-to-Earn sur Polygon, TypeScript/React). "
                "Il utilise quotidiennement : GitHub Copilot CLI, Ollama (Mac M1 16GB), agents IA, outils Web3/Blockchain.\n\n"
                "Pour ce repo, génère une réponse Telegram HTML en français avec EXACTEMENT ce format (pas de markdown) :\n"
                "⭐ <b>{owner}/{repo}</b>\n"
                "• [1 phrase : qu'est-ce que c'est ?]\n"
                "• [1 phrase : est-ce utile pour son contexte ?]\n"
                "Verdict : [Garder ⭐ / Unstar 🗑️] — [raison courte en 5 mots max]\n\n"
                "Règles strictes : HTML simple uniquement (<b>, <i>), max 5 lignes, pas d'inventions."
            ),
        },
        {"role": "user", "content": context},
    ]

    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "max_tokens": 250,
        "temperature": 0.2,
    }

    try:
        resp = requests.post(AI_URL, headers=AI_HEADERS, json=payload, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            text = resp.json()["choices"][0]["message"]["content"].strip()
            return re.sub(r"[ \t]+", " ", text).strip()
        print(f"AI {resp.status_code} for {full_name}")
    except Exception as exc:
        print(f"AI error for {full_name}: {exc}")

    # Fallback sans IA
    return (
        f"⭐ <b>{html.escape(full_name)}</b>\n"
        f"• {html.escape(description or 'Pas de description.')}\n"
        f"• {language} — {stars} ⭐\n"
        f"Verdict : ❓ — analyse IA indisponible"
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
last_seen = load_state()
cutoff = last_seen if last_seen else (datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS))
print(f"Checking stars since: {cutoff.isoformat()}")

starred_raw = safe_get(
    f"https://api.github.com/users/{GH_USERNAME}/starred?sort=created&direction=desc&per_page=30",
    GH_HEADERS_STAR,
    [],
)

if not starred_raw:
    print("Aucun résultat de l'API starred.")
    sys.exit(0)

new_stars: List[Tuple[datetime, Dict]] = []
for item in starred_raw:
    starred_at_str = item.get("starred_at", "")
    repo_data = item.get("repo", item)
    try:
        starred_at = datetime.fromisoformat(starred_at_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        continue
    if starred_at > cutoff:
        new_stars.append((starred_at, repo_data))

new_stars = new_stars[:MAX_STARS]
print(f"Nouveaux favoris: {len(new_stars)}")

if not new_stars:
    print("Aucun nouveau favori — rien envoyé.")
    save_state(datetime.now(timezone.utc))
    sys.exit(0)

summaries: List[str] = []
for starred_at, repo_data in new_stars:
    name = repo_data.get("full_name", "?")
    print(f"Traitement: {name}")
    summaries.append(summarize_repo(repo_data))

count = len(new_stars)
header = f"📌 <b>Nouveaux favoris GitHub — {today}</b> ({count} repo{'s' if count > 1 else ''})\n"
message = header + "\n\n".join(summaries)

send_telegram(message)

most_recent = max(s[0] for s in new_stars)
save_state(most_recent)

print("✅ Done")
