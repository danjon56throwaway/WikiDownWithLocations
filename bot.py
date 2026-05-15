rom __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("wikidown")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CHECK_URL = os.getenv("CHECK_URL", "https://scp-wiki.wikidot.com/").strip()
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
FAIL_THRESHOLD = int(os.getenv("FAIL_THRESHOLD", "3"))
RECOVERY_THRESHOLD = int(os.getenv("RECOVERY_THRESHOLD", "2"))
SLOW_THRESHOLD_SECONDS = float(os.getenv("SLOW_THRESHOLD_SECONDS", "5.0"))
CHECKHOST_MAX_NODES = int(os.getenv("CHECKHOST_MAX_NODES", "999"))
AUTO_MANAGE_ALERT_CHANNEL_PERMISSIONS = os.getenv(
    "AUTO_MANAGE_ALERT_CHANNEL_PERMISSIONS", "true"
).lower() in {"1", "true", "yes", "on"}
ALERT_CATEGORY_NAME = os.getenv("ALERT_CATEGORY_NAME", "Wikidot Alerts")
WATCH_ROLE_CHANNEL_NAME = os.getenv("WATCH_ROLE_CHANNEL_NAME", "watch-roles")
ALERT_CONFIG_FILE = Path(os.getenv("ALERT_CONFIG_FILE", "alert_config.json"))
ALERT_IMAGE_FILE = Path(os.getenv("ALERT_IMAGE_FILE", "image.png"))
GUILD_ID = int(os.getenv("GUILD_ID", "0") or "0")

CHECKHOST_START_URL = "https://check-host.net/check-http"
CHECKHOST_RESULT_URL = "https://check-host.net/check-result/{request_id}"
CHECKHOST_NODES_URL = "https://check-host.net/nodes/hosts"
CHECKHOST_HEADERS = {"Accept": "application/json"}

# Region definitions. Keep emoji unicode and unique.
REGIONS: dict[str, dict[str, str]] = {
    "global": {
        "label": "Global",
        "role": "Watch - Global",
        "channel": "alerts-global",
        "emoji": "1️⃣",
    },
    "north-america": {
        "label": "North America",
        "role": "Watch - North America",
        "channel": "alerts-north-america",
        "emoji": "2️⃣",
    },
    "europe": {
        "label": "Europe",
        "role": "Watch - Europe",
        "channel": "alerts-europe",
        "emoji": "3️⃣",
    },
    "asia-pacific": {
        "label": "Asia-Pacific",
        "role": "Watch - Asia-Pacific",
        "channel": "alerts-asia-pacific",
        "emoji": "4️⃣",
    },
    "middle-east": {
        "label": "Middle East",
        "role": "Watch - Middle East",
        "channel": "alerts-middle-east",
        "emoji": "5️⃣",
    },
    "south-america": {
        "label": "South America",
        "role": "Watch - South America",
        "channel": "alerts-south-america",
        "emoji": "6️⃣",
    },
}
EMOJI_TO_REGION = {v["emoji"]: k for k, v in REGIONS.items()}
ROLE_NAMES = {v["role"] for v in REGIONS.values()}

# Location ignore list. These are known blocklist/noise locations for the target.
DEFAULT_IGNORED_LOCATIONS = {
    "hong kong, hong kong",
    "isfahan, iran",
    "karaj, iran",
    "tehran, iran",
    "netanya, israel",
    "meppel, netherlands",
    "ekaterinburg, russia",
    "moscow, russia",
    "saint petersburg, russia",
    "st petersburg, russia",
    "stockholm, sweden",
    "coventry, uk",
    "coventry, united kingdom",
    "atlanta, usa",
    "atlanta, united states",
}

COUNTRY_TO_REGION = {
    # North America
    "usa": "north-america",
    "united states": "north-america",
    "canada": "north-america",
    "mexico": "north-america",
    # South America
    "argentina": "south-america",
    "brazil": "south-america",
    "chile": "south-america",
    "colombia": "south-america",
    "ecuador": "south-america",
    "peru": "south-america",
    "venezuela": "south-america",
    "uruguay": "south-america",
    # Europe / Eurasia-ish
    "austria": "europe",
    "belarus": "europe",
    "belgium": "europe",
    "bulgaria": "europe",
    "croatia": "europe",
    "czech republic": "europe",
    "denmark": "europe",
    "estonia": "europe",
    "finland": "europe",
    "france": "europe",
    "germany": "europe",
    "greece": "europe",
    "hungary": "europe",
    "ireland": "europe",
    "italy": "europe",
    "latvia": "europe",
    "lithuania": "europe",
    "moldova": "europe",
    "netherlands": "europe",
    "norway": "europe",
    "poland": "europe",
    "portugal": "europe",
    "romania": "europe",
    "russia": "europe",
    "serbia": "europe",
    "slovakia": "europe",
    "spain": "europe",
    "sweden": "europe",
    "switzerland": "europe",
    "uk": "europe",
    "united kingdom": "europe",
    "ukraine": "europe",
    # Asia-Pacific
    "australia": "asia-pacific",
    "china": "asia-pacific",
    "hong kong": "asia-pacific",
    "india": "asia-pacific",
    "indonesia": "asia-pacific",
    "japan": "asia-pacific",
    "kazakhstan": "asia-pacific",
    "malaysia": "asia-pacific",
    "new zealand": "asia-pacific",
    "philippines": "asia-pacific",
    "singapore": "asia-pacific",
    "south korea": "asia-pacific",
    "taiwan": "asia-pacific",
    "thailand": "asia-pacific",
    "vietnam": "asia-pacific",
    # Middle East
    "bahrain": "middle-east",
    "iran": "middle-east",
    "iraq": "middle-east",
    "israel": "middle-east",
    "jordan": "middle-east",
    "kuwait": "middle-east",
    "lebanon": "middle-east",
    "oman": "middle-east",
    "qatar": "middle-east",
    "saudi arabia": "middle-east",
    "turkey": "middle-east",
    "uae": "middle-east",
    "united arab emirates": "middle-east",
}


