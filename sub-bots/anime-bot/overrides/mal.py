"""
LJTA (Learn Japanese Through Anime) - MyAnimeList link (anime bot only)

Members link their MyAnimeList account with /mal link. Then, in both directions:
- Discord -> MAL: every /log_anime adds those episodes to that anime on their MAL list.
- MAL -> Discord: every few minutes, new episodes they mark as watched on MAL are logged
  as normal anime logs and posted in the MAL log channel, looking the same as /log_anime.
  At most 5 hours a day come in this way. When a member reaches the cap they're pinged
  once, and further MAL updates that day are dropped (not logged or posted, ever).

Setup (.env), after registering an app at https://myanimelist.net/apiconfig:
  MAL_CLIENT_ID       the app's Client ID (required)
  MAL_CLIENT_SECRET   the app's Client Secret, if it has one
  MAL_REDIRECT_URI    the App Redirect URL registered for the app (e.g. http://localhost/)
Linking works without a public website: after approving on MAL, the member copies the
address their browser lands on and pastes it into the bot.
"""

import asyncio
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SETTINGS - edit IDs, limits and messages here.
# ---------------------------------------------------------------------------
MAL_LOG_CHANNEL_ID = 1444163978749280276   # MAL -> Discord logs and cap notices go here
DAILY_CAP_HOURS = 5                       # max anime time per member per day (UTC) from MAL
POLL_MINUTES = 10                         # how often linked lists are checked
FALLBACK_EPISODE_MINUTES = 24             # when MAL doesn't know an episode's length

# Posted once when a member reaches the cap. {user} pings them; {hours} is the cap.
CAP_MESSAGE = (
    "{user} you've reached your daily MAL cap limit of {hours} hours. "
    "No more MyAnimeList updates will be logged for you today."
)
# Shown above each MAL log so the channel shows whose log it is (no ping).
IMPORT_HEADER = "{user} logged on MyAnimeList"

