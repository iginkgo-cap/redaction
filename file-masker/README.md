# file-masker

Mask names, account numbers and other sensitive text in PDF files **on your own machine**, before sending the files to anyone else.

- Runs entirely offline in a Docker container. It never connects to the internet: no telemetry, no update checks.
- Files are processed in memory and discarded as soon as the masked PDF is returned. Nothing is stored on the server.
- Masking rules (the text to mask and its replacement) are saved only in your own browser.

> 中文简介：在本机或公司内网离线运行的 PDF 打码工具。容器不联网，服务端不保存任何文件；打码关键词只保存在各自的浏览器里。

## Install

The release package contains one image per CPU architecture. Pick the one that matches the machine:

| Machine | File |
|---|---|
| Most servers / VPS, Windows PCs, Intel Macs | `file-masker-<version>-amd64.tar.gz` |
| Apple Silicon Macs (M1/M2/M3/…), ARM servers | `file-masker-<version>-arm64.tar.gz` |

Verify the download, then load the image:

```bash
shasum -a 256 -c SHA256SUMS
docker load -i file-masker-<version>-amd64.tar.gz
```

## Run

**On a personal computer** (only this computer can open the page):

```bash
docker run -d --name file-masker --restart unless-stopped \
  --tmpfs /tmp:size=512m --memory 2g --cpus 2 \
  -p 127.0.0.1:8080:8080 file-masker:<version>
```

**Shared inside an office network** (colleagues open `http://<server-ip>:8080`). There is no login, so keep the port behind your firewall or VPN:

```bash
docker run -d --name file-masker --restart unless-stopped \
  --tmpfs /tmp:size=512m --memory 2g --cpus 2 \
  -p 8080:8080 file-masker:<version>
```

`--tmpfs /tmp` keeps any temporary upload data in memory only.

## Use

1. Open `http://localhost:8080`.
2. Add PDF files (drag and drop, or click to select).
3. Add masking rules: the text to mask, and what to replace it with.
4. Check the preview on the right, then click **Mask & Download**.

How matching works:

- Case-insensitive: `Zhang San` also masks `ZHANG SAN`.
- Spaces are ignored once the rule is long enough (3 non-space characters, or 2 Chinese characters), so `ZhangSan` also masks `Zhang San` and `张三` also masks `张 三` (PDFs often contain extra spaces between letters).
- Leave "Replace with" empty to remove the text entirely.
- Each rule needs at least 3 characters. Rules made only of Chinese characters may be 2 characters long (e.g. `张三`). Short Latin text or digits such as `Li` or `12` are rejected because they appear inside too many other words and numbers.
- Matching is by substring, so a rule may also hit unrelated text that contains it (e.g. `张三` inside `主张三个月`). This only masks more, never less — check the preview.
- Scanned pages (images without a text layer) cannot be masked; the file is rejected with the page numbers.
- **Masking can miss text. Always check each PDF before sharing it.**

## Upgrade

```bash
docker load -i file-masker-<new-version>-amd64.tar.gz
docker rm -f file-masker
docker run ... file-masker:<new-version>   # same options as above
```

The server keeps no data, so nothing needs to be migrated. Masking rules stay in your browser.

## Uninstall

```bash
docker rm -f file-masker
docker rmi file-masker:<version>
```

## Configuration

Optional environment variables (`docker run -e NAME=value ...`):

| Variable | Default | Meaning |
|---|---|---|
| `MAX_FILE_MB` | `100` | Largest PDF accepted |
| `MAX_PAGES` | `2000` | Largest page count accepted |
| `MAX_CONCURRENT` | `2` | Files masked at the same time; others wait |
| `QUEUE_TIMEOUT_S` | `60` | How long a request may wait for a free slot |
| `PROCESS_TIMEOUT_S` | `120` | How long masking one file may take |

## Mail mode

An optional second mode watches one or more mailboxes, masks every new email (subject, sender, body, attachment names, PDF attachments, PDFs inside zips, plain-text attachments) and either **writes the masked copy to a folder** for a downstream agent to pick up, or **sends it to another mailbox**. **This is the only feature that makes network connections**, and only when you turn it on.