def canonical_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def normalize_location(city: str, country: str) -> str:
    city = city.strip() or "Unknown"
    country = country.strip() or "Unknown"
    return f"{city}, {country}"


def location_key(location: str) -> str:
    return canonical_text(location)


def invert_location_key(location: str) -> str:
    parts = [p.strip() for p in location.split(",", 1)]
    if len(parts) == 2:
        return canonical_text(f"{parts[1]}, {parts[0]}")
    return canonical_text(location)


def is_ignored_location(location: str) -> bool:
    key = location_key(location)
    inv = invert_location_key(location)
    return key in DEFAULT_IGNORED_LOCATIONS or inv in DEFAULT_IGNORED_LOCATIONS


def infer_region(location: str, country: str = "") -> str:
    # Prefer explicit country from Check-Host node metadata; fall back to location suffix.
    candidates = []
    if country:
        candidates.append(canonical_text(country))
    if "," in location:
        candidates.append(canonical_text(location.rsplit(",", 1)[-1]))
    candidates.append(canonical_text(location))
    for candidate in candidates:
        if candidate in COUNTRY_TO_REGION:
            return COUNTRY_TO_REGION[candidate]
    return "global"


@dataclass
class NodeMeta:
    node: str
    country_code: str = ""
    country: str = "Unknown"
    city: str = "Unknown"
    location: str = "Unknown, Unknown"
    region: str = "global"


@dataclass
class NodeResult:
    node: str
    location: str
    region: str
    ignored: bool = False
    ok: bool = False
    slow: bool = False
    response_time: Optional[float] = None
    status_code: Optional[int] = None
    message: str = ""
    ip: Optional[str] = None
    raw: Any = None
    parse_error: Optional[str] = None

    @property
    def bad(self) -> bool:
        return (not self.ok) or self.slow

    @property
    def reason(self) -> str:
        if self.ignored:
            return "ignored"
        if self.parse_error:
            return f"parse error: {self.parse_error}"
        if self.slow and self.ok:
            return "slow"
        if not self.ok:
            return "down"
        return "ok"


@dataclass
class RegionState:
    failing_streak: int = 0
    recovery_streak: int = 0
    in_incident: bool = False
    last_alert_at: Optional[datetime] = None


@dataclass
class CheckSnapshot:
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    request_id: Optional[str] = None
    results: list[NodeResult] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def monitored_results(self) -> list[NodeResult]:
        return [r for r in self.results if not r.ignored]

    @property
    def ignored_count(self) -> int:
        return len([r for r in self.results if r.ignored])

    @property
    def healthy_count(self) -> int:
        return len([r for r in self.monitored_results if not r.bad])

    @property
    def bad_count(self) -> int:
        return len([r for r in self.monitored_results if r.bad])

    def bad_by_region(self) -> dict[str, list[NodeResult]]:
        grouped: dict[str, list[NodeResult]] = {k: [] for k in REGIONS}
        for result in self.monitored_results:
            if result.bad:
                grouped.setdefault(result.region, []).append(result)
        return grouped


class JsonStore:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("Could not load %s: %s", self.path, exc)
                self.data = {}
        else:
            self.data = {}

    def save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)
store = JsonStore(ALERT_CONFIG_FILE)
http_session: Optional[aiohttp.ClientSession] = None
node_cache: dict[str, NodeMeta] = {}
latest_snapshot = CheckSnapshot(error="No check has completed yet.")
region_states: dict[str, RegionState] = {key: RegionState() for key in REGIONS}
node_failure_streaks: dict[str, int] = {}


def region_choices() -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=v["label"], value=k) for k, v in REGIONS.items()]


async def get_json(url: str, *, params: Optional[dict[str, Any]] = None, timeout: float = 20) -> Any:
    if http_session is None:
        raise RuntimeError("HTTP session is not initialized")
    async with http_session.get(url, headers=CHECKHOST_HEADERS, params=params, timeout=timeout) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


async def load_checkhost_nodes() -> dict[str, NodeMeta]:
    global node_cache
    try:
        data = await get_json(CHECKHOST_NODES_URL, timeout=20)
    except Exception as exc:
        log.warning("Could not fetch Check-Host nodes metadata: %s", exc)
        return node_cache

    nodes_raw = data.get("nodes", data) if isinstance(data, dict) else {}
    parsed: dict[str, NodeMeta] = {}
    if isinstance(nodes_raw, dict):
        for node, info in nodes_raw.items():
            country_code = ""
            country = "Unknown"
            city = "Unknown"
            if isinstance(info, list):
                # Common shape: [country_code, country_name, city, ...]
                if len(info) > 0:
                    country_code = str(info[0] or "")
                if len(info) > 1:
                    country = str(info[1] or "Unknown")
                if len(info) > 2:
                    city = str(info[2] or "Unknown")
            elif isinstance(info, dict):
                country_code = str(info.get("country_code") or info.get("cc") or "")
                country = str(info.get("country") or info.get("country_name") or "Unknown")
                city = str(info.get("city") or "Unknown")
            location = normalize_location(city, country)
            parsed[node] = NodeMeta(
                node=node,
                country_code=country_code,
                country=country,
                city=city,
                location=location,
                region=infer_region(location, country),
            )
    if parsed:
        node_cache = parsed
    return node_cache


