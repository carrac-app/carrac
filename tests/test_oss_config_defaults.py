"""Config defaults that must not ship a known secret or a real domain."""

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_config(tmp_path, **env):
    """Import carrac.config in a clean subprocess (it reads env at import)."""
    base = {k: v for k, v in os.environ.items()
            if k not in ('SECRET_KEY', 'DATABASE_URL', 'SITE_URL',
                         'DISCORD_INVITE_URL', 'BETA_END_TS')}
    base.update(env)
    base['PYTHONPATH'] = str(ROOT)
    return subprocess.run(
        [sys.executable, '-c',
         'from carrac.config import Config as C; '
         'print(C.SITE_URL, "|", C.DISCORD_INVITE_URL)'],
        cwd=tmp_path, env=base, capture_output=True, text=True, timeout=60,
    )


def _env_example_value(key):
    for line in (ROOT / '.env.example').read_text().splitlines():
        m = re.match(rf'^{key}=(.*)$', line)
        if m:
            return m.group(1).strip()
    raise AssertionError(f'{key} not in .env.example')


def test_env_example_secret_key_placeholder_fails_fast_in_prod(tmp_path):
    placeholder = _env_example_value('SECRET_KEY')
    r = _load_config(tmp_path, SECRET_KEY=placeholder,
                     DATABASE_URL='postgresql://u:p@localhost/db')
    assert r.returncode != 0
    assert 'SECRET_KEY must be set' in r.stderr


def test_real_key_still_boots_in_prod(tmp_path):
    r = _load_config(tmp_path, SECRET_KEY='Zk3-9fQvLm2xT7pRw8aYb4NcDe6HsUj1',
                     DATABASE_URL='postgresql://u:p@localhost/db')
    assert r.returncode == 0, r.stderr


def test_site_url_and_discord_defaults_are_inert(tmp_path):
    r = _load_config(tmp_path)
    assert r.returncode == 0, r.stderr
    site, discord = [p.strip() for p in r.stdout.strip().split('|')]
    assert site == 'http://localhost:5002'
    assert discord == ''


def test_beta_cohort_tag_is_off_unless_configured(monkeypatch):
    from carrac import integrations
    monkeypatch.delenv('BETA_END_TS', raising=False)
    assert integrations._beta_cohort_tag_now() is None
    monkeypatch.setenv('BETA_END_TS', '2999-01-01T00:00:00+00:00')
    assert integrations._beta_cohort_tag_now() == integrations.BETA_COHORT_NAME
    monkeypatch.setenv('BETA_END_TS', '2000-01-01T00:00:00+00:00')
    assert integrations._beta_cohort_tag_now() is None


def test_widget_script_is_served_with_the_instances_own_origin(client, monkeypatch):
    monkeypatch.setenv('SITE_URL', 'https://my.example.org/')
    body = client.get('/widget/scriptable.js').get_data(as_text=True)
    assert 'const API_BASE = "https://my.example.org"' in body
    assert 'carrac.example.invalid' not in body
    assert 'carrac.cc' not in body

    monkeypatch.delenv('SITE_URL')
    body = client.get('/widget/scriptable.js', base_url='http://host.test').get_data(as_text=True)
    assert 'const API_BASE = "http://host.test"' in body


def test_discord_link_hidden_when_unset(client):
    html = client.get('/contact').get_data(as_text=True)
    assert 'discord.gg' not in html


_GOOD_KEY = 'Zk3-9fQvLm2xT7pRw8aYb4NcDe6HsUj1'
_PROD_DB = 'postgresql://u:p@localhost/db'


def test_env_example_admin_email_placeholder_fails_fast_in_prod(tmp_path):
    placeholder = _env_example_value('ADMIN_EMAIL')
    r = _load_config(tmp_path, SECRET_KEY=_GOOD_KEY, DATABASE_URL=_PROD_DB,
                     ADMIN_EMAIL=placeholder)
    assert r.returncode != 0
    assert 'ADMIN_EMAIL is still a placeholder' in r.stderr


def test_example_domains_rejected_for_admin_email_in_prod(tmp_path):
    for addr in ('a@example.org', 'B@EXAMPLE.NET'):
        r = _load_config(tmp_path, SECRET_KEY=_GOOD_KEY, DATABASE_URL=_PROD_DB,
                         ADMIN_EMAIL=addr)
        assert r.returncode != 0, addr


def test_real_admin_email_and_unset_still_boot_in_prod(tmp_path):
    for env in ({'ADMIN_EMAIL': 'owner@mycompany.io'}, {}):
        r = _load_config(tmp_path, SECRET_KEY=_GOOD_KEY, DATABASE_URL=_PROD_DB, **env)
        assert r.returncode == 0, r.stderr


def test_admin_email_placeholder_tolerated_outside_prod(tmp_path):
    r = _load_config(tmp_path, ADMIN_EMAIL='you@example.com')
    assert r.returncode == 0, r.stderr


def test_paddle_price_ids_default_empty_and_maps_are_inert(tmp_path):
    base = {k: v for k, v in os.environ.items() if not k.startswith('PADDLE_')}
    base['PYTHONPATH'] = str(ROOT)
    r = subprocess.run(
        [sys.executable, '-c',
         'from carrac.config import Config as C; '
         'print(C.PADDLE_PRICE_MEMBER, C.PADDLE_PRICE_TOPUP_25, '
         'C.paddle_price_to_tier(), C.paddle_price_to_credits(), C.paddle_configured())'],
        cwd=tmp_path, env=base, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "  {} {} False\n"
