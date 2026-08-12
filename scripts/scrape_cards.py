"""Scrape Duel Masters cards from db.duelmasters.us into data/cards.json.

Usage: python3 scripts/scrape_cards.py [set_code ...]   (default: dm-01)
"""
import json
import re
import sys
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

BASE = "https://db.duelmasters.us"
OUT = Path(__file__).resolve().parent.parent / "data" / "cards.json"


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "personal-dm-game/0.1"})
    with urllib.request.urlopen(req) as r:
        return r.read().decode("utf-8")


def list_card_ids(set_code: str) -> list[str]:
    html = fetch(f"{BASE}/search?card_set={set_code}")
    return re.findall(r'class="results-row" data-id="(\d+)"', html)


class CardViewParser(HTMLParser):
    """Pulls labelled <p><b>Field:</b> value</p> pairs and rules-text <li> items."""

    def __init__(self):
        super().__init__()
        self.fields: dict[str, str] = {}
        self.rules: list[str] = []
        self._label = None
        self._buf: list[str] = []
        self._in_rules = False
        self._in_li = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "ul" and attrs.get("id") == "rules-text":
            self._in_rules = True
        elif tag == "li" and self._in_rules:
            self._in_li = True
            self._buf = []

    def handle_endtag(self, tag):
        if tag == "ul" and self._in_rules:
            self._in_rules = False
        elif tag == "li" and self._in_li:
            self._in_li = False
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if text:
                self.rules.append(text)
        elif tag == "p" and self._label:
            value = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            self.fields[self._label] = value
            self._label = None

    def handle_data(self, data):
        if self._in_li:
            self._buf.append(data)
            return
        m = re.match(r"\s*([A-Za-z ]+):\s*$", data)
        if m and not self._label:
            self._label = m.group(1).strip().lower().replace(" ", "_")
            self._buf = []
        elif self._label:
            self._buf.append(data)


def scrape_card(card_id: str, set_code: str) -> dict:
    html = fetch(f"{BASE}/cardview/{card_id}")
    p = CardViewParser()
    p.feed(html)
    f = p.fields
    power = f.get("power", "")
    power_num = None
    if power:
        m = re.match(r"(\d+)", power)
        if m:
            power_num = int(m.group(1))
    return {
        "id": card_id,
        "set": set_code,
        "name": f.get("name", ""),
        "civilization": f.get("civilization", "").strip(),
        "type": f.get("card_type", ""),
        "cost": int(f["cost"]) if f.get("cost", "").isdigit() else None,
        "race": f.get("race") or None,
        "power": power_num,
        "power_text": power or None,  # keeps e.g. "6000+" for power attackers
        "rules_text": p.rules,
        "image": f"https://img.duelmasters.us/{card_id}.webp",
    }


def main():
    sets = sys.argv[1:] or ["dm-01"]
    cards = []
    if OUT.exists():
        cards = json.loads(OUT.read_text())
    have = {c["id"] for c in cards}
    for set_code in sets:
        ids = list_card_ids(set_code)
        print(f"{set_code}: {len(ids)} cards")
        for i, cid in enumerate(ids):
            if cid in have:
                continue
            cards.append(scrape_card(cid, set_code))
            time.sleep(0.3)  # be polite to the fan-run server
            if (i + 1) % 20 == 0:
                print(f"  {i + 1}/{len(ids)}")
    cards.sort(key=lambda c: int(c["id"]))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(cards, indent=1))
    print(f"wrote {len(cards)} cards -> {OUT}")


if __name__ == "__main__":
    main()
