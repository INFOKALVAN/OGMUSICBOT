# ═══════════════════════════════════════════════════════════
#        😎  INDU MUSIC BOT  😎
#   GitHub : github.com/ItsMeIndu0/InduMusic
#   Developer : @ItsMeInduBots | Telegram
#   Module : Resume Stream Command
# ═══════════════════════════════════════════════════════════

from pyrogram import filters
from pyrogram.types import Message

from INDUMUSIC import app
from INDUMUSIC.core.call import INDU
from INDUMUSIC.utils.database import is_music_playing, music_on
from INDUMUSIC.utils.decorators import AdminRightsCheck
from INDUMUSIC.utils.colored_buttons import buttons_to_inline_markup
from INDUMUSIC.utils.inline import close_markup
from config import BANNED_USERS


@app.on_message(filters.command(["resume", "cresume"]) & filters.group & ~BANNED_USERS)
@AdminRightsCheck
async def resume_com(cli, message: Message, _, chat_id):
    if await is_music_playing(chat_id):
        return await message.reply_text(_["admin_3"])
    await music_on(chat_id)
    await INDU.resume_stream(chat_id)
    await message.reply_text(
        text=_["admin_4"].format(message.from_user.mention),
        reply_markup=buttons_to_inline_markup(close_markup(_))
    )

# ═══════════════════════════════════════════════════════════
#        😎  INDU MUSIC BOT  😎
#   github.com/ItsMeIndu0/InduMusic
# ═══════════════════════════════════════════════════════════
