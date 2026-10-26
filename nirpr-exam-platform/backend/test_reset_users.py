import sqlite3
import pytest
from reset_users import reset


def test_production_rejects_example_signing_key(monkeypatch):
    import security_config
    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.setenv('SECRET_KEY', 'change-this-to-a-long-random-string-in-production')
    with pytest.raises(RuntimeError, match='SECRET_KEY'):
        security_config.signing_key()


@pytest.mark.asyncio
async def test_reset_requires_execution_and_backup_and_preserves_content(tmp_path):
    path = tmp_path / 'source.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE users (id INTEGER PRIMARY KEY);
            CREATE TABLE questions (id INTEGER PRIMARY KEY, created_by INTEGER REFERENCES users(id));
            CREATE TABLE attempts (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id));
            CREATE TABLE answers (id INTEGER PRIMARY KEY, attempt_id INTEGER NOT NULL REFERENCES attempts(id));
            CREATE TABLE audit_logs (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), details TEXT);
            INSERT INTO users VALUES (1);
            INSERT INTO questions VALUES (1, 1);
            INSERT INTO attempts VALUES (1, 1);
            INSERT INTO answers VALUES (1, 1);
            INSERT INTO audit_logs VALUES (1, 1, 'retained');
        ''')
    url = f'sqlite+aiosqlite:///{path.as_posix()}'
    await reset(url)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM users').fetchone()[0] == 1
    backup_dir = tmp_path / 'backups'
    await reset(url, execute=True, backup_dir=backup_dir)
    with sqlite3.connect(path) as db:
        for table in ('users', 'attempts', 'answers'):
            assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
        assert db.execute('SELECT * FROM questions').fetchall() == [(1, None)]
        assert db.execute('SELECT * FROM audit_logs').fetchall() == [(1, None, 'retained')]
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    with sqlite3.connect(next(backup_dir.glob('*.sqlite3'))) as backup:
        assert backup.execute('SELECT count(*) FROM users').fetchone()[0] == 1


@pytest.mark.asyncio
async def test_failed_backup_does_not_clear_users(tmp_path):
    path = tmp_path / 'source.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE users(id INTEGER PRIMARY KEY); INSERT INTO users VALUES(1);')
    blocked = tmp_path / 'not-a-directory'
    blocked.write_text('blocked')
    with pytest.raises(FileExistsError):
        await reset(f'sqlite+aiosqlite:///{path.as_posix()}', execute=True, backup_dir=blocked)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM users').fetchone()[0] == 1
