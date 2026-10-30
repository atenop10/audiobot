"""Ek baar chalao: userbot account ka phone + OTP daalke session file banata hai."""
import os
from dotenv import load_dotenv
from telethon import TelegramClient
load_dotenv()
os.makedirs("data", exist_ok=True)
c = TelegramClient("data/userbot", int(os.environ["API_ID"]), os.environ["API_HASH"])
c.start()
me = c.loop.run_until_complete(c.get_me())
print("Logged in as:", me.first_name, me.id)
c.disconnect()