async def start_checkhost_check() -> tuple[str, dict[str, NodeMeta]]:
    params = {
        "host": CHECK_URL,
        "max_nodes": CHECKHOST_MAX_NODES,
    }
    data = await get_json(CHECKHOST_START_URL, params=params, timeout=30)
    if not isinstance(data, dict) or not data.get("request_id"):
        raise RuntimeError(f"Check-Host did not return request_id: {data!r}")
    request_id = str(data["request_id"])

    # Start response usually includes selected nodes; prefer these for exact location list.
    selected_nodes: dict[str, NodeMeta] = {}
    nodes = data.get("nodes")
    if isinstance(nodes, dict):
        for node, info in nodes.items():
            country_code = ""
            country = "Unknown"
            city = "Unknown"
            if isinstance(info, list):
                if len(info) > 0:
                    country_code = str(info[0] or "")
                if len(info) > 1:
                    country = str(info[1] or "Unknown")
                if len(info) > 2:
                    city = str(info[2] or "Unknown")
            elif isinstance(info, dict):
                country_code = str(info.get("country_code") or info.get("cc") or "")
                country = str(info.get("country") or info.get("country_name") or "Unknown")
                city = str(info.get("city") or "Unknown")
            location = normalize_location(city, country)
            selected_nodes[node] = NodeMeta(
                node=node,
                country_code=country_code,
                country=country,
                city=city,
                location=location,
                region=infer_region(location, country),
            )

    if not selected_nodes:
        await load_checkhost_nodes()
        selected_nodes = {node: meta for node, meta in node_cache.items()}

    return request_id, selected_nodes


def parse_status_code(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(str(value).split()[0])
    except Exception:
        return None


def parse_http_result(node: str, raw: Any, meta: NodeMeta) -> NodeResult:
    result = NodeResult(
        node=node,
        location=meta.location,
        region=meta.region,
        ignored=is_ignored_location(meta.location),
        raw=raw,
    )
    try:
        # HTTP result expected shape: [[1, 0.13, "OK", "200", "1.2.3.4"]]
        # Some failures arrive as [None, {"message": "Connect timeout"}].
        if raw is None:
            result.ok = False
            result.message = "No result yet"
            return result

        if isinstance(raw, list) and len(raw) >= 2 and raw[0] is None and isinstance(raw[1], dict):
            result.ok = False
            result.message = str(raw[1].get("message") or raw[1])
            return result

        if isinstance(raw, list) and raw and isinstance(raw[0], list):
            row = raw[0]
        elif isinstance(raw, list):
            row = raw
        else:
            result.ok = False
            result.message = f"Unexpected result shape: {type(raw).__name__}"
            return result

        success = row[0] if len(row) > 0 else 0
        response_time = row[1] if len(row) > 1 else None
        message = row[2] if len(row) > 2 else ""
        status_code = row[3] if len(row) > 3 else None
        ip = row[4] if len(row) > 4 else None

        result.response_time = float(response_time) if response_time is not None else None
        result.message = str(message or "")
        result.status_code = parse_status_code(status_code)
        result.ip = str(ip) if ip is not None else None
        result.ok = bool(success) and result.status_code is not None and 200 <= result.status_code < 400
        result.slow = bool(result.ok and result.response_time is not None and result.response_time > SLOW_THRESHOLD_SECONDS)
        return result
    except Exception as exc:
        result.ok = False
        result.parse_error = f"{type(exc).__name__}: {exc}"
        return result


async def fetch_checkhost_results(request_id: str, selected_nodes: dict[str, NodeMeta]) -> CheckSnapshot:
    snapshot = CheckSnapshot(request_id=request_id)
    # Poll a few times because Check-Host results arrive gradually.
    result_data: dict[str, Any] = {}
    for attempt in range(8):
        await asyncio.sleep(2 if attempt < 4 else 4)
        data = await get_json(CHECKHOST_RESULT_URL.format(request_id=request_id), timeout=30)
        if isinstance(data, dict):
            result_data = data
        completed = 0
        total = len(selected_nodes) or len(result_data)
        for node in selected_nodes or result_data:
            value = result_data.get(node) if isinstance(result_data, dict) else None
            if value is not None:
                completed += 1
        if total and completed >= max(1, int(total * 0.9)):
            break

    for node, meta in selected_nodes.items():
        raw = result_data.get(node)
        snapshot.results.append(parse_http_result(node, raw, meta))

    # Include any surprise nodes missing from start response.
    for node, raw in result_data.items():
        if node in selected_nodes:
            continue
        meta = node_cache.get(node) or NodeMeta(node=node, location=node, region="global")
        snapshot.results.append(parse_http_result(node, raw, meta))

    return snapshot


async def run_check() -> CheckSnapshot:
    try:
        await load_checkhost_nodes()
        request_id, selected_nodes = await start_checkhost_check()
        snapshot = await fetch_checkhost_results(request_id, selected_nodes)
        return snapshot
    except Exception as exc:
        log.exception("Check failed")
        return CheckSnapshot(error=f"{type(exc).__name__}: {exc}")


def format_result_line(result: NodeResult) -> str:
    status = result.status_code if result.status_code is not None else "n/a"
    elapsed = f"{result.response_time:.2f}s" if result.response_time is not None else "n/a"
    msg = result.message or result.reason
    return f"{result.location}: {result.reason.upper()} / {elapsed} / {status} / {msg}"


def chunk_text(lines: list[str], *, limit: int = 1800) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = line[:limit]
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [""]


def get_guild_config(guild_id: int) -> dict[str, Any]:
    guilds = store.data.setdefault("guilds", {})
    return guilds.setdefault(str(guild_id), {})


def get_configured_channel_id(guild_id: int, key: str) -> Optional[int]:
    cfg = get_guild_config(guild_id)
    channels = cfg.get("channels", {})
    value = channels.get(key)
    return int(value) if value else None


def get_configured_role_id(guild_id: int, region: str) -> Optional[int]:
    cfg = get_guild_config(guild_id)
    roles = cfg.get("roles", {})
    value = roles.get(region)
    return int(value) if value else None


async def edit_setup_status(interaction: discord.Interaction, text: str) -> None:
    try:
        await interaction.followup.send(text, ephemeral=True)
    except discord.HTTPException:
        try:
            await interaction.edit_original_response(content=text)
        except Exception:
            pass


async def ensure_role(guild: discord.Guild, region: str) -> discord.Role:
    role_name = REGIONS[region]["role"]
    existing_id = get_configured_role_id(guild.id, region)
    role = guild.get_role(existing_id) if existing_id else None
    if role is None:
        role = discord.utils.get(guild.roles, name=role_name)
    me = guild.me
    if role is not None:
        if me and role >= me.top_role:
            raise RuntimeError(
                f"Role hierarchy problem: existing role '{role.name}' is not below the bot's highest role. "
                "Move the bot role above all Watch roles."
            )
        if not role.mentionable:
            role = await asyncio.wait_for(
                role.edit(mentionable=True, reason="Wikidot alert setup"), timeout=20
            )
        return role
    return await asyncio.wait_for(
        guild.create_role(name=role_name, mentionable=True, reason="Wikidot alert setup"),
        timeout=20,
    )



def bot_channel_overwrite() -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        add_reactions=True,
        manage_messages=False,
        create_public_threads=True,
        create_private_threads=True,
        send_messages_in_threads=True,
    )


