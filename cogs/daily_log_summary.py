"""Daily immersion log summary.

Replaces the old kizuna-only daily summary. It covers every activity recorded
for the day — kizuna texthooker auto-logs and manual ``/log_*`` submissions both
live in the same activities table — and congratulates the day's #1 member in an
admin-configured channel.

Configuration lives in config.json per guild:
    daily_summary_channel          where the summary embed is posted
    daily_summary_ping_channel     where the #1 congratulation ping is sent
                                   (falls back to the summary channel)
    daily_summary_congrats_message optional template for the congratulation
                                   (defaults to DEFAULT_CONGRATS_MESSAGE)
    daily_summary_title            optional embed title template
    daily_summary_description      optional embed description template
    daily_summary_footer           optional embed footer template
    daily_summary_color            optional embed colour (#RRGGBB)
    daily_summary_show_titles      show the "Most logged titles" section
    daily_summary_show_leaderboard show the "Leaderboard" section
    daily_summary_show_characters  show characters read in the Overview field
    daily_summary_congrats_show_characters
                                   append characters read to the #1 message

The summary for a UTC day is posted shortly after that day ends (00:05 UTC).
"""

import json
import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

logger = logging.getLogger(__name__)

SUMMARY_CHANNEL_KEY = 'daily_summary_channel'
SUMMARY_PING_CHANNEL_KEY = 'daily_summary_ping_channel'
SUMMARY_CONGRATS_KEY = 'daily_summary_congrats_message'
TITLE_KEY = 'daily_summary_title'
DESCRIPTION_KEY = 'daily_summary_description'
FOOTER_KEY = 'daily_summary_footer'
COLOR_KEY = 'daily_summary_color'
SHOW_TITLES_KEY = 'daily_summary_show_titles'
SHOW_LEADERBOARD_KEY = 'daily_summary_show_leaderboard'
SHOW_CHARACTERS_KEY = 'daily_summary_show_characters'
CONGRATS_SHOW_CHARACTERS_KEY = 'daily_summary_congrats_show_characters'
STATE_FILE = 'data/daily_summary_state.json'

DEFAULT_SUMMARY_TITLE = "📊 Daily Immersion Summary — {month} {day}, {year}"
DEFAULT_SUMMARY_FOOTER = "Includes kizuna and manual logs. Days and times are UTC."
SUMMARY_PLACEHOLDERS_HINT = (
    "Placeholders: `{month}`, `{day}`, `{year}`, `{date}`, `{total}`, `{logs}`, "
    "`{users}`, `{reading}`, `{listening}`, `{characters}`, `{kizuna_logs}`, "
    "`{manual_logs}`. Server emoji such as `<:name:id>` work too."
)
# Sample values used to validate templates when they are saved and to preview them.
SAMPLE_PLACEHOLDER_VALUES = {
    'month': 'November',
    'day': '18',
    'year': '2026',
    'date': 'November 18, 2026',
    'total': '12h 30m',
    'logs': '42',
    'users': '7',
    'reading': '9h 10m',
    'listening': '3h 20m',
    'characters': '1,234,567',
    'kizuna_logs': '35',
    'manual_logs': '7',
}

# Supported placeholders in a custom congratulation message. ``{users}`` is
# required so the winner is always pinged.
DEFAULT_CONGRATS_MESSAGE = (
    "🎉 Congratulations {users}! You were **#1** on the {date} immersion "
    "leaderboard with **{duration}**."
)
CONGRATS_PLACEHOLDERS_HINT = (
    "Placeholders: `{users}` (winner mention), `{duration}`, `{characters}`, "
    "`{date}`. Server emoji such as `<:name:id>` (or `<a:name:id>`) work too."
)

# 00:05 UTC: the day being summarised is complete, and a log that lands exactly
# at midnight is still counted.
RUN_TIME = time(hour=0, minute=5, tzinfo=timezone.utc)


def _format_duration(seconds: Any) -> str:
    """Render a duration in seconds as e.g. ``2h 15m``."""
    total = int(seconds or 0)
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _format_count(value: Any) -> str:
    """Render a large count with thousands separators, e.g. ``1,234,567``."""
    try:
        return f"{int(value or 0):,}"
    except (TypeError, ValueError):
        return "0"


