from datetime import date
from pathlib import Path

import pytest

from file_masker.mail.config import ConfigError, load_config

EXAMPLE = Path(__file__).parents[2] / "mail.example.toml"
SECRET = "s3cret-pw"

BASE = f'''
[run]
output_root = "/srv/masked"

[oauth.google]
client_id = "gid.apps.googleusercontent.com"
client_secret = "{SECRET}"

[oauth.microsoft]
client_id = "ms-client"
tenant = "look-tenant-id"

[[tenant]]
code = "lq"
  [[tenant.mailbox]]
  address = "LQ-Ops@example.com"
  provider = "gmail"
  since = 2026-10-01

[[tenant]]
code = "look"
  [[tenant.mailbox]]
  address = "ops@look.example"
  provider = "graph"
  [[tenant.mailbox]]
  address = "backup@look.example"
  provider = "imap"
  password = "{SECRET}"

[[tenant]]
code = "demo"
  [[tenant.mailbox]]
  address = "demo_mask_source@example.com"
  provider = "imap"
  password = "{SECRET}"
  sink = "smtp"
  smtp_to = "demo_mask_target@example.com"
'''


def write(tmp_path, text):
    path = tmp_path / "mail.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_full_config(tmp_path):
    cfg = load_config(write(tmp_path, BASE))
    assert [t.code for t in cfg.tenants] == ["lq", "look", "demo"]
    assert cfg.run.poll_seconds == 60 and cfg.run.retain_days == 90 and cfg.run.max_per_poll == 50
    assert cfg.run.lookback_minutes == 10 and cfg.run.timezone == "Asia/Shanghai"
    assert cfg.run.output_root == "/srv/masked" and cfg.run.originals_root == ""
    assert cfg.google.client_id == "gid.apps.googleusercontent.com"
    assert cfg.google.redirect_uri == "http://localhost:18090/api/mail/oauth/callback"
    assert cfg.microsoft.tenant == "look-tenant-id"

    lq = cfg.mailbox("lq-ops@example.com")  # 地址不区分大小写
    assert lq.tenant == "lq" and lq.provider == "gmail" and lq.sink == "fs" and lq.since == date(2026, 10, 1)
    assert lq.password is None

    backup = cfg.mailbox("backup@look.example")
    assert backup.provider == "imap" and backup.imap_host == "imap.larksuite.com" and backup.imap_port == 993
    assert backup.sink == "fs" and backup.smtp_to is None

    demo = cfg.mailbox("demo_mask_source@example.com")
    assert demo.sink == "smtp" and demo.smtp_to == "demo_mask_target@example.com"
    assert demo.smtp_host == "smtp.larksuite.com" and demo.smtp_port == 465
    assert [m.address for m in cfg.mailboxes()] == [
        "LQ-Ops@example.com", "ops@look.example", "backup@look.example", "demo_mask_source@example.com"]
    assert SECRET not in repr(cfg)


def test_example_file_loads_once_secrets_are_filled(tmp_path):
    text = EXAMPLE.read_text(encoding="utf-8").replace('client_secret = ""', f'client_secret = "{SECRET}"')
    cfg = load_config(write(tmp_path, text))
    assert {t.code for t in cfg.tenants} >= {"lq", "look", "demo"}
    assert cfg.mailbox("demo_mask_source@example.com").sink == "smtp"


def test_example_file_without_secret_is_rejected():
    with pytest.raises(ConfigError, match="oauth.google.client_secret"):
        load_config(EXAMPLE)


def test_oauth_sections_only_required_when_used(tmp_path):
    cfg = load_config(write(tmp_path, f'''
[[tenant]]
code = "demo"
  [[tenant.mailbox]]
  address = "a@example.com"
  provider = "imap"
  password = "{SECRET}"
'''))
    assert cfg.google is None and cfg.microsoft is None and cfg.run.output_root == "/srv/masked"


@pytest.mark.parametrize("old,new,msg", [
    ('code = "lq"', 'code = "l q"', "tenant code"),
    ('code = "lq"', 'code = "lq/x"', "tenant code"),
    ('code = "lq"', 'code = "look"', "Duplicate tenant"),
    ('code = "lq"', 'code = "LOOK"', "Duplicate tenant"),
    ('address = "backup@look.example"', 'address = "lq-ops@EXAMPLE.com"', "Duplicate mailbox"),
    ('provider = "graph"', 'provider = "pop3"', "provider"),
    (f'provider = "imap"\n  password = "{SECRET}"\n  sink', 'provider = "imap"\n  sink', "password"),
    ('smtp_to = "demo_mask_target@example.com"', 'smtp_to = "demo_mask_source@example.com"', "smtp_to"),
    ('sink = "smtp"\n  smtp_to = "demo_mask_target@example.com"', 'sink = "smtp"', "smtp_to"),
    ('sink = "smtp"', 'sink = "s3"', "sink"),
    ('output_root = "/srv/masked"', 'output_root = "/srv"', "output_root"),
    ('output_root = "/srv/masked"', 'output_root = "relative/dir"', "output_root"),
    ('output_root = "/srv/masked"', 'output_root = "/srv/masked"\npoll_seconds = 1', "run.poll_seconds"),
    ('output_root = "/srv/masked"', 'output_root = "/srv/masked"\noriginals_root = "/srv/masked/originals"', "originals_root"),
    ('output_root = "/srv/masked"', 'output_root = "/srv/masked/out"\noriginals_root = "/srv/masked"', "originals_root"),
    ('output_root = "/srv/masked"', 'output_root = "/srv/masked"\noriginals_root = "/srv/masked"', "originals_root"),
    ('output_root = "/srv/masked"', 'output_root = "/srv/masked"\noriginals_root = "originals"', "originals_root"),
    ('output_root = "/srv/masked"', 'output_root = "/srv/masked"\ntimezone = "Mars/Olympus"', "timezone"),
    ('since = 2026-10-01', 'since = "soon"', "since"),
    ('[oauth.google]\nclient_id = "gid.apps.googleusercontent.com"', '[oauth.google]', "oauth.google.client_id"),
    ('[oauth.microsoft]\nclient_id = "ms-client"\ntenant = "look-tenant-id"', '[oauth.microsoft]\nclient_id = "ms-client"', "oauth.microsoft.tenant"),
])
def test_invalid_config(tmp_path, old, new, msg):
    text = BASE.replace(old, new, 1)
    assert text != BASE, "patch did not apply"
    with pytest.raises(ConfigError, match=msg) as exc:
        load_config(write(tmp_path, text))
    assert SECRET not in str(exc.value)


def test_originals_root(tmp_path):
    cfg = load_config(write(tmp_path, BASE.replace('output_root = "/srv/masked"', 'output_root = "/srv/masked"\noriginals_root = "/srv/originals"')))
    assert cfg.run.originals_root == "/srv/originals"


def test_missing_oauth_section_when_provider_used(tmp_path):
    text = BASE.replace('[oauth.microsoft]\nclient_id = "ms-client"\ntenant = "look-tenant-id"\n', "")
    with pytest.raises(ConfigError, match="oauth.microsoft"):
        load_config(write(tmp_path, text))


def test_missing_file_and_bad_toml(tmp_path):
    with pytest.raises(ConfigError, match="Cannot read"):
        load_config(tmp_path / "nope.toml")
    with pytest.raises(ConfigError, match="Cannot read"):
        load_config(write(tmp_path, "[run\nbroken"))
