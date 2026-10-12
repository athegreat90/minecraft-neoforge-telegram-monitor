"""A fake upstream world (Mojang, NeoForge Maven, Telegram) for end-to-end tests.

Everything is served from the real captured fixtures through ``httpx.MockTransport``; no test
ever touches the network or the real Telegram API.
"""

import copy
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from release_monitor.app import Monitor, build_monitor
from release_monitor.config import Config
from release_monitor.models import Source
from release_monitor.sources.minecraft import (
    ARTICLE_BASE,
    MANIFEST_URL,
    PATCH_NOTES_BASE,
    PATCH_NOTES_FEED_URL,
)
from release_monitor.sources.neoforge import METADATA_URL, changelog_url, pom_url
from release_monitor.storage import Repository, open_database
from release_monitor.timeutil import to_iso
from tests.fixtures_util import (
    FAKE_TOKEN,
    FakeWeb,
    fixture_json,
    fixture_text,
    make_config,
    no_sleep,
)
from tests.helpers import FakeClock

TELEGRAM_BASE = f"https://api.telegram.org/bot{FAKE_TOKEN}"
SEND_URL = f"{TELEGRAM_BASE}/sendMessage"
GET_ME_URL = f"{TELEGRAM_BASE}/getMe"
LAST_MODIFIED = "Mon, 12 Oct 2026 09:30:00 GMT"

MC_CHAT = "@mc_test_channel"
NF_CHAT = "@nf_test_channel"


@dataclass(frozen=True, slots=True)
class Sent:
    chat_id: str
    text: str


