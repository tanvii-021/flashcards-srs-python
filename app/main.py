from datetime import datetime, timezone

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import cache, db
from .schedule import (
    DEFAULT_EASE,
    DEFAULT_TZ,
    CardError,
    apply_answer,
    deck_summary,
    due_cards,
    is_repeat_in_same_day,
    queue_for,
    study_day,
    validate_grade,
)

app = FastAPI(title="flashcards-srs")

QUEUE_TTL = 36 * 60 * 60     # comfortably longer than one 4am-to-4am study day
DAILY_LIMIT = 40


@app.get("/health")
def health():
    out = {"status": "ok", "postgres": False, "redis": False}
    try:
        db.query("SELECT 1")
        out["postgres"] = True
    except Exception as e:
        out["pg_error"] = str(e)
    try:
        cache.client().ping()
        out["redis"] = True
    except Exception as e:
        out["redis_error"] = str(e)
    return out if out["postgres"] and out["redis"] else JSONResponse(out, status_code=503)


def _now():
    return datetime.now(timezone.utc)


def _queue_key(user, day):
    """The study day is IN the key. That is the whole trick: at 00:02 the key
    is still yesterday's, so the queue you are half way through does not get
    rebuilt under you."""
    return f"queue:{user}:{day.isoformat()}"


def _cards_for(user, deck_id=None):
    sql = ("SELECT c.id, c.deck_id, c.front, c.back, d.name AS deck,"
           " coalesce(p.repetitions,0) AS repetitions,"
           " coalesce(p.ease,%s) AS ease, coalesce(p.interval_days,0) AS interval,"
           " p.due_on, p.last_reviewed"
           " FROM cards c JOIN decks d ON d.id=c.deck_id"
           " LEFT JOIN progress p ON p.card_id=c.id AND p.user_name=%s"
           " WHERE TRUE")
    params = [DEFAULT_EASE, user]
    if deck_id:
        sql += " AND c.deck_id=%s"
        params.append(deck_id)
    sql += " ORDER BY c.id"
    return [{**r, "ease": float(r["ease"]), "interval": float(r["interval"])}
            for r in db.query(sql, tuple(params))]


@app.get("/decks")
def decks(user: str = "demo"):
    today = study_day(_now(), DEFAULT_TZ)
    rows = db.query("SELECT id, name, subject FROM decks ORDER BY id")
    out = []
    for d in rows:
        cards = _cards_for(user, d["id"])
        out.append({**d, **deck_summary(cards, today)})
    return {"user": user, "study_day": today.isoformat(), "decks": out}


@app.post("/study/{user}/start")
def start_study(user: str, deck_id: int = 0, limit: int = DAILY_LIMIT):
    """Build today's queue once, then pop from it as you revise.

    Redis earns its place here. Working out which cards are due is a join over
    every card and every piece of progress; doing it again for each card you
    answer is both slow and WRONG, because answering a card changes what is
    due and the list would reshuffle under you mid-session. So the queue is
    built once per study day, keyed by that study day, and popped from.
    """
    if limit < 1 or limit > 500:
        raise HTTPException(400, "limit must be between 1 and 500")
    today = study_day(_now(), DEFAULT_TZ)
    key = _queue_key(user, today)
    c = cache.client()
    existing = c.llen(key)
    if existing:
        return {"user": user, "study_day": today.isoformat(), "built": False,
                "remaining": existing,
                "note": "today's queue already exists - resuming it"}

    cards = _cards_for(user, deck_id or None)
    ids = queue_for(cards, today, limit)
    if not ids:
        raise HTTPException(404, "nothing is due today - come back tomorrow")
    c.rpush(key, *[str(i) for i in ids])
    c.expire(key, QUEUE_TTL)
    return {"user": user, "study_day": today.isoformat(), "built": True,
            "remaining": len(ids), "card_ids": ids}


@app.get("/study/{user}/next")
def next_card(user: str):
    """Peek at the next card without consuming it."""
    today = study_day(_now(), DEFAULT_TZ)
    key = _queue_key(user, today)
    cid = cache.client().lindex(key, 0)
    if cid is None:
        raise HTTPException(404, "no queue for today - POST /study/{user}/start first")
    row = db.one("SELECT c.id, c.front, c.back, d.name AS deck FROM cards c"
                 " JOIN decks d ON d.id=c.deck_id WHERE c.id=%s", (int(cid),))
    if not row:
        cache.client().lpop(key)
        raise HTTPException(404, "that card has been deleted; try again")
    return {"user": user, "study_day": today.isoformat(),
            "remaining": cache.client().llen(key), **row}


