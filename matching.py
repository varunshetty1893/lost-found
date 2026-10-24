"""Explainable matching between lost and found reports.

Matches are *suggestions only*. This module never changes a report, a claim or custody, and it
deliberately reads only PUBLIC fields (title, category, description, location, date). The private
identifying details of a report are never compared, so a match can not leak a hidden answer.

Initial weights (sum 100). They are starting values to be tuned with real data; pass a different
dict to score_pair()/find_matches() or edit DEFAULT_WEIGHTS.
"""
import re
from datetime import datetime

DEFAULT_WEIGHTS = {"category": 30, "name": 25, "location": 20, "date": 15, "description": 10}
MIN_SCORE = 40
STOP_WORDS = {"the", "and", "with", "for", "was", "has", "have", "near", "from", "this", "that", "its", "are",
              "not", "but", "inside", "left", "lost", "found", "item", "area", "side", "floor"}
REASON_TEXT = {"category": "Same category", "name": "Similar name", "location": "Same or nearby place",
               "date": "Compatible dates", "description": "Similar description"}


def words(text):
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2 and w not in STOP_WORDS}


def _dice(a, b):
    return 2 * len(a & b) / (len(a) + len(b)) if a and b else 0.0


def _overlap(a, b):
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def _date_points(lost_date, found_date, weight):
    try:
        lost = datetime.strptime(lost_date[:10], "%Y-%m-%d").date()
        found = datetime.strptime(found_date[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return 0
    gap = (found - lost).days
    if gap < -1:                       # found before it was lost: not compatible
        return 0
    gap = max(gap, 0)
    if gap <= 3:
        return weight
    if gap <= 14:
        return round(weight * 2 / 3)
    if gap <= 30:
        return round(weight / 3)
    return 0


def score_pair(lost, found, weights=None):
    """Return (score 0-100, [reason keys]). `lost` and `found` are rows/dicts with public fields."""
    w = weights or DEFAULT_WEIGHTS
    parts = {}
    parts["category"] = w["category"] if lost["category"] == found["category"] else 0
    parts["name"] = round(w["name"] * _dice(words(lost["title"]), words(found["title"])))
    parts["location"] = round(w["location"] * _overlap(words(lost["location"]), words(found["location"])))
    parts["date"] = _date_points(lost["event_date"], found["event_date"], w["date"])
    parts["description"] = round(w["description"] * _dice(words(lost["description"]), words(found["description"])))
    reasons = [k for k, v in parts.items() if v > 0]
    return sum(parts.values()), reasons


def is_actionable(item):
    """Only published, unfinished reports with no approved claim can appear as a match."""
    if item["status"] != "active":
        return False
    if item["type"] == "found":
        return item["custody_status"] not in ("returned", "disputed") and not item["approved_claim_id"]
    return True


def find_matches(db, item, limit=5, weights=None, min_score=MIN_SCORE):
    """Best opposite-type reports for `item`: [{'item': row, 'score': int, 'reasons': [text, ...]}]."""
    if not is_actionable(item):
        return []
    other = "found" if item["type"] == "lost" else "lost"
    rows = db.execute("SELECT * FROM items WHERE type = ? AND status = 'active' AND user_id != ? AND id != ? "
                      "ORDER BY id DESC LIMIT 300", (other, item["user_id"], item["id"])).fetchall()
    out = []
    for r in rows:
        if not is_actionable(r):
            continue
        lost, found = (item, r) if item["type"] == "lost" else (r, item)
        score, reasons = score_pair(lost, found, weights)
        if score >= min_score:
            out.append({"item": r, "score": score, "reasons": [REASON_TEXT[k] for k in reasons]})
    out.sort(key=lambda m: (-m["score"], -m["item"]["id"]))
    return out[:limit]
