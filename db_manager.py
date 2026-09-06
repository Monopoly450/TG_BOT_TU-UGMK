import os
import logging
import asyncpg
import json

logger = logging.getLogger("db_manager")

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "ugmk_postgres_pass")
DB_NAME = os.getenv("DB_NAME", "tu_bot")

class DBManager:
    def __init__(self):
        self.pool = None

    async def connect(self):
        if self.pool is not None:
            return
        try:
            self.pool = await asyncpg.create_pool(
                host=DB_HOST,
                user=DB_USER,
                password=DB_PASSWORD,
                database=DB_NAME,
                min_size=5,
                max_size=20
            )
            logger.info("Successfully connected to PostgreSQL database pool.")
        except Exception as e:
            logger.critical(f"Failed to connect to PostgreSQL: {e}")
            raise e

    async def init_db(self):
        await self.connect()
        async with self.pool.acquire() as conn, conn.transaction():
            # Bot and dashboard may start simultaneously; serialize schema changes.
            await conn.execute("SELECT pg_advisory_xact_lock(742619005)")
            # Create tables
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    username VARCHAR(255),
                    group_name VARCHAR(255),
                    custom_ai_key VARCHAR(512),
                    ai_model VARCHAR(255) DEFAULT 'openrouter/free',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                ALTER TABLE users ADD COLUMN IF NOT EXISTS is_blacklisted BOOLEAN DEFAULT FALSE;
                ALTER TABLE users ALTER COLUMN ai_model TYPE VARCHAR(255);
                ALTER TABLE users ALTER COLUMN ai_model SET DEFAULT 'openrouter/free';
                UPDATE users
                SET ai_model = 'openrouter/free'
                WHERE ai_model IS NULL OR ai_model = 'gemini-1.5-flash';
                
                CREATE TABLE IF NOT EXISTS ai_requests (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL,
                    prompt TEXT,
                    response TEXT,
                    model_used VARCHAR(255),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE TABLE IF NOT EXISTS events (
                    id SERIAL PRIMARY KEY,
                    title VARCHAR(255) NOT NULL,
                    description TEXT,
                    event_date TIMESTAMP,
                    link TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE TABLE IF NOT EXISTS channels (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    link TEXT,
                    category VARCHAR(255),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE TABLE IF NOT EXISTS polls (
                    id SERIAL PRIMARY KEY,
                    creator_id BIGINT NOT NULL,
                    group_name VARCHAR(255) NOT NULL,
                    question TEXT NOT NULL,
                    options JSONB NOT NULL,
                    poll_id_tg VARCHAR(255),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE TABLE IF NOT EXISTS poll_votes (
                    id SERIAL PRIMARY KEY,
                    poll_id INT REFERENCES polls(id) ON DELETE CASCADE,
                    telegram_id BIGINT NOT NULL,
                    option_index INT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(poll_id, telegram_id)
                );
                
                CREATE TABLE IF NOT EXISTS settings (
                    key VARCHAR(255) PRIMARY KEY,
                    value TEXT
                );
            """)
            await conn.execute("ALTER TABLE ai_requests ALTER COLUMN model_used TYPE VARCHAR(255)")
            # One-time cleanup is isolated from the application schema.
            from pathlib import Path
            await conn.execute(Path(__file__).with_name("migrations").joinpath("001_remove_legacy_services.sql").read_text())
            logger.info("PostgreSQL database tables initialized.")

    # User operations
    async def get_user(self, telegram_id: int):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", telegram_id)

    async def register_or_update_user(self, telegram_id: int, username: str = None, group_name: str = None):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (telegram_id, username, group_name)
                VALUES ($1, $2, $3)
                ON CONFLICT (telegram_id) DO UPDATE 
                SET username = COALESCE($2, users.username),
                    group_name = COALESCE($3, users.group_name)
            """, telegram_id, username, group_name)

    async def set_user_ai_key(self, telegram_id: int, api_key: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (telegram_id, custom_ai_key) VALUES ($1, $2)
                ON CONFLICT (telegram_id) DO UPDATE SET custom_ai_key = $2
            """, telegram_id, api_key)

    async def set_user_ai_model(self, telegram_id: int, model: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (telegram_id, ai_model)
                VALUES ($1, $2)
                ON CONFLICT (telegram_id) DO UPDATE SET ai_model = $2
            """, telegram_id, model)

    async def set_user_blacklist(self, telegram_id: int, is_blacklisted: bool):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET is_blacklisted = $2 WHERE telegram_id = $1", telegram_id, is_blacklisted)

    async def is_user_blacklisted(self, telegram_id: int) -> bool:
        async with self.pool.acquire() as conn:
            val = await conn.fetchval("SELECT is_blacklisted FROM users WHERE telegram_id = $1", telegram_id)
            return bool(val)

    async def get_user_ai_key(self, telegram_id: int) -> str:
        row = await self.get_user(telegram_id)
        return row['custom_ai_key'] if row else None

    # AI history
    async def log_ai_request(self, telegram_id: int, prompt: str, response: str, model_used: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO ai_requests (telegram_id, prompt, response, model_used)
                VALUES ($1, $2, $3, $4)
            """, telegram_id, prompt, response, model_used)


    # Event operations (Афиша)
    async def get_events(self):
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM events ORDER BY event_date ASC")

    async def add_event(self, title: str, description: str, event_date, link: str):
        async with self.pool.acquire() as conn:
            return await conn.fetchval("""
                INSERT INTO events (title, description, event_date, link)
                VALUES ($1, $2, $3, $4) RETURNING id
            """, title, description, event_date, link)

    async def delete_event(self, event_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM events WHERE id = $1", event_id)

    async def update_event(self, event_id: int, title: str, description: str, event_date, link: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE events 
                SET title = $1, description = $2, event_date = $3, link = $4
                WHERE id = $5
            """, title, description, event_date, link, event_id)

    # Channel operations (Каталог)
    async def get_channels(self):
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM channels ORDER BY category, name")

    async def add_channel(self, name: str, link: str, category: str):
        async with self.pool.acquire() as conn:
            return await conn.fetchval("""
                INSERT INTO channels (name, link, category)
                VALUES ($1, $2, $3) RETURNING id
            """, name, link, category)

    async def delete_channel(self, channel_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM channels WHERE id = $1", channel_id)

    # Starosta polls
    async def create_poll(self, creator_id: int, group_name: str, question: str, options: list, poll_id_tg: str = None) -> int:
        async with self.pool.acquire() as conn:
            options_json = json.dumps(options)
            return await conn.fetchval("""
                INSERT INTO polls (creator_id, group_name, question, options, poll_id_tg)
                VALUES ($1, $2, $3, $4, $5) RETURNING id
            """, creator_id, group_name, question, options_json, poll_id_tg)

    async def get_active_poll_for_group(self, group_name: str):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("""
                SELECT * FROM polls WHERE group_name = $1 ORDER BY created_at DESC LIMIT 1
            """, group_name)

    async def get_poll_by_tg_id(self, poll_id_tg: str):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM polls WHERE poll_id_tg = $1", poll_id_tg)

    async def get_poll(self, poll_id: int):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM polls WHERE id = $1", poll_id)

    async def vote_poll(self, poll_id: int, telegram_id: int, option_index: int):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO poll_votes (poll_id, telegram_id, option_index)
                VALUES ($1, $2, $3)
                ON CONFLICT (poll_id, telegram_id) DO UPDATE SET option_index = $3
            """, poll_id, telegram_id, option_index)

    async def get_poll_results(self, poll_id: int):
        async with self.pool.acquire() as conn:
            votes = await conn.fetch("""
                SELECT option_index, COUNT(*) as count 
                FROM poll_votes 
                WHERE poll_id = $1 
                GROUP BY option_index
            """, poll_id)
            total = await conn.fetchval("SELECT COUNT(*) FROM poll_votes WHERE poll_id = $1", poll_id)
            # convert to standard dict
            res_dict = {r['option_index']: r['count'] for r in votes}
            return res_dict, total or 0

    async def get_poll_voted_users(self, poll_id: int):
        async with self.pool.acquire() as conn:
            return await conn.fetch("""
                SELECT v.telegram_id, v.option_index, u.username 
                FROM poll_votes v
                LEFT JOIN users u ON u.telegram_id = v.telegram_id
                WHERE v.poll_id = $1
            """, poll_id)

    # --- Dashboard and Key activation methods ---
    async def get_all_users(self):
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM users ORDER BY id DESC")


    async def get_user_ai_requests(self, telegram_id: int):
        async with self.pool.acquire() as conn:
            return await conn.fetch("""
                SELECT * FROM ai_requests WHERE telegram_id = $1 ORDER BY created_at DESC LIMIT 50
            """, telegram_id)

    async def get_setting(self, key: str) -> str:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT value FROM settings WHERE key = $1", key)

    async def set_setting(self, key: str, value: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO settings (key, value)
                VALUES ($1, $2)
                ON CONFLICT (key) DO UPDATE SET value = $2
            """, key, value)

db_manager = DBManager()
