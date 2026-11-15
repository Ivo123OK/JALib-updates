#!/usr/bin/env python3
"""
JALib - info o nowej wersji na kanal #changelog na Discordzie (przez webhook).

Uruchamiane przez GitHub Actions, gdy zmieni sie latest.json. Wysyla wiadomosc tylko wtedy,
gdy "latest" jest nowsze niz w poprzednim commicie.

Tresc: pole "discord" z latest.json (dokladny opis od Claude: tekst albo lista linii),
a gdy go nie ma - sekcja changelogu tej wersji. Na koncu link do pobrania.

Sekret: DISCORD_CHANGELOG_WEBHOOK - link webhooka kanalu #changelog.
"""
import json
import os
import subprocess
import sys
import urllib.request


def version_key(v):
    out = []
    for p in str(v).split("."):
        try:
            out.append(int("".join(c for c in p if c.isdigit()) or 0))
        except ValueError:
            out.append(0)
    return out


def previous_latest():
    try:
        raw = subprocess.check_output(["git", "show", "HEAD~1:latest.json"], stderr=subprocess.DEVNULL)
        return json.loads(raw).get("latest")
    except Exception:
        return None


def section(changelog, version):
    """Linie changelogu od '# version' do nastepnego '# '."""
    out, on = [], False
    for line in changelog or []:
        if line.startswith("# "):
            if on:
                break
            on = line[2:].split(" ")[0].strip() == version
            continue
        if on:
            out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return out


def to_markdown(lines):
    md = []
    for l in lines:
        if l.startswith("## "):
            md.append("**" + l[3:].strip() + "**")
        elif l.startswith("- "):
            md.append("• " + l[2:].strip())
        else:
            md.append(l)
    return "\n".join(md).strip()


def build_payload(data):
    v = data["latest"]
    detail = data.get("discord")
    if isinstance(detail, list):
        detail = "\n".join(detail)
    body = (detail or "").strip() or to_markdown(section(data.get("changelog"), v)) or "Nowa wersja JALib."
    link = data.get("download") or (data.get("downloads") or {}).get(v) or ""
    if link:
        body += f"\n\n⬇️ **[Pobierz JALib {v}]({link})**"
    if len(body) > 4000:
        body = body[:3990] + "…"
    return {
        "username": "JALib",
        "embeds": [{
            "title": f"📚 JALib {v}",
            "url": link or None,
            "description": body,
            "color": 0x57F287,
            "footer": {"text": "Fabric 1.21.4 • JALib"},
        }],
        "allowed_mentions": {"parse": []},
    }


def main():
    hook = os.environ.get("DISCORD_CHANGELOG_WEBHOOK", "").strip()
    with open("latest.json", encoding="utf-8") as f:
        data = json.load(f)
    v, prev = data.get("latest"), previous_latest()
    if not v or (prev and version_key(v) <= version_key(prev)):
        print(f"Wersja sie nie zwiekszyla ({prev} -> {v}) - nic nie wysylam.")
        return 0
    payload = build_payload(data)
    if not hook:
        print("Brak sekretu DISCORD_CHANGELOG_WEBHOOK - pomijam. Wiadomosc wygladalaby tak:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    req = urllib.request.Request(hook + ("&" if "?" in hook else "?") + "wait=true",
                                 data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "JALib-changelog/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        print("Wyslano na #changelog:", r.status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
