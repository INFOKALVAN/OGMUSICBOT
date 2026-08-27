# ═══════════════════════════════════════════════════════════
#        😎  INDU MUSIC BOT  😎
#   GitHub : github.com/ItsMeIndu0/InduMusic
#   Developer : @ItsMeInduBots | Telegram
#   Module : Voice Chat Call Handler & Stream Manager
# ═══════════════════════════════════════════════════════════

import asyncio
import logging
import os
import traceback
from datetime import datetime, timedelta
from typing import Union

from ntgcalls import TelegramServerError
from pyrogram import Client
from pyrogram.errors import FloodWait, ChatAdminRequired
from pytgcalls import PyTgCalls
from pytgcalls.exceptions import NoActiveGroupCall
from pytgcalls.types import AudioQuality, ChatUpdate, MediaStream, StreamEnded, Update, VideoQuality

import config
from strings import get_string
from INDUMUSIC import LOGGER, YouTube, app
from INDUMUSIC.misc import db
from INDUMUSIC.utils.stream.autoplay import is_autoplay_on
from INDUMUSIC.utils.database import (
    add_active_chat,
    add_active_video_chat,
    get_lang,
    get_loop,
    group_assistant,
    is_autoend,
    music_on,
    remove_active_chat,
    remove_active_video_chat,
    set_loop,
)
from INDUMUSIC.utils.exceptions import AssistantErr
from INDUMUSIC.utils.formatters import check_duration, seconds_to_min, speed_converter
from INDUMUSIC.utils.inline.play import colored_stream_markup, colored_stream_markup_timer
from INDUMUSIC.utils.colored_buttons import buttons_to_inline_markup, smart_send_photo
from INDUMUSIC.utils.stream.autoclear import auto_clean

logger = logging.getLogger(__name__)
from INDUMUSIC.utils.thumbnails import get_thumb, get_thumb_url
from INDUMUSIC.utils.errors import capture_internal_err, send_large_error
from INDUMUSIC.utils.pastebin import INDUBIN

autoend = {}
counter = {}

def dynamic_media_stream(path: str, video: bool = False, ffmpeg_params: str = None) -> MediaStream:
    return MediaStream(
        audio_path=path,
        media_path=path,
        audio_parameters=AudioQuality.STUDIO,
        video_parameters=VideoQuality.HD_720p if video else VideoQuality.SD_360p,
        video_flags=(MediaStream.Flags.AUTO_DETECT if video else MediaStream.Flags.IGNORE),
        ffmpeg_parameters=ffmpeg_params,
    )

async def _colored_send_photo(original_chat_id, photo, caption, buttons, db_ref, chat_id, markup_type):
    """Send photo with colored buttons via smart wrapper (auto fallback)."""
    run = await smart_send_photo(
        chat_id=original_chat_id,
        photo=photo,
        caption=caption,
        reply_markup=buttons,
    )
    playlist = db.get(chat_id)
    if playlist and len(playlist) > 0:
        playlist[0]["mystic"] = run
        playlist[0]["markup"] = markup_type
        playlist[0]["base_caption"] = caption
    return run


async def _clear_(chat_id: int) -> None:
    popped = db.pop(chat_id, None)
    if popped:
        await auto_clean(popped)
    db[chat_id] = []
    await remove_active_video_chat(chat_id)
    await remove_active_chat(chat_id)
    await set_loop(chat_id, 0)

