import html
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

GH_TOKEN = os.environ["GH_TOKEN"]
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
MODEL_NAME = os.getenv("DIGEST_MODEL", "gpt-4o")

REQUEST_TIMEOUT = 20
REPO = os.getenv("SFL_REPO", "sunflower-land/sunflower-land")
MAX_RELEASES = max(1, int(os.getenv("SFL_RELEASE_MAX", "3")))
MAX_PRS_PER_RELEASE = max(1, int(os.getenv("SFL_RELEASE_PRS", "12")))
MAX_X_POSTS = max(1, int(os.getenv("SFL_X_MAX_POSTS", "3")))
TELEGRAM_CHUNK = 3800
STATE_FILE = "data/sfl_release_seen.json"
MAX_SEEN = 200

GH_HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
}
AI_HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Content-Type": "application/json",
}
AI_URL = "https://models.inference.ai.azure.com/chat/completions"

KEYWORD_WEIGHTS = {
    "economy": ["price", "cost", "sell", "buy", "reward", "balance", "mint", "burn", "sfl"],
    "progression": ["xp", "level", "boost", "skill", "upgrade", "quest"],
    "timing": ["cooldown", "duration", "delay", "readyAt", "hours", "minutes", "seconds"],
    "gameplay": ["harvest", "craft", "drop", "spawn", "resource", "recipe", "fish", "crop", "trade"],
    "stability": ["fix", "bug", "prevent", "guard", "error", "crash", "null", "undefined"],
}

LOW_SIGNAL_WORDS = ["chore", "typo", "docs", "readme", "test", "refactor", "lint"]
HIGH_SIGNAL_WORDS = ["feat", "feature", "rebalance", "economy", "trade", "reward", "craft", "harvest"]

PRIORITY_PATHS = ("src/features/", "src/game/", "src/lib/")


def safe_get(url: str, default: Any, params: Optional[Dict[str, Any]] = None, retries: int = 2) -> Any:
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=GH_HEADERS, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            print(f"GET {url} -> {resp.status_code} {resp.text[:120]}")
            return default
        except requests.RequestException as exc:
            if attempt < retries:
                print(f"GET retry {attempt + 1}/{retries}: {exc}")
                time.sleep(2)
            else:
                print(f"GET abandon: {exc}")
    return default


