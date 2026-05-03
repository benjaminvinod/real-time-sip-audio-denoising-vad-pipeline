import sqlite3
import json
import os
from datetime import datetime

# ─────────────────────────────────────────────
# PATH SETUP
# ─────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR   = os.path.join(BASE_DIR, "db")
DB_PATH  = os.path.join(DB_DIR, "calls.db")

# Ensure db folder exists
os.makedirs(DB_DIR, exist_ok=True)


# ─────────────────────────────────────────────
# INIT DB
# ─────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        call_id TEXT,
        timestamp TEXT,
        transcript TEXT,
        summary TEXT,
        intent TEXT,
        sentiment TEXT,
        risk_level TEXT,
        suggested_action TEXT,
        meta TEXT,
        llm_status TEXT DEFAULT 'no_llm',
        llm_error TEXT
    )
    """)

    conn.commit()
    conn.close()
    print("📦 [DB] Initialized")


# ─────────────────────────────────────────────
# SAVE CALL
# ─────────────────────────────────────────────
def save_call(call_id, transcript, report, meta=None, llm_status="no_llm", llm_error=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        cur.execute("""
        INSERT INTO calls (
            call_id,
            timestamp,
            transcript,
            summary,
            intent,
            sentiment,
            risk_level,
            suggested_action,
            meta,
            llm_status,
            llm_error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            call_id,
            datetime.utcnow().isoformat(),
            transcript,
            report.get("summary"),
            report.get("intent"),
            report.get("sentiment"),
            report.get("risk_level"),
            report.get("suggested_action"),
            json.dumps(meta or {}),
            llm_status,
            llm_error
        ))

        conn.commit()
        conn.close()

        print(f"💾 [DB] Saved call: {call_id}")

    except Exception as e:
        print(f"❌ [DB] Save error: {e}")


# ─────────────────────────────────────────────
# GET RECENT CALLS
# ─────────────────────────────────────────────
def get_recent_calls(limit=10):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
    SELECT call_id, timestamp, summary, intent, sentiment, risk_level
    FROM calls
    ORDER BY id DESC
    LIMIT ?
    """, (limit,))

    rows = cur.fetchall()
    conn.close()

    return [
        {
            "call_id": r[0],
            "timestamp": r[1],
            "summary": r[2],
            "intent": r[3],
            "sentiment": r[4],
            "risk_level": r[5],
        }
        for r in rows
    ]


# ─────────────────────────────────────────────
# GET FULL CALL
# ─────────────────────────────────────────────
def get_call(call_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
    SELECT *
    FROM calls
    WHERE call_id = ?
    ORDER BY id DESC
    LIMIT 1
    """, (call_id,))

    row = cur.fetchone()
    conn.close()

    if not row:
        return None

    return {
        "call_id": row[1],
        "timestamp": row[2],
        "transcript": row[3],
        "summary": row[4],
        "intent": row[5],
        "sentiment": row[6],
        "risk_level": row[7],
        "suggested_action": row[8],
        "meta": json.loads(row[9] or "{}"),
    }