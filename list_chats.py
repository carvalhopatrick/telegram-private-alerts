import os
from telethon.sync import TelegramClient
from telethon.utils import get_peer_id
from dotenv import load_dotenv

load_dotenv()
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION_NAME = os.environ["TG_SESSION_NAME"]

with TelegramClient(SESSION_NAME, API_ID, API_HASH) as client:
    for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            peer_id = get_peer_id(dialog.entity)
            print(f"{dialog.name} | peer_id={peer_id} | raw_id={dialog.id}")