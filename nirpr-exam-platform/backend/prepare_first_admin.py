"""Prepare a one-time SQL file for the owner to run in pgAdmin.

Does not connect to a database. Prompts privately for a password, hashes it
locally, and writes SQL to the user's temporary directory, outside Git.
"""
import getpass
import os
from pathlib import Path
import tempfile

import bcrypt
from email_validator import validate_email, EmailNotValidError


def sql_literal(value):
    if '\x00' in value:
        raise ValueError('Values cannot contain a null character.')
    return "'" + value.replace("'", "''") + "'"


def build_sql(email, first_name, surname, password):
    email = validate_email(email.strip(), check_deliverability=False).normalized
    first_name, surname = first_name.strip(), surname.strip()
    if not first_name or not surname or len(first_name) > 100 or len(surname) > 100:
        raise ValueError('Enter a first name and surname, each at most 100 characters.')
    if len(password) < 14 or len(password.encode('utf-8')) > 72:
        raise ValueError('Use a password of at least 14 characters and at most 72 UTF-8 bytes.')
    password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt(rounds=12)).decode('ascii')
    values = ', '.join(sql_literal(v) for v in (email, password_hash, surname, first_name))
    return f"""-- Run this entire file in pgAdmin Query Tool, connected to nirpr_exam.
-- Creates the FIRST Super Admin only. No demo data or schema changes.
-- The password is hashed. Treat this file as private and delete after use.
BEGIN;
SET LOCAL standard_conforming_strings = on;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '15s';
LOCK TABLE public.users IN SHARE ROW EXCLUSIVE MODE;
DO $bootstrap$
BEGIN
    IF EXISTS (SELECT 1 FROM public.users WHERE lower(role::text) = 'super_admin') THEN
        RAISE EXCEPTION 'A Super Admin already exists. No account was created.';
    END IF;
END
$bootstrap$;
WITH new_admin AS (
    INSERT INTO public.users (
        email, hashed_password, surname, first_name, role,
        is_active, email_verified, login_attempts, created_at,
        created_by_admin, must_change_password
    ) VALUES (
        {values}, 'SUPER_ADMIN',
        TRUE, TRUE, 0, timezone('UTC', now()), TRUE, FALSE
    )
    RETURNING id
)
INSERT INTO public.audit_logs (user_id, action, entity_type, entity_id, details, created_at)
SELECT id, 'first_super_admin_bootstrap', 'user', id,
       '{{"method":"owner_pgadmin_setup"}}'::json, timezone('UTC', now())
FROM new_admin;
COMMIT;

SELECT id, email, role, is_active
FROM public.users WHERE lower(role::text) = 'super_admin';
"""


def main():
    print('Prepare the first Super Admin account. This script does not connect to a database.')
    email = input('Your admin email: ')
    first_name = input('Your first name: ')
    surname = input('Your surname: ')
    password = getpass.getpass('Choose a private password (at least 14 characters): ')
    if password != getpass.getpass('Confirm password: '):
        raise ValueError('Passwords do not match.')
    sql = build_sql(email, first_name, surname, password)
    fd, filename = tempfile.mkstemp(prefix='nirpr-first-admin-', suffix='.sql')
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        output.write(sql)
    print(f'Prepared private SQL file: {Path(filename)}')
    print('In pgAdmin select nirpr_exam, open Query Tool, then open this file.')
    print('Review it and execute the ENTIRE file once to create your account.')
    print('If it fails, execute ROLLBACK; before trying again. Do not run only the INSERT.')
    print('After success, delete this file and sign in at your deployed app URL followed by /admin-login')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, EmailNotValidError) as error:
        print(f'No setup file created: {error}')
        raise SystemExit(1)