def selector_everyone_overwrite() -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(
        view_channel=True,
        send_messages=False,
        read_message_history=True,
        add_reactions=True,
        create_public_threads=False,
        create_private_threads=False,
        send_messages_in_threads=False,
    )


def private_everyone_overwrite() -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(
        view_channel=False,
        send_messages=False,
        read_message_history=False,
    )


def watch_role_overwrite(*, can_send: bool = False) -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(
        view_channel=True,
        send_messages=can_send,
        read_message_history=True,
        add_reactions=False,
        create_public_threads=False,
        create_private_threads=False,
        send_messages_in_threads=False,
    )


async def apply_selector_permissions(channel: discord.TextChannel) -> None:
    guild = channel.guild
    await asyncio.wait_for(channel.set_permissions(guild.default_role, overwrite=selector_everyone_overwrite()), timeout=20)
    if guild.me:
        await asyncio.wait_for(channel.set_permissions(guild.me, overwrite=bot_channel_overwrite()), timeout=20)


async def apply_alert_channel_permissions(
    channel: discord.TextChannel,
    *,
    region_role: discord.Role,
    global_role: Optional[discord.Role],
) -> None:
    guild = channel.guild
    await asyncio.wait_for(channel.set_permissions(guild.default_role, overwrite=private_everyone_overwrite()), timeout=20)
    if guild.me:
        await asyncio.wait_for(channel.set_permissions(guild.me, overwrite=bot_channel_overwrite()), timeout=20)
    await asyncio.wait_for(channel.set_permissions(region_role, overwrite=watch_role_overwrite()), timeout=20)
    if global_role and global_role != region_role:
        await asyncio.wait_for(channel.set_permissions(global_role, overwrite=watch_role_overwrite()), timeout=20)


def selector_channel_overwrites(guild: discord.Guild) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: selector_everyone_overwrite(),
    }
    if guild.me:
        overwrites[guild.me] = bot_channel_overwrite()
    return overwrites


def alert_channel_overwrites(
    guild: discord.Guild,
    *,
    region_role: discord.Role,
    global_role: Optional[discord.Role],
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: private_everyone_overwrite(),
        region_role: watch_role_overwrite(),
    }
    if guild.me:
        overwrites[guild.me] = bot_channel_overwrite()
    if global_role and global_role != region_role:
        overwrites[global_role] = watch_role_overwrite()
    return overwrites


async def find_required_category(guild: discord.Guild, *, name: str) -> discord.CategoryChannel:
    category = discord.utils.get(guild.categories, name=name)
    if not isinstance(category, discord.CategoryChannel):
        raise RuntimeError(
            f"Required category '{name}' does not exist. Create it manually, make it visible to the bot, then rerun /setupalerts."
        )
    return category


async def find_required_text_channel(guild: discord.Guild, *, name: str) -> discord.TextChannel:
    channel = discord.utils.get(guild.text_channels, name=name)
    if not isinstance(channel, discord.TextChannel):
        raise RuntimeError(
            f"Required channel #{name} does not exist. Create it manually, make it visible to the bot, then rerun /setupalerts."
        )
    return channel


def build_selector_message() -> str:
    lines = [
        "**Wikidot Alert Subscriptions**",
        "",
        "React to add or remove a regional watch role.",
        "",
    ]
    for region, data in REGIONS.items():
        lines.append(f"{data['emoji']} — **{data['label']}**")
    return "\n".join(lines)


async def ensure_selector_message(channel: discord.TextChannel, guild_id: int) -> discord.Message:
    cfg = get_guild_config(guild_id)
    message_id = cfg.get("selector_message_id")
    message: Optional[discord.Message] = None
    if message_id:
        try:
            message = await channel.fetch_message(int(message_id))
        except discord.NotFound:
            message = None
        except discord.Forbidden as exc:
            raise RuntimeError(f"Bot cannot fetch messages in #{channel.name}: {exc}") from exc

    content = build_selector_message()
    if message is None:
        message = await asyncio.wait_for(channel.send(content), timeout=20)
    else:
        await asyncio.wait_for(message.edit(content=content), timeout=20)

    for region in REGIONS.values():
        emoji = region["emoji"]
        try:
            await asyncio.wait_for(message.add_reaction(emoji), timeout=10)
        except discord.HTTPException:
            # Usually already present or emoji issue; continue with other emojis.
            log.warning("Could not add reaction %s to selector message", emoji)
    return message


