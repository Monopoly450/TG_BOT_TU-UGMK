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
        async with self.pool.acquire() as conn:
            # Create tables
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    username VARCHAR(255),
                    group_name VARCHAR(255),
                    custom_ai_key VARCHAR(512),
                    ai_model VARCHAR(50) DEFAULT 'gemini-1.5-flash',
                    vpn_enabled BOOLEAN DEFAULT FALSE,
                    vpn_key TEXT,
                    ai_balance INT DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                ALTER TABLE users ADD COLUMN IF NOT EXISTS ai_balance INT DEFAULT 0;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS vpn_expires_at TIMESTAMP;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS ai_expires_at TIMESTAMP;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS vpn_purchased_at TIMESTAMP;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS ai_purchased_at TIMESTAMP;
                
                CREATE TABLE IF NOT EXISTS ai_keys (
                    id SERIAL PRIMARY KEY,
                    key_value VARCHAR(255) UNIQUE NOT NULL,
                    request_limit INT DEFAULT 100,
                    used_by BIGINT REFERENCES users(telegram_id) ON DELETE SET NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    used_at TIMESTAMP
                );
                
                CREATE TABLE IF NOT EXISTS ai_requests (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL,
                    prompt TEXT,
                    response TEXT,
                    model_used VARCHAR(50),
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

    async def set_user_ai_key(self, telegram_id: int, api_key: str, expires_at = None, purchased_at = None):
        async with self.pool.acquire() as conn:
            if api_key:
                await conn.execute("""
                    INSERT INTO users (telegram_id, custom_ai_key, ai_expires_at, ai_purchased_at)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (telegram_id) DO UPDATE SET custom_ai_key = $2, ai_expires_at = $3, ai_purchased_at = COALESCE($4, users.ai_purchased_at)
                """, telegram_id, api_key, expires_at, purchased_at)
            else:
                await conn.execute("""
                    UPDATE users SET custom_ai_key = NULL, ai_expires_at = NULL WHERE telegram_id = $1
                """, telegram_id)

    async def set_user_ai_model(self, telegram_id: int, model: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (telegram_id, ai_model)
                VALUES ($1, $2)
                ON CONFLICT (telegram_id) DO UPDATE SET ai_model = $2
            """, telegram_id, model)

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

    async def set_user_vpn(self, telegram_id: int, enabled: bool, key: str = None, expires_at = None, purchased_at = None):
        async with self.pool.acquire() as conn:
            if enabled:
                await conn.execute("""
                    INSERT INTO users (telegram_id, vpn_enabled, vpn_key, vpn_expires_at, vpn_purchased_at)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (telegram_id) DO UPDATE SET vpn_enabled = $2, vpn_key = COALESCE($3, users.vpn_key), vpn_expires_at = $4, vpn_purchased_at = COALESCE($5, users.vpn_purchased_at)
                """, telegram_id, enabled, key, expires_at, purchased_at)
            else:
                await conn.execute("""
                    UPDATE users SET vpn_enabled = FALSE, vpn_expires_at = NULL WHERE telegram_id = $1
                """, telegram_id)

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

    async def update_user_subscription(self, telegram_id: int, vpn_enabled: bool, ai_balance: int):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE users 
                SET vpn_enabled = $2, ai_balance = $3 
                WHERE telegram_id = $1
            """, telegram_id, vpn_enabled, ai_balance)

    async def generate_ai_key(self, request_limit: int = 100) -> str:
        import uuid
        key_val = f"UGMK-AI-{uuid.uuid4().hex[:8].upper()}"
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO ai_keys (key_value, request_limit)
                VALUES ($1, $2)
            """, key_val, request_limit)
        return key_val

    async def activate_ai_key(self, key_value: str, telegram_id: int) -> int:
        async with self.pool.acquire() as conn:
            # Check key
            row = await conn.fetchrow("""
                SELECT * FROM ai_keys WHERE key_value = $1 AND used_by IS NULL
            """, key_value)
            if not row:
                return 0
                
            limit = row['request_limit']
            # Register user if not exists
            await self.register_or_update_user(telegram_id)
            
            # Mark key as used
            await conn.execute("""
                UPDATE ai_keys 
                SET used_by = $2, used_at = CURRENT_TIMESTAMP 
                WHERE key_value = $1
            """, key_value, telegram_id)
            
            # Update user balance
            await conn.execute("""
                UPDATE users 
                SET ai_balance = ai_balance + $2 
                WHERE telegram_id = $1
            """, telegram_id, limit)
            
            return limit

    async def check_user_ai_balance(self, telegram_id: int) -> int:
        row = await self.get_user(telegram_id)
        return row['ai_balance'] if row else 0

    async def decrement_user_ai_balance(self, telegram_id: int) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT ai_balance FROM users WHERE telegram_id = $1", telegram_id)
            if not row or row['ai_balance'] <= 0:
                return False
            await conn.execute("UPDATE users SET ai_balance = ai_balance - 1 WHERE telegram_id = $1", telegram_id)
            return True

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
