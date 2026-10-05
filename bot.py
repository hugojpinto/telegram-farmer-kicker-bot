"""Telegram bot that removes new members whose bio contains a Telegram link.

Add the bot as an administrator (with "Ban users" / "Add members" and "Delete
messages" rights) to a group or channel. It watches for new members and join requests, fetches each
user's bio and removes anyone whose bio links to Telegram. Members who joined
recently are checked again a few minutes after joining, whenever they send a
message, and in a sweep of all recent members that runs while the chat is
active. That catches people who add the link after joining.
"""

import asyncio
import logging
import os
import re
import sqlite3
import time

from dotenv import load_dotenv
from telegram import ChatMember, ChatMemberUpdated, Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("kicker")

BOT_TOKEN = os.environ["BOT_TOKEN"]
# "kick" removes the user but lets them rejoin later; "ban" blocks them for good.
ACTION = os.getenv("ACTION", "kick").lower()
# Also treat bare @username mentions in the bio as Telegram links.
MATCH_MENTIONS = os.getenv("MATCH_MENTIONS", "false").lower() == "true"
# Members who joined less than this many days ago get their bio re-checked when they post.
NEW_MEMBER_DAYS = float(os.getenv("NEW_MEMBER_DAYS", "14"))
DB_PATH = os.getenv("DB_PATH", "members.db")
# Bios are cached briefly so a chatty user doesn't trigger a getChat call on every message.
BIO_CACHE_SECONDS = 300
# New members get a second bio check this long after joining.
JOIN_RECHECK_SECONDS = 360
# While a chat is active, all of its recent members are re-checked at most this often.
SWEEP_INTERVAL_SECONDS = 600

LINK_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/\S+"
    r"|tg://\S+",
    re.IGNORECASE,
)
MENTION_PATTERN = re.compile(r"(?<!\w)@[a-zA-Z][\w]{3,31}")


def has_telegram_link(bio: str | None) -> bool:
    if not bio:
        return False
    if LINK_PATTERN.search(bio):
        return True
    return MATCH_MENTIONS and bool(MENTION_PATTERN.search(bio))


# Telegram doesn't expose when someone joined a chat, so the bot records it itself.
db = sqlite3.connect(DB_PATH)
db.execute(
    "CREATE TABLE IF NOT EXISTS joins ("
    "chat_id INTEGER, user_id INTEGER, joined_at REAL, PRIMARY KEY (chat_id, user_id))"
)
db.commit()


def record_join(chat_id: int, user_id: int) -> None:
    db.execute("INSERT OR REPLACE INTO joins VALUES (?, ?, ?)", (chat_id, user_id, time.time()))
    db.commit()


