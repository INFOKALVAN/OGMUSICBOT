"""Mass group administration commands for INDUMUSIC.

Commands:
    /kickall   - remove all non-admin/non-bot members
    /banall    - ban all non-admin/non-bot members
    /unbanall  - unban all currently banned members
    /muteall   - mute all non-admin/non-bot members
    /unmuteall - unmute all non-admin/non-bot members
    /unpinall  - unpin all messages

All destructive commands require the group owner/sudoer to confirm them.
"""

import asyncio
import logging
import re
from typing import Awaitable, Callable

from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus, ChatMembersFilter
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import CallbackQuery, ChatPermissions, Message

from INDUMUSIC import app
from INDUMUSIC.utils.colored_buttons import styled_button, send_message_colored
from INDUMUSIC.utils.permissions import is_owner_or_sudoer, mention

LOGGER = logging.getLogger(__name__)

MASS_CMDS = (
    "kickall",
    "banall",
    "unbanall",
    "muteall",
    "unmuteall",
    "unpinall",
)

_MASS_REGEX = rf"^({'|'.join(map(re.escape, MASS_CMDS))})_(yes|no)$"

# Telegram can return FloodWait for large groups.  Keep this deliberately
# conservative so the command does not stop halfway through a group.
REQUEST_DELAY = 0.15


def _confirmation_keyboard(cmd: str):
    return [
        [
            styled_button("Yes", callback_data=f"{cmd}_yes", style="success"),
            styled_button("No", callback_data=f"{cmd}_no", style="danger"),
        ]
    ]


def _is_protected_member(member) -> bool:
    """Return True for bots, the owner, and administrators.

    Telegram does not allow a bot to kick/ban/restrict administrators.
    Skipping them prevents a large number of avoidable RPC errors.
    """
    user = getattr(member, "user", None)
    if not user:
        return True
    if getattr(user, "is_bot", False):
        return True

    status = getattr(member, "status", None)
    return status in (
        ChatMemberStatus.OWNER,
        ChatMemberStatus.ADMINISTRATOR,
    )


async def _sleep_after_request() -> None:
    await asyncio.sleep(REQUEST_DELAY)


async def _run_with_flood_wait(operation: Callable[[], Awaitable[object]]):
    """Run a Telegram request and transparently retry FloodWait once.

    A second FloodWait is allowed to propagate so the caller can report the
    actual Telegram error instead of looping forever.
    """
    try:
        return await operation()
    except FloodWait as exc:
        wait_seconds = max(int(getattr(exc, "value", 1)), 1)
        LOGGER.warning("Telegram FloodWait: sleeping %s seconds", wait_seconds)
        await asyncio.sleep(wait_seconds)
        return await operation()


async def _send_result(client: Client, chat_id: int, action: str, success: int, errors: int):
    await client.send_message(
        chat_id,
        f"<b>{action}</b> completed.\n\n"
        f"✅ Success: <code>{success}</code>\n"
        f"❌ Failed: <code>{errors}</code>",
    )


@app.on_message(filters.command(list(MASS_CMDS)) & filters.group)
async def ask_mass_confirm(client: Client, message: Message):
    if not message.from_user:
        return

    cmd = (message.command[0] or "").lower().lstrip("/")
    if cmd not in MASS_CMDS:
        return

    try:
        ok, owner = await is_owner_or_sudoer(
            client,
            message.chat.id,
            message.from_user.id,
        )
    except Exception:
        LOGGER.exception("Owner/sudo check failed for /%s", cmd)
        return await message.reply_text("❌ Unable to verify your permissions right now.")

    if not ok:
        owner_m = (
            mention(owner.id, owner.first_name)
            if owner and getattr(owner, "id", None)
            else "the group owner"
        )
        return await message.reply_text(f"❌ Only {owner_m} may run /{cmd}.")

    try:
        await send_message_colored(
            chat_id=message.chat.id,
            text=f"⚠️ {message.from_user.mention}, confirm <code>/{cmd}</code> for this group?",
            reply_markup=_confirmation_keyboard(cmd),
        )
    except Exception:
        LOGGER.exception("Failed to send confirmation for /%s", cmd)
        await message.reply_text(f"⚠️ Confirm <code>/{cmd}</code> by replying with yes/no.")