async def ensure_setup(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        raise RuntimeError("This command must be run in a server.")

    cfg = get_guild_config(guild.id)
    cfg.setdefault("roles", {})
    cfg.setdefault("channels", {})

    await interaction.edit_original_response(content="Setup: creating/updating watch roles...")
    roles: dict[str, discord.Role] = {}
    for region in REGIONS:
        try:
            roles[region] = await ensure_role(guild, region)
        except discord.Forbidden as exc:
            raise RuntimeError(f"Discord denied permission while creating/updating role '{REGIONS[region]['role']}': {exc}") from exc
        cfg["roles"][region] = str(roles[region].id)

    await interaction.edit_original_response(content=f"Setup: finding existing category '{ALERT_CATEGORY_NAME}'...")
    category = await find_required_category(guild, name=ALERT_CATEGORY_NAME)
    cfg["category_id"] = str(category.id)

    await interaction.edit_original_response(content="Setup: finding existing #watch-roles...")
    selector_channel = await find_required_text_channel(guild, name=WATCH_ROLE_CHANNEL_NAME)
    cfg["channels"]["selector"] = str(selector_channel.id)

    # Do not manage #watch-roles permissions. This channel is intentionally managed manually
    # because some servers apply category/channel overwrites that block bot permission edits
    # unless Administrator is granted. The bot only needs to read/send/react here.
    await interaction.edit_original_response(content="Setup: using existing #watch-roles permissions...")

    await interaction.edit_original_response(content="Setup: finding existing regional alert channels...")
    global_role = roles.get("global")
    for region, data in REGIONS.items():
        channel = await find_required_text_channel(guild, name=data["channel"])
        cfg["channels"][region] = str(channel.id)

        if AUTO_MANAGE_ALERT_CHANNEL_PERMISSIONS:
            await interaction.edit_original_response(content=f"Setup: applying permissions for #{channel.name}...")
            try:
                await apply_alert_channel_permissions(
                    channel,
                    region_role=roles[region],
                    global_role=global_role,
                )
            except discord.Forbidden as exc:
                raise RuntimeError(
                    f"Discord denied permission while setting #{channel.name} permissions: {exc}. "
                    "Make the channel visible to the bot first, then rerun /setupalerts."
                ) from exc

    await interaction.edit_original_response(content="Setup: creating/updating reaction-role message...")
    try:
        selector_message = await ensure_selector_message(selector_channel, guild.id)
    except discord.Forbidden as exc:
        raise RuntimeError(f"Discord denied permission while creating/updating reaction-role message: {exc}") from exc
    cfg["selector_message_id"] = str(selector_message.id)

    store.save()
    channel_mentions = []
    for r in REGIONS:
        ch = guild.get_channel(int(cfg["channels"][r]))
        if ch:
            channel_mentions.append(ch.mention)
    await interaction.edit_original_response(
        content=(
            "Setup complete.\n"
            f"Selector: {selector_channel.mention}\n"
            "Alert channels: " + ", ".join(channel_mentions)
        )
    )

def get_member_watch_regions(member: discord.Member) -> list[str]:
    role_names = {role.name for role in member.roles}
    regions = [region for region, data in REGIONS.items() if data["role"] in role_names]
    return regions


async def add_watch_role(member: discord.Member, region: str) -> None:
    guild = member.guild
    role_id = get_configured_role_id(guild.id, region)
    role = guild.get_role(role_id) if role_id else discord.utils.get(guild.roles, name=REGIONS[region]["role"])
    if role is None:
        raise RuntimeError("Watch roles are not configured. Ask an admin to run /setupalerts.")
    await member.add_roles(role, reason="Wikidot watch role subscription")


async def remove_watch_role(member: discord.Member, region: str) -> None:
    guild = member.guild
    role_id = get_configured_role_id(guild.id, region)
    role = guild.get_role(role_id) if role_id else discord.utils.get(guild.roles, name=REGIONS[region]["role"])
    if role is None:
        return
    await member.remove_roles(role, reason="Wikidot watch role unsubscribe")


def result_state_label(result: NodeResult) -> str:
    if result.ok and not result.slow:
        return "OK"
    if result.ok and result.slow:
        return "SLOW"
    return "DOWN"


def node_streak_key(result: NodeResult) -> str:
    return result.node or result.location


def display_location_short(result: NodeResult) -> str:
    # User-facing status should read like "Miami" rather than "Miami, United States".
    if "," in result.location:
        return result.location.split(",", 1)[0].strip() or result.location
    return result.location


def update_node_failure_streaks(snapshot: CheckSnapshot) -> None:
    active_keys: set[str] = set()
    for result in snapshot.monitored_results:
        key = node_streak_key(result)
        active_keys.add(key)
        if result.bad:
            node_failure_streaks[key] = node_failure_streaks.get(key, 0) + 1
        else:
            node_failure_streaks[key] = 0
    for key in list(node_failure_streaks):
        if key not in active_keys:
            node_failure_streaks.pop(key, None)


def watched_node_status_for_regions(regions: list[str]) -> str:
    if latest_snapshot.error:
        return f"Latest check error: {latest_snapshot.error}"
    if "global" in regions:
        regions = ["north-america", "europe", "asia-pacific", "middle-east", "south-america"]
    lines = [
        f"Latest check: <t:{int(latest_snapshot.checked_at.timestamp())}:R>",
        "",
    ]
    by_region: dict[str, list[NodeResult]] = {region: [] for region in REGIONS}
    for result in latest_snapshot.monitored_results:
        by_region.setdefault(result.region, []).append(result)

    for region in regions:
        data = REGIONS[region]
        results = sorted(by_region.get(region, []), key=lambda r: (display_location_short(r), r.node))
        lines.append(f"**{data['label']}:**")
        if not results:
            lines.append("No current node data for this region.")
            lines.append("")
            continue
        for result in results:
            elapsed = f"{result.response_time:.2f} s" if result.response_time is not None else "n/a"
            streak = node_failure_streaks.get(node_streak_key(result), 0)
            lines.append(
                f"{display_location_short(result)}: {result_state_label(result)} | {elapsed} | "
                f"Failed checks in a row: {streak}"
            )
        lines.append("")
    return "\n".join(lines).strip()


def status_for_regions(regions: list[str]) -> str:
    if latest_snapshot.error:
        return f"Latest check error: {latest_snapshot.error}"
    lines = [
        f"Latest check: <t:{int(latest_snapshot.checked_at.timestamp())}:R>",
        f"Monitored nodes: {len(latest_snapshot.monitored_results)}",
        "",
    ]
    bad_by_region = latest_snapshot.bad_by_region()
    for region in regions:
        data = REGIONS[region]
        bad = bad_by_region.get(region, [])
        if not bad:
            lines.append(f"{data['emoji']} **{data['label']}**: healthy")
        else:
            lines.append(f"{data['emoji']} **{data['label']}**: {len(bad)} bad/slow node(s)")
            for result in bad[:8]:
                lines.append(f"  - {format_result_line(result)}")
            if len(bad) > 8:
                lines.append(f"  - ...and {len(bad) - 8} more")
    return "\n".join(lines)


async def get_alert_channel(guild: discord.Guild, region: str) -> Optional[discord.TextChannel]:
    channel_id = get_configured_channel_id(guild.id, region)
    channel = guild.get_channel(channel_id) if channel_id else discord.utils.get(guild.text_channels, name=REGIONS[region]["channel"])
    return channel if isinstance(channel, discord.TextChannel) else None


async def send_region_alert(guild: discord.Guild, region: str, bad_results: list[NodeResult], *, recovered: bool = False) -> None:
    channel = await get_alert_channel(guild, region)
    if channel is None:
        log.warning("No alert channel configured for region %s in guild %s", region, guild.id)
        return
    role_id = get_configured_role_id(guild.id, region)
    role = guild.get_role(role_id) if role_id else discord.utils.get(guild.roles, name=REGIONS[region]["role"])
    mention = role.mention if role else f"@{REGIONS[region]['role']}"
    allowed = discord.AllowedMentions(roles=True, users=False, everyone=False)

    if recovered:
        embed = discord.Embed(
            title="WE ARE SO BACK",
            description="WikiDown has re-established connection in your location!",
        )
        await channel.send(content=mention, embed=embed, allowed_mentions=allowed, silent=True)
        return

    has_down = any(not r.ok for r in bad_results)
    phrase = "been unable to reach" if has_down else "had struggle connecting to"
    locations = []
    seen = set()
    for result in bad_results:
        loc = result.location
        if loc not in seen:
            seen.add(loc)
            locations.append(loc)
    if not locations:
        locations = [REGIONS[region]["label"]]
    bullet_list = "\n".join(f"- {loc}" for loc in locations[:25])
    if len(locations) > 25:
        bullet_list += f"\n- ...and {len(locations) - 25} more"
    embed = discord.Embed(
        title="IT'S SO OVER",
        description=(
            f"WikiDown has recently {phrase} the wiki at the following locations. "
            "Users in this area may experience difficulty accessing the site:\n"
            f"{bullet_list}"
        ),
    )

    file: Optional[discord.File] = None
    if ALERT_IMAGE_FILE.exists():
        file = discord.File(str(ALERT_IMAGE_FILE), filename="image.png")
        embed.set_image(url="attachment://image.png")
    else:
        log.warning("Alert image file not found: %s", ALERT_IMAGE_FILE)

    if file:
        await channel.send(content=mention, embed=embed, file=file, allowed_mentions=allowed, silent=True)
    else:
        await channel.send(content=mention, embed=embed, allowed_mentions=allowed, silent=True)


async def evaluate_alerts(snapshot: CheckSnapshot) -> None:
    if snapshot.error:
        return
    bad_by_region = snapshot.bad_by_region()
    guilds = list(bot.guilds)
    if not guilds:
        return

    for region in REGIONS:
        if region == "global":
            # Global is treated separately: if two or more non-global regions are currently bad,
            # send global incident notifications.
            continue
        state = region_states.setdefault(region, RegionState())
        bad_results = bad_by_region.get(region, [])
        if bad_results:
            state.failing_streak += 1
            state.recovery_streak = 0
            if not state.in_incident and state.failing_streak >= FAIL_THRESHOLD:
                state.in_incident = True
                state.last_alert_at = datetime.now(timezone.utc)
                for guild in guilds:
                    await send_region_alert(guild, region, bad_results, recovered=False)
        else:
            state.failing_streak = 0
            if state.in_incident:
                state.recovery_streak += 1
                if state.recovery_streak >= RECOVERY_THRESHOLD:
                    state.in_incident = False
                    state.recovery_streak = 0
                    for guild in guilds:
                        await send_region_alert(guild, region, [], recovered=True)

    # Global incident if multiple regions are in incident simultaneously.
    incident_regions = [r for r in REGIONS if r != "global" and region_states.get(r, RegionState()).in_incident]
    global_state = region_states.setdefault("global", RegionState())
    if len(incident_regions) >= 2:
        global_state.failing_streak += 1
        global_state.recovery_streak = 0
        if not global_state.in_incident:
            global_state.in_incident = True
            global_bad: list[NodeResult] = []
            for r in incident_regions:
                global_bad.extend(bad_by_region.get(r, []))
            for guild in guilds:
                await send_region_alert(guild, "global", global_bad, recovered=False)
    else:
        global_state.failing_streak = 0
        if global_state.in_incident:
            global_state.recovery_streak += 1
            if global_state.recovery_streak >= RECOVERY_THRESHOLD:
                global_state.in_incident = False
                global_state.recovery_streak = 0
                for guild in guilds:
                    await send_region_alert(guild, "global", [], recovered=True)


@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def monitor_loop() -> None:
    global latest_snapshot
    snapshot = await run_check()
    latest_snapshot = snapshot
    if snapshot.error:
        log.warning("[CHECK ERROR] %s", snapshot.error)
        return
    update_node_failure_streaks(snapshot)
    log.info(
        "[%s] %s/%s healthy; %s bad/slow; filtered=%s",
        "OK" if snapshot.bad_count == 0 else "FAIL",
        snapshot.healthy_count,
        len(snapshot.monitored_results),
        snapshot.bad_count,
        snapshot.ignored_count,
    )
    await evaluate_alerts(snapshot)


@monitor_loop.before_loop
async def before_monitor() -> None:
    await bot.wait_until_ready()


@bot.event
async def on_ready() -> None:
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession()

    if GUILD_ID:
        guild_obj = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild_obj)
        synced = await bot.tree.sync(guild=guild_obj)
        sync_target = f"guild {GUILD_ID}"
    else:
        synced = await bot.tree.sync()
        sync_target = "global"

    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")
    log.info("Synced %s slash command(s) to %s: %s", len(synced), sync_target, ", ".join(cmd.name for cmd in synced))
    if not monitor_loop.is_running():
        monitor_loop.start()


