from datetime import date, datetime, timedelta
import pytest
from app import schedule as s


def test_validate_grade_valid():
  for g in range(6):
    assert s.validate_grade(g) == g
    assert s.validate_grade(str(g)) == g


def test_validate_grade_invalid():
  with pytest.raises(s.CardError):
    s.validate_grade(-1)
  with pytest.raises(s.CardError):
    s.validate_grade(6)
  with pytest.raises(s.CardError):
    s.validate_grade("invalid")


def test_is_pass_boundary():
  assert s.is_pass(3) is True
  assert s.is_pass(4) is True
  assert s.is_pass(5) is True
  assert s.is_pass(2) is False
  assert s.is_pass(1) is False
  assert s.is_pass(0) is False


def test_next_ease_values_and_floor():
  # Grade 5 increases ease, 4 maintains, 3 lowers, 0 penalizes heavily
  assert s.next_ease(2.5, 5) == 2.6
  assert s.next_ease(2.5, 4) == 2.5
  assert s.next_ease(2.5, 3) == 2.36
  assert s.next_ease(2.5, 0) == 1.7

  # Prove the 1.3 floor holds after 10 consecutive failures
  ease = 2.5
  for _ in range(10):
    ease = s.next_ease(ease, 0)
  assert ease == 1.3


def test_study_day_boundary():
  # 23:58 and 00:02 on consecutive dates belong to the SAME study day
  dt1 = datetime(2026, 10, 16, 23, 58, tzinfo=s.DEFAULT_TZ)
  dt2 = datetime(2026, 10, 17, 0, 2, tzinfo=s.DEFAULT_TZ)
  assert s.study_day(dt1) == date(2026, 10, 16)
  assert s.study_day(dt2) == date(2026, 10, 16)

  # 03:59 belongs to previous study day; 04:00 starts the new study day
  dt_prev = datetime(2026, 10, 17, 3, 59, tzinfo=s.DEFAULT_TZ)
  dt_curr = datetime(2026, 10, 17, 4, 0, tzinfo=s.DEFAULT_TZ)
  assert s.study_day(dt_prev) == date(2026, 10, 16)
  assert s.study_day(dt_curr) == date(2026, 10, 17)


def test_study_day_naive_and_errors():
  naive = datetime(2026, 10, 17, 10, 0)
  assert isinstance(s.study_day(naive), date)
  with pytest.raises(s.CardError):
    s.study_day("not-a-datetime")


def test_is_repeat_in_same_day():
  dt1 = datetime(2026, 10, 16, 23, 58, tzinfo=s.DEFAULT_TZ)
  dt2 = datetime(2026, 10, 17, 0, 2, tzinfo=s.DEFAULT_TZ)
  dt3 = datetime(2026, 10, 17, 5, 0, tzinfo=s.DEFAULT_TZ)
  assert s.is_repeat_in_same_day(None, dt1) is False
  assert s.is_repeat_in_same_day(dt1, dt2) is True
  assert s.is_repeat_in_same_day(dt1, dt3) is False


def test_due_date():
  today = date(2026, 10, 16)
  assert s.due_date(today, 5) == date(2026, 10, 21)
  dt = datetime(2026, 10, 16, 12, 0, tzinfo=s.DEFAULT_TZ)
  assert s.due_date(dt, 1) == date(2026, 10, 17)
  with pytest.raises(s.CardError):
    s.due_date(today, -1)


def test_review_progression_and_immutability():
  card = {"repetitions": 0, "ease": 2.5, "interval": 0}
  original = dict(card)

  # Pass 1 -> interval 1
  r1 = s.review(card, 4)
  assert card == original  # Assert input dict is never mutated
  assert r1["repetitions"] == 1
  assert r1["interval"] == 1
  assert r1["passed"] is True

  # Pass 2 -> interval 6
  r2 = s.review(r1, 4)
  assert r2["repetitions"] == 2
  assert r2["interval"] == 6

  # Pass 3 -> interval 6 * 2.5 = 15
  r3 = s.review(r2, 4)
  assert r3["repetitions"] == 3
  assert r3["interval"] == 15

  # Lapse -> repetitions reset to 0, interval resets to 1
  lapse = s.review(r3, 1)
  assert lapse["repetitions"] == 0
  assert lapse["interval"] == 1
  assert lapse["passed"] is False
  assert lapse["lapsed"] is True


def test_apply_answer_repeat_session():
  card = {"repetitions": 1, "ease": 2.5, "interval": 1}
  t1 = datetime(2026, 10, 16, 10, 0, tzinfo=s.DEFAULT_TZ)
  res1 = s.apply_answer(card, 4, t1)
  assert res1["same_session"] is False
  assert res1["interval"] == 6

  # Answering again within the same study day updates ease but leaves interval alone
  t2 = datetime(2026, 10, 16, 11, 0, tzinfo=s.DEFAULT_TZ)
  res2 = s.apply_answer(card, 5, t2, last_reviewed=t1)
  assert res2["same_session"] is True
  assert res2["interval"] == 1
  assert "already answered today" in res2["note"]

  # Lapse in repeat session
  res3 = s.apply_answer(card, 1, t2, last_reviewed=t1)
  assert res3["passed"] is False
  assert res3["lapsed"] is True


def test_due_cards_and_queue():
  today = date(2026, 10, 16)
  c1 = {"id": 1, "due_on": date(2026, 10, 14), "ease": 2.5}  # 2 days overdue
  c2 = {
      "id": 2,
      "due_on": date(2026, 10, 10),
      "ease": 2.5,
  }  # 6 days overdue, higher ease
  c3 = {
      "id": 3,
      "due_on": date(2026, 10, 10),
      "ease": 1.5,
  }  # 6 days overdue, lower ease (hardest first)
  c4 = {"id": 4, "due_on": date(2026, 10, 20), "ease": 2.5}  # future, not due
  c5 = {"id": 5}  # new card, due now

  cards = [c1, c2, c3, c4, c5]
  sorted_cards = s.due_cards(cards, today)
  assert sorted_cards[0]["id"] == 3  # Hardest overdue first
  assert sorted_cards[1]["id"] == 2
  assert sorted_cards[2]["id"] == 1
  assert 4 not in [c["id"] for c in sorted_cards]
  assert s.queue_for(cards, today, limit=2) == [3, 2]


def test_deck_summary():
  today = date(2026, 10, 16)
  assert s.deck_summary([], today) == {
      "cards": 0,
      "new": 0,
      "due_today": 0,
      "learning": 0,
      "mature": 0,
      "average_ease": 0.0,
  }

  cards = [
      {"id": 1, "repetitions": 1, "interval": 5, "due_on": today, "ease": 2.5},
      {"id": 2, "repetitions": 3, "interval": 25, "due_on": today, "ease": 2.3},
      {"id": 3, "repetitions": 0, "interval": 0, "ease": 2.5},
  ]
  summary = s.deck_summary(cards, today)
  assert summary["cards"] == 3
  assert summary["new"] == 1
  assert summary["due_today"] == 3
  assert summary["learning"] == 1
  assert summary["mature"] == 1
  assert summary["average_ease"] == round((2.5 + 2.3 + 2.5) / 3, 3)