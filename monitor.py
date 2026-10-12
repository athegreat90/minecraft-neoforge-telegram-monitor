#!/usr/bin/env python3
"""Poll official Minecraft and NeoForge sources; post new versions to Telegram."""
from __future__ import annotations

import argparse
import email.utils
import html
import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

LOG = logging.getLogger('release_monitor')
MOJANG_MANIFEST = 'https://piston-meta.mojang.com/mc/game/version_manifest_v2.json'
NEOFORGE_BASE = 'https://maven.neoforged.net/releases/net/neoforged/neoforge'
MC_NEWS = 'https://www.minecraft.net/en-us/article/'
DB_PATH = Path(os.getenv('DB_PATH', '/data/releases.sqlite3'))
USER_AGENT = 'MinecraftNeoForgeTelegramMonitor/1.0 (release announcements)'


def setting(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {'true', 'yes', '1', 'on'}


def request(url: str, *, data: bytes | None = None, method: str = 'GET',
            headers: dict[str, str] | None = None):
    request_headers = {'User-Agent': USER_AGENT, 'Accept': '*/*'}
    request_headers.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=request_headers)
    return urllib.request.urlopen(req, timeout=25)


def fetch_text(url: str) -> str:
    with request(url) as resp:
        return resp.read().decode('utf-8-sig', errors='replace')


def fetch_json(url: str):
    return json.loads(fetch_text(url))


def get_minecraft_versions() -> list[dict]:
    manifest = fetch_json(MOJANG_MANIFEST)
    include_snapshots = setting('MINECRAFT_SNAPSHOTS', False)
    allowed = {'release', 'snapshot'} if include_snapshots else {'release'}
    return [v for v in reversed(manifest['versions']) if v.get('type') in allowed]


def get_neoforge_versions() -> list[dict]:
    text = fetch_text(f'{NEOFORGE_BASE}/maven-metadata.xml')
    root = ET.fromstring(text)
    min_major = int(os.getenv('NEOFORGE_MIN_MAJOR', '26'))
    include_betas = setting('NEOFORGE_BETAS', True)
    versions = []
    for elt in root.findall('.//version'):
        v = (elt.text or '').strip()
        match = re.match(r'^(\d+)\.', v)
        if not match or int(match.group(1)) < min_major:
            continue
        if not include_betas and re.search(r'(alpha|beta|rc|snapshot|pre)', v, re.I):
            continue
        versions.append({'id': v})
    # Maven metadata normally lists versions in publication order.
    return versions


def date_string(iso: str | None, *, fallback: str = 'Unavailable') -> str:
    if not iso:
        return fallback
    try:
        dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
        return dt.astimezone(ZoneInfo('Europe/Madrid')).strftime('%d %b %Y, %H:%M %Z')
    except (ValueError, TypeError):
        return fallback


def neoforge_changelog_url(version: str) -> str:
    safe = urllib.parse.quote(version, safe='.-_')
    return f'{NEOFORGE_BASE}/{safe}/neoforge-{safe}-changelog.txt'


def neoforge_details(version: str) -> tuple[str, str, str]:
    url = neoforge_changelog_url(version)
    try:
        with request(url) as resp:
            data = resp.read().decode('utf-8-sig', errors='replace')
            lm = resp.headers.get('Last-Modified')
        date = ''
        if lm:
            dt = email.utils.parsedate_to_datetime(lm)
            date = dt.astimezone(ZoneInfo('Europe/Madrid')).strftime('%d %b %Y, %H:%M %Z')
        # The file is cumulative: find the actual version being announced.
        line_re = re.compile(r'^\s*-\s*`' + re.escape(version) + r'`\s*(.*)$')
        change = next((m.group(1).strip() for line in data.splitlines()
                       if (m := line_re.match(line))), '')
        return change or 'See the official cumulative changelog.', date, url
    except (urllib.error.URLError, OSError, ValueError) as ex:
        LOG.warning('NeoForge changelog %s unavailable: %s', version, ex)
        return 'See the official changelog when available.', '', url


