"""固定 F01 无版本业务结构，供只读识别与首轮迁移使用。"""

BUSINESS_SQL = {
    "advisor_exchanges": (
        "CREATE TABLE IF NOT EXISTS advisor_exchanges (id INTEGER PRIMARY KEY, run_id TEXT "
        "NOT NULL, interrupt_id TEXT NOT NULL, question TEXT NOT NULL, answer TEXT, status "
        "TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL, finished_at TEXT)"
    ),
    "completion_records": (
        "CREATE TABLE IF NOT EXISTS completion_records (run_id TEXT NOT NULL, content_hash "
        "TEXT NOT NULL, confirmed_at TEXT NOT NULL, PRIMARY KEY(run_id, content_hash))"
    ),
    "run_diagnostics": (
        "CREATE TABLE IF NOT EXISTS run_diagnostics (id INTEGER PRIMARY KEY, run_id TEXT "
        "NOT NULL, recorded_at TEXT NOT NULL, event TEXT NOT NULL, data TEXT NOT NULL)"
    ),
    "research_search_attempts": (
        "CREATE TABLE IF NOT EXISTS research_search_attempts (id INTEGER PRIMARY KEY, "
        "run_id TEXT NOT NULL, chapter INTEGER, channel TEXT NOT NULL, recorded_at TEXT NOT"
        " NULL DEFAULT CURRENT_TIMESTAMP)"
    ),
    "research_candidates": (
        "CREATE TABLE IF NOT EXISTS research_candidates (run_id TEXT NOT NULL, context_hash"
        " TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY "
        "KEY(run_id,context_hash,kind,id))"
    ),
    "research_library_versions": (
        "CREATE TABLE IF NOT EXISTS research_library_versions (run_id TEXT NOT NULL, "
        "context_hash TEXT NOT NULL, artifact_id TEXT NOT NULL, version INTEGER NOT NULL, "
        "content_hash TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, recorded_at TEXT"
        " NOT NULL, data TEXT NOT NULL, PRIMARY "
        "KEY(run_id,context_hash,artifact_id,version,content_hash,kind,id))"
    ),
    "research_garbage": ("CREATE TABLE IF NOT EXISTS research_garbage (filename TEXT PRIMARY KEY)"),
    "research_library_current": (
        "CREATE TABLE IF NOT EXISTS research_library_current (run_id TEXT, context_hash "
        "TEXT, kind TEXT, id TEXT, data TEXT NOT NULL, PRIMARY "
        "KEY(run_id,context_hash,kind,id))"
    ),
    "research_exclusions": (
        "CREATE TABLE IF NOT EXISTS research_exclusions (run_id TEXT, context_hash TEXT, "
        "kind TEXT, id TEXT, data TEXT NOT NULL, PRIMARY KEY(run_id,context_hash,kind,id))"
    ),
    "research_runs": (
        "CREATE TABLE IF NOT EXISTS research_runs (run_id TEXT PRIMARY KEY, context_hash "
        "TEXT NOT NULL, data TEXT NOT NULL)"
    ),
    "research_events": (
        "CREATE TABLE IF NOT EXISTS research_events (id INTEGER PRIMARY KEY, run_id TEXT "
        "NOT NULL, context_hash TEXT NOT NULL, recorded_at TEXT NOT NULL, data TEXT NOT "
        "NULL)"
    ),
    "writing_evidence": (
        "CREATE TABLE IF NOT EXISTS writing_evidence (id INTEGER PRIMARY KEY, run_id TEXT "
        "NOT NULL, context_hash TEXT NOT NULL, artifact_id TEXT NOT NULL, version INTEGER "
        "NOT NULL, recorded_at TEXT NOT NULL, data TEXT NOT NULL)"
    ),
}
SEARCH_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS research_search_attempts_run_chapter "
    "ON research_search_attempts(run_id,chapter)"
)
