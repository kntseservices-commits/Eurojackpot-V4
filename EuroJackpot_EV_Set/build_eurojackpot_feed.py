#!/usr/bin/env python3
"""Build a same-origin Eurojackpot feed for Drawline.

Source: Veikkaus (Finnish state operator) public draw-results API.
  https://www.veikkaus.fi/api/draw-results/v1/games/EJACKPOT/draws/by-week/YYYY-Www
That API is key-free but does not send CORS headers, so the browser cannot call it
directly. We fetch it server-side and write a normalized, same-origin JSON file that
Drawline can poll: eurojackpot.json
"""
import datetime as dt
import re
import json
import urllib.request

WEEKS = 30
OUT = "/home/user/workspace/lottery-viewer/eurojackpot.json"
API = "https://www.veikkaus.fi/api/draw-results/v1/games/EJACKPOT/draws/by-week/{}"


def fetch(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Drawline/1.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def cents(v):
    return round(v / 100, 2) if isinstance(v, (int, float)) else None


def main():
    today = dt.date.today()
    draws = []
    seen = set()
    for back in range(WEEKS):
        d = today - dt.timedelta(weeks=back)
        iso = d.isocalendar()
        week = "%d-W%02d" % (iso[0], iso[1])
        try:
            payload = fetch(API.format(week))
        except Exception as exc:  # noqa: BLE001
            print("skip", week, exc)
            continue
        for raw in payload or []:
            res = (raw.get("results") or [{}])[0]
            main_nums = [int(n) for n in res.get("primary") or []]
            if not main_nums:
                continue
            did = raw.get("id")
            if did in seen:
                continue
            seen.add(did)
            tiers = {t.get("id"): t for t in raw.get("prizeTiers") or []}
            top = tiers.get("1") or {}
            jackpots = {j.get("id"): j.get("amount") for j in raw.get("jackpots") or []}
            total_winners = sum((t.get("shareCount") or 0) for t in raw.get("prizeTiers") or [])
            draws.append({
                "id": "eurojackpot-%s" % did,
                "game": "eurojackpot",
                "gameName": "Eurojackpot",
                "drawnAt": dt.datetime.fromtimestamp(raw["drawTime"] / 1000, dt.timezone.utc).isoformat(),
                "numbers": main_nums,
                "extra": {"label": "Euro numbers", "numbers": [int(n) for n in res.get("secondary") or []]},
                "jackpot": cents(top.get("shareAmount")) or cents(jackpots.get("PRIMARY")),
                "jackpotWinners": top.get("shareCount") or 0,
                "totalWinners": total_winners,
                "currency": "EUR",
                "source": "veikkaus.fi",
            })
    draws.sort(key=lambda d: d["drawnAt"], reverse=True)
    out = {
        "game": "eurojackpot",
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "https://www.veikkaus.fi/api/draw-results/v1/games/EJACKPOT",
        "count": len(draws),
        "draws": draws,
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print("wrote", OUT, len(draws), "draws")

    # Inline the same feed into the single-file app so it works offline / off disk.
    html_path = "/home/user/workspace/lottery-viewer/index.html"
    html = open(html_path, encoding="utf-8").read()
    inline = json.dumps(out, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    new, n = re.subn(
        r'(<script type="application/json" id="ejFeed">)(.*?)(</script>)',
        lambda m: m.group(1) + inline + m.group(3),
        html, count=1, flags=re.S)
    if not n:
        raise SystemExit("ejFeed script tag not found in index.html")
    open(html_path, "w", encoding="utf-8").write(new)
    print("inlined feed into", html_path)
    if draws:
        print(json.dumps(draws[0], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
