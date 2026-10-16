import os
from fastapi.testclient import TestClient
import psycopg
import pytest
from app.main import app


@pytest.fixture(scope="session", autouse=True)
def setup_database():
  db_url = os.getenv(
      "DATABASE_URL",
      "postgresql://postgres:postgrespassword@localhost:5432/flashcards_db",
  )
  try:
    conn = psycopg.connect(db_url)
    with conn.cursor() as cur:
      if os.path.exists("migrations/001_schema.sql"):
        with open("migrations/001_schema.sql") as f:
          cur.execute(f.read())
      if os.path.exists("migrations/002_seed.sql"):
        with open("migrations/002_seed.sql") as f:
          cur.execute(f.read())
    conn.commit()
    conn.close()
  except Exception as e:
    pytest.fail(
        f"PostgreSQL connection failed during integration setup: {e}"
    )


def test_health():
  client = TestClient(app)
  res = client.get("/health")
  assert res.status_code == 200
  data = res.json()
  assert data.get("status") == "ok"
  assert data.get("postgres") is True
  assert data.get("redis") is True


def test_get_decks():
  client = TestClient(app)
  res = client.get("/decks?user=testuser")
  assert res.status_code == 200
  data = res.json()
  assert "decks" in data
  assert len(data["decks"]) > 0


def test_study_lifecycle():
  client = TestClient(app)
  start_res = client.post("/study/testuser/start?deck_id=1")
  assert start_res.status_code == 200

  next_res = client.get("/study/testuser/next")
  assert next_res.status_code == 200
  card = next_res.json()
  card_id = card.get("id", 1)

  ans_res = client.post(
      "/study/testuser/answer", json={"card_id": card_id, "grade": 4}
  )
  assert ans_res.status_code == 200
  assert ans_res.json().get("message") == "Card reviewed successfully!"

  assert client.get("/study/testuser/due").status_code == 200
  assert client.get("/stats/testuser").status_code == 200