@bot.event
async def on_disconnect() -> None:
    log.warning("Bot disconnected")


async def close_session() -> None:
    global http_session
    if http_session and not http_session.closed:
        await http_session.close()


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    if payload.guild_id is None or payload.user_id == (bot.user.id if bot.user else None):
        return
    cfg = get_guild_config(payload.guild_id)
    if str(payload.message_id) != str(cfg.get("selector_message_id")):
        return
    region = EMOJI_TO_REGION.get(str(payload.emoji))
    if not region:
        return
    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    member = guild.get_member(payload.user_id)
    if member is None:
        try:
            member = await guild.fetch_member(payload.user_id)
        except discord.HTTPException:
            return
    try:
        await add_watch_role(member, region)
    except Exception as exc:
        log.warning("Could not add watch role: %s", exc)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent) -> None:
    if payload.guild_id is None:
        return
    cfg = get_guild_config(payload.guild_id)
    if str(payload.message_id) != str(cfg.get("selector_message_id")):
        return
    region = EMOJI_TO_REGION.get(str(payload.emoji))
    if not region:
        return
    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    member = guild.get_member(payload.user_id)
    if member is None:
        try:
            member = await guild.fetch_member(payload.user_id)
        except discord.HTTPException:
            return
    try:
        await remove_watch_role(member, region)
    except Exception as exc:
        log.warning("Could not remove watch role: %s", exc)