@app.on_callback_query(filters.regex(_MASS_REGEX))
async def handle_mass_confirm(client: Client, callback: CallbackQuery):
    if not callback.message or not callback.from_user:
        return await callback.answer("This confirmation is no longer available.", show_alert=True)

    data = callback.data or ""
    parts = data.rsplit("_", 1)
    if len(parts) != 2:
        return await callback.answer("Invalid confirmation.", show_alert=True)

    cmd, answer = parts
    chat_id = callback.message.chat.id
    uid = callback.from_user.id

    try:
        ok, _owner = await is_owner_or_sudoer(client, chat_id, uid)
    except Exception:
        LOGGER.exception("Owner/sudo callback check failed")
        return await callback.answer("Unable to verify permissions.", show_alert=True)

    if not ok:
        return await callback.answer(
            "Only the group owner/sudoer can confirm this action.",
            show_alert=True,
        )

    if answer == "no":
        await callback.answer("Canceled.")
        try:
            await callback.message.edit_text(f"❌ <code>/{cmd}</code> canceled.")
        except RPCError:
            pass
        return

    await callback.answer("Processing...")

    # Check the bot's actual administrator privileges before starting.
    try:
        me = await client.get_me()
        bot_member = await client.get_chat_member(chat_id, me.id)
        priv = getattr(bot_member, "privileges", None)
    except RPCError as exc:
        LOGGER.exception("Unable to inspect bot permissions")
        return await callback.message.edit_text(
            f"❌ Cannot check my group permissions.\n<code>{exc}</code>"
        )

    if not priv:
        return await callback.message.edit_text(
            "❌ I must be an administrator in this group."
        )

    permission_ok = {
        "kickall": bool(getattr(priv, "can_restrict_members", False)),
        "banall": bool(getattr(priv, "can_restrict_members", False)),
        "unbanall": bool(getattr(priv, "can_restrict_members", False)),
        "muteall": bool(getattr(priv, "can_restrict_members", False)),
        "unmuteall": bool(getattr(priv, "can_restrict_members", False)),
        "unpinall": bool(getattr(priv, "can_pin_messages", False)),
    }.get(cmd, False)

    if not permission_ok:
        return await callback.message.edit_text(
            "❌ I do not have the required administrator permission for this command."
        )

    try:
        await callback.message.edit_text(f"⏳ Running <code>/{cmd}</code>...\nPlease wait.")
    except RPCError:
        pass

    handlers = {
        "kickall": _do_kickall,
        "banall": _do_banall,
        "unbanall": _do_unbanall,
        "muteall": _do_muteall,
        "unmuteall": _do_unmuteall,
        "unpinall": _do_unpinall,
    }

    try:
        result = await handlers[cmd](client, chat_id)
        # Individual handlers return (success, errors), except unpinall where
        # the same convention is used for consistency.
        success, errors = result
        try:
            await callback.message.edit_text(
                f"✅ <code>/{cmd}</code> completed.\n\n"
                f"Success: <code>{success}</code>\n"
                f"Failures: <code>{errors}</code>"
            )
        except RPCError:
            pass
    except FloodWait as exc:
        wait_seconds = max(int(getattr(exc, "value", 1)), 1)
        LOGGER.exception("Mass action stopped by FloodWait")
        try:
            await callback.message.edit_text(
                f"⚠️ Telegram rate limit reached.\n"
                f"Please wait <code>{wait_seconds}</code> seconds and try again."
            )
        except RPCError:
            pass
    except RPCError as exc:
        LOGGER.exception("Mass action RPC error: /%s", cmd)
        try:
            await callback.message.edit_text(
                f"❌ Telegram error during <code>/{cmd}</code>:\n<code>{exc}</code>"
            )
        except RPCError:
            pass
    except Exception as exc:
        LOGGER.exception("Unexpected mass action error: /%s", cmd)
        try:
            await callback.message.edit_text(
                f"❌ Error during <code>/{cmd}</code>:\n<code>{exc}</code>"
            )
        except RPCError:
            pass