### Configure

1. Copy `mail.example.toml` to `mail.toml` (git-ignored; `chmod 600 mail.toml`). One file per environment: keep a `mail.local.toml` with test mailboxes for your machine and `mail.toml` with the real ones on the ops host, and point `MAIL_CONFIG` at the right one. Never list the same mailbox in both: each instance keeps its own progress and would process every email twice. The file holds:
   - `[run]`: polling interval, `output_root` (absolute, at least two levels deep), optional `originals_root` (see below), retention days, timezone for date folders, per-poll limit, lookback window.
   - `[oauth.google]` / `[oauth.microsoft]`: the OAuth client used for Gmail / Microsoft 365 mailboxes (required only when such a mailbox is configured).
   - One `[[tenant]]` per Ginkgo tenant (`code` = first folder level), each with `[[tenant.mailbox]]` entries: `address`, `provider` (`imap` with a password, `gmail` or `graph` via OAuth), `sink` (`fs`, the default, or `smtp` with `smtp_to`), and an optional `since` date for a one-time backfill on first run.
2. Masking rules are **not** in the file: set them per tenant on the `/mail` page. They are stored in the service's SQLite database and survive restarts. Until a tenant has rules, its emails are held.

### Run

```bash
MAIL_CONFIG=./mail.toml MAIL_DATA_DIR=./data .venv/bin/uvicorn file_masker.web.app:app --port 8080
```

With Docker (this is how it runs on the ops machine):

```bash
docker run -d --name file-masker --restart unless-stopped \
  --tmpfs /tmp:size=512m --memory 2g --cpus 2 \
  --user "$(id -u):$(id -g)" \
  -p 18090:8080 \
  -v "$HOME/file-masker/mail.toml:/config/mail.toml:ro" \
  -v "$HOME/file-masker/data:/data" \
  -v "$HOME/file-masker/masked:/srv/masked" \
  -e MAIL_CONFIG=/config/mail.toml \
  file-masker:<version>
```

- `--user` runs the container as you, so it can read your `chmod 600` config and write the mounted folders as your user.
- `/data` (`MAIL_DATA_DIR`, default `/data`) holds `state.db` (cursors, processed emails, rules, OAuth refresh tokens — kept `chmod 600`) and the instance lock. Only one instance may use a data directory; a second one refuses to start.
- `/srv/masked` must match `output_root`. Give the agent's user read access with an inheritable ACL on the host folder; the service writes directories `750` and files `640`.
- If you set `originals_root`, mount it too (e.g. `-v "$HOME/file-masker/originals:/srv/originals"`). It holds **unmasked** emails. The service writes directories `700` and files `600`; only add an inheritable read ACL for the agent's user if the agent must read the originals (for example, images that were dropped from the masked copy).
- The container starts without an HTTP access log so that OAuth codes never land in a log file.

### Authorize Gmail / Microsoft 365 mailboxes

Open `http://<host>:18090/mail`, pick the tenant tab and click **Authorize** next to the mailbox:

- **Microsoft 365**: a code is shown; open the link, enter the code and sign in as that mailbox. The page polls until the sign-in completes.
- **Gmail**: open the sign-in link and sign in as that mailbox. Google redirects to `http://localhost:18090/api/mail/oauth/callback` (the `redirect_uri` in the config, which must be registered in the Google Cloud project). If your browser cannot reach the service at that address, forward the port (`ssh -L 18090:127.0.0.1:18090 <host>`) or paste the full address of the page you land on into the panel.

The service checks that the account that signed in is the configured mailbox, then stores only the refresh token. An attempt expires after 10 minutes. **Disconnect** removes the stored token and stops reading the mailbox; the permission granted in the Google / Microsoft account itself is not revoked (remove it there if you want it gone). The same can be done from a shell:

```bash
docker exec -it file-masker python -m file_masker.mail auth --paste ops@example.com
docker exec -it file-masker python -m file_masker.mail auth --disconnect ops@example.com
```

