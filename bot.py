import os, asyncio
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import (InlineKeyboardMarkup, InlineKeyboardButton,
                           WebAppInfo, MenuButtonWebApp, Message)

bot = Bot(os.environ["BOT_TOKEN"])
dp = Dispatcher()
URL = os.environ["WEBAPP_URL"]  # alamat trycloudflare, diisi otomatis oleh workflow


@dp.message(CommandStart())
async def start(m: Message, command: CommandObject):
    url = URL
    if command.args and command.args.startswith("room_"):
        url += "?code=" + command.args[5:]
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Main sekarang", web_app=WebAppInfo(url=url))]])
    await m.answer("Ngumpul: pilih game, buat room, ajak teman.", reply_markup=kb)


async def main():
    # alamat berubah tiap workflow dijalankan, jadi tombol menu dipasang ulang saat start
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
        text="Main", web_app=WebAppInfo(url=URL)))
    await dp.start_polling(bot)


asyncio.run(main())