@app.post("/study/{user}/answer")
def answer(user: str, payload: dict = Body(...)):
    """Grade the card you were just shown, 0 to 5."""
    try:
        grade = validate_grade(payload.get("grade"))
    except CardError as e:
        raise HTTPException(400, str(e))
    card_id = payload.get("card_id")
    card = db.one("SELECT id, deck_id, front FROM cards WHERE id=%s", (card_id,))
    if not card:
        raise HTTPException(404, "no such card")

    now = _now()
    today = study_day(now, DEFAULT_TZ)
    prior = db.one("SELECT repetitions, ease, interval_days AS interval, due_on,"
                   " last_reviewed FROM progress WHERE user_name=%s AND card_id=%s",
                   (user, card["id"]))
    state = ({"repetitions": prior["repetitions"], "ease": float(prior["ease"]),
              "interval": float(prior["interval"])} if prior else {})
    last = prior["last_reviewed"] if prior else None

    result = apply_answer(state, grade, now, last, DEFAULT_TZ)

    db.query(
        "INSERT INTO progress (user_name, card_id, repetitions, ease, interval_days,"
        " due_on, last_reviewed) VALUES (%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (user_name, card_id) DO UPDATE SET"
        " repetitions=EXCLUDED.repetitions, ease=EXCLUDED.ease,"
        " interval_days=EXCLUDED.interval_days, due_on=EXCLUDED.due_on,"
        " last_reviewed=EXCLUDED.last_reviewed",
        (user, card["id"], result["repetitions"], result["ease"],
         result["interval"], result["due_on"], now), fetch=False)
    db.query("INSERT INTO answers (user_name, card_id, grade, study_day, answered_at)"
             " VALUES (%s,%s,%s,%s,%s)", (user, card["id"], grade, today, now),
             fetch=False)

    key = _queue_key(user, today)
    c = cache.client()
    head = c.lindex(key, 0)
    if head is not None and int(head) == int(card["id"]):
        c.lpop(key)
    else:
        c.lrem(key, 1, str(card["id"]))
    # A card you failed comes back at the END of today's queue, not tomorrow.
    if result["lapsed"] and not result["same_session"]:
        c.rpush(key, str(card["id"]))
        c.expire(key, QUEUE_TTL)

    return {"message": "Card reviewed successfully!",
            "user": user, "card_id": card["id"], "front": card["front"],
            "grade": grade, "passed": result["passed"],
            "repetitions": result["repetitions"], "ease": result["ease"],
            "interval_days": result["interval"],
            "due_on": result["due_on"].isoformat(),
            "study_day": today.isoformat(),
            "same_session": result["same_session"],
            "ease_floored": result["ease_floored"],
            "note": result["note"] or ("Card passed!" if result["passed"] else "Card lapsed - will reappear today"),
            "remaining": c.llen(key)}


@app.get("/study/{user}/due")
def due(user: str, deck_id: int = 0):
    """What is due today, straight from the database, ignoring the cache."""
    today = study_day(_now(), DEFAULT_TZ)
    cards = _cards_for(user, deck_id or None)
    ready = due_cards(cards, today)
    return {"user": user, "study_day": today.isoformat(),
            "day_starts_at": "04:00 +05:30",
            "due": [{"id": c["id"], "deck": c["deck"], "front": c["front"],
                     "ease": c["ease"], "interval": c["interval"],
                     "due_on": c["due_on"].isoformat() if c["due_on"] else None,
                     "repeat_today": is_repeat_in_same_day(
                         c["last_reviewed"], _now(), DEFAULT_TZ)}
                    for c in ready],
            "total_due": len(ready)}


@app.get("/stats/{user}")
def stats(user: str):
    rows = db.query("SELECT study_day, count(*) AS answers,"
                    " count(*) FILTER (WHERE grade >= 3) AS passed,"
                    " round(avg(grade), 2) AS avg_grade"
                    " FROM answers WHERE user_name=%s GROUP BY study_day"
                    " ORDER BY study_day DESC LIMIT 14", (user,))
    if not rows:
        raise HTTPException(404, "that user has not answered anything yet")
    return {"user": user,
            "by_study_day": [{"study_day": r["study_day"].isoformat(),
                              "answers": r["answers"], "passed": r["passed"],
                              "avg_grade": float(r["avg_grade"])} for r in rows],
            "note": "a study day runs 04:00 to 04:00, so a midnight session is"
                    " one day, not two"}
