# Flashcards with Spaced Repetition

**Language:** Python (FastAPI) &nbsp;|&nbsp; **Needs:** Postgres + Redis

This is a **starter**. The application already works. Your job is everything
that gets it building, tested and running in CI.

---

## You do not need Python installed

You will build this into a container, and the container brings its own
Python 3.12. You are not being asked to extend the app — you are being asked
to ship it.

---

## 1. What this app needs

| | |
|---|---|
| **Runtime** | Python 3.12 |
| **Install dependencies** | `pip install -r requirements.txt` |
| **Start the app** | `uvicorn app.main:app --host 0.0.0.0 --port 8080` |
| **Listens on** | port 8080, bound to `0.0.0.0` |
| **Environment variables** | `DATABASE_URL`, `REDIS_URL` |
| **Needs running first** | Postgres, Redis, and the migrations applied |

### What it does

Decks of cards, and an algorithm that decides which of them you see today. Grade each card 0 to 5 as you answer it; SM-2 turns that grade into an interval, so a card you find easy drifts out to months and one you fumble is back tomorrow. The day's queue is built once and popped from as you revise.

### Endpoints

```
GET  /health
GET  /decks?user=demo                   every deck with new / due / mature counts
POST /study/{user}/start?deck_id=1      build today's queue (idempotent)
GET  /study/{user}/next                 peek at the next card
POST /study/{user}/answer  {"card_id":2,"grade":4}
GET  /study/{user}/due                  what is due, straight from the database
GET  /stats/{user}                      answers per study day
```

`/health` reports Postgres and Redis **separately**. If it says
`postgres: false` the app started fine and your compose wiring is wrong —
do not go looking in the application code.

### Migrations

`migrations/` holds `.sql` files applied **in filename order** before the app
starts. They create the tables and insert sample data. A container running
`psql` over them in order is enough; you do not need a migration tool.

---

## 2. What you must write

| File | What it has to do |
|---|---|
| `Dockerfile` | Install dependencies **before** copying source, pin the base image, do not run as root. |
| `docker-compose.yml` | App + Postgres + Redis + a migration step, one `docker compose up`. |
| `.circleci/config.yml` | lint → unit tests → integration tests → secret scan → image build |
| Unit tests | For `app/schedule.py`. No database, no network. |
| Integration tests | Against a real Postgres and Redis as CircleCI service containers. |

Then push your image to **your own Docker Hub account**, tagged `:1.0`.

### When it works

```bash
docker compose up --build
curl localhost:8080/health
```

```json
{"status":"ok","postgres":true,"redis":true}
```

---

## Where the marks are

`app/schedule.py` is **pure logic** — plain functions over plain data, no
database and no HTTP. Start your tests there. Use pytest:
`pytest --cov=app --cov-report=term-missing`. Minimum 70%.

This module is close to perfect for unit testing - pure arithmetic with sharp boundaries and a published specification. Start with `next_ease` at every grade, then prove the 1.3 floor holds after ten failures in a row. Then `study_day`: 23:58 and 00:02 on consecutive dates must return the SAME day, and 03:59 and 04:00 must not.

## Why Redis is here

Today's due-card queue, per user, keyed by the **study day**. Working out what is due is a join over every card and every row of progress; redoing it after each answer is both slow and wrong, because answering a card changes what is due and the list would reshuffle under you mid-session. So it is built once, popped from as you go, and expires by itself. A card you fail goes to the back of the same queue rather than waiting until tomorrow.

## The hard part

**A card answered at 11:58pm and again at 12:02am is two sessions by the clock but one by intent.** Revising past midnight is the normal case, not the edge case. If 00:02 counts as a new day then the same card is shown twice in four minutes, its interval is advanced twice, and the schedule quietly stops meaning anything. Decide where your day boundary is, put the study day in the cache key rather than the calendar date, and decide what a second answer within one sitting is allowed to change. Say what you chose and why.

Write your answer in your README. It is worth more marks than the feature.

---

## Getting unstuck

| Symptom | Almost always |
|---|---|
| `/health` says `postgres: false` | Wrong hostname. In compose the host is the **service name**, not `localhost`. |
| Page will not load, logs fine | No `ports:` mapping, or bound to `127.0.0.1` not `0.0.0.0`. |
| `relation "..." does not exist` | Migrations did not run, or the app started before they finished. |
| Build takes minutes each time | `COPY . .` is above your dependency install. |
| CI cannot reach the database | In CircleCI service containers the host **is** `localhost` — opposite of compose. |

## My design decision: the day boundary

I set the study-day boundary at 4 AM local time (IST), not midnight.
A student revising at 23:58 and again at 00:02 is in one sitting, so both
answers belong to the same study day. Anything before 04:00 counts as the
previous day.

The Redis queue key uses the study day, not the calendar date, so the day's
queue is built once and does not reset at midnight.

If a card is answered a second time within the same study day, only its ease
is updated. The interval and repetition count stay as they are, because the
student has not yet "slept on it". Otherwise answering twice in one evening
would advance the interval twice and the schedule would stop meaning anything.