def forget_join(chat_id: int, user_id: int) -> None:
    db.execute("DELETE FROM joins WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    db.commit()


def is_new_member(chat_id: int, user_id: int) -> bool:
    row = db.execute(
        "SELECT joined_at FROM joins WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
    ).fetchone()
    return row is not None and time.time() - row[0] < NEW_MEMBER_DAYS * 86400


def recent_members(chat_id: int) -> list[int]:
    rows = db.execute(
        "SELECT user_id FROM joins WHERE chat_id = ? AND joined_at >= ?",
        (chat_id, time.time() - NEW_MEMBER_DAYS * 86400),
    ).fetchall()
    return [row[0] for row in rows]


def prune_old_joins() -> None:
    db.execute("DELETE FROM joins WHERE joined_at < ?", (time.time() - NEW_MEMBER_DAYS * 86400,))
    db.commit()


bio_cache: dict[int, tuple[float, str | None]] = {}
# Discussion group id -> id of the channel it's linked to (None if it isn't linked).
linked_channels: dict[int, int | None] = {}
# Chat id -> when its recent members were last swept.
last_sweep: dict[int, float] = {}


async def linked_channel(context: ContextTypes.DEFAULT_TYPE, group_id: int) -> int | None:
    if group_id not in linked_channels:
        try:
            linked_channels[group_id] = (await context.bot.get_chat(group_id)).linked_chat_id
        except TelegramError as e:
            log.warning("Could not look up linked channel for %s: %s", group_id, e)
            return None
    return linked_channels[group_id]


async def fetch_bio(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, use_cache: bool = False
) -> str | None:
    if use_cache and (cached := bio_cache.get(user_id)) and time.time() - cached[0] < BIO_CACHE_SECONDS:
        return cached[1]
    try:
        bio = (await context.bot.get_chat(user_id)).bio
    except TelegramError as e:
        log.warning("Could not fetch bio for user %s: %s", user_id, e)
        return None
    bio_cache[user_id] = (time.time(), bio)
    return bio


async def remove_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> None:
    await context.bot.ban_chat_member(chat_id, user_id)
    if ACTION == "kick":
        # Unbanning right away turns the ban into a kick, so the user can rejoin later.
        await context.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
    forget_join(chat_id, user_id)


def in_chat(member: ChatMember) -> bool:
    return member.status in (ChatMember.MEMBER, ChatMember.OWNER, ChatMember.ADMINISTRATOR) or (
        member.status == ChatMember.RESTRICTED and member.is_member
    )


def joined(update: ChatMemberUpdated) -> bool:
    return not in_chat(update.old_chat_member) and in_chat(update.new_chat_member)


def left(update: ChatMemberUpdated) -> bool:
    return in_chat(update.old_chat_member) and not in_chat(update.new_chat_member)


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cmu = update.chat_member
    chat = cmu.chat
    user = cmu.new_chat_member.user
    if left(cmu):
        forget_join(chat.id, user.id)
        return
    if not joined(cmu) or user.is_bot:
        return

    bio = await fetch_bio(context, user.id)
    log.info("%s (%s) joined %r (%s). Bio: %r", user.full_name, user.id, chat.title, chat.id, bio)
    if not has_telegram_link(bio):
        record_join(chat.id, user.id)
        context.job_queue.run_once(
            recheck_join, JOIN_RECHECK_SECONDS, data=(chat.id, chat.title, user.id, user.full_name)
        )
        return

    try:
        await remove_user(context, chat.id, user.id)
        log.info(
            "Removed %s (%s) from %r (%s). Bio: %r",
            user.full_name, user.id, chat.title, chat.id, bio,
        )
    except TelegramError as e:
        log.error("Failed to remove %s from %s: %s", user.id, chat.id, e)


async def recheck_join(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Checks a new member's bio a second time, shortly after they joined."""
    chat_id, chat_title, user_id, full_name = context.job.data
    if not is_new_member(chat_id, user_id):
        return
    bio = await fetch_bio(context, user_id)
    if not has_telegram_link(bio):
        return
    try:
        await remove_user(context, chat_id, user_id)
        log.info(
            "Removed %s (%s) from %r (%s) on the re-check after joining. Bio: %r",
            full_name, user_id, chat_title, chat_id, bio,
        )
    except TelegramError as e:
        log.error("Failed to remove %s from %s: %s", user_id, chat_id, e)


async def sweep_recent_members(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Re-checks the bio of everyone who joined the chat recently."""
    try:
        for user_id in recent_members(chat_id):
            bio = await fetch_bio(context, user_id, use_cache=True)
            if has_telegram_link(bio):
                try:
                    await remove_user(context, chat_id, user_id)
                    log.info("Removed recent member %s from %s in a sweep. Bio: %r", user_id, chat_id, bio)
                except TelegramError as e:
                    log.error("Failed to remove %s from %s: %s", user_id, chat_id, e)
            # Spread the getChat calls out to stay clear of Telegram's rate limits.
            await asyncio.sleep(0.1)
    finally:
        last_sweep[chat_id] = time.time()


def maybe_sweep(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    if time.time() - last_sweep.get(chat_id, 0) < SWEEP_INTERVAL_SECONDS:
        return
    last_sweep[chat_id] = time.time()
    # Runs in the background so a long sweep doesn't hold up other updates.
    context.application.create_task(sweep_recent_members(context, chat_id))


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-checks the bio of recently joined members when they post, in case they added a link after joining.

    Channel subscribers can only write as comments in the channel's discussion group,
    so for those the join date in the linked channel counts too. Any message also
    triggers a sweep of the other recent members, at most once per SWEEP_INTERVAL_SECONDS.
    """
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg:
        return
    chat_ids = [cid for cid in (chat.id, await linked_channel(context, chat.id)) if cid]
    for cid in chat_ids:
        maybe_sweep(context, cid)
    if not user or user.is_bot:
        return
    new_in = [cid for cid in chat_ids if is_new_member(cid, user.id)]
    if not new_in:
        return

    bio = await fetch_bio(context, user.id, use_cache=True)
    if not has_telegram_link(bio):
        return

    try:
        await msg.delete()
    except TelegramError as e:
        log.warning("Could not delete message %s in %s: %s", msg.message_id, chat.id, e)
    for chat_id in {chat.id, *new_in}:
        try:
            await remove_user(context, chat_id, user.id)
            log.info(
                "Removed recent member %s (%s) from %s after they posted in %r. Bio: %r",
                user.full_name, user.id, chat_id, chat.title, bio,
            )
        except TelegramError as e:
            log.error("Failed to remove %s from %s: %s", user.id, chat_id, e)


async def prune_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    prune_old_joins()
    now = time.time()
    for user_id, (fetched_at, _) in list(bio_cache.items()):
        if now - fetched_at >= BIO_CACHE_SECONDS:
            del bio_cache[user_id]


async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles chats that require admin approval. Links get declined, everyone else waits for a human."""
    req = update.chat_join_request
    bio = req.bio if req.bio is not None else await fetch_bio(context, req.from_user.id)
    if not has_telegram_link(bio):
        return
    try:
        await req.decline()
        log.info(
            "Declined join request from %s (%s) to %r. Bio: %r",
            req.from_user.full_name, req.from_user.id, req.chat.title, bio,
        )
    except TelegramError as e:
        log.error("Failed to decline join request from %s: %s", req.from_user.id, e)


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logs when the bot is added, promoted or removed, so setup problems are easy to spot."""
    cmu = update.my_chat_member
    status = cmu.new_chat_member.status
    log.info("Bot status in %r (%s, %s) is now: %s", cmu.chat.title, cmu.chat.id, cmu.chat.type, status)
    if status != ChatMember.ADMINISTRATOR and cmu.chat.type != ChatType.PRIVATE:
        log.warning("The bot must be an administrator in %r to see and remove new members.", cmu.chat.title)
    elif status == ChatMember.ADMINISTRATOR and not cmu.new_chat_member.can_delete_messages:
        log.warning("The bot needs the 'Delete messages' right in %r to remove offending posts.", cmu.chat.title)


def main() -> None:
    if ACTION not in ("kick", "ban"):
        raise SystemExit("ACTION must be 'kick' or 'ban'")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, on_message))
    app.job_queue.run_repeating(prune_job, interval=3600, first=10)

    log.info(
        "Starting bot (action=%s, match_mentions=%s, new_member_days=%s)",
        ACTION, MATCH_MENTIONS, NEW_MEMBER_DAYS,
    )
    # chat_member updates are only delivered when explicitly requested.
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
