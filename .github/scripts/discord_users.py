#!/usr/bin/env python3
"""
JALib - lista uzytkownikow na Discordzie.

Mod JALib (2.0.4+) po wejsciu na serwer wysyla przez webhook na kanal #uzytkownicy wiadomosc:
    jalib-user <nick> <wersja>
najwyzej raz dziennie na nick (gracz moze to wylaczyc w Ustawieniach moda).

Ten skrypt (co ~5 min w GitHub Actions):
  1. czyta nowe zgloszenia z kanalu,
  2. dopisuje / aktualizuje graczy (nick, wersja, pierwszy i ostatni raz) - stan trzymany tylko w samej
     wiadomosci z lista na Discordzie (nic nie trafia do publicznego repo),
  3. edytuje jedna (albo kilka, gdy lista jest dluga) wiadomosc bota z lista,
  4. kasuje pojedyncze zgloszenia, zeby na kanale zostala sama lista.

Sekrety: DISCORD_BOT_TOKEN, DISCORD_USERS_CHANNEL (ID kanalu #uzytkownicy).
Bot potrzebuje na tym kanale: Wyswietlanie kanalu, Wysylanie wiadomosci, Czytanie historii, Zarzadzanie wiadomosciami.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

API = "https://discord.com/api/v10"
ROW = re.compile(r"^`([A-Za-z0-9_]{3,16})` – (\d+(?:\.\d+){1,3}) · od (\d\d)\.(\d\d)\.(\d{4}) · ostatnio (\d\d)\.(\d\d)\.(\d{4})\s*$")
LINE = re.compile(r"^\s*jalib-user\s+([A-Za-z0-9_]{3,16})\s+(\d+(?:\.\d+){1,3})\s*$")
MAX_MSG = 1900

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Warsaw")
except Exception:
    TZ = timezone(timedelta(hours=1))


def api(method, path, token, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "Authorization": "Bot " + token,
        "User-Agent": "DiscordBot (https://github.com/Ivo123OK/JALib-updates, 1.0)",
        "Content-Type": "application/json",
    })
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                try:
                    wait = float(json.loads(e.read()).get("retry_after", 1))
                except Exception:
                    wait = 1.0
                time.sleep(min(wait, 10) + 0.3)
                continue
            raise


def local_date(ts: str) -> str:
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TZ)
    except Exception:
        d = datetime.now(TZ)
    return d.strftime("%Y-%m-%d")


def ver_key(v):
    return tuple(int(x) for x in v.split("."))


def apply_reports(state, messages):
    """messages: z API (dowolna kolejnosc). Zwraca (zmieniono?, id_do_skasowania)."""
    users = state.setdefault("users", {})
    changed, delete = False, []
    for m in sorted(messages, key=lambda x: x.get("timestamp", "")):
        if not m.get("webhook_id"):
            continue  # zwykle wiadomosci ludzi i bota zostawiamy
        delete.append(m["id"])
        hit = LINE.match(m.get("content") or "")
        if not hit:
            continue  # smieci z webhooka - tylko kasujemy
        nick, ver = hit.group(1), hit.group(2)
        day = local_date(m.get("timestamp", ""))
        key = nick.lower()
        u = users.get(key)
        if u is None:
            users[key] = {"nick": nick, "version": ver, "first": day, "last": day}
            changed = True
        else:
            before = dict(u)
            u["nick"] = nick
            if day >= u.get("last", ""):
                u["last"] = day
                u["version"] = ver
            if day < u.get("first", day):
                u["first"] = day
            changed |= before != u
    return changed, delete


def state_from_list(messages, bot_id):
    """Odczytuje graczy z wlasnych wiadomosci bota z lista (najstarsza pierwsza)."""
    mine = [m for m in messages if (m.get("author") or {}).get("id") == bot_id and not m.get("webhook_id")
            and (m.get("content") or "").startswith("📋")]
    mine.sort(key=lambda m: int(m["id"]))
    users = {}
    for m in mine:
        for line in (m.get("content") or "").split("\n"):
            r = ROW.match(line.strip())
            if r:
                nick = r.group(1)
                users[nick.lower()] = {"nick": nick, "version": r.group(2),
                                       "first": f"{r.group(5)}-{r.group(4)}-{r.group(3)}",
                                       "last": f"{r.group(8)}-{r.group(7)}-{r.group(6)}"}
    return {"users": users, "messages": [m["id"] for m in mine], "rendered": [m.get("content") for m in mine]}


def fmt_day(d):
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        return d


def render(state):
    users = sorted(state.get("users", {}).values(), key=lambda u: (u.get("first", ""), u["nick"].lower()))
    head = f"📋 **Użytkownicy JALib: {len(users)}**\n*nick – wersja · od kiedy · ostatnio*\n"
    lines = [f"`{u['nick']}` – {u['version']} · od {fmt_day(u['first'])} · ostatnio {fmt_day(u['last'])}" for u in users]
    if not lines:
        lines = ["*(jeszcze nikt)*"]
    chunks, cur = [], head
    for l in lines:
        if len(cur) + len(l) + 1 > MAX_MSG:
            chunks.append(cur.rstrip())
            cur = "📋 *(ciąg dalszy)*\n"
        cur += l + "\n"
    chunks.append(cur.rstrip())
    return chunks


def sync_list(token, channel, state, chunks):
    ids = list(state.get("messages", []))
    rendered = state.get("rendered", [])
    new_ids = []
    for i, text in enumerate(chunks):
        body = {"content": text, "allowed_mentions": {"parse": []}}
        mid = ids[i] if i < len(ids) else None
        if mid and i < len(rendered) and rendered[i] == text:
            new_ids.append(mid)
            continue
        if mid:
            try:
                api("PATCH", f"/channels/{channel}/messages/{mid}", token, body)
                new_ids.append(mid)
                continue
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
        msg = api("POST", f"/channels/{channel}/messages", token, body)
        new_ids.append(msg["id"])
        time.sleep(0.4)
    for extra in ids[len(chunks):]:
        try:
            api("DELETE", f"/channels/{channel}/messages/{extra}", token)
        except urllib.error.HTTPError:
            pass
    state["messages"] = new_ids
    state["rendered"] = chunks


def main():
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip().strip("\"'").strip()
    if token.lower().startswith("bot "):
        token = token[4:].strip()
    channel = re.sub(r"\D", "", os.environ.get("DISCORD_USERS_CHANNEL", "").strip().rstrip("/").split("/")[-1])
    if not token or not channel:
        print("Brak DISCORD_BOT_TOKEN albo DISCORD_USERS_CHANNEL - lista uzytkownikow pominieta.")
        return 0
    try:
        me = api("GET", "/users/@me", token)["id"]
        messages, before = [], None
        for _ in range(5):  # do 500 ostatnich wiadomosci
            q = "?limit=100" + (f"&before={before}" if before else "")
            page = api("GET", f"/channels/{channel}/messages{q}", token) or []
            messages += page
            if len(page) < 100:
                break
            before = page[-1]["id"]
    except urllib.error.HTTPError as e:
        hint = {401: "zly token bota", 403: "bot nie widzi kanalu #uzytkownicy (daj mu dostep)", 404: "zle ID kanalu w DISCORD_USERS_CHANNEL"}.get(e.code, "")
        print(f"Blad Discorda {e.code}: {hint}")
        return 1

    state = state_from_list(messages, me)
    changed, delete = apply_reports(state, messages)
    chunks = render(state)
    if changed or chunks != state.get("rendered") or not state.get("messages"):
        sync_list(token, channel, state, chunks)
        print(f"Lista: {len(state['users'])} graczy.")
    else:
        print("Lista bez zmian.")

    gone = 0
    for mid in delete:
        try:
            api("DELETE", f"/channels/{channel}/messages/{mid}", token)
            gone += 1
            time.sleep(0.3)
        except urllib.error.HTTPError as e:
            if e.code == 403:
                print("Bot nie moze kasowac zgloszen - daj mu uprawnienie 'Zarzadzanie wiadomosciami' na kanale #uzytkownicy.")
                break
    if delete:
        print(f"Skasowano {gone}/{len(delete)} zgloszen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
