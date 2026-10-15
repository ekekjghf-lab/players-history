import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
BOT_USERNAME = os.getenv('BOT_USERNAME', 'players_history_bot')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY', '')
DB_NAME = os.getenv('DB_NAME', str(ROOT / 'football.db'))
