import asyncio
import json
import logging
import os
import time
from collections import defaultdict

from dotenv import load_dotenv
from vkbottle import API, Bot, Keyboard, KeyboardButtonColor, Text
from vkbottle.bot import Message

# ================== ТИШИНА В КОНСОЛИ ==================
logging.getLogger("vkbottle").setLevel(logging.WARNING)

# ================== ЗАГРУЗКА НАСТРОЕК ==================
load_dotenv()

BOT_TOKEN = os.getenv("VK_BOT_TOKEN")
SERVICE_TOKEN = os.getenv("VK_SERVICE_TOKEN")
GROUP_IDS_RAW = os.getenv("VK_GROUP_IDS", "")
COOLDOWN_SECONDS = float(os.getenv("VK_COOLDOWN_SECONDS", "15"))

if not BOT_TOKEN:
    raise SystemExit("[ОШИБКА] VK_BOT_TOKEN не задан в .env")
if not SERVICE_TOKEN:
    raise SystemExit("[ОШИБКА] VK_SERVICE_TOKEN не задан в .env")

try:
    GROUP_IDS = [int(x.strip()) for x in GROUP_IDS_RAW.split(",") if x.strip()]
except ValueError:
    raise SystemExit("[ОШИБКА] VK_GROUP_IDS должен быть списком чисел через запятую")
if not GROUP_IDS:
    raise SystemExit("[ОШИБКА] VK_GROUP_IDS пуст")

POSTS_PER_GROUP = int(os.getenv("VK_POSTS_PER_GROUP", "10"))

# ================== СОСТОЯНИЕ ==================
STATE_FILE = "state.json"
state_lock = asyncio.Lock()
peer_locks = defaultdict(asyncio.Lock)
last_request: dict[int, float] = {}


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("seen", {})
            data.setdefault("known", {})
            return data
        except Exception as e:
            print(f"[!] Не удалось прочитать {STATE_FILE}: {e}. Начинаю с чистого листа.")
    return {"seen": {}, "known": {}}


def save_state() -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[!] Не удалось сохранить {STATE_FILE}: {e}")


state = load_state()

# ================== КЛАВИАТУРА И ТЕКСТ ПОМОЩИ ==================

def make_keyboard() -> Keyboard:
    """Клавиатура с кнопками. Показывается под полем ввода."""
    return (
        Keyboard(one_time=False, inline=False)
        .add(Text("Новости"), color=KeyboardButtonColor.PRIMARY)
        .add(Text("Сброс"), color=KeyboardButtonColor.SECONDARY)
        .row()
        .add(Text("Помощь"), color=KeyboardButtonColor.SECONDARY)
    )


HELP_TEXT = (
    "👋 Привет! Я показываю лучшие посты из заданных сообществ.\n\n"
    "📰 «Новости» — подборка: по одному лучшему посту из каждой группы.\n"
    "    • Сначала показываю новые посты (которых раньше не было).\n"
    "    • Если новых нет — следующий по популярности из старых.\n"
    "    • Если все посты из последних 10 уже показаны — пишу «Пусто».\n\n"
    "♻️ «Сброс» — очищаю память. После этого посты снова пойдут по кругу.\n\n"
    "⏱ Между запросами «Новости» есть пауза — это защита от спама.\n\n"
    "Просто нажми на кнопку ниже или напиши команду вручную."
)

# ================== ЛОГИКА ==================
api = API(token=SERVICE_TOKEN)
bot = Bot(token=BOT_TOKEN)


async def get_candidate_for_group(group_id: int, peer_id: int):
    try:
        response = await api.wall.get(owner_id=group_id, count=POSTS_PER_GROUP)
    except Exception as e:
        print(f"[ОШИБКА] Чтение группы {group_id}: {e}")
        return None, "error"

    posts = response.items
    if not posts:
        return None, "no_posts"

    current_ids = {p.id for p in posts}
    posts_by_id = {p.id: p for p in posts}

    peer_key = str(peer_id)
    group_key = str(group_id)

    async with state_lock:
        seen = set(state["seen"].get(peer_key, []))
        known = set(state["known"].get(peer_key, {}).get(group_key, []))

        new_ids = current_ids - known
        old_ids = current_ids & known

        state["known"].setdefault(peer_key, {})[group_key] = list(current_ids)

        new_posts = [
            posts_by_id[i] for i in new_ids
            if f"{group_id}_{i}" not in seen
        ]
        old_posts = [
            posts_by_id[i] for i in old_ids
            if f"{group_id}_{i}" not in seen
        ]

        new_posts.sort(key=lambda p: p.likes.count if p.likes else 0, reverse=True)
        old_posts.sort(key=lambda p: p.likes.count if p.likes else 0, reverse=True)

        candidates = new_posts + old_posts

        if not candidates:
            save_state()
            return None, "empty"

        chosen = candidates[0]
        state["seen"].setdefault(peer_key, []).append(f"{group_id}_{chosen.id}")
        save_state()
        return chosen, "ok"