def call_ai(messages: List[Dict[str, str]], max_tokens: int = 1000, temperature: float = 0.2) -> str:
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    try:
        resp = requests.post(AI_URL, headers=AI_HEADERS, json=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        print(f"Model API error: {exc}")
        return ""

    if resp.status_code != 200:
        print(f"Model API failed: {resp.status_code} {resp.text[:160]}")
        return ""

    try:
        return str(resp.json()["choices"][0]["message"]["content"]).strip()
    except Exception:
        print("Model API invalid response")
        return ""


def load_seen() -> Set[str]:
    try:
        with open(STATE_FILE, "r") as f:
            raw = json.load(f)
        if isinstance(raw, list):
            return set(str(x) for x in raw)
    except Exception:
        pass
    return set()


def save_seen(tags: Set[str]) -> None:
    os.makedirs("data", exist_ok=True)
    vals = list(tags)[-MAX_SEEN:]
    with open(STATE_FILE, "w") as f:
        json.dump(vals, f)


def parse_pr_numbers_from_release(body: str) -> List[int]:
    if not body:
        return []
    nums = re.findall(r"\(#(\d+)\)", body)
    seen: Set[int] = set()
    ordered: List[int] = []
    for n in nums:
        val = int(n)
        if val in seen:
            continue
        seen.add(val)
        ordered.append(val)
        if len(ordered) >= MAX_PRS_PER_RELEASE:
            break
    return ordered


def is_priority_file(path: str) -> bool:
    lower = path.lower()
    if not lower.endswith((".ts", ".tsx", ".js", ".jsx")):
        return False
    return any(prefix in lower for prefix in PRIORITY_PATHS)


def extract_patch_signals(patch: str) -> List[str]:
    if not patch:
        return []
    out: List[str] = []
    for raw in patch.split("\n"):
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        line = raw[1:].strip()
        if not line:
            continue
        if line.startswith(("//", "*", "import ", "export ", "interface ", "type ")):
            continue
        if len(line) > 160:
            line = line[:157] + "..."
        out.append(line)
        if len(out) >= 18:
            break
    return out


def score_pr(title: str, body: str, files: List[Dict[str, Any]], signals: List[str]) -> Tuple[int, List[str]]:
    corpus = f"{title} {body} {' '.join(signals)}".lower()
    reasons: List[str] = []
    score = 0

    if any(w in corpus for w in HIGH_SIGNAL_WORDS):
        score += 2
        reasons.append("changement gameplay/economie visible")

    if any(w in corpus for w in LOW_SIGNAL_WORDS):
        score -= 2
        reasons.append("changement probablement mineur")

    changed_files = len(files)
    if changed_files >= 8:
        score += 1
        reasons.append("surface de changement large")

    priority_files = sum(1 for f in files if is_priority_file(str(f.get("filename", ""))))
    if priority_files >= 2:
        score += 1
        reasons.append("impacte des fichiers coeur du jeu")

    additions = sum(int(f.get("additions", 0) or 0) for f in files)
    deletions = sum(int(f.get("deletions", 0) or 0) for f in files)
    if additions + deletions >= 120:
        score += 1
        reasons.append("volume de diff significatif")

    for category, words in KEYWORD_WEIGHTS.items():
        if any(word in corpus for word in words):
            score += 1
            reasons.append(f"indices {category}")
            break

    return score, reasons


def build_fallback_report(release: Dict[str, Any], analyzed: List[Dict[str, Any]], interesting: List[Dict[str, Any]]) -> str:
    tag = str(release.get("tag_name") or "?")
    name = str(release.get("name") or "Sans titre")
    date = str(release.get("published_at") or "")[:10]
    lines = [
        f"🚀 <b>Sunflower Land release: {html.escape(tag)}</b>",
        f"<i>{html.escape(name)} | {html.escape(date)}</i>",
        "",
        f"PR analysees: {len(analyzed)} | PR interessantes: {len(interesting)}",
        "",
    ]
    for pr in interesting[:MAX_X_POSTS]:
        lines.append(f"• <b>#{pr['number']}</b> {html.escape(pr['title'])}")
        lines.append(f"  Score: {pr['score']} | Pourquoi: {html.escape('; '.join(pr['reasons'][:2]) or 'signal utile')}.")
    if not interesting:
        lines.append("Aucune PR vraiment interessante aujourd'hui pour un post X.")
    return "\n".join(lines).strip()


def build_ai_report(release: Dict[str, Any], analyzed: List[Dict[str, Any]], interesting: List[Dict[str, Any]]) -> str:
    rel = {
        "tag": release.get("tag_name"),
        "name": release.get("name"),
        "published_at": release.get("published_at"),
        "url": release.get("html_url"),
    }
    compact = []
    for pr in analyzed:
        compact.append(
            {
                "number": pr["number"],
                "title": pr["title"],
                "score": pr["score"],
                "reasons": pr["reasons"][:3],
                "changed_files": pr["changed_files"],
                "additions": pr["additions"],
                "deletions": pr["deletions"],
                "signals": pr["signals"][:6],
                "body": pr["body"][:350],
            }
        )

    messages = [
        {
            "role": "system",
            "content": (
                "Tu es un analyste produit/jeu. Tu dois resumer une release GitHub de Sunflower Land en francais.\n"
                "Objectif: aider a publier sur X uniquement les updates vraiment interessantes.\n"
                "Format Telegram HTML strict (pas de markdown):\n"
                "1) Ligne titre: 🚀 <b>Release {tag}</b> — nom\n"
                "2) Ligne resume court (1 phrase)\n"
                "3) Section 'PR interessantes pour X'\n"
                "4) Pour chaque PR interessante (max 3):\n"
                "   - <b>#numero titre</b>\n"
                "   - • ce qui change concretement (max 18 mots)\n"
                "   - • impact joueur (max 18 mots)\n"
                "   - • pourquoi c'est publiable sur X (max 14 mots)\n"
                "5) Section 'Post X proposes' avec 1 a 3 posts FR (max 240 caracteres chacun)\n"
                "6) Si rien d'interessant, le dire explicitement\n"
                "Interdit d'inventer. Utilise seulement les preuves fournies."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "release": rel,
                    "analyzed_count": len(analyzed),
                    "interesting_count": len(interesting),
                    "prs": compact,
                },
                ensure_ascii=False,
            ),
        },
    ]
    return call_ai(messages, max_tokens=1200, temperature=0.2)