Inside the container the CLI cannot receive Google's redirect (the host port belongs to the service), so `--paste` asks you to paste the address you land on; without Docker, omit it and the CLI listens on `--port` (default 18090) itself.

### Output layout (`sink = "fs"`)

```
<output_root>/<tenant>/<mailbox>/<YYYY-MM-DD>/<dir-id>/
  mail.json        # provider, mail_id, message_id, dedup_fingerprint, received_at, masked subject/from/body, attachment names, rules_version…
  <name>.pdf       # masked attachments (names sanitized)
  .ready           # written last; consumers must ignore directories without it
```

`<dir-id>` is the first 32 hex characters of `sha256(provider + "\0" + mail_id)`; the date folder follows `received_at` in the configured timezone. A directory is written atomically (temporary directory, fsync, rename, then `.ready`). Directories older than `retain_days` are deleted once a day. `mail.json` lists attachments that could not be masked under `skipped` (masked name and reason); those files are not in the folder.

When anything in an email could not be masked (a skipped attachment, a dropped image, or the whole email held) and `originals_root` is set, the original email is kept as `<originals_root>/<tenant>/<mailbox>/<YYYY-MM-DD>/<dir-id>.eml`, next to `<dir-id>.json` with the reason, skipped attachments and the number of removed images. It follows the same retention.

### How it works

- Only emails that arrive **after** the service first sees a mailbox are processed (or from `since` when set). Progress is kept per mailbox in SQLite; the source email is marked (IMAP flag, Gmail label `file-masker-processed`, Microsoft category) for people, not for deduplication.
- Polling re-scans a 10-minute lookback window every round and the last 24 hours once a day, so late-arriving emails are still picked up.
- Masking uses the same matching rules as the PDF page; in HTML, text split across tags is still matched. Images (including ones sent as `application/octet-stream` with an image file name) and S/MIME signatures are dropped.
- An attachment that cannot be masked (scanned or encrypted PDFs, types other than PDF, zip and plain text, unsupported zips) is **left out** and the rest of the email is delivered; the sent email's footer and `mail.json` say how many were left out.
- An email is **held, not delivered**, if there are no rules, its encoding cannot be read, text to mask is still found after masking, the mail server rejects it permanently, or the output directory already holds a different email.
- One mailbox failing (network, expired authorization) does not stop the others. The page shows each mailbox's authorization state, last check and last error, the tenant's rules and its last 50 results. Passwords, hosts and tokens are never shown. There is no login: anyone who can open the page can change rules and authorize mailboxes — keep the port on the internal network.

## Docker Desktop licensing

On Windows and macOS the image runs under Docker Desktop. Docker Desktop requires a paid subscription for companies with more than 250 employees or more than USD 10 million in annual revenue. Free alternatives that can run the same image: [Rancher Desktop](https://rancherdesktop.io/), [Podman Desktop](https://podman-desktop.io/).

## License and source code

file-masker is licensed under the **GNU Affero General Public License v3.0** (see `LICENSE`). It uses [PyMuPDF](https://pymupdf.readthedocs.io/) 1.24.10, which is also AGPL-3.0.

The complete corresponding source code is included in the release package under `source/`:

- `source/file-masker-<version>.tar.gz` — this project
- `source/pymupdf-1.24.10.tar.gz` — PyMuPDF

## Development

```bash
python3.10 -m venv .venv && .venv/bin/pip install -e ".[web,mail,dev]"
.venv/bin/pytest                         # engine, web service and mail mode tests

cd frontend && npm ci
npm test                                 # frontend tests
npm run build                            # writes ../file_masker/web/static
cd .. && .venv/bin/uvicorn file_masker.web.app:app --port 8080
```

- The masking engine (`file_masker/engine.py`) is also used as a library by Ginkgo's bank-statement service. Its only dependency is `pymupdf`; the web service lives behind the `web` extra.
- The page in `frontend/` is ported from the Ginkgo compliance-tools masking page. `frontend/copy-manifest.json` records the source commit and which files were changed, so later updates can be synced.
- Release: tag `v<version>` (matching `pyproject.toml`), check out the tag, then run `scripts/release.sh <version>`. The release package is written to `dist/`.