# ================== ОБРАБОТЧИКИ КОМАНД ==================

@bot.on.message(text=["Новости", "новости", "/новости", "НОВОСТИ"])
async def news_handler(message: Message):
    peer_id = message.peer_id

    async with peer_locks[peer_id]:
        now = time.monotonic()
        last = last_request.get(peer_id, 0.0)
        elapsed = now - last

        if elapsed < COOLDOWN_SECONDS:
            remaining = COOLDOWN_SECONDS - elapsed
            print(f"[!] {peer_id}: кулдаун, осталось {remaining:.1f} сек.")
            await message.answer(
                f"⏳ Подожди {remaining:.0f} сек. перед следующим запросом.",
                keyboard=make_keyboard(),
            )
            return

        last_request[peer_id] = now

        print(f"[→] {message.from_id} написал 'Новости' в чат {peer_id}")
        await message.answer("Ищу новости...", keyboard=make_keyboard())

        for group_id in GROUP_IDS:
            post, status = await get_candidate_for_group(group_id, peer_id)

            if post:
                attachment = f"wall{post.owner_id}_{post.id}"
                try:
                    await message.answer(attachment=attachment)
                    likes = post.likes.count if post.likes else 0
                    print(f"[←] {peer_id}: отправлен пост {attachment} (лайков {likes})")
                except Exception as e:
                    print(f"[ОШИБКА] Не удалось отправить {attachment}: {e}")
                    await message.answer(
                        f"Не удалось отправить пост из группы {group_id}",
                        keyboard=make_keyboard(),
                    )
            else:
                if status == "empty":
                    await message.answer(
                        f"Группа {group_id}: Пусто (все посты уже показаны). "
                        f"Сбрось память кнопкой «Сброс», чтобы увидеть их снова.",
                        keyboard=make_keyboard(),
                    )
                elif status == "no_posts":
                    await message.answer(
                        f"Группа {group_id}: Пусто (нет постов).",
                        keyboard=make_keyboard(),
                    )
                elif status == "error":
                    await message.answer(
                        f"Группа {group_id}: Ошибка чтения.",
                        keyboard=make_keyboard(),
                    )

        print(f"[←] Обработка запроса от {peer_id} завершена")


@bot.on.message(text=["Сброс", "сброс", "/reset", "/сброс", "СБРОС"])
async def reset_handler(message: Message):
    peer_id = message.peer_id
    peer_key = str(peer_id)

    async with peer_locks[peer_id]:
        async with state_lock:
            removed_seen = len(state["seen"].pop(peer_key, []))
            removed_groups = len(state["known"].pop(peer_key, {}))
            save_state()

        print(
            f"[↺] {peer_id}: сброс памяти "
            f"(удалено seen={removed_seen}, known_groups={removed_groups})"
        )
        await message.answer(
            "♻️ Память сброшена. Посты снова будут показываться по кругу.",
            keyboard=make_keyboard(),
        )


@bot.on.message(text=[
    "Помощь", "помощь", "/help", "/помощь", "Help", "help",
    "Начать", "начать", "/start", "Старт", "старт", "Меню", "меню",
])
async def help_handler(message: Message):
    print(f"[?] {message.from_id} запросил помощь в чате {message.peer_id}")
    await message.answer(HELP_TEXT, keyboard=make_keyboard())


# ВАЖНО: этот обработчик должен идти ПОСЛЕДНИМ
@bot.on.message()
async def fallback_handler(message: Message):
    """Срабатывает, если ни одна команда не совпала."""
    print(f"[?] {message.from_id}: неизвестная команда «{message.text}»")
    await message.answer(
        "Не знаю такую команду. Вот что я умею:\n\n" + HELP_TEXT,
        keyboard=make_keyboard(),
    )


if __name__ == "__main__":
    print("=" * 50)
    print("Бот запускается... (Ctrl+C для остановки)")
    print(f"Группы для анализа: {GROUP_IDS}")
    print(f"Кулдаун между запросами: {COOLDOWN_SECONDS:.0f} сек.")
    print("Команды: 'Новости' — подборка, 'Сброс' — очистить историю, 'Помощь' — справка")
    print("=" * 50)
    bot.run_forever()