def _slug(version: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", version.lower()).strip("-")


def _metadata_xml(versions: list[str]) -> str:
    items = "".join(f"<version>{version}</version>" for version in versions)
    return (
        "<metadata><groupId>net.neoforged</groupId><artifactId>neoforge</artifactId>"
        f"<versioning><versions>{items}</versions></versioning></metadata>"
    )


class World:
    """Mutable fake upstream plus the monitor wired to it."""

    def __init__(self, directory: Path, **overrides: Any) -> None:
        self.directory = directory
        self.config: Config = make_config(
            db_path=directory / "data" / "releases.sqlite3", **overrides
        )
        self.web = FakeWeb()
        self.clock = FakeClock()
        self.messages: list[Sent] = []
        self.telegram_mode = "ok"  # ok | down | ambiguous | forbidden
        self.down: set[Source] = set()

        self.manifest: dict[str, Any] = copy.deepcopy(fixture_json("version_manifest_v2.json"))
        self.feed: dict[str, Any] = copy.deepcopy(fixture_json("java_patch_notes.json"))
        self.notes: dict[str, str] = {}
        self.articles: set[str] = set()
        self.nf_versions: list[str] = re.findall(
            r"<version>([^<]+)</version>", fixture_text("neoforge-maven-metadata.xml")
        )
        self.nf_entries: dict[str, str] = {}
        self.nf_changelogs: set[str] = set()
        self.nf_poms: set[str] = set()

        self.web.route(SEND_URL, self._telegram_send, method="POST")
        self.web.route(GET_ME_URL, self._telegram_get_me, method="POST")
        self.publish()
        self._open: list[Any] = []
        self.monitor: Monitor | None = None
        self.repo: Repository | None = None

    # -- upstream state -----------------------------------------------------------------

    def add_minecraft(
        self,
        version: str,
        *,
        kind: str = "release",
        notes: str | None = "<h1>Changes</h1><ul><li>Fixed a thing</ul>",
        article: bool = True,
    ) -> None:
        stamp = to_iso(self.clock.now).replace("Z", "+00:00")
        self.manifest["versions"].insert(
            0,
            {
                "id": version,
                "type": kind,
                "url": f"https://piston-meta.mojang.com/v1/packages/x/{version}.json",
                "time": stamp,
                "releaseTime": stamp,
                "sha1": "0" * 40,
                "complianceLevel": 1,
            },
        )
        if notes is not None:
            path = f"javaPatchNotes/{_slug(version)}.json"
            self.feed["entries"].insert(0, {"version": version, "type": kind, "contentPath": path})
            self.notes[path] = notes
        if article and kind == "release":
            self.articles.add(f"minecraft-java-edition-{_slug(version)}")
        self.publish()

    def add_neoforge(self, *versions: str, changelog: bool = True, pom: bool = True) -> None:
        self.nf_versions.extend(versions)
        for version in versions:
            if changelog:
                self.publish_changelog(version)
            if pom:
                self.nf_poms.add(version)
        self.publish()

    def publish_changelog(self, version: str, text: str | None = None) -> None:
        self.nf_entries[version] = text or f"Change shipped in {version} (#{len(self.nf_entries)})"
        self.nf_changelogs.add(version)
        self.publish()

    def take_down(self, source: Source) -> None:
        self.down.add(source)
        self.publish()

    def bring_up(self, source: Source) -> None:
        self.down.discard(source)
        self.publish()

    def _changelog_text(self) -> str:
        fresh = [f" - `{v}` {text}" for v, text in reversed(self.nf_entries.items())]
        return "\n".join([*fresh, fixture_text("neoforge-26.3.0.76-changelog.txt")])

    def publish(self) -> None:
        web = self.web
        if Source.MINECRAFT in self.down:
            web.add(MANIFEST_URL, error=httpx.ConnectError("manifest down"))
        else:
            web.add(MANIFEST_URL, text=json.dumps(self.manifest))
        web.add(PATCH_NOTES_FEED_URL, text=json.dumps(self.feed))
        for path, body in self.notes.items():
            web.add(PATCH_NOTES_BASE + path, text=json.dumps({"body": body}))
        for slug in self.articles:
            web.add(ARTICLE_BASE + slug, text="<html></html>")
        if Source.NEOFORGE in self.down:
            web.add(METADATA_URL, error=httpx.ConnectError("maven down"))
        else:
            web.add(METADATA_URL, text=_metadata_xml(self.nf_versions))
        text = self._changelog_text()
        for version in self.nf_changelogs:
            web.add(changelog_url(version), text=text)
        for version in self.nf_poms:
            web.add(pom_url(version), method="HEAD", headers={"Last-Modified": LAST_MODIFIED})

    # -- fake Telegram ------------------------------------------------------------------

    def _telegram_send(self, request: httpx.Request) -> httpx.Response:
        if self.telegram_mode == "ambiguous":
            raise httpx.ReadTimeout("no answer", request=request)
        if self.telegram_mode == "forbidden":
            return httpx.Response(
                403,
                json={"ok": False, "description": f"Forbidden: bot {FAKE_TOKEN} was blocked"},
                request=request,
            )
        if self.telegram_mode == "down":
            return httpx.Response(
                503, json={"ok": False, "description": "Service Unavailable"}, request=request
            )
        body = json.loads(request.content)
        self.messages.append(Sent(body["chat_id"], body["text"]))
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": len(self.messages)}}, request=request
        )

    @staticmethod
    def _telegram_get_me(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": True, "result": {"id": 1, "username": "test_bot"}}, request=request
        )

    def texts(self, chat_id: str) -> list[str]:
        return [item.text for item in self.messages if item.chat_id == chat_id]

    # -- the monitor under test ---------------------------------------------------------

    def start(self, **overrides: Any) -> Monitor:
        """(Re)open the database and build a monitor, like a process start would."""
        self.stop()
        config = replace(self.config, **overrides) if overrides else self.config
        conn = open_database(config.db_path)
        client = self.web.client()
        self._open = [conn, client]
        self.monitor = build_monitor(config, conn, client, clock=self.clock, sleep=no_sleep)
        self.repo = self.monitor.repository
        return self.monitor

    def stop(self) -> None:
        for resource in self._open:
            resource.close()
        self._open = []
        self.monitor = None
        self.repo = None

    def now(self) -> datetime:
        return self.clock.now