async def _do_kickall(client: Client, chat_id: int):
    kicked = 0
    errors = 0

    async for member in client.get_chat_members(chat_id):
        if _is_protected_member(member):
            continue

        user_id = member.user.id
        try:
            # Kick = temporary ban followed by unban, allowing the user to
            # rejoin if the group permits it.
            await _run_with_flood_wait(
                lambda uid=user_id: client.ban_chat_member(chat_id, uid)
            )
            await _sleep_after_request()
            await _run_with_flood_wait(
                lambda uid=user_id: client.unban_chat_member(chat_id, uid)
            )
            kicked += 1
        except Exception:
            errors += 1
            LOGGER.exception("Failed to kick user %s", user_id)
        await _sleep_after_request()

    return kicked, errors


async def _do_banall(client: Client, chat_id: int):
    banned = 0
    errors = 0

    async for member in client.get_chat_members(chat_id):
        if _is_protected_member(member):
            continue

        user_id = member.user.id
        try:
            await _run_with_flood_wait(
                lambda uid=user_id: client.ban_chat_member(chat_id, uid)
            )
            banned += 1
        except Exception:
            errors += 1
            LOGGER.exception("Failed to ban user %s", user_id)
        await _sleep_after_request()

    return banned, errors


async def _do_unbanall(client: Client, chat_id: int):
    unbanned = 0
    errors = 0

    async for member in client.get_chat_members(
        chat_id,
        filter=ChatMembersFilter.BANNED,
    ):
        user = getattr(member, "user", None)
        if not user:
            continue

        try:
            await _run_with_flood_wait(
                lambda uid=user.id: client.unban_chat_member(chat_id, uid)
            )
            unbanned += 1
        except Exception:
            errors += 1
            LOGGER.exception("Failed to unban user %s", user.id)
        await _sleep_after_request()

    return unbanned, errors


# Explicit permissions are required. ChatPermissions() with no arguments does
# not reliably communicate the intended mute state across Pyrogram versions.
MUTE_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_media_messages=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    can_invite_users=False,
)

UNMUTE_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_media_messages=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_invite_users=True,
)


async def _do_muteall(client: Client, chat_id: int):
    muted = 0
    errors = 0

    async for member in client.get_chat_members(chat_id):
        if _is_protected_member(member):
            continue

        user_id = member.user.id
        try:
            await _run_with_flood_wait(
                lambda uid=user_id: client.restrict_chat_member(
                    chat_id,
                    uid,
                    permissions=MUTE_PERMISSIONS,
                )
            )
            muted += 1
        except Exception:
            errors += 1
            LOGGER.exception("Failed to mute user %s", user_id)
        await _sleep_after_request()

    return muted, errors


async def _do_unmuteall(client: Client, chat_id: int):
    unmuted = 0
    errors = 0

    async for member in client.get_chat_members(chat_id):
        if _is_protected_member(member):
            continue

        user_id = member.user.id
        try:
            await _run_with_flood_wait(
                lambda uid=user_id: client.restrict_chat_member(
                    chat_id,
                    uid,
                    permissions=UNMUTE_PERMISSIONS,
                )
            )
            unmuted += 1
        except Exception:
            errors += 1
            LOGGER.exception("Failed to unmute user %s", user_id)
        await _sleep_after_request()

    return unmuted, errors


async def _do_unpinall(client: Client, chat_id: int):
    try:
        await _run_with_flood_wait(
            lambda: client.unpin_all_chat_messages(chat_id)
        )
        return 1, 0
    except Exception:
        LOGGER.exception("Failed to unpin messages in %s", chat_id)
        return 0, 1
