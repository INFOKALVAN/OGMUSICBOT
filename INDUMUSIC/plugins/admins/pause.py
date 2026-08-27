# ═══════════════════════════════════════════════════════════
#        😎  INDU MUSIC BOT  😎
#   GitHub : github.com/ItsMeIndu0/InduMusic
#   Developer : @ItsMeInduBots | Telegram
#   Module : Pause Stream Command
# ═══════════════════════════════════════════════════════════

from pyrogram import filters
from pyrogram.types import Message

from INDUMUSIC import app
from INDUMUSIC.core.call import INDU
from INDUMUSIC.utils.database import is_music_playing, music_off
from INDUMUSIC.utils.decorators import AdminRightsCheck
from INDUMUSIC.utils.colored_buttons import buttons_to_inline_markup
from INDUMUSIC.utils.inline import close_markup
from config import BANNED_USERS


@app.on_message(filters.command(["pause", "cpause"]) & filters.group & ~BANNED_USERS)
@AdminRightsCheck
async def pause_admin(cli, message: Message, _, chat_id):
    if not await is_music_playing(chat_id):
        return await message.reply_text(_["admin_1"])
    await music_off(chat_id)
    await INDU.pause_stream(chat_id)
    await message.reply_text(
        text=_["admin_2"].format(message.from_user.mention),
        reply_markup=buttons_to_inline_markup(close_markup(_))
    )

# ═══════════════════════════════════════════════════════════
#        😎  INDU MUSIC BOT  😎
#   github.com/ItsMeIndu0/InduMusic
# ═══════════════════════════════════════════════════════════
