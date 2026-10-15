"""Run once after deploying to public HTTPS. Config from environment/.env."""
import os
import asyncio
from dotenv import load_dotenv
from telegram import Bot, MenuButtonWebApp, WebAppInfo
load_dotenv()
async def main():
    token=os.getenv('BOT_TOKEN','').strip()
    url=os.getenv('WEBAPP_URL','').strip()
    if not token: raise SystemExit('Укажи BOT_TOKEN в .env')
    if not url.startswith('https://'): raise SystemExit('WEBAPP_URL должен начинаться с https://')
    async with Bot(token) as bot:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text='⚽ Players History',web_app=WebAppInfo(url=url)))
        print('Кнопка Mini App подключена:',url)
if __name__=='__main__': asyncio.run(main())