def minecraft_release_notes(version: str) -> tuple[list[str], str | None]:
    """Retrieve a short non-exhaustive highlights list from official Minecraft.net.

    URL detection is intentionally conservative: never claim an unverified article exists.
    """
    slug = 'minecraft-java-edition-' + re.sub(r'[^a-z0-9]+', '-', version.lower()).strip('-')
    url = MC_NEWS + slug
    try:
        page = fetch_text(url)
        soup = BeautifulSoup(page, 'html.parser')
        h1 = soup.find('h1')
        if not h1 or version.lower() not in h1.get_text(' ', strip=True).lower():
            return [], None
        highlights = []
        for heading in soup.find_all(['h2', 'h3']):
            title = heading.get_text(' ', strip=True).lower()
            if not (title.startswith('new features') or title == 'changes'
                    or title.startswith('fixed bugs') or title == 'technical changes'):
                continue
            item = heading.find_next(['h1', 'h2', 'h3', 'ul', 'ol'])
            if item is None or item.name not in {'ul', 'ol'}:
                continue
            for li in item.find_all('li', recursive=False):
                desc = li.get_text(' ', strip=True)
                if desc and len(desc) < 220 and desc not in highlights:
                    highlights.append(desc)
                if len(highlights) >= 5:
                    return highlights, url
        return highlights, url
    except (urllib.error.URLError, OSError) as ex:
        LOG.info('Minecraft release notes for %s not available yet: %s', version, ex)
        return [], None


def minecraft_message(info: dict) -> str:
    ver = info['id']
    kind = info.get('type', 'release')
    kind_label = 'Stable release' if kind == 'release' else 'Snapshot / pre-release'
    release_time = date_string(info.get('releaseTime'))
    lines = [f'⛏️ <b>Minecraft Java — {html.escape(ver)}</b>',
             f'🏷️ Type: {kind_label}', f'📅 Released: {release_time}']
    notes_url = None
    notes = []
    if kind == 'release':
        notes, notes_url = minecraft_release_notes(ver)
    if notes:
        lines.append('\n<b>Changelog highlights</b>')
        lines.extend(f'• {html.escape(n)}' for n in notes)
    elif kind == 'release':
        lines.append('\nℹ️ Official notes may not have been published yet.')
    if notes_url:
        lines.append(f'\n📖 <a href="{html.escape(notes_url, quote=True)}">Official full changelog</a>')
    else:
        lines.append('\n📖 <a href="https://www.minecraft.net/en-us/articles">Minecraft news</a>')
    return '\n'.join(lines)[:3900]


def neoforge_message(info: dict) -> str:
    v = info['id']
    change, artifact_date, url = neoforge_details(v)
    build_type = 'Beta / pre-release' if re.search(r'alpha|beta|rc|snapshot|pre', v, re.I) else 'Regular build'
    stamp = (f'📅 Artifact modified: {artifact_date}' if artifact_date
             else '📅 Publication date unavailable; detected by this monitor')
    return (f'🛠️ <b>NeoForge — {html.escape(v)}</b>\n'
            f'🏷️ Type: {build_type}\n{stamp}\n\n'
            f'<b>Changes in this build</b>\n• {html.escape(change[:2300])}\n\n'
            f'📖 <a href="{html.escape(url, quote=True)}">Full cumulative changelog</a>')


