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
STATE_FILE = 'data/daily_summary_state.json'

# Supported placeholders in a custom congratulation message. ``{users}`` is
# required so the winner is always pinged.
DEFAULT_CONGRATS_MESSAGE = (
    "🎉 Congratulations {users}! You were **#1** on the {date} immersion "
    "leaderboard with **{duration}**."
)
CONGRATS_PLACEHOLDERS_HINT = (
    "Placeholders: `{users}` (winner mention), `{duration}`, `{date}`. "
    "Server emoji such as `<:name:id>` (or `<a:name:id>`) work too."
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


class DailyLogSummaryCog(commands.Cog):
    """Posts a daily summary of all immersion logs and congratulates the top member."""

    def __init__(self, bot):
        self.bot = bot
        # guild id -> ISO date already summarised, so a restart around the run
        # time cannot post the same day twice.
        self._posted_days: Dict[str, str] = self._load_state()

    # ------------------------------------------------------------------
    # Lifecycle / persistence
    # ------------------------------------------------------------------

    async def cog_load(self):
        self.post_daily_summary.start()

    async def cog_unload(self):
        self.post_daily_summary.cancel()

    def _load_state(self) -> Dict[str, str]:
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items()}
        except Exception:
            logger.exception("Daily summary: could not read %s", STATE_FILE)
        return {}

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            tmp_path = f"{STATE_FILE}.tmp"
            with open(tmp_path, 'w') as f:
                json.dump(self._posted_days, f, indent=2)
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

    async def _build_embed(self, guild: Optional[discord.Guild], day: date, stats: Dict[str, Any]) -> discord.Embed:
        total = stats.get('duration', 0) or 0
        logs = stats.get('logs', 0) or 0
        users = stats.get('users', 0) or 0
        kizuna_logs = stats.get('kizuna_logs', 0) or 0
        manual_logs = max(0, logs - kizuna_logs)

        embed = discord.Embed(
            title=f"📊 Daily Immersion Summary — {day:%B} {day.day}, {day.year}",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Overview",
            value=(
                f"**Logged time:** {_format_duration(total)}\n"
                f"**Logs:** {logs} from {users} member{'s' if users != 1 else ''}\n"
                f"**Reading:** {_format_duration(stats.get('reading_duration', 0))} • "
                f"**Listening:** {_format_duration(stats.get('listening_duration', 0))}\n"
                f"**Source:** {kizuna_logs} kizuna • {manual_logs} manual"
            ),
            inline=False,
        )

        titles = stats.get('titles') or []
        if titles:
            lines = []
            for entry in titles[:5]:
                media = (entry.get('media_type') or entry.get('primary_type') or "").replace('_', ' ').title()
                line = f"• **{entry.get('name') or 'Unknown'}** — {_format_duration(entry.get('duration', 0))}"
                if media:
                    line += f" ({media})"
                lines.append(line)
            embed.add_field(name="📚 Most logged titles", value="\n".join(lines), inline=False)

        top_users = stats.get('top_users') or []
        if top_users:
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

        embed.set_footer(
            text="Includes kizuna and manual logs. Days and times are UTC."
        )
        return embed

    async def _congratulate_top(self, guild: discord.Guild, day: date, stats: Dict[str, Any]) -> None:
        """Ping the day's #1 member (or everyone tied for #1) in the configured channel."""
        top_users = stats.get('top_users') or []
        if not top_users:
            return

        channel = (
            self._configured_channel(guild, SUMMARY_PING_CHANNEL_KEY)
            or self._configured_channel(guild, SUMMARY_CHANNEL_KEY)
        )
        if channel is None:
            logger.warning(
                "Daily summary: no congratulation channel configured for guild %s", guild.id
            )
            return

        best_duration = int(top_users[0].get('duration', 0) or 0)
        tied = [
            entry for entry in top_users
            if int(entry.get('duration', 0) or 0) == best_duration
        ]

        mentions = []
        for entry in tied:
            try:
                user_id = int(entry['user_id'])
            except (KeyError, TypeError, ValueError):
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
            mentions.append(f"<@{user_id}>")

        if not mentions:
            return

        if len(mentions) == 1:
            who = mentions[0]
        else:
            who = ", ".join(mentions[:-1]) + f" and {mentions[-1]}"

        template = (
            self._guild_config(guild.id).get(SUMMARY_CONGRATS_KEY)
            or DEFAULT_CONGRATS_MESSAGE
        )
        message = self._render_congrats(
            template,
            users=who,
            duration=_format_duration(best_duration),
            date=f"{day:%B} {day.day}",
        )

        try:
            await channel.send(
                message,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Daily summary: could not send the congratulation message in guild %s", guild.id
            )

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

    async def _post_for_guild(self, guild: discord.Guild, day: date, force: bool = False) -> bool:
        """Post one guild's summary (and ping). Returns True when it was posted."""
        channel = self._configured_channel(guild, SUMMARY_CHANNEL_KEY)
        if channel is None:
            logger.debug(
                "Daily summary: guild %s has no summary channel configured; skipping", guild.id
            )
            return False

        day_key = day.isoformat()
        if not force and self._posted_days.get(str(guild.id)) == day_key:
            logger.debug("Daily summary: %s already posted for guild %s", day_key, guild.id)
            return False

        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        stats = await self._summary_for(start, end)
        if stats is None:
            return False

        if stats.get('logs', 0):
            embed = await self._build_embed(guild, day, stats)
        else:
            embed = discord.Embed(
                title=f"📊 Daily Immersion Summary — {day:%B} {day.day}, {day.year}",
                description="No immersion logs were recorded.",
                color=discord.Color.dark_grey(),
            )

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

        await self._congratulate_top(guild, day, stats)

        self._posted_days[str(guild.id)] = day_key
        self._save_state()
        return True

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
    # Admin command
    # ------------------------------------------------------------------

    @app_commands.command(
        name="manage_daily_summary",
        description="[ADMIN] Configure or test the daily immersion log summary",
    )
    @app_commands.describe(
        action="What to do.",
        summary_channel="(setup/set_summary_channel) Channel for the daily summary embed.",
        ping_channel="(setup/set_ping_channel) Channel for the #1 congratulation ping.",
        date="(run) Day to summarise as YYYY-MM-DD. Defaults to yesterday (UTC).",
        message="(set_congrats_message) Template with {users}, {duration}, {date}. Server emoji allowed. Omit to reset.",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="setup", value="setup"),
        app_commands.Choice(name="Set summary channel", value="set_summary_channel"),
        app_commands.Choice(name="Set congratulation channel", value="set_ping_channel"),
        app_commands.Choice(name="Set congratulation message", value="set_congrats_message"),
        app_commands.Choice(name="status", value="status"),
        app_commands.Choice(name="run", value="run"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def manage_daily_summary(
        self,
        interaction: discord.Interaction,
        action: str,
        summary_channel: Optional[discord.TextChannel] = None,
        ping_channel: Optional[discord.TextChannel] = None,
        date: Optional[str] = None,
        message: Optional[str] = None,
    ):
        if action == "status":
            await self._show_status(interaction)
            return

        if action == "run":
            await self._run_now(interaction, date)
            return

        if action == "set_congrats_message":
            await self._set_congrats_message(interaction, message)
            return

        if action == "setup":
            await self._set_channels(interaction, summary_channel, ping_channel)
            return

        if action == "set_summary_channel":
            # Omitting the channel clears it, which is how the summary is turned off.
            await self._set_channels(
                interaction, summary_channel, None, clear_summary=summary_channel is None
            )
            return

        # set_ping_channel: omitting the channel falls back to the summary channel.
        await self._set_channels(
            interaction, None, ping_channel, clear_ping=ping_channel is None
        )

    async def _set_channels(
        self,
        interaction: discord.Interaction,
        summary_channel: Optional[discord.TextChannel],
        ping_channel: Optional[discord.TextChannel],
        *,
        clear_summary: bool = False,
        clear_ping: bool = False,
    ):
        """Update the summary and/or congratulation channels independently."""
        config = self.bot.config.setdefault('guilds', {}).setdefault(
            str(interaction.guild.id), {}
        )
        changes = []

        if summary_channel is not None:
            config[SUMMARY_CHANNEL_KEY] = str(summary_channel.id)
            changes.append(f"Summary channel set to {summary_channel.mention}")
        elif clear_summary:
            config.pop(SUMMARY_CHANNEL_KEY, None)
            changes.append("Summary channel cleared (the daily summary is now off)")

        if ping_channel is not None:
            config[SUMMARY_PING_CHANNEL_KEY] = str(ping_channel.id)
            changes.append(f"#1 congratulation channel set to {ping_channel.mention}")
        elif clear_ping:
            config.pop(SUMMARY_PING_CHANNEL_KEY, None)
            changes.append(
                "#1 congratulation channel cleared (the ping will use the summary channel)"
            )

        if not changes:
            await interaction.response.send_message(
                "Nothing to change — pass `summary_channel` and/or `ping_channel`.",
                ephemeral=True,
            )
            return

        self.bot.save_config()

        embed = discord.Embed(
            title="✅ Daily summary channels updated",
            description="\n".join(f"• {change}" for change in changes),
            color=discord.Color.green(),
        )
        summary_id = config.get(SUMMARY_CHANNEL_KEY)
        ping_id = config.get(SUMMARY_PING_CHANNEL_KEY)
        if summary_id and ping_id and str(summary_id) == str(ping_id):
            embed.add_field(
                name="ℹ️ Same channel",
                value="Both are set to the same channel, so the #1 ping will appear "
                      "there too. Pass a different `ping_channel` to separate them.",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _show_status(self, interaction: discord.Interaction):
        guild = interaction.guild
        summary_channel = self._configured_channel(guild, SUMMARY_CHANNEL_KEY)
        ping_channel = self._configured_channel(guild, SUMMARY_PING_CHANNEL_KEY)
        last_posted = self._posted_days.get(str(guild.id))

        lines = [
            f"**Summary channel:** {summary_channel.mention if summary_channel else 'not set'}",
            f"**Congratulation channel:** {ping_channel.mention if ping_channel else '(same as the summary channel)'}",
            f"**Schedule:** daily at **00:05 UTC** (summarises the UTC day that just ended)",
            f"**Last posted:** {last_posted or 'never'}",
        ]
        if summary_channel and ping_channel and summary_channel.id == ping_channel.id:
            lines.append("⚠️ Both are set to the **same** channel.")
        embed = discord.Embed(
            title="📊 Daily Log Summary",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="ℹ️ What it includes",
            value="All immersion logs for the day — kizuna auto-logs and manual "
                  "`/log_*` submissions. The #1 member by time is pinged in the "
                  "congratulation channel.",
            inline=False,
        )
        embed.add_field(
            name="🧪 Test",
            value="`/manage_daily_summary action:run` posts yesterday's summary now.",
            inline=False,
        )
        template = (
            self._guild_config(guild.id).get(SUMMARY_CONGRATS_KEY)
            or DEFAULT_CONGRATS_MESSAGE
        )
        embed.add_field(
            name="💬 Congratulation message",
            value="```" + template[:800] + "```\n" + CONGRATS_PLACEHOLDERS_HINT,
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _set_congrats_message(self, interaction: discord.Interaction, message: Optional[str]):
        """Store a per-guild congratulation template, or clear it to restore the default."""
        config = self.bot.config.setdefault('guilds', {}).setdefault(
            str(interaction.guild.id), {}
        )

        if message is None or not message.strip():
            config.pop(SUMMARY_CONGRATS_KEY, None)
            self.bot.save_config()
            await interaction.response.send_message(
                "✅ Congratulation message reset to the default:\n"
                f"```{DEFAULT_CONGRATS_MESSAGE}```",
                ephemeral=True,
            )
            return

        template = message.strip()
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
            preview = template.format(
                users=interaction.user.mention,
                duration="2h 15m",
                date="November 18",
            )
        except (KeyError, IndexError, ValueError) as exc:
            detail = str(exc).strip("'\"") or "malformed braces"
            await interaction.response.send_message(
                f"❌ Invalid template ({detail}). Only `{{users}}`, `{{duration}}` "
                "and `{{date}}` are supported.",
                ephemeral=True,
            )
            return

        config[SUMMARY_CONGRATS_KEY] = template
        self.bot.save_config()

        embed = discord.Embed(
            title="✅ Congratulation message updated",
            description=(
                "**Template:**\n"
                f"```{template}```\n"
                "**Preview:**\n"
                f"{preview}"
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(text="Omit `message` to reset to the default.")
        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _run_now(self, interaction: discord.Interaction, day_value: Optional[str]):
        await interaction.response.defer(ephemeral=True)
        day = (datetime.now(timezone.utc) - timedelta(days=1)).date()
        if day_value:
            try:
                day = datetime.strptime(day_value.strip(), "%Y-%m-%d").date()
            except ValueError:
                await interaction.followup.send(
                    "❌ `date` must be in YYYY-MM-DD format.", ephemeral=True
                )
                return

        posted = await self._post_for_guild(interaction.guild, day, force=True)
        if posted:
            await interaction.followup.send(
                f"✅ Posted the summary for **{day.isoformat()}**.", ephemeral=True
            )
        elif self._configured_channel(interaction.guild, SUMMARY_CHANNEL_KEY) is None:
            await interaction.followup.send(
                "❌ No summary channel is set. Use "
                "`/manage_daily_summary action:set_channel channel:#channel` first.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "❌ Could not post the summary. Check the bot log for the reason "
                "(usually missing Send Messages / Embed Links permission).",
                ephemeral=True,
            )


async def setup(bot):
    await bot.add_cog(DailyLogSummaryCog(bot))
