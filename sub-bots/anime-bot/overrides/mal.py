"""
LJTA (Learn Japanese Through Anime) - MyAnimeList link (anime bot only)

Members link their MyAnimeList account with /mal link. Then, in both directions:
- Discord -> MAL: every /log_anime adds those episodes to that anime on their MAL list.
- MAL -> Discord: every few minutes, new episodes they mark as watched on MAL are logged
  as normal anime logs and posted in the MAL log channel, looking the same as /log_anime.
  At most 5 hours a day come in this way. When a member reaches the cap they're pinged
  once, and further MAL updates that day are dropped (not logged or posted, ever).
- Undo works both ways, only for logs synced while linked: undoing such a log in Discord
  takes its episodes back off MAL, and removing episodes on MAL undoes (or shortens) the
  synced logs from the last MAL_UNDO_DAYS days. Logs from before linking never affect the
  other side.
- A member can't log more episodes of an anime (all their logs together) than MAL says it has
  (the anime ImmersionCog's _anime_total_episodes uses anime_info here).

Setup: the MAL app's Client ID is built in (DEFAULT_CLIENT_ID below; it's public anyway).
Its Client Secret is NOT in the code: an admin enters it once per server with
/manage_mal action:Connect. The bot checks it with MAL, stores it in data/anime_mal.db,
and turns MAL syncing on for that server only. Servers where nobody entered the correct
secret can't use the MAL link. .env can still override: MAL_CLIENT_ID, MAL_CLIENT_SECRET,
MAL_REDIRECT_URI (servers still need /manage_mal to be turned on).
If the secret stops working (regenerated or revoked on MAL), syncing pauses everywhere and
members who log anime are told an admin needs to run /manage_mal again.
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
MAL_LOG_CHANNEL_ID = 1415741464545329272   # MAL -> Discord logs and cap notices go here
DAILY_CAP_HOURS = 5                       # max anime time per member per day (UTC) from MAL
POLL_MINUTES = 10                         # how often linked lists are checked
FALLBACK_EPISODE_MINUTES = 24             # when MAL doesn't know an episode's length
MAL_UNDO_DAYS = 7                         # removing episodes on MAL undoes synced logs this recent

# Posted once when a member reaches the cap. {user} pings them; {hours} is the cap.
CAP_MESSAGE = (
    "{user} you've reached your daily MAL cap limit of {hours} hours. "
    "No more MyAnimeList updates will be logged for you today."
)
# Shown above each MAL log so the channel shows whose log it is (no ping).
IMPORT_HEADER = "{user} logged on MyAnimeList"
# Posted (no ping) when episodes removed on MAL undo or shorten synced logs; {changes} is one line per log.
REMOVAL_MESSAGE = "{user} removed episodes on MyAnimeList, so these logs changed:\n{changes}"

# Shown (only to them) when someone logs anime while their MAL sync can't work.
EXPIRED_MEMBER_NOTICE = (
    "⚠️ Your MyAnimeList link has expired, so this log wasn't added to your MAL list and new "
    "episodes on MAL aren't being logged. Use `/mal link` to reconnect."
)
EXPIRED_SERVER_NOTICE = (
    "⚠️ MyAnimeList syncing is paused in this server because its MAL connection has expired, "
    "so this log wasn't added to your MAL list. An admin needs to run `/manage_mal` again."
)
SECRET_CHECK_HOURS = 6   # how often the stored app secret is re-checked with MAL

# The bot's MyAnimeList app (registered at https://myanimelist.net/apiconfig). Only the
# Client ID is here (it's in every login link anyway); the secret is set with /manage_mal.
DEFAULT_CLIENT_ID = "c6bb34ef29405bb8a3bdb6258c5ac65a"
DEFAULT_REDIRECT_URI = "http://localhost/"

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
        # Show "1. Open MyAnimeList" before "2. Paste the address" (the paste button is
        # added by its decorator first, so move it after the link)
        self.remove_item(self.paste)
        self.add_item(discord.ui.Button(label="1. Open MyAnimeList", style=discord.ButtonStyle.link, url=auth_url))
        self.add_item(self.paste)

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
        self.stored_secret: str = ""          # set with /manage_mal (loaded in init_db)
        self.app_broken = False               # MAL stopped accepting the secret
        self.enabled_guilds: set = set()      # servers where an admin connected MAL
        self.push_tasks: Dict[str, asyncio.Task] = {}   # user_id -> their latest Discord -> MAL push
        self.last_secret_check: Optional[datetime] = None
        self.anime_cache: Dict[int, Tuple[datetime, Tuple[int, bool]]] = {}   # mal_id -> (fetched, info)

    # ---------------- setup ----------------

    @property
    def client_id(self) -> str:
        return os.getenv("MAL_CLIENT_ID", "").strip() or DEFAULT_CLIENT_ID

    @property
    def client_secret(self) -> str:
        return os.getenv("MAL_CLIENT_SECRET", "").strip() or self.stored_secret

    @property
    def redirect_uri(self) -> str:
        return os.getenv("MAL_REDIRECT_URI", "").strip() or DEFAULT_REDIRECT_URI

    async def cog_load(self):
        await self.init_db()
        self.poll.start()

    async def init_db(self):
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
                CREATE TABLE IF NOT EXISTS mal_app (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS mal_guilds (
                    guild_id TEXT PRIMARY KEY,
                    enabled_by TEXT,
                    enabled_at TEXT
                );
                -- Discord logs synced with MAL while linked, so an undo on one side can be
                -- mirrored on the other. The log's episodes on MAL are start_ep+1 .. end_ep.
                CREATE TABLE IF NOT EXISTS mal_sync (
                    activity_id INTEGER PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    anime_id INTEGER NOT NULL,
                    start_ep INTEGER NOT NULL,
                    end_ep INTEGER NOT NULL,
                    direction TEXT NOT NULL,      -- 'to_mal' (logged in Discord) or 'from_mal'
                    prev_status TEXT,             -- to_mal: MAL status before (NULL = not on the list)
                    created_at TEXT NOT NULL,
                    undone INTEGER NOT NULL DEFAULT 0
                );
            """)
            await db.commit()
            async with db.execute("SELECT key, value FROM mal_app") as cur:
                app = dict(await cur.fetchall())
            async with db.execute("SELECT guild_id FROM mal_guilds") as cur:
                self.enabled_guilds = {row[0] for row in await cur.fetchall()}
        self.stored_secret = app.get("client_secret") or ""
        self.app_broken = app.get("broken") == "1"

    # ---------------- server connection (/manage_mal) ----------------

    async def _set_app_value(self, key: str, value: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT OR REPLACE INTO mal_app (key, value) VALUES (?, ?)", (key, value))
            await db.commit()

    async def set_app_broken(self, broken: bool):
        if self.app_broken != broken:
            self.app_broken = broken
            await self._set_app_value("broken", "1" if broken else "0")
            if broken:
                logger.error("[MAL] MyAnimeList no longer accepts the app secret; syncing paused until /manage_mal")

    def guild_ready(self, guild_id) -> bool:
        """MAL syncing works in this server: connected there, and the secret still works."""
        return bool(self.client_id and self.client_secret and not self.app_broken
                    and str(guild_id) in self.enabled_guilds)

    async def check_secret(self, secret: str) -> Optional[bool]:
        """Ask MAL whether this app secret is right, without any member's login: a refresh with
        a fake token is refused as "invalid_client" only when the app itself is wrong.
        Returns True / False, or None if MAL couldn't be asked."""
        try:
            status, body = await self._http("POST", TOKEN_URL, data={
                "client_id": self.client_id, "client_secret": secret,
                "grant_type": "refresh_token", "refresh_token": "connection-check",
            })
        except Exception as e:
            logger.warning(f"[MAL] Couldn't reach MyAnimeList to check the secret: {e}")
            return None
        if body.get("error") == "invalid_client":
            return False
        if status >= 500 or not body.get("error"):
            return None
        return True

    async def log_notice(self, guild, member) -> Optional[str]:
        """After someone uses /log_anime: a notice for them if their MAL sync can't work
        (their link expired, or the server's MAL connection expired). None otherwise."""
        if guild is None or str(guild.id) not in self.enabled_guilds:
            return None
        user_id = str(member.id)
        link = await self.get_link(user_id)
        if not link:
            return None
        task = self.push_tasks.get(user_id)
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=15)   # let this log's MAL update finish first
        if self.app_broken:
            return EXPIRED_SERVER_NOTICE
        link = await self.get_link(user_id)
        if link and link["broken"]:
            return EXPIRED_MEMBER_NOTICE
        return None

    @app_commands.command(name="manage_mal", description="[ADMIN] Connect this server to the bot's MyAnimeList app, or disconnect it")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.describe(action="What to do", secret="The MAL app's Client Secret (only needed to connect)")
    @app_commands.choices(action=[
        app_commands.Choice(name="Connect", value="connect"),
        app_commands.Choice(name="Disconnect", value="disconnect"),
        app_commands.Choice(name="Show status", value="status"),
    ])
    async def manage_mal(self, interaction: discord.Interaction, action: app_commands.Choice[str], secret: Optional[str] = None):
        perms = getattr(interaction.user, "guild_permissions", None)
        if not (perms and perms.administrator):
            await interaction.response.send_message("You need administrator permission for this.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)   # only the admin sees this, secret included
        guild_id = str(interaction.guild_id)

        if action.value == "connect":
            if not secret or not secret.strip():
                reply = "Add the MyAnimeList app's Client Secret in the `secret` option."
            else:
                ok = await self.check_secret(secret.strip())
                if ok is None:
                    reply = "Couldn't reach MyAnimeList to check the secret. Please try again in a minute."
                elif not ok:
                    reply = ("MyAnimeList rejected that secret. Copy the Client Secret from "
                             "https://myanimelist.net/apiconfig and try again.")
                else:
                    self.stored_secret = secret.strip()
                    await self._set_app_value("client_secret", self.stored_secret)
                    await self.set_app_broken(False)
                    self.last_secret_check = now_utc()
                    async with aiosqlite.connect(self.db_path) as db:
                        await db.execute("INSERT OR REPLACE INTO mal_guilds (guild_id, enabled_by, enabled_at) VALUES (?, ?, ?)",
                                         (guild_id, str(interaction.user.id), now_utc().isoformat()))
                        await db.commit()
                    self.enabled_guilds.add(guild_id)
                    reply = "✅ MyAnimeList is connected in this server. Members can now use `/mal link`."
                    logger.info(f"[MAL] {interaction.user} connected MAL in guild {guild_id}")

        elif action.value == "disconnect":
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("DELETE FROM mal_guilds WHERE guild_id = ?", (guild_id,))
                await db.commit()
            self.enabled_guilds.discard(guild_id)
            reply = ("MyAnimeList is disconnected in this server: `/mal link` is off and nothing syncs here. "
                     "Members' links are kept, so connecting again resumes syncing.")

        else:
            if guild_id not in self.enabled_guilds:
                reply = "MyAnimeList isn't connected in this server. Use `/manage_mal action:Connect secret:<Client Secret>`."
            elif self.app_broken or not self.client_secret:
                reply = "⚠️ MyAnimeList's connection has expired (the secret no longer works). Run `/manage_mal action:Connect` with the current Client Secret."
            else:
                links = await self.all_links()
                reply = f"MyAnimeList is connected in this server and working. {len(links)} members have a working link."
        await interaction.followup.send(reply, ephemeral=True)

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

    async def add_sync(self, activity_id: int, user_id: str, anime_id: int, start: int, end: int,
                       direction: str, prev_status: Optional[str] = None):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO mal_sync (activity_id, user_id, anime_id, start_ep, end_ep, direction, "
                "prev_status, created_at, undone) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (int(activity_id), user_id, int(anime_id), start, end, direction, prev_status, now_utc().isoformat()),
            )
            await db.commit()

    async def get_sync(self, activity_id: int) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM mal_sync WHERE activity_id = ? AND undone = 0", (int(activity_id),)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def recent_syncs(self, user_id: str, anime_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Synced logs from the last MAL_UNDO_DAYS days that aren't undone, top episodes first."""
        cutoff = (now_utc() - timedelta(days=MAL_UNDO_DAYS)).isoformat()
        query = "SELECT * FROM mal_sync WHERE user_id = ? AND undone = 0 AND created_at >= ?"
        args: list = [user_id, cutoff]
        if anime_id is not None:
            query += " AND anime_id = ?"
            args.append(int(anime_id))
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query + " ORDER BY end_ep DESC, activity_id DESC", args) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def _sync_db(self, sql: str, args: tuple):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(sql, args)
            await db.commit()

    # ---------------- HTTP ----------------

    async def _http(self, method: str, url: str, *, headers=None, params=None, data=None,
                    timeout: Optional[float] = None) -> Tuple[int, Dict[str, Any]]:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        extra = {"timeout": aiohttp.ClientTimeout(total=timeout)} if timeout else {}
        async with self.session.request(method, url, headers=headers, params=params, data=data, **extra) as resp:
            try:
                body = await resp.json(content_type=None)
            except Exception:
                body = None
            return resp.status, body if isinstance(body, dict) else {}

    async def _token_request(self, fields: Dict[str, str]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Returns (tokens, None) or (None, MAL's error code). "invalid_client" means MAL no
        longer accepts the app's secret: the server's connection has expired."""
        data = dict(fields, client_id=self.client_id)
        if self.client_secret:
            data["client_secret"] = self.client_secret
        status, body = await self._http("POST", TOKEN_URL, data=data)
        if status != 200 or "access_token" not in body:
            error = body.get("error") or f"http_{status}"
            logger.warning(f"[MAL] Token request ({fields.get('grant_type')}) failed: {status} {error}")
            if error == "invalid_client":
                await self.set_app_broken(True)
            return None, error
        return body, None

    async def _refresh(self, link: Dict[str, Any]) -> Optional[str]:
        tokens, error = await self._token_request({"grant_type": "refresh_token", "refresh_token": link.get("refresh_token") or ""})
        if tokens is None and (error == "invalid_client" or error.startswith("http_5")):
            return None   # the app's problem (or MAL being down), not this member's link
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

    async def anime_info(self, mal_id: int) -> Optional[Tuple[int, bool]]:
        """(number of episodes, still airing) from MyAnimeList's public data; needs only the
        Client ID, no member login. 0 episodes = MAL doesn't know yet. None if MAL can't be
        reached (then MAL doesn't cap the log). Cached for 12 hours."""
        cached = self.anime_cache.get(int(mal_id))
        if cached and now_utc() - cached[0] < timedelta(hours=12):
            return cached[1]
        try:
            status, body = await self._http(
                "GET", f"{API_URL}/anime/{int(mal_id)}", headers={"X-MAL-CLIENT-ID": self.client_id},
                params={"fields": "num_episodes,status"}, timeout=6,
            )
        except Exception as e:
            logger.warning(f"[MAL] Couldn't read the episode count of MAL anime {mal_id}: {e!r}")
            return None
        if status != 200:
            logger.warning(f"[MAL] Couldn't read the episode count of MAL anime {mal_id}: {status}")
            return None
        info = (int(body.get("num_episodes") or 0), body.get("status") in ("currently_airing", "not_yet_aired"))
        self.anime_cache[int(mal_id)] = (now_utc(), info)
        return info

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
        guild_id = str(interaction.guild_id)
        if guild_id in self.enabled_guilds and (self.app_broken or not self.client_secret):
            await interaction.response.send_message(EXPIRED_SERVER_NOTICE.replace(", so this log wasn't added to your MAL list", ""), ephemeral=True)
            return
        if not self.guild_ready(guild_id):
            await interaction.response.send_message(
                "MyAnimeList linking isn't set up in this server. An admin can turn it on with `/manage_mal`.",
                ephemeral=True,
            )
            return
        auth_url = self.start_link(str(interaction.user.id))
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

    def start_link(self, user_id: str) -> str:
        """Begin linking: remember a fresh PKCE verifier for this member and return MAL's
        login address. (Also used by the standalone mal-test.py check.)"""
        verifier = secrets.token_urlsafe(96)[:128]   # PKCE code verifier (43-128 chars)
        state = secrets.token_urlsafe(16)
        self.pending[user_id] = (verifier, state, now_utc())
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "state": state,
            "code_challenge": verifier,          # MAL only supports the "plain" method
            "code_challenge_method": "plain",
        }
        if self.redirect_uri:
            params["redirect_uri"] = self.redirect_uri
        return f"{AUTH_URL}?{urlencode(params)}"

    async def complete_link(self, user_id: str, pasted: str) -> Tuple[bool, str]:
        """Finish linking from the address the member pasted. Returns (linked?, message for
        the member). Only the member sees the message; it names their MAL account."""
        pending = self.pending.get(user_id)
        if not pending or now_utc() - pending[2] > timedelta(minutes=LINK_TIMEOUT_MINUTES):
            return False, "That link expired. Please start again with `/mal link`."
        verifier, state, _ = pending
        pasted = pasted.strip()
        query = parse_qs(urlparse(pasted).query) if "code=" in pasted else {}
        code = (query.get("code") or [pasted if "=" not in pasted else ""])[0]
        if not code or (query.get("state") and query["state"][0] != state):
            return False, "That doesn't look like the right address. Please start again with `/mal link`."

        fields = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier}
        if self.redirect_uri:
            fields["redirect_uri"] = self.redirect_uri
        tokens, error = await self._token_request(fields)
        if tokens is None and error == "invalid_client":
            return False, "MyAnimeList syncing is paused in this server: its MAL connection has expired. An admin needs to run `/manage_mal` again."
        if tokens is None:
            return False, "MyAnimeList didn't accept that. Please start again with `/mal link`."
        self.pending.pop(user_id, None)

        link = {"user_id": user_id, "access_token": tokens["access_token"], "refresh_token": tokens.get("refresh_token"),
                "expires_at": (now_utc() + timedelta(seconds=int(tokens.get("expires_in") or 3600))).isoformat()}
        status, me = await self._api(link, "GET", "/users/@me")
        if status != 200 or "id" not in me:
            return False, "Couldn't read your MyAnimeList account. Please try again later."

        async with self.lock:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("SELECT user_id FROM mal_links WHERE mal_user_id = ?", (me["id"],)) as cur:
                    row = await cur.fetchone()
                if row and row[0] != user_id:
                    return False, "That MyAnimeList account is already linked to another Discord account."
                await db.execute("DELETE FROM mal_snapshot WHERE user_id = ?", (user_id,))
                await db.execute("DELETE FROM mal_sync WHERE user_id = ?", (user_id,))   # a new link starts fresh
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

        logger.info(f"[MAL] Discord user {user_id} linked MAL account {me.get('name')} ({me['id']})")
        return True, (
            f"✅ Linked to MyAnimeList account {me.get('name')}. Your existing list ({len(entries)} anime) "
            "isn't logged; only episodes you mark as watched from now on."
        )

    async def finish_link(self, interaction: discord.Interaction, pasted: str):
        """Discord side of linking: everything here is only shown to the member."""
        await interaction.response.defer(ephemeral=True)
        _, message = await self.complete_link(str(interaction.user.id), pasted)
        await interaction.followup.send(message, ephemeral=True)

    @mal.command(name="unlink", description="Unlink your MyAnimeList account")
    async def mal_unlink(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        async with self.lock:
            async with aiosqlite.connect(self.db_path) as db:
                cur = await db.execute("DELETE FROM mal_links WHERE user_id = ?", (user_id,))
                await db.execute("DELETE FROM mal_snapshot WHERE user_id = ?", (user_id,))
                await db.execute("DELETE FROM mal_sync WHERE user_id = ?", (user_id,))
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
        if str(interaction.guild_id) in self.enabled_guilds and (self.app_broken or not self.client_secret):
            lines.append("⚠️ MyAnimeList syncing is paused in this server: its MAL connection has expired. An admin needs to run `/manage_mal` again.")
        elif not self.guild_ready(interaction.guild_id):
            lines.append("MyAnimeList isn't connected in this server, so nothing syncs here.")
        lines.append(
            f"Logged from MyAnimeList today: {seconds / 3600:.1f}h of {DAILY_CAP_HOURS}h"
            + (" (cap reached; more MAL updates today are ignored)." if capped else ".")
        )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ---------------- Discord -> MAL ----------------

    @commands.Cog.listener()
    async def on_anime_activity_logged(self, activity, source: str):
        """After /log_anime: add the episodes to the member's MAL list."""
        if source != "command":
            return
        # Remembered so /log_anime's expiry notice can wait for this update to finish
        self.push_tasks[str(activity.user_id)] = asyncio.current_task()
        if not self.guild_ready(getattr(activity, "guild_id", None)):
            return
        link = await self.get_link(str(activity.user_id))
        if not link or link["broken"]:
            return
        meta = activity.meta or {}
        mal_id = (meta.get("anilist") or {}).get("mal_id")
        episodes = int(meta.get("episodes") or 0)
        if not mal_id or episodes <= 0:
            return
        await self.push_to_mal(link, int(mal_id), episodes, getattr(activity, "id", None),
                               complete=bool(meta.get("logged_as_complete")))

    async def push_to_mal(self, link: Dict[str, Any], mal_id: int, episodes: int,
                          activity_id: Optional[int] = None, *, complete: bool = False):
        """Add `episodes` to this anime on the member's MAL list (Watching, or Completed at the
        last episode; `complete` = logged with "complete", so straight to Completed). Shows
        already Completed on MAL are left alone. Remembers which MAL episodes the log added,
        so undoing it on either side can be mirrored."""
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
                before = int(current.get("num_episodes_watched") or 0)
                watched = before + episodes
                new_status = "watching"
                if total and (complete or watched >= total):
                    watched, new_status = total, "completed"
                status, _ = await self._api(
                    link, "PATCH", f"/anime/{mal_id}/my_list_status",
                    data={"num_watched_episodes": str(watched), "status": new_status},
                )
                if status != 200:
                    logger.warning(f"[MAL] Couldn't update MAL anime {mal_id} for {link['user_id']}: {status}")
                    return
                # So the MAL -> Discord check doesn't log these episodes a second time. Only this
                # change is counted, so episodes marked on MAL since the last check still get logged.
                snapshot = (await self.get_snapshot(link["user_id"])).get(int(mal_id), 0)
                await self.set_snapshot(link["user_id"], int(mal_id), snapshot + watched - before)
                if activity_id and watched > before:
                    await self.add_sync(activity_id, link["user_id"], int(mal_id), before, watched,
                                        "to_mal", current.get("status"))
        except Exception as e:
            logger.exception(f"[MAL] Sync to MAL failed for {link['user_id']}: {e}")

    # ---------------- undo, both ways ----------------

    @commands.Cog.listener()
    async def on_activity_undone(self, activity_id: int, user_id: str):
        """A log was undone in Discord (Undo button or /log_undo). If it was synced with MAL
        while linked, take its episodes back off the member's MAL list too. Logs that were
        never synced (e.g. from before linking) leave MAL alone."""
        try:
            async with self.lock:
                row = await self.get_sync(activity_id)
                if row is None:
                    return
                await self._sync_db("UPDATE mal_sync SET undone = 1 WHERE activity_id = ?", (int(activity_id),))
                link = await self.get_link(row["user_id"])
                if not link or link["broken"] or not self.client_secret or self.app_broken:
                    return
                await self.remove_from_mal(link, row)
        except Exception as e:
            logger.exception(f"[MAL] Undo on MAL failed for log {activity_id}: {e}")

    async def remove_from_mal(self, link: Dict[str, Any], row: Dict[str, Any]):
        """Take one synced log's episodes back off the member's MAL list (call inside the lock)."""
        user_id, anime_id = link["user_id"], int(row["anime_id"])
        count = int(row["end_ep"]) - int(row["start_ep"])
        if count <= 0:
            return
        status, details = await self._api(link, "GET", f"/anime/{anime_id}", params={"fields": "num_episodes,my_list_status"})
        if status != 200:
            logger.warning(f"[MAL] Couldn't read MAL anime {anime_id} for {user_id} to undo: {status}")
            return
        current = details.get("my_list_status")
        if current:   # (if it's no longer on their list, there's nothing to take off)
            watched = int(current.get("num_episodes_watched") or 0)
            new_watched = max(0, watched - count)
            if row["direction"] == "to_mal" and row["prev_status"] is None and new_watched == 0 \
                    and not current.get("score"):
                # The log had added the anime to their list: take it off again
                status, _ = await self._api(link, "DELETE", f"/anime/{anime_id}/my_list_status")
                ok = status in (200, 404)
            else:
                data = {"num_watched_episodes": str(new_watched)}
                if row["direction"] == "to_mal" and row["prev_status"] and new_watched == int(row["start_ep"]):
                    data["status"] = row["prev_status"]   # back to how it was before the log
                elif current.get("status") == "completed" and new_watched < watched:
                    data["status"] = "watching"
                status, _ = await self._api(link, "PATCH", f"/anime/{anime_id}/my_list_status", data=data)
                ok = status == 200
            if not ok:
                logger.warning(f"[MAL] Couldn't undo on MAL anime {anime_id} for {user_id}: {status}")
                return
            # Only our change, so the MAL -> Discord check doesn't see it as a removal on MAL
            snapshot = (await self.get_snapshot(user_id)).get(anime_id, 0)
            await self.set_snapshot(user_id, anime_id, max(0, snapshot - (watched - new_watched)))
        # Synced logs above this one move down on the MAL list
        await self._sync_db(
            "UPDATE mal_sync SET start_ep = MAX(start_ep - ?, 0), end_ep = MAX(end_ep - ?, 0) "
            "WHERE user_id = ? AND anime_id = ? AND undone = 0 AND start_ep >= ?",
            (count, count, user_id, anime_id, int(row["end_ep"])),
        )

    async def undo_removed(self, link: Dict[str, Any], immersion, anime_id: int, watched: int) -> List[str]:
        """Episodes above `watched` were removed on MAL: undo the synced logs they belonged to,
        or shorten a log that lost only some (call inside the lock). Logs from before linking,
        or synced more than MAL_UNDO_DAYS days ago, are left alone. Returns a line per log."""
        user_id, changes = link["user_id"], []
        for row in await self.recent_syncs(user_id, anime_id):
            if int(row["end_ep"]) <= watched:
                break   # top episodes first, so the rest are all still on MAL
            activity = await immersion.activity_repo.get_activity_by_id(row["activity_id"])
            if activity is None or str(activity.user_id) != user_id:
                await self._sync_db("UPDATE mal_sync SET undone = 1 WHERE activity_id = ?", (row["activity_id"],))
                continue   # already gone from Discord
            episodes = int((activity.meta or {}).get("episodes") or 0)
            keep = 0 if watched <= int(row["start_ep"]) else episodes - (int(row["end_ep"]) - watched)
            if keep <= 0:
                # A plain delete, not _undo_activity: that would take the episodes off MAL again
                await immersion.activity_repo.delete_activity(activity.id, user_id)
                await self._sync_db("UPDATE mal_sync SET undone = 1 WHERE activity_id = ?", (activity.id,))
                changes.append(f"Undid log `{activity.id}`: {activity.name} ({episodes} episodes)")
            else:
                duration = round((activity.duration or 0) * keep / episodes)
                async with aiosqlite.connect(immersion.db.db_path) as db:
                    await db.execute(
                        "UPDATE immersion_activities SET duration = ?, "
                        "meta = json_set(COALESCE(meta, '{}'), '$.episodes', ?) WHERE id = ? AND deleted_at IS NULL",
                        (duration, keep, activity.id),
                    )
                    await db.commit()
                immersion.activity_repo.invalidate_user_cache(activity.user_id, activity.guild_id)
                await self._sync_db("UPDATE mal_sync SET end_ep = ? WHERE activity_id = ?", (watched, activity.id))
                changes.append(f"Shortened log `{activity.id}`: {activity.name} ({episodes} → {keep} episodes)")
        if changes:
            try:
                await immersion.goal_service.refresh_user_goals(user_id)
            except Exception as e:
                logger.warning(f"[MAL] Goal refresh after MAL removal failed for {user_id}: {e}")
        return changes

    # ---------------- MAL -> Discord ----------------

    @tasks.loop(minutes=POLL_MINUTES)
    async def poll(self):
        if not self.client_secret:
            return
        # Every few hours, check MAL still accepts the secret, so an expired connection is
        # noticed (and members told) even before a login needs refreshing
        if self.last_secret_check is None or now_utc() - self.last_secret_check > timedelta(hours=SECRET_CHECK_HOURS):
            ok = await self.check_secret(self.client_secret)
            if ok is not None:
                self.last_secret_check = now_utc()
                await self.set_app_broken(not ok)
        channel = self.bot.get_channel(MAL_LOG_CHANNEL_ID)
        if channel is None:
            logger.warning(f"[MAL] MAL log channel {MAL_LOG_CHANNEL_ID} not found; MAL -> Discord paused")
            return
        if not self.guild_ready(channel.guild.id):
            return   # MAL not connected (or expired) in the server that has the MAL log channel
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
        imported, changes = False, []
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
                if watched < before:
                    # Episodes removed on MAL: undo the synced logs they came from (not while
                    # rewatching, when MAL's count starts over)
                    if not list_status.get("is_rewatching"):
                        changes += await self.undo_removed(link, immersion, anime_id, watched)
                    continue
                new_episodes = watched - before
                if member is None or member.bot or banned or capped:
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
                    await self.add_sync(activity_id, link["user_id"], anime_id, before, before + fits, "from_mal")
                    # What was actually logged (fewer episodes if it hit the anime's episode count)
                    logged = await immersion.activity_repo.get_activity_by_id(activity_id)
                    used += logged.duration if logged is not None else fits * minutes_each * 60
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

            # Anime taken off the MAL list don't show up in it at all: check recently synced ones
            seen = {int(entry["node"]["id"]) for entry in entries}
            for anime_id in sorted({int(r["anime_id"]) for r in await self.recent_syncs(link["user_id"])} - seen):
                status, details = await self._api(link, "GET", f"/anime/{anime_id}", params={"fields": "my_list_status"})
                if status == 200 and not details.get("my_list_status"):
                    await self.set_snapshot(link["user_id"], anime_id, 0)
                    changes += await self.undo_removed(link, immersion, anime_id, 0)

        if changes:
            mention = member.mention if member is not None else f"<@{link['user_id']}>"
            await channel.send(REMOVAL_MESSAGE.format(user=mention, changes="\n".join(changes))[:2000],
                               allowed_mentions=discord.AllowedMentions.none())

        if (imported or changes) and member is not None:
            # Class roles, and the penalty/reward checks, follow the new total
            try:
                self.bot.dispatch("anime_log_changed", guild, member)
                await immersion._sync_roles_for_member(member, channel, source="mal_import")
            except Exception as e:
                logger.warning(f"[MAL] Role sync after import failed for {member}: {e}")


async def setup(bot):
    await bot.add_cog(MalCog(bot))