@bot.tree.command(name="setupalerts", description="Configure existing alert category/channels, roles, permissions, and reactions.")
@app_commands.default_permissions(administrator=True)
async def setupalerts(interaction: discord.Interaction) -> None:
    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only administrators can run this command.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        await ensure_setup(interaction)
    except Exception as exc:
        log.exception("Setup failed")
        await interaction.edit_original_response(content=f"Setup failed: {type(exc).__name__}: {exc}")


@bot.tree.command(name="mywatches", description="Show node-level status for your current watch roles.")
async def mywatches(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
        return
    regions = get_member_watch_regions(interaction.user)
    if not regions:
        await interaction.response.send_message("You do not currently have any watch roles. Use the reaction menu in #watch-roles.", ephemeral=True)
        return
    text = watched_node_status_for_regions(regions)
    chunks = chunk_text(text.splitlines())
    await interaction.response.send_message(chunks[0], ephemeral=True)
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=True)

@bot.tree.command(name="status", description="Show the latest node-level Wikidot monitor status.")
async def status(interaction: discord.Interaction) -> None:
    if latest_snapshot.error:
        await interaction.response.send_message(f"Latest check error: {latest_snapshot.error}", ephemeral=True)
        return

    lines = [
        f"Latest check: <t:{int(latest_snapshot.checked_at.timestamp())}:R>",
        f"Target: `{CHECK_URL}`",
        f"Healthy: {latest_snapshot.healthy_count}/{len(latest_snapshot.monitored_results)} monitored nodes",
        f"Bad/slow: {latest_snapshot.bad_count}",
        "",
        "**Accessible Check-Host nodes**",
    ]

    region_order = ["north-america", "europe", "asia-pacific", "middle-east", "south-america", "global"]
    by_region: dict[str, list[NodeResult]] = {region: [] for region in REGIONS}
    for result in sorted(latest_snapshot.monitored_results, key=lambda r: (REGIONS.get(r.region, {}).get("label", r.region), r.location, r.node)):
        by_region.setdefault(result.region, []).append(result)

    for region in region_order:
        results = by_region.get(region, [])
        if not results:
            continue
        data = REGIONS.get(region, {"label": region, "emoji": ""})
        lines.append("")
        lines.append(f"{data['emoji']} **{data['label']}**")
        for result in results:
            status = result.status_code if result.status_code is not None else "n/a"
            elapsed = f"{result.response_time:.2f}s" if result.response_time is not None else "n/a"
            state = result_state_label(result)
            msg = result.message or "n/a"
            streak = node_failure_streaks.get(node_streak_key(result), 0)
            lines.append(f"- **{result.location}**: {state}; status={status}; time={elapsed}; failed checks={streak}; msg={msg}")

    chunks = chunk_text(lines)
    await interaction.response.send_message(chunks[0], ephemeral=True)
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=True)


