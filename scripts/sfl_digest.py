import html
import os
import re
from typing import Any, Dict, List

import requests
from datetime import datetime, timezone, timedelta

GH_TOKEN = os.environ["GH_TOKEN"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GH_HEADERS = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
AI_HEADERS = {"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json"}
REQUEST_TIMEOUT = 20
MAX_PRS = 10
MAX_TOTAL_CHARS = 9000
TELEGRAM_CHUNK = 3800
MODEL_NAME = os.getenv("DIGEST_MODEL", "gpt-4o")
DIGEST_WINDOW_HOURS = int(os.getenv("DIGEST_WINDOW_HOURS", "8"))

REPO = "sunflower-land/sunflower-land"
GH_API = f"https://api.github.com/repos/{REPO}"
AI_URL = "https://models.inference.ai.azure.com/chat/completions"

EXCLUDED_PATH_PARTS = (
    "__snapshots__",
    ".test.",
    ".spec.",
    "docs/",
    "public/",
    "locales/",
    "metadata/",
)
EXCLUDED_EXTS = (
    ".md",
    ".lock",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".json",
)
PRIORITY_PATHS = (
    "src/features/",
    "src/game/",
    "src/lib/",
)

KEYWORD_MAP = {
    "economie": ["price", "cost", "sell", "buy", "reward", "balance", "sfl", "mint", "burn"],
    "progression": ["xp", "level", "boost", "skill", "upgrade", "quest"],
    "timing": ["cooldown", "seconds", "minutes", "hours", "readyAt", "duration", "delay"],
    "gameplay": ["harvest", "craft", "drop", "spawn", "resource", "recipe", "fish", "crop"],
    "stabilite": ["fix", "bug", "prevent", "guard", "null", "undefined", "error"],
}

CATEGORY_EMOJI = {
    "economie": "💰",
    "progression": "⬆️",
    "timing": "⏱️",
    "gameplay": "🎮",
    "stabilite": "🔧",
    "mise a jour de contenu": "✨",
}

cutoff = datetime.now(timezone.utc) - timedelta(hours=DIGEST_WINDOW_HOURS)
today = datetime.now().strftime("%d/%m/%Y")

def safe_json_get(url: str, headers: Dict[str, str], default):
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        print(f"GitHub GET error: {url} -> {exc}")
        return default

    if resp.status_code != 200:
        print(f"GitHub GET failed: {url} -> {resp.status_code} {resp.text[:180]}")
        return default

    try:
        return resp.json()
    except ValueError:
        print(f"GitHub GET invalid JSON: {url}")
        return default

def is_relevant_file(filename: str) -> bool:
    lower = filename.lower()
    if any(x in lower for x in EXCLUDED_PATH_PARTS):
        return False
    if lower.endswith(EXCLUDED_EXTS):
        return False
    if not lower.endswith((".ts", ".tsx", ".js", ".jsx")):
        return False
    return any(path in lower for path in PRIORITY_PATHS)

def extract_patch_signals(patch: str) -> List[str]:
    if not patch:
        return []

    signals = []
    for raw in patch.split("\n"):
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        line = raw[1:].strip()
        if not line or line.startswith("//") or line.startswith("*"):
            continue
        if line.startswith(("import ", "export ", "interface ", "type ")):
            continue
        if len(line) > 200:
            line = line[:197] + "..."
        signals.append(line)
        if len(signals) >= 20:
            break
    return signals

def classify_change(pr_title: str, patches: List[str]) -> str:
    corpus = (pr_title + " " + " ".join(patches)).lower()
    matched = []
    for label, words in KEYWORD_MAP.items():
        if any(word.lower() in corpus for word in words):
            matched.append(label)
    if not matched:
        return "mise a jour de contenu"
    if len(matched) == 1:
        return matched[0]
    return f"{matched[0]} + {matched[1]}"

def fallback_impact(pr_title: str, change_type: str) -> str:
    title = pr_title.lower()
    if any(k in title for k in ["fix", "bug", "patch"]):
        return "Stabilite amelioree: moins de blocages en jeu."
    if "balance" in title or "rebalance" in title:
        return "Ajustement d'equilibrage sur l'economie ou la progression."
    if "new" in title or "feat" in title or "add" in title:
        return "Nouveau contenu ou nouvelle interaction disponible en jeu."
    return f"Impact principal: {change_type}."


def call_ai(messages: List[Dict[str, str]], max_tokens: int) -> str:
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }

    try:
        resp = requests.post(AI_URL, headers=AI_HEADERS, json=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        print(f"Model API error ({MODEL_NAME}): {exc}")
        return ""

    if resp.status_code != 200:
        print(f"Model API failed ({MODEL_NAME}): {resp.status_code} {resp.text[:180]}")
        return ""

    try:
        text = resp.json()["choices"][0]["message"]["content"].strip()
    except (ValueError, KeyError, IndexError, TypeError):
        print(f"Model API invalid response ({MODEL_NAME})")
        return ""

    text = re.sub(r"[ \t]+", " ", text).strip()
    if text:
        print(f"Model used: {MODEL_NAME}")
    return text


def fallback_digest(prs: List[Dict[str, Any]]) -> str:
    if not prs:
        return f"<b>Sunflower Land — {today}</b>\nAucune PR mergee sur les {DIGEST_WINDOW_HOURS}h."

    lines = [f"🌻 <b>Sunflower Land — {today}</b> ({len(prs)} PR)", ""]
    for pr in prs:
        emoji = CATEGORY_EMOJI.get(str(pr["change_type"]).split(" + ")[0], "✨")
        files = html.escape(", ".join(pr["file_names"][:4]))
        impact = html.escape(fallback_impact(str(pr["title"]), str(pr["change_type"])))
        lines.append(f"{emoji} <b>#{pr['number']}</b> — {html.escape(str(pr['title']))}")
        lines.append(f"• {impact}")
        if files:
            lines.append(f"📁 <i>{files}</i>")
        lines.append("")
    return "\n".join(lines).strip()


def ai_generate_digest(prs: List[Dict[str, Any]]) -> str:
    if not prs:
        return f"<b>Sunflower Land — {today}</b>\nAucune PR mergee sur les {DIGEST_WINDOW_HOURS}h."

    context_parts = []
    for pr in prs:
        lines = [
            f"PR #{pr['number']} — {pr['title']}",
            f"Categorie: {pr['change_type']}",
        ]
        if pr["file_names"]:
            lines.append("Fichiers: " + ", ".join(pr["file_names"][:6]))
        if pr["patch_signals"]:
            lines.append("Preuves diff:")
            lines.extend(f"+ {signal}" for signal in pr["patch_signals"][:10])
        context_parts.append("\n".join(lines))

    messages = [
        {
            "role": "system",
            "content": (
                f"Tu analyses les mises a jour Sunflower Land sur les {DIGEST_WINDOW_HOURS} dernieres heures. "
                "Retourne un digest Telegram HTML en francais, compact mais utile, sans invention.\n"
                "Regles: \n"
                "- 1 seul bloc final, pas de markdown, HTML simple uniquement (<b>, <i>)\n"
                "- Commencer par: 🌻 <b>Sunflower Land — " + today + "</b> (X PR)\n"
                "- Ensuite 1 bloc par PR pertinente\n"
                "- Chaque bloc doit respecter exactement ce format:\n"
                "  1) ligne titre: [emoji] <b>#PR</b> — titre\n"
                "  2) ligne detail: commence par '• ' (phrase courte, max 18 mots)\n"
                "  3) ligne detail optionnelle: commence par '• ' (max 18 mots)\n"
                "  4) ligne fichiers: commence par '📁 ' puis fichiers en <i>italique</i>\n"
                "  5) ligne vide entre deux PR\n"
                "- Interdit de faire un gros paragraphe continu\n"
                "- Utiliser les chiffres exacts seulement s'ils sont presents dans les preuves\n"
                "- Si la preuve est insuffisante, rester factuel et ne pas inventer\n"
                "- Style: direct, explicatif, utile pour comprendre le fonctionnement du jeu\n"
                "- Taille totale max: 3200 caracteres"
            ),
        },
        {
            "role": "user",
            "content": "\n\n".join(context_parts),
        },
    ]

    text = call_ai(messages=messages, max_tokens=900)
    if not text:
        return fallback_digest(prs)
    return text

def split_for_telegram(message: str, max_len: int) -> List[str]:
    chunks = []
    current = []
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

# --- PRs mergees ---
prs_raw = safe_json_get(
    f"{GH_API}/pulls?state=closed&sort=updated&direction=desc&per_page=50",
    GH_HEADERS,
    [],
)

merged_prs = []
for p in prs_raw:
    merged_at = p.get("merged_at")
    if not merged_at:
        continue
    try:
        merged_dt = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
    except ValueError:
        continue
    if merged_dt > cutoff:
        merged_prs.append(p)

merged_prs = merged_prs[:MAX_PRS]
print(f"PRs mergees retenues: {len(merged_prs)}")

# --- Traitement par PR ---
digest_prs = []

for pr in merged_prs:
    pr_num = pr["number"]
    pr_title = pr["title"]

    files = safe_json_get(
        f"{GH_API}/pulls/{pr_num}/files?per_page=40",
        GH_HEADERS,
        [],
    )
    if not isinstance(files, list):
        files = []

    relevant = [f for f in files if is_relevant_file(f.get("filename", ""))]

    # PR sans fichier TypeScript pertinent = traduction/chore : pas d'IA
    if not relevant:
        title_lower = pr_title.lower()
        if any(k in title_lower for k in ["chore", "translat", "i18n", "locale", ".json", "bump", "typo"]):
            print(f"PR #{pr_num} ignoree (chore/traduction): {pr_title}")
            continue
        digest_prs.append(
            {
                "number": pr_num,
                "title": pr_title,
                "file_names": [],
                "patch_signals": [],
                "change_type": "mise a jour de contenu",
            }
        )
        continue

    focus = relevant[:6]

    file_names = [f.get("filename", "").split("/")[-1] for f in focus if f.get("filename")]

    patch_signals = []
    for f in focus:
        patch_signals.extend(extract_patch_signals(f.get("patch", "")))
        if len(patch_signals) >= 20:
            break

    change_type = classify_change(pr_title, patch_signals)
    digest_prs.append(
        {
            "number": pr_num,
            "title": pr_title,
            "file_names": file_names,
            "patch_signals": patch_signals,
            "change_type": change_type,
        }
    )

# --- Message final ---
if not digest_prs:
    print(f"Aucune PR pertinente sur les {DIGEST_WINDOW_HOURS}h — rien envoyé.")
    exit(0)

message = ai_generate_digest(digest_prs)

if len(message) > MAX_TOTAL_CHARS:
    message = message[: MAX_TOTAL_CHARS - 40].rstrip() + "\n... digest tronque"

# --- Envoi Telegram (HTML) ---
chunks = split_for_telegram(message, TELEGRAM_CHUNK)
for chunk in chunks:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "HTML"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        print(f"Telegram API error: {exc}")
        continue

    if r.status_code != 200:
        print(f"Telegram failed: {r.status_code} {r.text[:180]}")
    else:
        print(f"Telegram: {r.status_code}")

print("✅ Done")