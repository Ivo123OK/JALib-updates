#!/usr/bin/env python3
"""
JALib - powiadomienia z Discorda.

Czyta ostatnie wiadomosci z kanalu odpowiedzi na Discordzie i zapisuje je do notifications.json,
z ktorego mod JALib pokazuje powiadomienia w zakladce "Powiadomienia".

Format wiadomosci na kanale (moze byc kilka odpowiedzi w jednej wiadomosci):

    Odpowiedź do: *all*
    "Treść dla wszystkich graczy"
    Odpowiedź do: niedzwiadekubus
    "Treść tylko dla gracza niedzwiadekubus"

Bot dodaje reakcje: ✅ = wiadomosc trafila do moda, ❌ = nie znaleziono "Odpowiedź do:".
Usuniecie wiadomosci na Discordzie usuwa tez powiadomienie z moda (przy nastepnym przebiegu).

Zmienne srodowiskowe (sekrety w repozytorium na GitHubie):
    DISCORD_BOT_TOKEN        - token bota (Bot -> Reset Token w Discord Developer Portal)
    DISCORD_NOTIFY_CHANNEL   - ID kanalu z odpowiedziami
    DISCORD_ALLOWED_USERS    - (opcjonalnie) ID uzytkownikow, ktorzy moga wysylac powiadomienia, oddzielone przecinkami
    DISCORD_AUTHOR_NAME      - (opcjonalnie) podpis w modzie zamiast nicku z Discorda, np. Ivo123OK
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

API = "https://discord.com/api/v10"
MAX_NOTES = 100

HEADER = re.compile(
    r"^\s*(?:[>*_~`]+\s*)?odpowied[zź]\s+do\s*[:\-–]?\s*(.*?)\s*$",
    re.IGNORECASE,
)
ALL_WORDS = {"all", "everyone", "wszyscy", "wszystkich", "wszystkim"}
QUOTES = "\"'“”„«»‚‘’`"


def clean_target(raw: str) -> str:
    t = raw.strip()
    t = re.sub(r"^[*_~`>\s]+|[*_~`\s]+$", "", t)  # *all*, **nick**, `nick`
    t = t.strip(QUOTES).strip().lstrip("@").strip()
    t = t.split()[0] if t else ""
    if t.lower() in ALL_WORDS:
        return "all"
    return t


def clean_text(lines):
    # usun puste linie na poczatku i koncu
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""
    text = "\n".join(l.rstrip() for l in lines).strip()
    # cudzyslowy wokol calej tresci: "Treść" / „Treść”
    if len(text) >= 2 and text[0] in QUOTES and text[-1] in QUOTES:
        text = text[1:-1].strip()
    # blok cytatu discorda "> tekst"
    text = "\n".join(re.sub(r"^>\s?", "", l) for l in text.split("\n"))
    return text.strip()


def parse_message(content: str):
    """Zwraca liste (do_kogo, tresc) z jednej wiadomosci."""
    blocks = []
    cur_to, cur_lines = None, []
    for line in (content or "").replace("\r\n", "\n").split("\n"):
        m = HEADER.match(line)
        if m:
            if cur_to is not None:
                blocks.append((cur_to, cur_lines))
            rest = m.group(1)
            # "Odpowiedź do: nick "treść"" w jednej linii
            inline = ""
            q = re.search(r"[\"“„](.+)[\"”]\s*$", rest)
            if q:
                inline = q.group(1)
                rest = rest[: q.start()]
            cur_to, cur_lines = clean_target(rest), ([inline] if inline else [])
        elif cur_to is not None:
            cur_lines.append(line)
    if cur_to is not None:
        blocks.append((cur_to, cur_lines))
    out = []
    for to, lines in blocks:
        text = clean_text(list(lines))
        if to and text:
            out.append((to, text))
    return out


def iso_ms(ts: str) -> int:
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return int(time.time() * 1000)


def build_notes(messages, allowed=None, author_name=None):
    """messages: lista wiadomosci z API Discorda (najnowsze pierwsze). Zwraca (notes, statusy) gdzie statusy = {msg_id: True/False}."""
    notes, status = [], {}
    for msg in messages:
        a = msg.get("author") or {}
        if a.get("bot") or msg.get("webhook_id"):
            continue
        if allowed and str(a.get("id")) not in allowed:
            continue
        if msg.get("type", 0) not in (0, 19):  # zwykla wiadomosc albo odpowiedz
            continue
        parsed = parse_message(msg.get("content", ""))
        status[msg["id"]] = bool(parsed)
        who = author_name or (msg.get("member") or {}).get("nick") or a.get("global_name") or a.get("username") or "Autor"
        for i, (to, text) in enumerate(parsed):
            notes.append({
                "id": f"{msg['id']}-{i}",
                "to": to,
                "text": text[:1500],
                "author": who,
                "date": iso_ms(msg.get("timestamp", "")),
            })
    notes.sort(key=lambda n: n["date"], reverse=True)  # stabilne: kolejnosc w wiadomosci zostaje
    return notes[:MAX_NOTES], status


# ------------------------------------------------------------------ Discord API

def api(method, path, token, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "Authorization": "Bot " + token,
        "User-Agent": "DiscordBot (https://github.com/Ivo123OK/JALib-updates, 1.0)",
        "Content-Type": "application/json",
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                try:
                    wait = float(json.loads(e.read()).get("retry_after", 1))
                except Exception:
                    wait = 1.0
                time.sleep(min(wait, 10) + 0.2)
                continue
            raise


def react(token, channel, msg, ok):
    have = {r.get("emoji", {}).get("name") for r in (msg.get("reactions") or []) if r.get("me")}
    want, other = ("✅", "❌") if ok else ("❌", "✅")
    try:
        if other in have:
            api("DELETE", f"/channels/{channel}/messages/{msg['id']}/reactions/{urllib.parse.quote(other)}/@me", token)
        if want not in have:
            api("PUT", f"/channels/{channel}/messages/{msg['id']}/reactions/{urllib.parse.quote(want)}/@me", token)
            time.sleep(0.4)
    except urllib.error.HTTPError as e:
        print(f"Nie udalo sie dodac reakcji ({e.code}) - bot potrzebuje uprawnienia 'Add Reactions'.")


def main():
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip().strip("\"'").strip()
    if token.lower().startswith("bot "):
        token = token[4:].strip()
    channel = re.sub(r"\D", "", os.environ.get("DISCORD_NOTIFY_CHANNEL", "").strip().rstrip("/").split("/")[-1])
    if not token or not channel:
        print("Brak DISCORD_BOT_TOKEN albo DISCORD_NOTIFY_CHANNEL - pomijam (ustaw sekrety w repozytorium).")
        return 0
    allowed = {re.sub(r"\D", "", x) for x in os.environ.get("DISCORD_ALLOWED_USERS", "").split(",") if re.sub(r"\D", "", x)} or None
    author_name = os.environ.get("DISCORD_AUTHOR_NAME", "").strip().lstrip("=:").strip().strip("\"'").strip() or None
    try:
        messages = api("GET", f"/channels/{channel}/messages?limit=100", token) or []
    except urllib.error.HTTPError as e:
        if e.code == 401:
            looks = token.count(".") == 2 and len(token) > 50
            print("Blad Discorda 401: zly token bota. " + ("Token wyglada poprawnie - moze zostal zresetowany? Zrob Reset Token i wpisz nowy." if looks
                  else "To nie wyglada na token bota (powinien miec 2 kropki i ~70 znakow). Wez go z zakladki Bot -> Reset Token, nie Application ID / Public Key / Client Secret."))
            return 1
        hint = {401: "zly token bota", 403: "bot nie widzi kanalu (dodaj go na serwer i daj dostep do kanalu)", 404: "zle ID kanalu"}.get(e.code, "")
        print(f"Blad Discorda {e.code}: {hint}")
        return 1
    if messages and all(not m.get("content") for m in messages if not (m.get("author") or {}).get("bot")):
        print("UWAGA: wiadomosci nie maja tresci - wlacz 'Message Content Intent' w Discord Developer Portal (Bot).")
    notes, status = build_notes(messages, allowed, author_name)

    out_file = os.environ.get("NOTIFY_FILE", "notifications.json")
    old = []
    if os.path.exists(out_file):
        try:
            with open(out_file, encoding="utf-8") as f:
                old = json.load(f).get("notifications", [])
        except Exception:
            old = []
    if notes != old:
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump({"notifications": notes}, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"Zapisano {len(notes)} powiadomien.")
    else:
        print("Bez zmian.")

    by_id = {m["id"]: m for m in messages}
    for mid, ok in status.items():
        react(token, channel, by_id[mid], ok)
    return 0


if __name__ == "__main__":
    sys.exit(main())
