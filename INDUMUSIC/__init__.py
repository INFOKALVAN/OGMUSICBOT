# ═══════════════════════════════════════════════════════════
#        😎  INDU MUSIC BOT  😎
#   GitHub : github.com/ItsMeIndu0/InduMusic
#   Developer : @ItsMeInduBots | Telegram
#   Module : Package Initialization & App Setup
# ═══════════════════════════════════════════════════════════

from INDUMUSIC.core.bot import INDU
from INDUMUSIC.core.dir import dirr
from INDUMUSIC.core.git import git
from INDUMUSIC.core.userbot import Userbot
from INDUMUSIC.misc import dbb, heroku

from .logging import LOGGER

dirr()
git()
dbb()
heroku()

app = INDU()
userbot = Userbot()


from .platforms import *

Apple = AppleAPI()
Carbon = CarbonAPI()
SoundCloud = SoundAPI()
Spotify = SpotifyAPI()
Resso = RessoAPI()
Telegram = TeleAPI()
YouTube = YouTubeAPI()

# ═══════════════════════════════════════════════════════════
#        😎  INDU MUSIC BOT  😎
#   github.com/ItsMeIndu0/InduMusic
# ═══════════════════════════════════════════════════════════