class Call:
    def __init__(self):
        self.userbot1 = Client(
            "InduXAssis1", config.API_ID, config.API_HASH, session_string=config.STRING1
        ) if config.STRING1 else None
        self.one = PyTgCalls(self.userbot1) if self.userbot1 else None

        self.userbot2 = Client(
            "InduXAssis2", config.API_ID, config.API_HASH, session_string=config.STRING2
        ) if config.STRING2 else None
        self.two = PyTgCalls(self.userbot2) if self.userbot2 else None

        self.userbot3 = Client(
            "InduXAssis3", config.API_ID, config.API_HASH, session_string=config.STRING3
        ) if config.STRING3 else None
        self.three = PyTgCalls(self.userbot3) if self.userbot3 else None

        self.userbot4 = Client(
            "InduXAssis4", config.API_ID, config.API_HASH, session_string=config.STRING4
        ) if config.STRING4 else None
        self.four = PyTgCalls(self.userbot4) if self.userbot4 else None

        self.userbot5 = Client(
            "InduXAssis5", config.API_ID, config.API_HASH, session_string=config.STRING5
        ) if config.STRING5 else None
        self.five = PyTgCalls(self.userbot5) if self.userbot5 else None

        self.active_calls: set[int] = set()


    @capture_internal_err
    async def pause_stream(self, chat_id: int) -> None:
        assistant = await group_assistant(self, chat_id)
        await assistant.pause(chat_id)

    @capture_internal_err
    async def resume_stream(self, chat_id: int) -> None:
        assistant = await group_assistant(self, chat_id)
        await assistant.resume(chat_id)

    @capture_internal_err
    async def mute_stream(self, chat_id: int) -> None:
        assistant = await group_assistant(self, chat_id)
        await assistant.mute(chat_id)

    @capture_internal_err
    async def unmute_stream(self, chat_id: int) -> None:
        assistant = await group_assistant(self, chat_id)
        await assistant.unmute(chat_id)

    @capture_internal_err
    async def stop_stream(self, chat_id: int) -> None:
        assistant = await group_assistant(self, chat_id)
        await _clear_(chat_id)
        if chat_id not in self.active_calls:
            return
        try:
            await assistant.leave_call(chat_id)
        except Exception:
            pass
        finally:
            self.active_calls.discard(chat_id)


    @capture_internal_err
    async def force_stop_stream(self, chat_id: int) -> None:
        assistant = await group_assistant(self, chat_id)
        try:
            check = db.get(chat_id)
            if check:
                check.pop(0)
        except (IndexError, KeyError):
            pass
        await remove_active_video_chat(chat_id)
        await remove_active_chat(chat_id)
        await _clear_(chat_id)
        if chat_id not in self.active_calls:
            return
        try:
            await assistant.leave_call(chat_id)
        except Exception:
            pass
        finally:
            self.active_calls.discard(chat_id)


    @capture_internal_err
    async def skip_stream(self, chat_id: int, link: str, video: Union[bool, str] = None, image: Union[bool, str] = None) -> None:
        assistant = await group_assistant(self, chat_id)
        stream = dynamic_media_stream(path=link, video=bool(video))
        await assistant.play(chat_id, stream)

    @capture_internal_err
    async def vc_users(self, chat_id: int) -> list:
        assistant = await group_assistant(self, chat_id)
        participants = await assistant.get_participants(chat_id)
        return [p.user_id for p in participants if not p.is_muted]

    @capture_internal_err
    async def seek_stream(self, chat_id: int, file_path: str, to_seek: str, duration: str, mode: str) -> None:
        assistant = await group_assistant(self, chat_id)
        ffmpeg_params = f"-ss {to_seek} -to {duration}"
        is_video = mode == "video"
        stream = dynamic_media_stream(path=file_path, video=is_video, ffmpeg_params=ffmpeg_params)
        await assistant.play(chat_id, stream)

    @capture_internal_err
    async def speedup_stream(self, chat_id: int, file_path: str, speed: float, playing: list) -> None:
        if not isinstance(playing, list) or not playing or not isinstance(playing[0], dict):
            raise AssistantErr("Invalid stream info for speedup.")

        assistant = await group_assistant(self, chat_id)
        base = os.path.basename(file_path)
        chatdir = os.path.join("playback", str(speed))
        os.makedirs(chatdir, exist_ok=True)
        out = os.path.join(chatdir, base)

        if not os.path.exists(out):
            vs = str(2.0 / float(speed))
            cmd = f'ffmpeg -i "{file_path}" -filter:v setpts={vs}*PTS -filter:a atempo={speed} "{out}"'
            proc = await asyncio.create_subprocess_shell(cmd, stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await proc.communicate()

        dur = int(await asyncio.get_event_loop().run_in_executor(None, check_duration, out))
        played, con_seconds = speed_converter(playing[0]["played"], speed)
        duration_min = seconds_to_min(dur)
        is_video = playing[0]["streamtype"] == "video"
        ffmpeg_params = f"-ss {played} -to {duration_min}"
        stream = dynamic_media_stream(path=out, video=is_video, ffmpeg_params=ffmpeg_params)

        if chat_id in db and db[chat_id] and db[chat_id][0].get("file") == file_path:
            await assistant.play(chat_id, stream)
        else:
            raise AssistantErr("Stream mismatch during speedup.")

        db[chat_id][0].update({
            "played": con_seconds,
            "dur": duration_min,
            "seconds": dur,
            "speed_path": out,
            "speed": speed,
            "old_dur": db[chat_id][0].get("dur"),
            "old_second": db[chat_id][0].get("seconds"),
        })


    @capture_internal_err
    async def stream_call(self, link: str) -> None:
        assistant = await group_assistant(self, config.LOGGER_ID)
        try:
            await assistant.play(config.LOGGER_ID, MediaStream(link))
            await asyncio.sleep(8)
        finally:
            try:
                await assistant.leave_call(config.LOGGER_ID)
            except:
                pass

    @capture_internal_err
    async def join_call(
        self,
        chat_id: int,
        original_chat_id: int,
        link: str,
        video: Union[bool, str] = None,
        image: Union[bool, str] = None,
    ) -> None:
        assistant = await group_assistant(self, chat_id)
        lang = await get_lang(chat_id)
        _ = get_string(lang)
        stream = dynamic_media_stream(path=link, video=bool(video))

        try:
            await assistant.play(chat_id, stream)
        except (NoActiveGroupCall, ChatAdminRequired):
            raise AssistantErr(_["call_8"])
        except TelegramServerError:
            raise AssistantErr(_["call_10"])
        except Exception as e:
            raise AssistantErr(
                f"ᴜɴᴀʙʟᴇ ᴛᴏ ᴊᴏɪɴ ᴛʜᴇ ɢʀᴏᴜᴘ ᴄᴀʟʟ.\nRᴇᴀsᴏɴ: {e}"
            )
        self.active_calls.add(chat_id)
        await add_active_chat(chat_id)
        await music_on(chat_id)
        if video:
            await add_active_video_chat(chat_id)

        if await is_autoend():
            counter[chat_id] = {}
            users = len(await assistant.get_participants(chat_id))
            if users == 1:
                autoend[chat_id] = datetime.now() + timedelta(minutes=1)


    @capture_internal_err
    async def play(self, client, chat_id: int) -> None:
        """
        Advance playback when PyTgCalls reports StreamEnded.

        IMPORTANT:
        - If the queue still has tracks, play the next queued track directly.
        - Only run autoplay when the queue is empty.
        - Never call _clear_() while a queued track is waiting; doing so marks
          the call inactive and makes stream() treat the next song as a new
          session, which was the main reason queued songs were not advancing.
        """
        check = db.get(chat_id)
        if not check or not isinstance(check, list) or not check:
            return

        loop = await get_loop(chat_id)

        try:
            # Loop mode: replay the current item without removing it.
            if loop and loop > 0:
                loop -= 1
                await set_loop(chat_id, loop)
                popped = check[0]
                repeat_current = True
            else:
                # Normal queue advance: remove the song that just ended.
                popped = check.pop(0)
                repeat_current = False

            if not popped or not isinstance(popped, dict):
                return

            last_title = popped.get("title", "")
            last_vidid = popped.get("vidid", "")
            last_chat_id = popped.get("chat_id", chat_id)
            last_video = str(popped.get("streamtype", "")).lower() == "video"
            queued_file = popped.get("file")
            queued_type = str(popped.get("streamtype", "")).lower()

            # Clean only the track that actually finished. Do not delete a
            # repeated track while loop mode is replaying it.
            if not repeat_current:
                await auto_clean(popped)

            # ================================================================
            # QUEUED TRACK EXISTS -> PLAY IT NOW
            # ================================================================
            if check and len(check) > 0:
                assistant = await group_assistant(self, chat_id)
                next_track = check[0]
                next_file = next_track.get("file")
                next_vidid = next_track.get("vidid", "")
                next_type = str(next_track.get("streamtype", "")).lower()
                next_video = next_type == "video"

                if not next_file:
                    raise AssistantErr("Queued track has no playable file.")

                mystic = None
                playable = next_file

                # YouTube queued item: download it before replacing the
                # finished stream. This follows the same download pipeline
                # used by /skip, including primary/fallback/yt-dlp methods.
                if isinstance(next_file, str) and next_file.startswith("vid_"):
                    try:
                        mystic = await app.send_message(
                            next_track.get("chat_id", chat_id),
                            "⏳ ᴘʟᴀʏɪɴɢ ɴᴇxᴛ sᴏɴɢ..."
                        )
                    except Exception:
                        mystic = None

                    playable, _direct = await YouTube.download(
                        next_vidid,
                        mystic,
                        video=next_video,
                        videoid=True,
                    )
                    if not playable:
                        raise AssistantErr("Unable to download the next queued song.")

                elif isinstance(next_file, str) and next_file.startswith("live_"):
                    n, playable = await YouTube.video(next_vidid, True)
                    if n == 0 or not playable:
                        raise AssistantErr("Unable to load the next live stream.")

                # index_ and Telegram/SoundCloud/file queues already contain
                # their playable path/URL, so they can be sent directly.
                stream_obj = dynamic_media_stream(
                    path=playable,
                    video=next_video,
                )
                await assistant.play(chat_id, stream_obj)

                # Keep the queue state alive and reset playback counters.
                next_track["played"] = 0
                next_track["seconds"] = next_track.get("old_dur", next_track.get("seconds", 0))

                if mystic:
                    try:
                        await mystic.delete()
                    except Exception:
                        pass
                return

            # ================================================================
            # QUEUE EMPTY -> OPTIONAL AUTOPLAY
            # ================================================================
            await _clear_(chat_id)

            autoplay_started = False
            if last_title and await is_autoplay_on(chat_id):
                try:
                    from INDUMUSIC.utils.stream.autoplay import auto_play_next
                    from INDUMUSIC.utils.database import is_active_chat as _is_chat_active

                    autoplay_started = await auto_play_next(
                        chat_id,
                        last_chat_id,
                        last_title,
                        last_vidid,
                        video=last_video,
                    )

                    # stream() can swallow join/play errors, so verify that
                    # the call was actually activated.
                    if autoplay_started and not await _is_chat_active(chat_id):
                        autoplay_started = False

                except Exception:
                    autoplay_started = False

            # If autoplay is disabled, leave the VC normally.
            if not autoplay_started and not await is_autoplay_on(chat_id):
                if chat_id in self.active_calls:
                    try:
                        await client.leave_call(chat_id)
                    except NoActiveGroupCall:
                        pass
                    except Exception:
                        pass
                    finally:
                        self.active_calls.discard(chat_id)

        except Exception:
            try:
                await _clear_(chat_id)
            except Exception:
                pass
            try:
                await client.leave_call(chat_id)
            except Exception:
                pass
    async def start(self) -> None:
        """Start all configured PyTgCalls assistant clients."""
        LOGGER(__name__).info("Starting PyTgCalls Clients...")

        assistants = [
            self.one if config.STRING1 else None,
            self.two if config.STRING2 else None,
            self.three if config.STRING3 else None,
            self.four if config.STRING4 else None,
            self.five if config.STRING5 else None,
        ]

        for assistant in assistants:
            if assistant is not None:
                await assistant.start()

    @capture_internal_err
    async def ping(self) -> str:
        pings = []
        if config.STRING1:
            pings.append(self.one.ping)
        if config.STRING2:
            pings.append(self.two.ping)
        if config.STRING3:
            pings.append(self.three.ping)
        if config.STRING4:
            pings.append(self.four.ping)
        if config.STRING5:
            pings.append(self.five.ping)
        return str(round(sum(pings) / len(pings), 3)) if pings else "0.0"

    @capture_internal_err
    async def decorators(self) -> None:
        assistants = list(filter(None, [self.one, self.two, self.three, self.four, self.five]))

        CRITICAL = (
            ChatUpdate.Status.KICKED
            | ChatUpdate.Status.LEFT_GROUP
            | ChatUpdate.Status.CLOSED_VOICE_CHAT
            | ChatUpdate.Status.DISCARDED_CALL
            | ChatUpdate.Status.BUSY_CALL
        )

        async def unified_update_handler(client, update: Update) -> None:
            try:
                if isinstance(update, ChatUpdate):
                    status = update.status
                    if (status & ChatUpdate.Status.LEFT_CALL) or (status & CRITICAL):
                        await self.stop_stream(update.chat_id)
                        return

                elif isinstance(update, StreamEnded):
                    # Handle both AUDIO and VIDEO stream endings.
                    # The original AUDIO-only guard meant video streams never
                    # triggered queue advance or autoplay.
                    assistant = await group_assistant(self, update.chat_id)
                    await self.play(assistant, update.chat_id)

            except Exception:
                import sys, traceback
                exc_type, exc_obj, exc_tb = sys.exc_info()
                err_msg = str(exc_obj)[:200]
                caption = (
                    f"🚨 <b>Stream Error</b>\n"
                    f"📍 <b>Type:</b> <code>{exc_type.__name__}</code>\n"
                    f"💬 <b>Error:</b> <code>{err_msg}</code>\n"
                    f"📌 <b>Chat:</b> <code>{getattr(update, 'chat_id', '?')}</code>"
                )
                try:
                    full_trace = "".join(traceback.format_exception(exc_type, exc_obj, exc_tb))
                    paste_url = await INDUBIN(full_trace)
                    if paste_url:
                        caption += f"\n🔗 <b>Log:</b> {paste_url}"
                except Exception:
                    pass
                try:
                    await app.send_message(config.LOGGER_ID, caption)
                except Exception:
                    pass

        for assistant in assistants:
            assistant.on_update()(unified_update_handler)


INDU = Call()

# ═══════════════════════════════════════════════════════════
#        😎  INDU MUSIC BOT  😎
#   github.com/ItsMeIndu0/InduMusic
# ═══════════════════════════════════════════════════════════
