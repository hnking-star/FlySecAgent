-- FlySecAgent SQLite schema.
-- 以 docs/details/02-数据库.md 为准。唯一偏差：
-- projects.current_observation_id 不声明为外键（循环引用），由 commit_publish 程序校验。

CREATE TABLE IF NOT EXISTS projects (
  session_id                   TEXT PRIMARY KEY,
  name                         TEXT,
  target                       TEXT NOT NULL,
  objective                    TEXT NOT NULL,
  observation_enabled          INTEGER NOT NULL DEFAULT 1,
  agent_turn_active            INTEGER NOT NULL DEFAULT 0,
  observer_paused              INTEGER NOT NULL DEFAULT 0,
  observer_session_id          TEXT,
  processed_record_id          INTEGER NOT NULL DEFAULT 0,
  pending_window_end           INTEGER,
  current_observation_id       INTEGER,         -- 无 FK；commit_publish 内部校验
  last_feedback_observation_id INTEGER,
  created_at                   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_records (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id         TEXT NOT NULL,
  call_key           TEXT,
  tool_name          TEXT NOT NULL,
  tool_input_json    TEXT NOT NULL,
  tool_response_json TEXT NOT NULL,
  metadata_json      TEXT NOT NULL,
  received_at        TEXT NOT NULL,
  FOREIGN KEY (session_id) REFERENCES projects(session_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS observations (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id           TEXT NOT NULL,
  trigger              TEXT NOT NULL,
  status               TEXT NOT NULL,
  start_record_id      INTEGER NOT NULL,
  end_record_id        INTEGER NOT NULL,
  base_observation_id  INTEGER,
  state_json           TEXT,
  map_text             TEXT,
  error                TEXT,
  tool_logs_json       TEXT NOT NULL DEFAULT '[]',
  started_at           TEXT NOT NULL,
  finished_at          TEXT,
  FOREIGN KEY (session_id)          REFERENCES projects(session_id) ON DELETE RESTRICT,
  FOREIGN KEY (base_observation_id) REFERENCES observations(id)     ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_records_dedup
  ON tool_records(session_id, call_key) WHERE call_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tool_records_window  ON tool_records(session_id, id);
CREATE INDEX IF NOT EXISTS idx_observations_session ON observations(session_id, id);
