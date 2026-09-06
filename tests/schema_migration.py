"""Integration test; run only against a disposable PostgreSQL via DB_* variables."""
import asyncio
import asyncpg
from db_manager import DBManager, DB_HOST, DB_USER, DB_PASSWORD, DB_NAME


async def main():
    connection = dict(host=DB_HOST, user=DB_USER, password=DB_PASSWORD, database=DB_NAME)
    admin = await asyncpg.connect(**connection)
    try:
        for schema, legacy in [('qa_fresh', False), ('qa_upgrade', True)]:
            await admin.execute(f'CREATE SCHEMA {schema}')
            manager = DBManager()
            manager.pool = await asyncpg.create_pool(**connection, server_settings={'search_path': schema})
            try:
                if legacy:
                    async with manager.pool.acquire() as conn:
                        await conn.execute('''
                            CREATE TABLE users (
                                id SERIAL PRIMARY KEY, telegram_id BIGINT UNIQUE NOT NULL,
                                username VARCHAR(255), group_name VARCHAR(255),
                                custom_ai_key VARCHAR(512), ai_model VARCHAR(50),
                                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                vpn_enabled BOOLEAN, vpn_key TEXT, vpn_expires_at TIMESTAMP,
                                vpn_purchased_at TIMESTAMP, ai_balance INT,
                                ai_expires_at TIMESTAMP, ai_purchased_at TIMESTAMP
                            );
                            CREATE TABLE ai_keys (id SERIAL PRIMARY KEY);
                            INSERT INTO users (telegram_id, group_name, custom_ai_key, ai_expires_at)
                            VALUES (1, 'Test group', 'generated-key', CURRENT_TIMESTAMP),
                                   (2, 'Test group', 'personal-key', NULL);
                        ''')
                await manager.init_db()
                await manager.init_db()
                async with manager.pool.acquire() as conn:
                    columns = await conn.fetch("SELECT column_name FROM information_schema.columns WHERE table_schema = $1 AND table_name = 'users'", schema)
                    assert not any(c['column_name'].startswith('vpn_') or c['column_name'] == 'ai_balance' for c in columns)
                    assert await conn.fetchval("SELECT to_regclass('ai_keys')") is None
                if legacy:
                    assert (await manager.get_user(1))['custom_ai_key'] is None
                    assert (await manager.get_user(2))['custom_ai_key'] == 'personal-key'
                    assert (await manager.get_user(1))['group_name'] == 'Test group'
                await manager.register_or_update_user(3, 'student', 'Test group')
                model_id = 'test/' + 'a' * 100 + ':free'
                await manager.set_user_ai_model(3, model_id)
                await manager.log_ai_request(3, 'Question', 'Answer', model_id)
                assert (await manager.get_user_ai_requests(3))[0]['model_used'] == model_id
                print(schema, 'passed: migration repeat, preserved users/keys, long model IDs')
            finally:
                await manager.pool.close()
    finally:
        await admin.close()


asyncio.run(main())
