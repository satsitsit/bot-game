import os, asyncio
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import (InlineKeyboardMarkup, InlineKeyboardButton,
                           WebAppInfo, MenuButtonWebApp, Message)

bot = Bot(os.environ["BOT_TOKEN"])
dp = Dispatcher()
URL = os.environ["WEBAPP_URL"]


@dp.message(CommandStart())
async def start(m: Message, command: CommandObject):
    url = URL
    if command.args and command.args.startswith("room_"):
        url += "?code=" + command.args[5:]
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Main sekarang", web_app=WebAppInfo(url=url))]])
    await m.answer(
        "<b>Ngumpul</b> — kumpul bareng teman main!\n\n"
        "Pilih game, buat room, ajak teman lewat kode. "
        "Ada Ludo, Werewolf, Truth or Dare, Tiga Monyet, Panjat Gunung, dan Distrik.",
        parse_mode="HTML", reply_markup=kb)


async def main():
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
        text="Main", web_app=WebAppInfo(url=URL)))
    await dp.start_polling(bot)

asyncio.run(main())