@bot.tree.command(name="forcecheck", description="Run a Check-Host check now.")
@app_commands.default_permissions(manage_guild=True)
async def forcecheck(interaction: discord.Interaction) -> None:
    global latest_snapshot
    await interaction.response.defer(ephemeral=True)
    latest_snapshot = await run_check()
    if latest_snapshot.error:
        await interaction.edit_original_response(content=f"Check failed: {latest_snapshot.error}")
        return
    update_node_failure_streaks(latest_snapshot)
    await evaluate_alerts(latest_snapshot)
    await interaction.edit_original_response(
        content=(
            f"Check complete. Healthy: {latest_snapshot.healthy_count}/{len(latest_snapshot.monitored_results)}; "
            f"bad/slow: {latest_snapshot.bad_count}."
        )
    )


@bot.tree.command(name="forcefail", description="Send a test outage/slow/recovery alert to configured alert channel(s).")
@app_commands.choices(region=region_choices())
@app_commands.choices(kind=[
    app_commands.Choice(name="Down", value="down"),
    app_commands.Choice(name="Slow", value="slow"),
    app_commands.Choice(name="Recovered", value="recovered"),
])
async def forcefail(
    interaction: discord.Interaction,
    region: Optional[app_commands.Choice[str]] = None,
    kind: Optional[app_commands.Choice[str]] = None,
) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
        return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only administrators can run this command.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    selected_kind = kind.value if kind else "down"
    selected_regions = [region.value] if region else [r for r in REGIONS if r != "global"]
    sent = 0
    for region_key in selected_regions:
        if selected_kind == "recovered":
            await send_region_alert(interaction.guild, region_key, [], recovered=True)
        else:
            fake = NodeResult(
                node="forcefail-test",
                location=f"Test Location, {REGIONS[region_key]['label']}",
                region=region_key,
                ok=(selected_kind == "slow"),
                slow=(selected_kind == "slow"),
                response_time=SLOW_THRESHOLD_SECONDS + 1.0 if selected_kind == "slow" else None,
                status_code=200 if selected_kind == "slow" else None,
                message="Forced test alert",
            )
            await send_region_alert(interaction.guild, region_key, [fake], recovered=False)
        sent += 1
    await interaction.edit_original_response(content=f"Sent {selected_kind} test alert to {sent} channel(s).")


@bot.tree.command(name="debugcheck", description="Show parser/debug summary for the latest check.")
@app_commands.default_permissions(manage_guild=True)
async def debugcheck(interaction: discord.Interaction) -> None:
    if latest_snapshot.error:
        await interaction.response.send_message(f"Latest check error: {latest_snapshot.error}", ephemeral=True)
        return
    parse_errors = [r for r in latest_snapshot.results if r.parse_error]
    slow = [r for r in latest_snapshot.monitored_results if r.slow]
    down = [r for r in latest_snapshot.monitored_results if not r.ok]
    await interaction.response.send_message(
        f"Nodes: {len(latest_snapshot.monitored_results)}; healthy: {latest_snapshot.healthy_count}; "
        f"slow: {len(slow)}; down: {len(down)}; parse errors: {len(parse_errors)}",
        ephemeral=True,
    )


@bot.tree.command(name="debugfailures", description="List current bad/slow monitored nodes.")
@app_commands.default_permissions(manage_guild=True)
async def debugfailures(interaction: discord.Interaction) -> None:
    if latest_snapshot.error:
        await interaction.response.send_message(f"Latest check error: {latest_snapshot.error}", ephemeral=True)
        return
    bad = [r for r in latest_snapshot.monitored_results if r.bad]
    if not bad:
        await interaction.response.send_message(
            f"Bad/slow nodes: 0/{len(latest_snapshot.monitored_results)} monitored",
            ephemeral=True,
        )
        return
    lines = [f"Bad/slow nodes: {len(bad)}/{len(latest_snapshot.monitored_results)} monitored", ""]
    lines.extend(format_result_line(r) for r in bad)
    chunks = chunk_text(lines)
    await interaction.response.send_message(chunks[0], ephemeral=True)
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=True)




@bot.tree.command(name="alertchannels", description="Show configured alert channels and watch roles.")
@app_commands.default_permissions(manage_guild=True)
async def alertchannels(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
        return
    cfg = get_guild_config(interaction.guild.id)
    lines = ["Configured Wikidot alert resources:", ""]
    selector_id = cfg.get("channels", {}).get("selector")
    selector = interaction.guild.get_channel(int(selector_id)) if selector_id else None
    lines.append(f"Selector: {selector.mention if selector else 'not configured'}")
    for region, data in REGIONS.items():
        role_id = cfg.get("roles", {}).get(region)
        channel_id = cfg.get("channels", {}).get(region)
        role = interaction.guild.get_role(int(role_id)) if role_id else None
        channel = interaction.guild.get_channel(int(channel_id)) if channel_id else None
        lines.append(f"{data['emoji']} {data['label']}: role={role.mention if role else 'missing'} channel={channel.mention if channel else 'missing'}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="locations", description="Show current monitored Check-Host locations by region.")
async def locations(interaction: discord.Interaction) -> None:
    if latest_snapshot.error or not latest_snapshot.results:
        await interaction.response.send_message("No location data available yet.", ephemeral=True)
        return
    grouped: dict[str, set[str]] = {r: set() for r in REGIONS}
    for result in latest_snapshot.monitored_results:
        grouped.setdefault(result.region, set()).add(result.location)
    lines = ["Current monitored locations:", ""]
    for region, data in REGIONS.items():
        if region == "global":
            continue
        locations = sorted(grouped.get(region, set()))
        lines.append(f"**{data['label']}** ({len(locations)})")
        lines.extend(f"- {loc}" for loc in locations[:20])
        if len(locations) > 20:
            lines.append(f"- ...and {len(locations) - 20} more")
        lines.append("")
    chunks = chunk_text(lines)
    await interaction.response.send_message(chunks[0], ephemeral=True)
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=True)


async def main() -> None:
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set")
    try:
        async with bot:
            await bot.start(DISCORD_TOKEN)
    finally:
        await close_session()


if __name__ == "__main__":
    asyncio.run(main())