class DailyLogSummaryCog(commands.Cog):
    """Posts a daily summary of all immersion logs and congratulates the top member."""

    def __init__(self, bot):
        self.bot = bot
        # Per-feature guild -> ISO date already posted, so a restart around the
        # run time cannot post the same day twice. Summary and ping are tracked
        # independently so either can be re-run without the other.
        state = self._load_state()
        self._posted_summary: Dict[str, str] = state['summary']
        self._posted_congrats: Dict[str, str] = state['congrats']

    # ------------------------------------------------------------------
    # Lifecycle / persistence
    # ------------------------------------------------------------------

    async def cog_load(self):
        self.post_daily_summary.start()

    async def cog_unload(self):
        self.post_daily_summary.cancel()

    def _load_state(self) -> Dict[str, Dict[str, str]]:
        empty = {'summary': {}, 'congrats': {}}
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    if 'summary' in data or 'congrats' in data:
                        return {
                            'summary': {str(k): str(v) for k, v in (data.get('summary') or {}).items()},
                            'congrats': {str(k): str(v) for k, v in (data.get('congrats') or {}).items()},
                        }
                    # Legacy shape: a flat guild -> date map that tracked the summary only.
                    return {
                        'summary': {str(k): str(v) for k, v in data.items()},
                        'congrats': {},
                    }
        except Exception:
            logger.exception("Daily summary: could not read %s", STATE_FILE)
        return empty

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            tmp_path = f"{STATE_FILE}.tmp"
            with open(tmp_path, 'w') as f:
                json.dump(
                    {'summary': self._posted_summary, 'congrats': self._posted_congrats},
                    f, indent=2,
                )
            os.replace(tmp_path, STATE_FILE)
        except Exception:
            logger.exception("Daily summary: could not write %s", STATE_FILE)

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _guild_config(self, guild_id: int) -> dict:
        return (self.bot.config.get('guilds') or {}).get(str(guild_id), {})

    def _configured_channel(self, guild: discord.Guild, key: str) -> Optional[discord.abc.GuildChannel]:
        channel_id = self._guild_config(guild.id).get(key)
        if not channel_id:
            return None
        try:
            return guild.get_channel(int(channel_id)) or guild.get_channel_or_thread(int(channel_id))
        except (TypeError, ValueError):
            logger.error(
                "Daily summary: invalid %s value %r for guild %s", key, channel_id, guild.id
            )
            return None

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    async def _summary_for(self, start: datetime, end: datetime) -> Optional[Dict[str, Any]]:
        immersion = self.bot.get_cog("ImmersionCog")
        repo = getattr(immersion, "activity_repo", None)
        if repo is None:
            logger.error("Daily summary: ImmersionCog activity repository is unavailable")
            return None
        return await repo.get_daily_activity_summary(start, end)

    async def _display_name(self, guild: Optional[discord.Guild], user_id: Any) -> str:
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return str(user_id)
        if guild is not None:
            member = guild.get_member(uid)
            if member is not None:
                return member.display_name
        try:
            user = await self.bot.fetch_user(uid)
            return user.display_name
        except (discord.NotFound, discord.HTTPException):
            return f"User {uid}"

    # ------------------------------------------------------------------
    # Rendering / posting
    # ------------------------------------------------------------------

    @staticmethod
    def _as_bool(value: Any, default: bool = True) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() in ('1', 'true', 'yes', 'on')

    @staticmethod
    def _parse_color(value: Any) -> Optional[discord.Color]:
        if value in (None, ''):
            return None
        try:
            return discord.Color.from_str(str(value).strip())
        except ValueError:
            return None

    def _appearance(self, guild_id: int) -> Dict[str, Any]:
        """Resolve the guild's summary appearance, falling back to the defaults."""
        config = self._guild_config(guild_id)
        return {
            'title': config.get(TITLE_KEY) or DEFAULT_SUMMARY_TITLE,
            'description': config.get(DESCRIPTION_KEY) or "",
            'footer': config.get(FOOTER_KEY) or DEFAULT_SUMMARY_FOOTER,
            'color': config.get(COLOR_KEY),
            'color_obj': self._parse_color(config.get(COLOR_KEY)) or discord.Color.blurple(),
            'show_titles': self._as_bool(config.get(SHOW_TITLES_KEY), True),
            'show_leaderboard': self._as_bool(config.get(SHOW_LEADERBOARD_KEY), True),
            'show_characters': self._as_bool(config.get(SHOW_CHARACTERS_KEY), False),
        }

    @staticmethod
    def _summary_placeholders(day: date, stats: Dict[str, Any]) -> Dict[str, str]:
        logs = stats.get('logs', 0) or 0
        kizuna_logs = stats.get('kizuna_logs', 0) or 0
        return {
            'month': f"{day:%B}",
            'day': str(day.day),
            'year': str(day.year),
            'date': f"{day:%B} {day.day}, {day.year}",
            'total': _format_duration(stats.get('duration', 0)),
            'logs': str(logs),
            'users': str(stats.get('users', 0) or 0),
            'reading': _format_duration(stats.get('reading_duration', 0)),
            'listening': _format_duration(stats.get('listening_duration', 0)),
            'characters': _format_count(stats.get('characters', 0)),
            'kizuna_logs': str(kizuna_logs),
            'manual_logs': str(max(0, logs - kizuna_logs)),
        }

    @staticmethod
    def _solve(template: str, values: Dict[str, str], default: str = "") -> str:
        """Render a template, falling back to ``default`` if a stored one is bad."""
        try:
            return template.format(**values)
        except (KeyError, IndexError, ValueError):
            logger.exception("Daily summary: invalid appearance template; using the default")
            if not default:
                return ""
            try:
                return default.format(**values)
            except (KeyError, IndexError, ValueError):
                return default

    async def _build_embed(self, guild: Optional[discord.Guild], day: date, stats: Dict[str, Any]) -> discord.Embed:
        appearance = self._appearance(guild.id) if guild is not None else {
            'title': DEFAULT_SUMMARY_TITLE,
            'description': "",
            'footer': DEFAULT_SUMMARY_FOOTER,
            'color': None,
            'color_obj': discord.Color.blurple(),
            'show_titles': True,
            'show_leaderboard': True,
            'show_characters': False,
        }
        values = self._summary_placeholders(day, stats)

        title = self._solve(appearance['title'], values, DEFAULT_SUMMARY_TITLE)
        if not title.strip():
            title = DEFAULT_SUMMARY_TITLE.format(**values)
        description = self._solve(appearance['description'], values) if appearance['description'] else ""
        footer = self._solve(appearance['footer'], values) if appearance['footer'] else ""

        total = stats.get('duration', 0) or 0
        logs = stats.get('logs', 0) or 0
        users = stats.get('users', 0) or 0
        kizuna_logs = stats.get('kizuna_logs', 0) or 0
        manual_logs = max(0, logs - kizuna_logs)

        overview = [
            f"**Logged time:** {_format_duration(total)}",
            f"**Logs:** {logs} from {users} member{'s' if users != 1 else ''}",
            (
                f"**Reading:** {_format_duration(stats.get('reading_duration', 0))} • "
                f"**Listening:** {_format_duration(stats.get('listening_duration', 0))}"
            ),
        ]
        if appearance['show_characters']:
            overview.append(f"**Characters read:** {_format_count(stats.get('characters', 0))}")
        overview.append(f"**Source:** {kizuna_logs} kizuna • {manual_logs} manual")

        embed = discord.Embed(
            title=title[:256],
            description=description[:4096] or None,
            color=appearance['color_obj'],
        )
        embed.add_field(
            name="Overview",
            value="\n".join(overview),
            inline=False,
        )

        titles = stats.get('titles') or []
        if appearance['show_titles'] and titles:
            lines = []
            for entry in titles[:5]:
                media = (entry.get('media_type') or entry.get('primary_type') or "").replace('_', ' ').title()
                line = f"• **{entry.get('name') or 'Unknown'}** — {_format_duration(entry.get('duration', 0))}"
                if media:
                    line += f" ({media})"
                lines.append(line)
            embed.add_field(name="📚 Most logged titles", value="\n".join(lines), inline=False)

        top_users = stats.get('top_users') or []
        if appearance['show_leaderboard'] and top_users:
            lines = []
            for index, entry in enumerate(top_users[:3], 1):
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(index, f"{index}.")
                name = await self._display_name(guild, entry.get('user_id'))
                entry_logs = entry.get('logs', 0) or 0
                lines.append(
                    f"{medal} **{name}** — {_format_duration(entry.get('duration', 0))} "
                    f"({entry_logs} log{'s' if entry_logs != 1 else ''})"
                )
            embed.add_field(name="🏆 Leaderboard", value="\n".join(lines), inline=False)

        if footer:
            embed.set_footer(text=footer[:2048])
        return embed

    @staticmethod
    def _render_congrats(template: str, **values: str) -> str:
        """Render the template, falling back to the default if a stored one is bad."""
        try:
            return template.format(**values)
        except (KeyError, IndexError, ValueError):
            logger.exception(
                "Daily summary: invalid congratulation template; using the default"
            )
            return DEFAULT_CONGRATS_MESSAGE.format(**values)

    def _compose_congrats(
        self,
        guild_id: int,
        *,
        users: str,
        duration: str,
        date: str,
        characters: int,
    ) -> str:
        """Render the #1 message, honouring the characters-read option.

        When the option is enabled the count is appended automatically, unless the
        template already places ``{characters}`` itself.
        """
        config = self._guild_config(guild_id)
        template = config.get(SUMMARY_CONGRATS_KEY) or DEFAULT_CONGRATS_MESSAGE
        rendered = _format_count(characters)
        text = self._render_congrats(
            template,
            users=users,
            duration=duration,
            date=date,
            characters=rendered,
        )
        if self._as_bool(config.get(CONGRATS_SHOW_CHARACTERS_KEY), False) and '{characters}' not in template:
            text += f"\n📖 **{rendered}** characters read"
        return text

    def _parse_day(self, value: Optional[str]) -> Optional[date]:
        """Parse an optional YYYY-MM-DD; default is the UTC day that just ended."""
        if not value:
            return (datetime.now(timezone.utc) - timedelta(days=1)).date()
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None

    async def _stats_for_day(self, day: date) -> Optional[Dict[str, Any]]:
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        return await self._summary_for(start, start + timedelta(days=1))

    async def _build_congrats_text(
        self,
        guild: discord.Guild,
        day: date,
        stats: Dict[str, Any],
        *,
        preview: bool = False,
    ) -> Optional[str]:
        """Render the #1 message.

        ``preview`` uses bold display names instead of mentions so a preview can
        never ping anybody.
        """
        top_users = stats.get('top_users') or []
        if not top_users:
            return None

        best_duration = int(top_users[0].get('duration', 0) or 0)
        tied = [
            entry for entry in top_users
            if int(entry.get('duration', 0) or 0) == best_duration
        ]

        names = []
        for entry in tied:
            try:
                user_id = int(entry['user_id'])
            except (KeyError, TypeError, ValueError):
                continue
            if preview:
                names.append(f"**{await self._display_name(guild, user_id)}**")
                continue
            # Only ping people who can actually see the message.
            if guild.get_member(user_id) is None:
                try:
                    await guild.fetch_member(user_id)
                except (discord.NotFound, discord.HTTPException):
                    logger.info(
                        "Daily summary: #1 user %s is not in guild %s; skipping the ping",
                        user_id, guild.id,
                    )
                    continue
            names.append(f"<@{user_id}>")

        if not names:
            return None

        who = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
        characters = 0
        for entry in tied:
            try:
                characters += int(entry.get('characters', 0) or 0)
            except (TypeError, ValueError):
                continue
        return self._compose_congrats(
            guild.id,
            users=who,
            duration=_format_duration(best_duration),
            date=f"{day:%B} {day.day}",
            characters=characters,
        )

    async def _build_no_log_embed(self, guild: discord.Guild, day: date, stats: Dict[str, Any]) -> discord.Embed:
        appearance = self._appearance(guild.id)
        values = self._summary_placeholders(day, stats)
        title = self._solve(appearance['title'], values, DEFAULT_SUMMARY_TITLE)
        if not title.strip():
            title = DEFAULT_SUMMARY_TITLE.format(**values)
        embed = discord.Embed(
            title=title[:256],
            description="No immersion logs were recorded.",
            color=appearance['color_obj'],
        )
        footer = self._solve(appearance['footer'], values) if appearance['footer'] else ""
        if footer:
            embed.set_footer(text=footer[:2048])
        return embed

    async def _build_summary_embed(self, guild: discord.Guild, day: date, stats: Dict[str, Any]) -> discord.Embed:
        if stats.get('logs', 0):
            return await self._build_embed(guild, day, stats)
        return await self._build_no_log_embed(guild, day, stats)

    async def _post_summary(self, guild: discord.Guild, day: date, stats: Dict[str, Any], force: bool = False) -> bool:
        """Post only the summary embed. Returns True when it was posted."""
        channel = self._configured_channel(guild, SUMMARY_CHANNEL_KEY)
        if channel is None:
            logger.debug(
                "Daily summary: guild %s has no summary channel configured; skipping", guild.id
            )
            return False

        day_key = day.isoformat()
        if not force and self._posted_summary.get(str(guild.id)) == day_key:
            logger.debug("Daily summary: summary %s already posted for guild %s", day_key, guild.id)
            return False

        embed = await self._build_summary_embed(guild, day, stats)
        try:
            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Daily summary: could not post in #%s (guild %s)",
                getattr(channel, 'name', channel.id), guild.id,
            )
            return False

        self._posted_summary[str(guild.id)] = day_key
        self._save_state()
        return True

    async def _post_congrats(self, guild: discord.Guild, day: date, stats: Dict[str, Any], force: bool = False) -> bool:
        """Send only the #1 congratulation. Returns True when it was sent."""
        day_key = day.isoformat()
        if not force and self._posted_congrats.get(str(guild.id)) == day_key:
            logger.debug("Daily summary: congrats %s already sent for guild %s", day_key, guild.id)
            return False

        channel = (
            self._configured_channel(guild, SUMMARY_PING_CHANNEL_KEY)
            or self._configured_channel(guild, SUMMARY_CHANNEL_KEY)
        )
        if channel is None:
            logger.warning(
                "Daily summary: no congratulation channel configured for guild %s", guild.id
            )
            return False

        message = await self._build_congrats_text(guild, day, stats)
        if message is None:
            return False

        try:
            await channel.send(
                message,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Daily summary: could not send the congratulation message in guild %s", guild.id
            )
            return False

        self._posted_congrats[str(guild.id)] = day_key
        self._save_state()
        return True

    async def _post_for_guild(self, guild: discord.Guild, day: date, force: bool = False) -> bool:
        """Full scheduled post: the summary followed by the #1 congratulation."""
        stats = await self._stats_for_day(day)
        if stats is None:
            return False
        posted = await self._post_summary(guild, day, stats, force=force)
        await self._post_congrats(guild, day, stats, force=force)
        return posted

    # ------------------------------------------------------------------
    # Scheduled run
    # ------------------------------------------------------------------

    @tasks.loop(time=RUN_TIME)
    async def post_daily_summary(self):
        # Summarise the UTC day that just ended.
        day = (datetime.now(timezone.utc) - timedelta(days=1)).date()
        for guild in list(self.bot.guilds):
            try:
                await self._post_for_guild(guild, day)
            except Exception:
                logger.exception("Daily summary: failed for guild %s", guild.id)

    @post_daily_summary.before_loop
    async def before_post_daily_summary(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Summary admin command
    # ------------------------------------------------------------------

    @app_commands.command(
        name="manage_daily_summary",
        description="[ADMIN] Configure, run or test the daily immersion summary",
    )
    @app_commands.describe(
        action="What to do.",
        channel="(setup/set_channel) Where the summary is posted. Omit on set_channel to clear.",
        date="(run/test) Day as YYYY-MM-DD. Defaults to yesterday (UTC).",
        title="(setup) Embed title template.",
        description="(setup) Embed description template; empty clears it.",
        footer="(setup) Embed footer template.",
        color="(setup) Embed colour as #RRGGBB.",
        show_titles="(setup) Show the 'Most logged titles' section.",
        show_leaderboard="(setup) Show the 'Leaderboard' section.",
        show_characters="(setup) Show the characters read next to the reading time.",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="setup", value="setup"),
        app_commands.Choice(name="Set channel", value="set_channel"),
        app_commands.Choice(name="Reset style", value="reset_style"),
        app_commands.Choice(name="status", value="status"),
        app_commands.Choice(name="run", value="run"),
        app_commands.Choice(name="test", value="test"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def manage_daily_summary(
        self,
        interaction: discord.Interaction,
        action: str,
        channel: Optional[discord.TextChannel] = None,
        date: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        footer: Optional[str] = None,
        color: Optional[str] = None,
        show_titles: Optional[bool] = None,
        show_leaderboard: Optional[bool] = None,
        show_characters: Optional[bool] = None,
    ):
        if action == "status":
            await self._show_summary_status(interaction)
            return

        if action == "run":
            await self._run_summary(interaction, date)
            return

        if action == "test":
            await self._test_summary(interaction, date)
            return

        if action == "reset_style":
            await self._reset_style(interaction)
            return

        if action == "set_channel":
            await self._set_summary_settings(
                interaction, channel, clear_channel=channel is None
            )
            return

        await self._set_summary_settings(
            interaction,
            channel,
            title=title,
            description=description,
            footer=footer,
            color=color,
            show_titles=show_titles,
            show_leaderboard=show_leaderboard,
            show_characters=show_characters,
        )

    async def _set_summary_settings(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        *,
        clear_channel: bool = False,
        title: Optional[str] = None,
        description: Optional[str] = None,
        footer: Optional[str] = None,
        color: Optional[str] = None,
        show_titles: Optional[bool] = None,
        show_leaderboard: Optional[bool] = None,
        show_characters: Optional[bool] = None,
    ) -> None:
        """Validate every supplied value first, then apply them all in one save."""
        # (config key, value, human readable change); value None means "remove".
        pending = []

        if channel is not None:
            pending.append((SUMMARY_CHANNEL_KEY, str(channel.id), f"Channel set to {channel.mention}"))
        elif clear_channel:
            pending.append((
                SUMMARY_CHANNEL_KEY, None,
                "Channel cleared (the daily summary is now off)",
            ))

        if title is not None:
            text = title.strip()
            if not text:
                await interaction.response.send_message(
                    "❌ `title` cannot be empty. Use `action:reset_style` to restore the default.",
                    ephemeral=True,
                )
                return
            if len(text) > 256:
                await interaction.response.send_message(
                    "❌ `title` must be 256 characters or fewer.", ephemeral=True
                )
                return
            error = self._template_error(text)
            if error:
                await interaction.response.send_message(
                    f"❌ Invalid `title` template ({error}).", ephemeral=True
                )
                return
            pending.append((TITLE_KEY, text, "Title updated"))

        if description is not None:
            text = description.strip()
            if not text:
                pending.append((DESCRIPTION_KEY, None, "Description cleared"))
            else:
                if len(text) > 4096:
                    await interaction.response.send_message(
                        "❌ `description` must be 4096 characters or fewer.", ephemeral=True
                    )
                    return
                error = self._template_error(text)
                if error:
                    await interaction.response.send_message(
                        f"❌ Invalid `description` template ({error}).", ephemeral=True
                    )
                    return
                pending.append((DESCRIPTION_KEY, text, "Description updated"))

        if footer is not None:
            text = footer.strip()
            if not text:
                await interaction.response.send_message(
                    "❌ `footer` cannot be empty. Use `action:reset_style` to restore the default.",
                    ephemeral=True,
                )
                return
            if len(text) > 2048:
                await interaction.response.send_message(
                    "❌ `footer` must be 2048 characters or fewer.", ephemeral=True
                )
                return
            error = self._template_error(text)
            if error:
                await interaction.response.send_message(
                    f"❌ Invalid `footer` template ({error}).", ephemeral=True
                )
                return
            pending.append((FOOTER_KEY, text, "Footer updated"))

        if color is not None:
            parsed = self._parse_color(color)
            if parsed is None:
                await interaction.response.send_message(
                    "❌ `color` must be a hex value like `#5865F2`.", ephemeral=True
                )
                return
            normalized = f"#{parsed.value:06x}"
            pending.append((COLOR_KEY, normalized, f"Colour set to `{normalized}`"))

        if show_titles is not None:
            pending.append((
                SHOW_TITLES_KEY, bool(show_titles),
                f"Titles section {'shown' if show_titles else 'hidden'}",
            ))

        if show_leaderboard is not None:
            pending.append((
                SHOW_LEADERBOARD_KEY, bool(show_leaderboard),
                f"Leaderboard section {'shown' if show_leaderboard else 'hidden'}",
            ))

        if show_characters is not None:
            pending.append((
                SHOW_CHARACTERS_KEY, bool(show_characters),
                f"Characters read {'shown' if show_characters else 'hidden'}",
            ))

        if not pending:
            await interaction.response.send_message(
                "Nothing to change — pass a channel and/or at least one style option.",
                ephemeral=True,
            )
            return

        config = self.bot.config.setdefault('guilds', {}).setdefault(
            str(interaction.guild.id), {}
        )
        for key, value, _ in pending:
            if value is None:
                config.pop(key, None)
            else:
                config[key] = value
        self.bot.save_config()

        appearance = self._appearance(interaction.guild.id)
        values = SAMPLE_PLACEHOLDER_VALUES
        preview_lines = [
            f"**Title:** {self._solve(appearance['title'], values, DEFAULT_SUMMARY_TITLE)}",
            f"**Description:** {self._solve(appearance['description'], values) or '_(none)_'}",
            f"**Footer:** {self._solve(appearance['footer'], values) or '_(none)_'}",
        ]
        embed = discord.Embed(
            title="✅ Daily summary updated",
            description="\n".join(f"• {change}" for _, _, change in pending),
            color=appearance['color_obj'],
        )
        embed.add_field(name="🧪 Preview", value="\n".join(preview_lines)[:1024], inline=False)
        embed.add_field(name="ℹ️ Placeholders", value=SUMMARY_PLACEHOLDERS_HINT, inline=False)
        embed.set_footer(
            text="Run /manage_daily_summary action:test for a private preview of the real embed."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _show_summary_status(self, interaction: discord.Interaction):
        guild = interaction.guild
        summary_channel = self._configured_channel(guild, SUMMARY_CHANNEL_KEY)
        appearance = self._appearance(guild.id)
        values = SAMPLE_PLACEHOLDER_VALUES

        lines = [
            f"**Channel:** {summary_channel.mention if summary_channel else 'not set'}",
            "**Schedule:** daily at **00:05 UTC** (summarises the UTC day that just ended)",
            f"**Last posted:** {self._posted_summary.get(str(guild.id)) or 'never'}",
            f"**Title:** {self._solve(appearance['title'], values, DEFAULT_SUMMARY_TITLE)}",
            f"**Description:** {self._solve(appearance['description'], values) or '_(none)_'}",
            f"**Footer:** {self._solve(appearance['footer'], values) or '_(none)_'}",
            f"**Colour:** {appearance['color'] or 'default (blurple)'}",
            f"**Titles section:** {'on' if appearance['show_titles'] else 'off'}",
            f"**Leaderboard section:** {'on' if appearance['show_leaderboard'] else 'off'}",
            f"**Characters read:** {'shown' if appearance['show_characters'] else 'hidden'}",
        ]
        embed = discord.Embed(
            title="📊 Daily immersion summary",
            description="\n".join(lines)[:4096],
            color=appearance['color_obj'],
        )
        embed.add_field(
            name="ℹ️ What it includes",
            value="All immersion logs for the day — kizuna auto-logs and manual "
                  "`/log_*` submissions. The #1 ping is separate; see "
                  "`/manage_daily_congrats`.",
            inline=False,
        )
        embed.add_field(name="ℹ️ Placeholders", value=SUMMARY_PLACEHOLDERS_HINT, inline=False)
        embed.add_field(
            name="🧪 Test",
            value="`/manage_daily_summary action:test` shows a private preview — "
                  "nothing is posted.",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _set_congrats_message(
        self,
        interaction: discord.Interaction,
        message: Optional[str],
        show_characters: Optional[bool] = None,
    ):
        """Store the congratulation template and/or the characters-read option."""
        config = self.bot.config.setdefault('guilds', {}).setdefault(
            str(interaction.guild.id), {}
        )
        template = message.strip() if message is not None else ""
        # Omitting everything (or passing an empty message) resets to the default;
        # passing only show_characters just flips that option.
        reset_requested = not template and show_characters is None

        if template:
            if len(template) > 1500:
                await interaction.response.send_message(
                    "❌ The message must be 1500 characters or fewer.", ephemeral=True
                )
                return
            if "{users}" not in template:
                await interaction.response.send_message(
                    "❌ Include `{users}` so the winner is pinged.", ephemeral=True
                )
                return
            try:
                template.format(
                    users=interaction.user.mention,
                    duration="2h 15m",
                    date="November 18",
                    characters="1,234,567",
                )
            except (KeyError, IndexError, ValueError) as exc:
                detail = str(exc).strip("'\"") or "malformed braces"
                await interaction.response.send_message(
                    f"❌ Invalid template ({detail}). Only `{{users}}`, `{{duration}}`, "
                    "`{{date}}` and `{{characters}}` are supported.",
                    ephemeral=True,
                )
                return

        changes = []
        if reset_requested:
            if SUMMARY_CONGRATS_KEY in config:
                config.pop(SUMMARY_CONGRATS_KEY, None)
                changes.append("Message reset to the default")
            else:
                changes.append("Message is already using the default")
        elif template:
            config[SUMMARY_CONGRATS_KEY] = template
            changes.append("Message updated")

        if show_characters is not None:
            if show_characters:
                config[CONGRATS_SHOW_CHARACTERS_KEY] = True
                changes.append("Characters read will be included")
            else:
                config.pop(CONGRATS_SHOW_CHARACTERS_KEY, None)
                changes.append("Characters read hidden")

        if not changes:
            await interaction.response.send_message(
                "Nothing to change — pass `message` and/or `show_characters`.",
                ephemeral=True,
            )
            return

        self.bot.save_config()

        active_template = config.get(SUMMARY_CONGRATS_KEY) or DEFAULT_CONGRATS_MESSAGE
        preview = self._compose_congrats(
            interaction.guild.id,
            users=interaction.user.mention,
            duration="2h 15m",
            date="November 18",
            characters=1234567,
        )
        embed = discord.Embed(
            title="✅ Congratulation message updated",
            description=(
                "\n".join(f"• {change}" for change in changes)
                + "\n\n**Template:**\n"
                + f"```{active_template}```\n"
                + "**Preview:**\n"
                + preview
            )[:4096],
            color=discord.Color.green(),
        )
        embed.set_footer(
            text="Omit `message` to reset to the default. {characters} = characters read."
        )
        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ------------------------------------------------------------------
    # #1 congratulation admin command
    # ------------------------------------------------------------------

    @app_commands.command(
        name="manage_daily_congrats",
        description="[ADMIN] Configure, run or test the daily #1 congratulation ping",
    )
    @app_commands.describe(
        action="What to do.",
        channel="(set_channel) Where the ping is sent. Omit to fall back to the summary channel.",
        message="(set_message) Template with {users}, {duration}, {date}, {characters}. Server emoji allowed. Omit to reset.",
        show_characters="(set_message) Also show how many characters the winner read.",
        date="(run/test) Day as YYYY-MM-DD. Defaults to yesterday (UTC).",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Set channel", value="set_channel"),
        app_commands.Choice(name="Set message", value="set_message"),
        app_commands.Choice(name="status", value="status"),
        app_commands.Choice(name="run", value="run"),
        app_commands.Choice(name="test", value="test"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def manage_daily_congrats(
        self,
        interaction: discord.Interaction,
        action: str,
        channel: Optional[discord.TextChannel] = None,
        message: Optional[str] = None,
        show_characters: Optional[bool] = None,
        date: Optional[str] = None,
    ):
        if action == "status":
            await self._show_congrats_status(interaction)
            return

        if action == "run":
            await self._run_congrats(interaction, date)
            return

        if action == "test":
            await self._test_congrats(interaction, date)
            return

        if action == "set_message":
            await self._set_congrats_message(interaction, message, show_characters)
            return

        await self._set_congrats_channel(interaction, channel)

    async def _set_congrats_channel(
        self, interaction: discord.Interaction, channel: Optional[discord.TextChannel]
    ):
        """Set the congratulation channel, or clear it to fall back to the summary channel."""
        config = self.bot.config.setdefault('guilds', {}).setdefault(
            str(interaction.guild.id), {}
        )

        if channel is None:
            if SUMMARY_PING_CHANNEL_KEY not in config:
                await interaction.response.send_message(
                    "ℹ️ The #1 ping already uses the summary channel. "
                    "Pass `channel` to give it its own channel.",
                    ephemeral=True,
                )
                return
            config.pop(SUMMARY_PING_CHANNEL_KEY, None)
            self.bot.save_config()
            await interaction.response.send_message(
                "✅ Congratulation channel cleared — the ping will use the summary channel.",
                ephemeral=True,
            )
            return

        config[SUMMARY_PING_CHANNEL_KEY] = str(channel.id)
        self.bot.save_config()

        summary_channel = self._configured_channel(interaction.guild, SUMMARY_CHANNEL_KEY)
        embed = discord.Embed(
            title="✅ Congratulation channel updated",
            description=f"• #1 congratulation channel set to {channel.mention}",
            color=discord.Color.green(),
        )
        if summary_channel is not None and summary_channel.id == channel.id:
            embed.add_field(
                name="ℹ️ Same channel",
                value="This is also the summary channel, so both messages appear there.",
                inline=False,
            )
        embed.set_footer(text="Omit `channel` to fall back to the summary channel.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @staticmethod
    def _template_error(template: str) -> Optional[str]:
        """Return why a template cannot be rendered, or None when it is valid."""
        try:
            template.format(**SAMPLE_PLACEHOLDER_VALUES)
            return None
        except (KeyError, IndexError, ValueError) as exc:
            return str(exc).strip("'\"") or "malformed braces"

    async def _show_congrats_status(self, interaction: discord.Interaction):
        guild = interaction.guild
        channel = self._configured_channel(guild, SUMMARY_PING_CHANNEL_KEY)
        fallback = self._configured_channel(guild, SUMMARY_CHANNEL_KEY)
        if channel is not None:
            target = channel.mention
        elif fallback is not None:
            target = f"(same as the summary channel — {fallback.mention})"
        else:
            target = "not set"

        template = (
            self._guild_config(guild.id).get(SUMMARY_CONGRATS_KEY)
            or DEFAULT_CONGRATS_MESSAGE
        )
        lines = [
            f"**Channel:** {target}",
            "**Schedule:** daily at **00:05 UTC**, right after the summary",
            f"**Last sent:** {self._posted_congrats.get(str(guild.id)) or 'never'}",
            f"**Characters read:** "
            + (
                "included"
                if self._as_bool(self._guild_config(guild.id).get(CONGRATS_SHOW_CHARACTERS_KEY), False)
                else "hidden"
            ),
        ]
        embed = discord.Embed(
            title="🏆 Daily #1 congratulation",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="💬 Message",
            value="```" + template[:800] + "```\n" + CONGRATS_PLACEHOLDERS_HINT,
            inline=False,
        )
        embed.add_field(
            name="🧪 Test",
            value="`/manage_daily_congrats action:test` shows a private preview — "
                  "nobody is pinged.",
            inline=False,
        )
        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _run_congrats(self, interaction: discord.Interaction, day_value: Optional[str]):
        """Send the real #1 congratulation now."""
        await interaction.response.defer(ephemeral=True)
        day = self._parse_day(day_value)
        if day is None:
            await interaction.followup.send(
                "❌ `date` must be in YYYY-MM-DD format.", ephemeral=True
            )
            return

        stats = await self._stats_for_day(day)
        if stats is None:
            await interaction.followup.send(
                "❌ Could not read the activity data. Check the bot log for the reason.",
                ephemeral=True,
            )
            return

        channel = (
            self._configured_channel(interaction.guild, SUMMARY_PING_CHANNEL_KEY)
            or self._configured_channel(interaction.guild, SUMMARY_CHANNEL_KEY)
        )
        if channel is None:
            await interaction.followup.send(
                "❌ No congratulation channel is set. Use "
                "`/manage_daily_congrats action:set_channel channel:#channel` first.",
                ephemeral=True,
            )
            return

        if not (stats.get('top_users') or []):
            await interaction.followup.send(
                f"ℹ️ There were no immersion logs on **{day.isoformat()}**, so there is "
                "nobody to congratulate.",
                ephemeral=True,
            )
            return

        sent = await self._post_congrats(interaction.guild, day, stats, force=True)
        if sent:
            await interaction.followup.send(
                f"✅ Sent the #1 congratulation for **{day.isoformat()}** in "
                f"{channel.mention}.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.followup.send(
                "❌ Could not send the congratulation message. Check the bot log for the "
                "reason (usually a missing Send Messages permission, or the #1 member "
                "has left the server).",
                ephemeral=True,
            )

    async def _test_congrats(self, interaction: discord.Interaction, day_value: Optional[str]):
        """Private preview of the #1 message. Never sends or pings anything."""
        await interaction.response.defer(ephemeral=True)
        day = self._parse_day(day_value)
        if day is None:
            await interaction.followup.send(
                "❌ `date` must be in YYYY-MM-DD format.", ephemeral=True
            )
            return

        stats = await self._stats_for_day(day)
        preview = None
        note = None
        if stats is not None:
            preview = await self._build_congrats_text(
                interaction.guild, day, stats, preview=True
            )
        if preview is None:
            # No logs, or nobody eligible: render the template with sample numbers so
            # the formatting can still be checked.
            preview = self._compose_congrats(
                interaction.guild.id,
                users=f"**{interaction.user.display_name}**",
                duration="2h 15m",
                date=f"{day:%B} {day.day}",
                characters=1234567,
            )
            note = (
                f"No immersion logs were found for **{day.isoformat()}**, so this "
                "preview uses your name and sample numbers."
            )

        channel = (
            self._configured_channel(interaction.guild, SUMMARY_PING_CHANNEL_KEY)
            or self._configured_channel(interaction.guild, SUMMARY_CHANNEL_KEY)
        )
        embed = discord.Embed(
            title="🧪 Preview — #1 congratulation",
            description=preview[:4096],
            color=discord.Color.gold(),
        )
        if note:
            embed.add_field(name="ℹ️ Note", value=note, inline=False)
        embed.add_field(
            name="📤 Would be sent to",
            value=channel.mention if channel else "❌ no channel configured",
            inline=False,
        )
        embed.set_footer(text="Preview only — nothing was posted and nobody was pinged.")
        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _reset_style(self, interaction: discord.Interaction):
        config = self.bot.config.setdefault('guilds', {}).setdefault(
            str(interaction.guild.id), {}
        )
        removed = []
        for key in (
            TITLE_KEY, DESCRIPTION_KEY, FOOTER_KEY, COLOR_KEY,
            SHOW_TITLES_KEY, SHOW_LEADERBOARD_KEY, SHOW_CHARACTERS_KEY,
        ):
            if key in config:
                config.pop(key, None)
                removed.append(key)
        if removed:
            self.bot.save_config()
            await interaction.response.send_message(
                "✅ Daily summary style reset to the defaults.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "ℹ️ The daily summary is already using the default style.", ephemeral=True
            )

    async def _run_summary(self, interaction: discord.Interaction, day_value: Optional[str]):
        """Post the real summary embed now."""
        await interaction.response.defer(ephemeral=True)
        day = self._parse_day(day_value)
        if day is None:
            await interaction.followup.send(
                "❌ `date` must be in YYYY-MM-DD format.", ephemeral=True
            )
            return

        channel = self._configured_channel(interaction.guild, SUMMARY_CHANNEL_KEY)
        if channel is None:
            await interaction.followup.send(
                "❌ No summary channel is set. Use "
                "`/manage_daily_summary action:set_channel channel:#channel` first.",
                ephemeral=True,
            )
            return

        stats = await self._stats_for_day(day)
        if stats is None:
            await interaction.followup.send(
                "❌ Could not read the activity data. Check the bot log for the reason.",
                ephemeral=True,
            )
            return

        posted = await self._post_summary(interaction.guild, day, stats, force=True)
        if posted:
            await interaction.followup.send(
                f"✅ Posted the summary for **{day.isoformat()}** in {channel.mention}.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.followup.send(
                "❌ Could not post the summary. Check the bot log for the reason "
                "(usually missing Send Messages / Embed Links permission).",
                ephemeral=True,
            )

    async def _test_summary(self, interaction: discord.Interaction, day_value: Optional[str]):
        """Private preview of the summary embed. Never posts anything."""
        await interaction.response.defer(ephemeral=True)
        day = self._parse_day(day_value)
        if day is None:
            await interaction.followup.send(
                "❌ `date` must be in YYYY-MM-DD format.", ephemeral=True
            )
            return

        stats = await self._stats_for_day(day)
        if stats is None:
            await interaction.followup.send(
                "❌ Could not read the activity data. Check the bot log for the reason.",
                ephemeral=True,
            )
            return

        embed = await self._build_summary_embed(interaction.guild, day, stats)
        channel = self._configured_channel(interaction.guild, SUMMARY_CHANNEL_KEY)
        lines = ["🧪 **Preview only** — nothing was posted and nobody was pinged."]
        lines.append(
            f"📤 Would be posted to {channel.mention}."
            if channel else "📤 No summary channel is configured yet."
        )
        if not stats.get('logs', 0):
            lines.append(
                f"ℹ️ No immersion logs were found for **{day.isoformat()}**, so this is "
                "the empty-day variant."
            )
        await interaction.followup.send(
            content="\n".join(lines),
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot):
    await bot.add_cog(DailyLogSummaryCog(bot))
