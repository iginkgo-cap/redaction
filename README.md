# Redaction Service

Self-hosted redaction for text, JSON and PDF files. It finds personal and financial data (names,
phone numbers, e-mail addresses, ID and bank card numbers, IBAN / SWIFT, crypto addresses, …) in
Chinese and English, replaces it with placeholders such as `[[PERSON_3]]`, and restores the original
values later — so documents can be sent to an external LLM or partner without exposing the data.

> 中文简介：自托管的脱敏服务。识别中英文文本、JSON 和 PDF 里的人名、电话、邮箱、证件号、银行卡、
> IBAN/SWIFT、加密地址等敏感信息，替换成 `[[PERSON_3]]` 这样的占位符，需要时再还原。
> 一条命令装在你自己的 Linux 服务器上，数据不出服务器。

What you get on your server:

| Component | What it does |
|---|---|
| Redaction API (`/v2`) | detect / redact / restore for text, JSON and PDF; placeholders are stored per namespace, values encrypted (AES-GCM) |
| PDF masking page | upload PDFs in the browser, mask the keywords you enter, download the result |
| Postgres | stores the placeholder ↔ value mappings |
| Caddy gateway | single entry point; login for the web page; automatic HTTPS when you give it a domain |

Everything runs in Docker on one machine. After installation the only outbound connections are the
ones you make (pulling updates); requests are processed on the server.

## Install

Requirements: a Linux server (Ubuntu, Debian, RHEL / Rocky / Alma …), amd64 or arm64, **4 CPUs,
8 GB RAM, 20 GB free disk**, root access, and internet access during installation. Docker is installed
automatically if it is missing.

```bash
curl -fsSL https://github.com/iginkgo-cap/redaction/releases/latest/download/install.sh | sudo bash
```

With your own domain (its DNS must already point to the server; ports 80 and 443 open) you get HTTPS:

```bash
curl -fsSL https://github.com/iginkgo-cap/redaction/releases/latest/download/install.sh | sudo bash -s -- --domain redact.example.com
```

The installer prints the web login, the API tokens and the URL at the end, and saves them to
`/opt/redaction/credentials.txt` (root only). **Back up `/opt/redaction/.env`**: it holds the encryption
keys, and without them the stored values cannot be decrypted.

Useful options (`install.sh --help` for all):

| Option | Meaning |
|---|---|
| `--domain <name>` | HTTPS with an automatic Let's Encrypt certificate |
| `--http-port <port>` / `--https-port <port>` | use other host ports (default 80 / 443) |
| `--hf-mirror <url>` | download the model from a Hugging Face mirror, e.g. `https://hf-mirror.com` (mainland China) |
| `--docker-mirror <name>` | mirror for installing Docker, e.g. `Aliyun` |
| `--version <tag>` | install a specific release |
| `--registry <prefix>` | pull images from your own registry instead of `ghcr.io/iginkgo-cap` |

Without `--domain` the service is served over plain HTTP: keep the port behind a firewall or VPN.

## Use

### PDF masking page

Open the URL from the installer, log in, add PDF files and the words to mask, check the preview and
download. Matching ignores case and extra spaces; scanned pages (images without text) cannot be masked.
**Masking can miss text — always check each PDF before sharing it.**

### API

All `/v2` calls are `POST` with JSON and need the header `X-Redaction-Token: <app token>`. The installer
creates one namespace, `default`.

Redact:

```bash
curl -s https://redact.example.com/v2/redact \
  -H "X-Redaction-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"namespace":"default","items":[{"id":"m1","text":"Please call Alice Chen at +1 415 555 0100"}]}'
```

```json
{"items":[{"id":"m1","text":"Please call [[PERSON_1]] at [[PHONE_1]]"}],
 "entities":[{"placeholder":"[[PERSON_1]]","value":"Alice Chen","kind":"PERSON","new":true, "...": "..."}],
 "degraded":false,"detectors_failed":[],"version":"v1.0.0-rc.3"}
```

Restore (e.g. after an LLM has answered using the placeholders):

```bash
curl -s https://redact.example.com/v2/restore \
  -H "X-Redaction-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"namespace":"default","items":[{"id":"a1","text":"I have called [[PERSON_1]]."}]}'
```

| Endpoint | Purpose |
|---|---|
| `/v2/detect` | find sensitive values, store nothing |
| `/v2/redact` | replace with placeholders and store the mapping; items can be `text`, `json` or `file` (PDF, base64) |
| `/v2/restore` | replace placeholders with the original values |
| `/v2/mappings/lookup` | placeholder → value, for callers that restore streams themselves |
| `/v2/namespaces/remove` | delete all data of a namespace (admin token) |

The same value always gets the same placeholder within a namespace. Use separate namespaces for data
that must not share placeholders (for example one per customer); see
[`config/namespaces.example.yaml`](config/namespaces.example.yaml) and edit
`/opt/redaction/namespaces.yaml`, then `docker compose -f /opt/redaction/docker-compose.yml restart redaction`.

## Operate

| Task | Command |
|---|---|
| Upgrade | re-run the install command (or add `--version <tag>`); keys, tokens and data are kept |
| Status / logs | `docker ps`, `docker logs redaction` |
| Back up the database | `bash /opt/redaction/backup.sh /var/backups/redaction 14` (add it to cron) |
| Stop and remove containers (keep data) | `sudo bash install.sh --uninstall` |
| Remove everything | `sudo bash install.sh --uninstall --purge` |

Each backup is a directory with one dump per database. To restore (also onto a new server installed
with the **same** `/opt/redaction/.env`), follow the commands at the top of `/opt/redaction/backup.sh`.

## Limitations

- Detection is statistical: it misses some values and flags some harmless ones. Review results where
  it matters.
- Chinese person and organisation names rely on a multilingual model and on patterns; recall is lower
  than for English names and for structured data (phone, ID, card numbers).
- `json` items: only string values are processed, not keys.
- PDF: text-layer PDFs only; scanned pages are kept as they are and reported as `scanned`.

### Optional: HanLP for Chinese names

Recognition of Chinese names improves with [HanLP](https://github.com/hankcs/HanLP). It is **not
included**: the `hanlp` package is Apache-2.0, but its pretrained models are licensed **CC BY-NC-SA 4.0
(non-commercial)**. If your use is permitted under that license (or you hold a commercial license from
the HanLP authors), build your own image with `--build-arg WITH_HANLP=1`, download the model with
`python scripts/download_models.py --with-hanlp`, and add `hanlp` to `REDACTION_DETECTORS`.

## Build from source

```bash
docker build -t redaction-service .                 # API
docker build -t file-masker file-masker             # PDF masking page
```

Development: Python 3.11, `pip install -r requirements.txt`, `pytest -m "not slow and not api"`.

## License

[AGPL-3.0](LICENSE). If you modify the service and let others use it over a network, you must offer them
the source of your version. Third-party components and their licenses: [NOTICE](NOTICE).