def split_for_telegram(message: str, max_len: int = TELEGRAM_CHUNK) -> List[str]:
    parts: List[str] = []
    current: List[str] = []
    current_len = 0
    for line in message.split("\n"):
        add_len = len(line) + 1
        if current and current_len + add_len > max_len:
            parts.append("\n".join(current))
            current = [line]
            current_len = add_len
        else:
            current.append(line)
            current_len += add_len
    if current:
        parts.append("\n".join(current))
    return parts


def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram non configure: message affiche dans les logs uniquement.")
        print(message)
        return

    for chunk in split_for_telegram(message):
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "HTML"},
                timeout=REQUEST_TIMEOUT,
            )
            print(f"Telegram: {resp.status_code}")
        except requests.RequestException as exc:
            print(f"Telegram error: {exc}")


def fetch_pr_details(pr_number: int) -> Optional[Dict[str, Any]]:
    pr = safe_get(f"https://api.github.com/repos/{REPO}/pulls/{pr_number}", None)
    if not pr:
        return None

    files = safe_get(f"https://api.github.com/repos/{REPO}/pulls/{pr_number}/files", [], params={"per_page": 100})
    if not isinstance(files, list):
        files = []

    signals: List[str] = []
    for f in files:
        signals.extend(extract_patch_signals(str(f.get("patch", ""))))
        if len(signals) >= 20:
            break

    title = str(pr.get("title") or "")
    body = str(pr.get("body") or "")
    score, reasons = score_pr(title, body, files, signals)

    return {
        "number": pr_number,
        "title": title,
        "body": body,
        "url": pr.get("html_url", ""),
        "changed_files": int(pr.get("changed_files", 0) or 0),
        "additions": int(pr.get("additions", 0) or 0),
        "deletions": int(pr.get("deletions", 0) or 0),
        "signals": signals,
        "score": score,
        "reasons": reasons,
    }


def main() -> None:
    seen = load_seen()
    releases = safe_get(
        f"https://api.github.com/repos/{REPO}/releases",
        [],
        params={"per_page": MAX_RELEASES},
    )
    if not isinstance(releases, list) or not releases:
        print("Aucune release disponible.")
        return

    to_process = [r for r in releases if str(r.get("tag_name") or "") not in seen]
    if not to_process:
        print("Aucune nouvelle release non analysee.")
        return

    for release in to_process:
        tag = str(release.get("tag_name") or "")
        name = str(release.get("name") or "")
        print(f"Analyse release: {tag} {name}")

        pr_numbers = parse_pr_numbers_from_release(str(release.get("body") or ""))
        if not pr_numbers:
            print(f"Aucune PR extraite de la release {tag}.")
            seen.add(tag)
            continue

        analyzed: List[Dict[str, Any]] = []
        for number in pr_numbers:
            detail = fetch_pr_details(number)
            if detail:
                analyzed.append(detail)
            time.sleep(0.3)

        analyzed.sort(key=lambda x: x["score"], reverse=True)
        interesting = [x for x in analyzed if x["score"] >= 2][:MAX_X_POSTS]

        ai_report = build_ai_report(release, analyzed, interesting)
        report = ai_report if ai_report else build_fallback_report(release, analyzed, interesting)
        send_telegram(report)

        seen.add(tag)
        time.sleep(1)

    save_seen(seen)
    print("✅ Done")


if __name__ == "__main__":
    main()