def send_telegram(bot_token: str, chat_id: str, message: str) -> None:
    payload = urllib.parse.urlencode({'chat_id': chat_id, 'text': message,
                                      'parse_mode': 'HTML', 'disable_web_page_preview': 'true'}).encode()
    # Keep token out of exception/log output, even if a network library includes URL in its error.
    url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
    try:
        with request(url, data=payload, method='POST',
                     headers={'Content-Type': 'application/x-www-form-urlencoded'}) as resp:
            result = json.loads(resp.read())
        if not result.get('ok'):
            raise RuntimeError('Telegram returned an unsuccessful result')
    except urllib.error.HTTPError as ex:
        raise RuntimeError(f'Telegram returned HTTP {ex.code}; check chat ID, token, admin permissions, or message') from None
    except urllib.error.URLError:
        raise RuntimeError('Could not connect to Telegram') from None


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute('CREATE TABLE IF NOT EXISTS sent '
               '(source TEXT NOT NULL, version TEXT NOT NULL, sent_at TEXT NOT NULL, '
               'PRIMARY KEY (source, version))')
    db.execute('CREATE TABLE IF NOT EXISTS initialized '
               '(source TEXT PRIMARY KEY, started_at TEXT NOT NULL)')
    db.commit()
    return db


def is_initialized(db: sqlite3.Connection, source: str) -> bool:
    return db.execute('SELECT 1 FROM initialized WHERE source=?', (source,)).fetchone() is not None


def remember(db: sqlite3.Connection, source: str, v: str) -> None:
    db.execute('INSERT OR IGNORE INTO sent VALUES (?, ?, ?)',
               (source, v, datetime.now(timezone.utc).isoformat()))
    db.commit()


def process_source(db: sqlite3.Connection, *, source: str, channel: str,
                   entries: list[dict], formatter, token: str) -> None:
    if not is_initialized(db, source):
        # Seed existing versions on first run. No thousands of historical posts.
        now = datetime.now(timezone.utc).isoformat()
        with db:
            db.executemany('INSERT OR IGNORE INTO sent VALUES (?, ?, ?)',
                           [(source, item['id'], now) for item in entries])
            db.execute('INSERT INTO initialized VALUES (?, ?)', (source, now))
        LOG.info('%s initialized; skipped %s historical versions', source, len(entries))
        return
    for info in entries:
        version = info['id']
        if db.execute('SELECT 1 FROM sent WHERE source=? AND version=?',
                      (source, version)).fetchone():
            continue
        try:
            message = formatter(info)
            send_telegram(token, channel, message)
            remember(db, source, version)  # Only commit after Telegram accepts it
            LOG.info('Published %s %s', source, version)
        except Exception as ex:
            LOG.error('Failed to publish %s %s: %s', source, version, ex)
            # Leave unsent for the next check.


def check(db: sqlite3.Connection, token: str, mc_id: str, neo_id: str) -> None:
    for source, chat_id, getter, formatter in (
        ('minecraft', mc_id, get_minecraft_versions, minecraft_message),
        ('neoforge', neo_id, get_neoforge_versions, neoforge_message),
    ):
        try:
            entries = getter()
            if not entries:
                raise ValueError('empty versions list; not initializing')
            process_source(db, source=source, channel=chat_id, entries=entries,
                           formatter=formatter, token=token)
        except (urllib.error.URLError, ValueError, ET.ParseError, KeyError) as ex:
            LOG.error('Could not fetch %s versions: %s', source, ex)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='Perform one polling cycle')
    parser.add_argument('--test', action='store_true', help='Send test posts to both channels without changing state')
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO').upper(),
                        format='%(asctime)s %(levelname)s %(message)s')
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    mc_id = os.environ.get('MINECRAFT_CHANNEL_ID', '').strip()
    neo_id = os.environ.get('NEOFORGE_CHANNEL_ID', '').strip()
    if not token or not mc_id or not neo_id:
        parser.error('Set TELEGRAM_BOT_TOKEN, MINECRAFT_CHANNEL_ID and NEOFORGE_CHANNEL_ID')
    if args.test:
        send_telegram(token, mc_id, '✅ <b>Minecraft monitor connected</b>')
        send_telegram(token, neo_id, '✅ <b>NeoForge monitor connected</b>')
        LOG.info('Test messages sent successfully')
        return
    interval = max(60, int(os.getenv('POLL_SECONDS', '300')))
    with connect_db() as db:
        while True:
            check(db, token, mc_id, neo_id)
            if args.once:
                break
            time.sleep(interval)


if __name__ == '__main__':
    main()
