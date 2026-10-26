import re
import bcrypt
import pytest
from prepare_first_admin import build_sql


def test_generated_setup_hashes_password_and_quotes_names():
    password = 'private-test-password-2026'
    sql = build_sql('owner@example.com', 'First', "O'Brien", password)
    assert password not in sql
    hashed = re.search(r'\$2b\$12\$[./A-Za-z0-9]{53}', sql).group()
    assert bcrypt.checkpw(password.encode(), hashed.encode())
    assert "'O''Brien'" in sql
    assert 'LOCK TABLE public.users' in sql
    assert "RAISE EXCEPTION 'A Super Admin already exists" in sql
    assert 'public.audit_logs' in sql
    assert 'CREATE TABLE' not in sql and 'ALTER TABLE' not in sql


@pytest.mark.parametrize('password', ['short', 'x' * 73])
def test_rejects_unsafe_password_length(password):
    with pytest.raises(ValueError):
        build_sql('owner@example.com', 'First', 'Last', password)


def test_rejects_empty_name():
    with pytest.raises(ValueError):
        build_sql('owner@example.com', ' ', 'Last', 'private-test-password-2026')