AUTH_URL = "https://myanimelist.net/v1/oauth2/authorize"
TOKEN_URL = "https://myanimelist.net/v1/oauth2/token"
API_URL = "https://api.myanimelist.net/v2"
ANILIST_URL = "https://graphql.anilist.co"
LIST_FIELDS = "list_status,num_episodes,average_episode_duration"
LINK_TIMEOUT_MINUTES = 15


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class PasteLinkModal(discord.ui.Modal, title="Finish linking MyAnimeList"):
    address = discord.ui.TextInput(
        label="Address you landed on after pressing Allow",
        style=discord.TextStyle.paragraph,
        placeholder="http://localhost/?code=...&state=...",
        max_length=2000,
    )

    def __init__(self, cog: "MalCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.finish_link(interaction, str(self.address.value))


class LinkView(discord.ui.View):
    def __init__(self, cog: "MalCog", auth_url: str):
        super().__init__(timeout=LINK_TIMEOUT_MINUTES * 60)
        self.cog = cog
        self.add_item(discord.ui.Button(label="1. Open MyAnimeList", style=discord.ButtonStyle.link, url=auth_url))

    @discord.ui.button(label="2. Paste the address", style=discord.ButtonStyle.primary)
    async def paste(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PasteLinkModal(self.cog))


class MalCog(commands.Cog):
    """Two-way MyAnimeList sync for the anime bot"""

    mal = app_commands.Group(name="mal", description="Link your MyAnimeList account", guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        # The main data/ folder (next to vnbot.db), which Docker keeps between rebuilds.
        # A separate file, so MAL logins stay out of the shared database and its backups.
        data_dir = Path(__file__).resolve().parents[3] / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = str(data_dir / "anime_mal.db")
        self.pending: Dict[str, Tuple[str, str, datetime]] = {}   # user_id -> (verifier, state, started)
        self.lock = asyncio.Lock()   # one MAL sync at a time, so pushes and imports never cross
        self.session: Optional[aiohttp.ClientSession] = None

    # ---------------- setup ----------------

    @property
    def client_id(self) -> str:
        return os.getenv("MAL_CLIENT_ID", "").strip()

    @property
    def client_secret(self) -> str:
        return os.getenv("MAL_CLIENT_SECRET", "").strip()

    @property
    def redirect_uri(self) -> str:
        return os.getenv("MAL_REDIRECT_URI", "").strip()

    async def cog_load(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS mal_links (
                    user_id TEXT PRIMARY KEY,
                    mal_user_id INTEGER UNIQUE,
                    mal_name TEXT,
                    access_token TEXT,
                    refresh_token TEXT,
                    expires_at TEXT,
                    linked_at TEXT,
                    broken INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS mal_snapshot (
                    user_id TEXT NOT NULL,
                    anime_id INTEGER NOT NULL,
                    episodes INTEGER NOT NULL,
                    PRIMARY KEY (user_id, anime_id)
                );
                CREATE TABLE IF NOT EXISTS mal_daily (
                    user_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    seconds INTEGER NOT NULL DEFAULT 0,
                    capped INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, day)
                );
                CREATE TABLE IF NOT EXISTS mal_anilist (
                    mal_id INTEGER PRIMARY KEY,
                    anilist_id INTEGER
                );
            """)
            await db.commit()
        self.poll.start()

    async def cog_unload(self):
        self.poll.cancel()
        if self.session and not self.session.closed:
            await self.session.close()

    # ---------------- storage ----------------

    async def get_link(self, user_id: str) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM mal_links WHERE user_id = ?", (user_id,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def all_links(self) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM mal_links WHERE broken = 0") as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def save_tokens(self, link: Dict[str, Any], tokens: Dict[str, Any]):
        link["access_token"] = tokens["access_token"]
        link["refresh_token"] = tokens.get("refresh_token") or link.get("refresh_token")
        link["expires_at"] = (now_utc() + timedelta(seconds=int(tokens.get("expires_in") or 3600))).isoformat()
        link["broken"] = 0
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE mal_links SET access_token = ?, refresh_token = ?, expires_at = ?, broken = 0 WHERE user_id = ?",
                (link["access_token"], link["refresh_token"], link["expires_at"], link["user_id"]),
            )
            await db.commit()

    async def get_snapshot(self, user_id: str) -> Dict[int, int]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT anime_id, episodes FROM mal_snapshot WHERE user_id = ?", (user_id,)) as cur:
                return {int(a): int(e) for a, e in await cur.fetchall()}

    async def set_snapshot(self, user_id: str, anime_id: int, episodes: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO mal_snapshot (user_id, anime_id, episodes) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, anime_id) DO UPDATE SET episodes = excluded.episodes",
                (user_id, anime_id, episodes),
            )
            await db.commit()

    async def get_daily(self, user_id: str, day: str) -> Tuple[int, bool]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT seconds, capped FROM mal_daily WHERE user_id = ? AND day = ?", (user_id, day)) as cur:
                row = await cur.fetchone()
                return (int(row[0]), bool(row[1])) if row else (0, False)

    async def set_daily(self, user_id: str, day: str, seconds: int, capped: bool):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO mal_daily (user_id, day, seconds, capped) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, day) DO UPDATE SET seconds = excluded.seconds, capped = excluded.capped",
                (user_id, day, seconds, int(capped)),
            )
            await db.commit()

    # ---------------- HTTP ----------------

    async def _http(self, method: str, url: str, *, headers=None, params=None, data=None) -> Tuple[int, Dict[str, Any]]:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        async with self.session.request(method, url, headers=headers, params=params, data=data) as resp:
            try:
                body = await resp.json(content_type=None)
            except Exception:
                body = None
            return resp.status, body if isinstance(body, dict) else {}

    async def _token_request(self, fields: Dict[str, str]) -> Optional[Dict[str, Any]]:
        data = dict(fields, client_id=self.client_id)
        if self.client_secret:
            data["client_secret"] = self.client_secret
        status, body = await self._http("POST", TOKEN_URL, data=data)
        if status != 200 or "access_token" not in body:
            logger.warning(f"[MAL] Token request ({fields.get('grant_type')}) failed: {status} {body.get('error')}")
            return None
        return body

    async def _refresh(self, link: Dict[str, Any]) -> Optional[str]:
        tokens = await self._token_request({"grant_type": "refresh_token", "refresh_token": link.get("refresh_token") or ""})
        if tokens is None:
            link["broken"] = 1
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("UPDATE mal_links SET broken = 1 WHERE user_id = ?", (link["user_id"],))
                await db.commit()
            logger.warning(f"[MAL] Login for {link['user_id']} expired; they need to /mal link again")
            return None
        await self.save_tokens(link, tokens)
        return link["access_token"]

    async def _api(self, link: Dict[str, Any], method: str, path: str, *, params=None, data=None) -> Tuple[int, Dict[str, Any]]:
        """Call the MAL API as this member, refreshing their login when needed."""
        expires = datetime.fromisoformat(link["expires_at"]) if link.get("expires_at") else now_utc()
        token = link.get("access_token")
        if not token or expires - now_utc() < timedelta(minutes=5):
            token = await self._refresh(link)
            if token is None:
                return 401, {}
        url = path if path.startswith("http") else API_URL + path
        status, body = await self._http(method, url, headers={"Authorization": f"Bearer {token}"}, params=params, data=data)
        if status == 401:
            token = await self._refresh(link)
            if token is None:
                return 401, {}
            status, body = await self._http(method, url, headers={"Authorization": f"Bearer {token}"}, params=params, data=data)
        return status, body

    async def fetch_list(self, link: Dict[str, Any], *, all_pages: bool) -> Optional[List[Dict[str, Any]]]:
        """The member's anime list, most recently updated first (one page of 100, or all)."""
        entries: List[Dict[str, Any]] = []
        path = "/users/@me/animelist"
        params = {"fields": LIST_FIELDS, "sort": "list_updated_at", "limit": "100", "nsfw": "true"}
        while path:
            status, body = await self._api(link, "GET", path, params=params)
            if status != 200:
                logger.warning(f"[MAL] Couldn't read the list of {link['user_id']}: {status}")
                return None
            entries.extend(body.get("data") or [])
            path = (body.get("paging") or {}).get("next") if all_pages else None
            params = None   # the "next" link already carries them
        return entries

    async def anilist_name(self, mal_id: int, title: str) -> str:
        """What to give the anime log builder: the AniList page for this MAL anime (so the log
        shows the same title, cover and button as /log_anime), or MAL's title if not on AniList."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT anilist_id FROM mal_anilist WHERE mal_id = ?", (mal_id,)) as cur:
                row = await cur.fetchone()
        if row is not None:
            anilist_id = row[0]
        else:
            query = "query ($id: Int) { Media(idMal: $id, type: ANIME) { id } }"
            try:
                if self.session is None or self.session.closed:
                    self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
                async with self.session.post(ANILIST_URL, json={"query": query, "variables": {"id": mal_id}}) as resp:
                    body = await resp.json(content_type=None)
                    status = resp.status
            except Exception as e:
                logger.warning(f"[MAL] AniList lookup for MAL {mal_id} failed: {e}")
                return title   # try again next time
            if status not in (200, 404):   # 404 = AniList doesn't have it
                logger.warning(f"[MAL] AniList lookup for MAL {mal_id} answered {status}")
                return title
            media = ((body or {}).get("data") or {}).get("Media") or {}
            anilist_id = media.get("id")
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("INSERT OR REPLACE INTO mal_anilist (mal_id, anilist_id) VALUES (?, ?)", (mal_id, anilist_id))
                await db.commit()
        return f"https://anilist.co/anime/{anilist_id}" if anilist_id else title

    # ---------------- linking ----------------

    @mal.command(name="link", description="Link your MyAnimeList account to sync anime logs both ways")
    async def mal_link(self, interaction: discord.Interaction):
        if not self.client_id:
            await interaction.response.send_message(
                "MyAnimeList linking isn't set up yet. An admin needs to add MAL_CLIENT_ID to the bot's settings.",
                ephemeral=True,
            )
            return
        verifier = secrets.token_urlsafe(96)[:128]   # PKCE code verifier (43-128 chars)
        state = secrets.token_urlsafe(16)
        self.pending[str(interaction.user.id)] = (verifier, state, now_utc())
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "state": state,
            "code_challenge": verifier,          # MAL only supports the "plain" method
            "code_challenge_method": "plain",
        }
        if self.redirect_uri:
            params["redirect_uri"] = self.redirect_uri
        auth_url = f"{AUTH_URL}?{urlencode(params)}"
        text = (
            "Link your MyAnimeList account\n"
            "1. Press Open MyAnimeList, log in if asked, and press Allow.\n"
            "2. Your browser then opens a page that may not load. That's expected: copy the whole "
            "address from the address bar.\n"
            "3. Press Paste the address and paste it.\n\n"
            "Once linked, your `/log_anime` logs are added to your MAL list, and episodes you mark as "
            f"watched on MAL are logged here (up to {DAILY_CAP_HOURS} hours a day). "
            f"This link works for {LINK_TIMEOUT_MINUTES} minutes."
        )
        await interaction.response.send_message(text, view=LinkView(self, auth_url), ephemeral=True)

    async def finish_link(self, interaction: discord.Interaction, pasted: str):
        user_id = str(interaction.user.id)
        pending = self.pending.get(user_id)
        if not pending or now_utc() - pending[2] > timedelta(minutes=LINK_TIMEOUT_MINUTES):
            await interaction.response.send_message("That link expired. Please start again with `/mal link`.", ephemeral=True)
            return
        verifier, state, _ = pending
        pasted = pasted.strip()
        query = parse_qs(urlparse(pasted).query) if "code=" in pasted else {}
        code = (query.get("code") or [pasted if "=" not in pasted else ""])[0]
        if not code or (query.get("state") and query["state"][0] != state):
            await interaction.response.send_message(
                "That doesn't look like the right address. Please start again with `/mal link`.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)

        fields = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier}
        if self.redirect_uri:
            fields["redirect_uri"] = self.redirect_uri
        tokens = await self._token_request(fields)
        if tokens is None:
            await interaction.followup.send("MyAnimeList didn't accept that. Please start again with `/mal link`.", ephemeral=True)
            return
        self.pending.pop(user_id, None)

        link = {"user_id": user_id, "access_token": tokens["access_token"], "refresh_token": tokens.get("refresh_token"),
                "expires_at": (now_utc() + timedelta(seconds=int(tokens.get("expires_in") or 3600))).isoformat()}
        status, me = await self._api(link, "GET", "/users/@me")
        if status != 200 or "id" not in me:
            await interaction.followup.send("Couldn't read your MyAnimeList account. Please try again later.", ephemeral=True)
            return

        async with self.lock:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("SELECT user_id FROM mal_links WHERE mal_user_id = ?", (me["id"],)) as cur:
                    row = await cur.fetchone()
                if row and row[0] != user_id:
                    await interaction.followup.send(
                        "That MyAnimeList account is already linked to another Discord account.", ephemeral=True
                    )
                    return
                await db.execute("DELETE FROM mal_snapshot WHERE user_id = ?", (user_id,))
                await db.execute(
                    "INSERT OR REPLACE INTO mal_links (user_id, mal_user_id, mal_name, access_token, refresh_token, "
                    "expires_at, linked_at, broken) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                    (user_id, me["id"], me.get("name"), link["access_token"], link["refresh_token"],
                     link["expires_at"], now_utc().isoformat()),
                )
                await db.commit()
            link = await self.get_link(user_id)
            # Starting point: what's already on the list is NOT logged, only what changes from now on
            entries = await self.fetch_list(link, all_pages=True) or []
            for entry in entries:
                await self.set_snapshot(user_id, entry["node"]["id"], int(entry["list_status"].get("num_episodes_watched") or 0))

        await interaction.followup.send(
            f"✅ Linked to MyAnimeList account {me.get('name')}. Your existing list ({len(entries)} anime) "
            "isn't logged; only episodes you mark as watched from now on.",
            ephemeral=True,
        )
        logger.info(f"[MAL] {interaction.user} ({user_id}) linked MAL account {me.get('name')} ({me['id']})")

    @mal.command(name="unlink", description="Unlink your MyAnimeList account")
    async def mal_unlink(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        async with self.lock:
            async with aiosqlite.connect(self.db_path) as db:
                cur = await db.execute("DELETE FROM mal_links WHERE user_id = ?", (user_id,))
                await db.execute("DELETE FROM mal_snapshot WHERE user_id = ?", (user_id,))
                await db.commit()
                removed = cur.rowcount
        self.pending.pop(user_id, None)
        await interaction.response.send_message(
            "Unlinked. Nothing syncs with MyAnimeList any more." if removed else "You don't have a MyAnimeList account linked.",
            ephemeral=True,
        )

    @mal.command(name="status", description="See your MyAnimeList link and today's MAL time")
    async def mal_status(self, interaction: discord.Interaction):
        link = await self.get_link(str(interaction.user.id))
        if not link:
            await interaction.response.send_message("No MyAnimeList account linked. Use `/mal link`.", ephemeral=True)
            return
        seconds, capped = await self.get_daily(link["user_id"], now_utc().date().isoformat())
        lines = [f"Linked to {link['mal_name']}."]
        if link["broken"]:
            lines.append("⚠️ The MyAnimeList login expired, so nothing syncs. Please `/mal link` again.")
        lines.append(
            f"Logged from MyAnimeList today: {seconds / 3600:.1f}h of {DAILY_CAP_HOURS}h"
            + (" (cap reached; more MAL updates today are ignored)." if capped else ".")
        )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ---------------- Discord -> MAL ----------------

    @commands.Cog.listener()
    async def on_anime_activity_logged(self, activity, source: str):
        """After /log_anime: add the episodes to the member's MAL list."""
        if source != "command" or not self.client_id:
            return
        link = await self.get_link(str(activity.user_id))
        if not link or link["broken"]:
            return
        mal_id = ((activity.meta or {}).get("anilist") or {}).get("mal_id")
        episodes = int((activity.meta or {}).get("episodes") or 0)
        if not mal_id or episodes <= 0:
            return
        try:
            async with self.lock:
                status, details = await self._api(link, "GET", f"/anime/{mal_id}", params={"fields": "num_episodes,my_list_status"})
                if status != 200:
                    logger.warning(f"[MAL] Couldn't read MAL anime {mal_id} for {link['user_id']}: {status}")
                    return
                current = details.get("my_list_status") or {}
                if current.get("status") == "completed":
                    return   # already finished on MAL: leave rewatches to the member
                total = int(details.get("num_episodes") or 0)   # 0 = unknown (still airing)
                watched = int(current.get("num_episodes_watched") or 0) + episodes
                new_status = "watching"
                if total and watched >= total:
                    watched, new_status = total, "completed"
                status, _ = await self._api(
                    link, "PATCH", f"/anime/{mal_id}/my_list_status",
                    data={"num_watched_episodes": str(watched), "status": new_status},
                )
                if status != 200:
                    logger.warning(f"[MAL] Couldn't update MAL anime {mal_id} for {link['user_id']}: {status}")
                    return
                # So the MAL -> Discord check doesn't log these episodes a second time
                await self.set_snapshot(link["user_id"], int(mal_id), watched)
        except Exception as e:
            logger.exception(f"[MAL] Sync to MAL failed for {link['user_id']}: {e}")

    # ---------------- MAL -> Discord ----------------

    @tasks.loop(minutes=POLL_MINUTES)
    async def poll(self):
        if not self.client_id:
            return
        channel = self.bot.get_channel(MAL_LOG_CHANNEL_ID)
        if channel is None:
            logger.warning(f"[MAL] MAL log channel {MAL_LOG_CHANNEL_ID} not found; MAL -> Discord paused")
            return
        for link in await self.all_links():
            try:
                await self.import_from_mal(link, channel)
            except Exception as e:
                logger.exception(f"[MAL] Import failed for {link['user_id']}: {e}")

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    async def import_from_mal(self, link: Dict[str, Any], channel: discord.TextChannel):
        """Log new episodes the member marked on MAL, up to the daily cap."""
        guild = channel.guild
        member = guild.get_member(int(link["user_id"]))
        immersion = self.bot.get_cog("ImmersionCog")
        if immersion is None:
            return
        imported = False
        async with self.lock:
            entries = await self.fetch_list(link, all_pages=False)
            if entries is None:
                return
            snapshot = await self.get_snapshot(link["user_id"])
            day = now_utc().date().isoformat()
            used, capped = await self.get_daily(link["user_id"], day)
            banned = member is not None and hasattr(immersion, "_is_banned_from_logging") \
                and immersion._is_banned_from_logging(member)

            for entry in reversed(entries):   # oldest change first, so the cap applies in order
                node, list_status = entry["node"], entry.get("list_status") or {}
                anime_id = int(node["id"])
                watched = int(list_status.get("num_episodes_watched") or 0)
                before = snapshot.get(anime_id, 0)   # not seen before = added to the list since linking
                if watched == before:
                    continue
                # Recorded first: whatever happens next, these episodes are never logged twice
                await self.set_snapshot(link["user_id"], anime_id, watched)
                new_episodes = watched - before
                if new_episodes <= 0 or member is None or member.bot or banned or capped:
                    continue

                seconds_each = int(node.get("average_episode_duration") or 0) or FALLBACK_EPISODE_MINUTES * 60
                minutes_each = max(1, round(seconds_each / 60))
                room = DAILY_CAP_HOURS * 3600 - used
                fits = min(new_episodes, room // (minutes_each * 60))
                if fits > 0:
                    title = node.get("title") or f"MAL anime {anime_id}"
                    name = await self.anilist_name(anime_id, title)
                    activity_id, embed, view, goals, progress_file, error = await immersion._log_anime_core(
                        link["user_id"], str(guild.id), name, fits, minutes_each, now_utc(), None,
                        extra_meta={"source": "myanimelist", "mal_anime_id": anime_id},
                        use_official_duration=False, source="myanimelist", fallback_name=title,
                    )
                    if error:
                        logger.info(f"[MAL] Skipped {anime_id} for {link['user_id']}: {error}")
                        continue
                    used += fits * minutes_each * 60
                    imported = True
                    await self.set_daily(link["user_id"], day, used, False)
                    kwargs = {"embed": embed, "view": view}
                    if progress_file:
                        kwargs["file"] = progress_file
                    await channel.send(IMPORT_HEADER.format(user=member.mention),
                                       allowed_mentions=discord.AllowedMentions.none(), **kwargs)
                    if goals:
                        await channel.send(embed=immersion._build_goal_completion_embed(goals),
                                           allowed_mentions=discord.AllowedMentions.none())
                if fits < new_episodes or used >= DAILY_CAP_HOURS * 3600:
                    capped = True
                    await self.set_daily(link["user_id"], day, used, True)
                    await channel.send(
                        CAP_MESSAGE.format(user=member.mention, hours=DAILY_CAP_HOURS),
                        allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[member]),
                    )

        if imported and member is not None:
            # Class roles, and the penalty/reward checks, follow the new total
            try:
                self.bot.dispatch("anime_log_changed", guild, member)
                await immersion._sync_roles_for_member(member, channel, source="mal_import")
            except Exception as e:
                logger.warning(f"[MAL] Role sync after import failed for {member}: {e}")


async def setup(bot):
    await bot.add_cog(MalCog(bot))
