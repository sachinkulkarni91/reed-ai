"""One-time calendar sign-in for auto-join.

Run from backend/ with the project venv. It prints a short code and a Microsoft
address; open that address, enter the code, and sign in as the bot's account
yourself. Only a refresh token is saved (data/graph-token.json).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.calendar_join import device_login  # noqa: E402

device_login()
