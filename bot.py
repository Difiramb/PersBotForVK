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
NEWS_GROUP_IDS_RAW = os.getenv("VK_GROUP_IDS", "")
ANIME_GROUP_IDS_RAW = os.getenv("VK_ANIME_GROUP_IDS", "")
PHOTO_GROUP_IDS_RAW = os.getenv("VK_PHOTO_GROUP_IDS", "")
COOLDOWN_SECONDS = float(os.getenv("VK_COOLDOWN_SECONDS", "15"))
POSTS_PER_GROUP = int(os.getenv("VK_POSTS_PER_GROUP", "10"))

if not BOT_TOKEN:
    raise SystemExit("[ОШИБКА] VK_BOT_TOKEN не задан")
if not SERVICE_TOKEN:
    raise SystemExit("[ОШИБКА] VK_SERVICE_TOKEN не задан")


def parse_ids(raw: str, var_name: str) -> list[int]:
    try:
        return [int(x.strip()) for x in raw.split(",") if x.strip()]
    except ValueError:
        raise SystemExit(f"[ОШИБКА] {var_name} должен быть списком чисел через запятую")


NEWS_GROUP_IDS = parse_ids(NEWS_GROUP_IDS_RAW, "VK_GROUP_IDS")
ANIME_GROUP_IDS = parse_ids(ANIME_GROUP_IDS_RAW, "VK_ANIME_GROUP_IDS")
PHOTO_GROUP_IDS = parse_ids(PHOTO_GROUP_IDS_RAW, "VK_PHOTO_GROUP_IDS")

if not NEWS_GROUP_IDS:
    raise SystemExit("[ОШИБКА] VK_GROUP_IDS пуст — заполните его")
if not ANIME_GROUP_IDS:
    raise SystemExit("[ОШИБКА] VK_ANIME_GROUP_IDS пуст — заполните его")
if not PHOTO_GROUP_IDS:
    raise SystemExit("[ОШИБКА] VK_PHOTO_GROUP_IDS пуст — заполните его")

# ================== КАТЕГОРИИ ==================
CATEGORIES = {
    "news": {
        "label": "Новости",
        "triggers": ["Новости", "новости", "/новости", "НОВОСТИ"],
        "group_ids": NEWS_GROUP_IDS,
    },
    "anime": {
        "label": "Аниме",
        "triggers": ["Аниме", "аниме", "/anime", "АНИМЕ"],
        "group_ids": ANIME_GROUP_IDS,
    },
    "photo": {
        "label": "Фото",
        "triggers": ["Фото", "фото", "/photo", "ФОТО"],
        "group_ids": PHOTO_GROUP_IDS,
    },
}

ALL_TRIGGERS: list[str] = []
for _cat in CATEGORIES.values():
    ALL_TRIGGERS.extend(_cat["triggers"])


def detect_category(text: str | None) -> str | None:
    if not text:
        return None
    for name, cat in CATEGORIES.items():
        if text in cat["triggers"]:
            return name
    return None


# ================== СОСТОЯНИЕ ==================
# ================== СОСТОЯНИЕ ==================
STATE_FILE = os.path.join("data", "state.json")   # ← изменили путь
state_lock = asyncio.Lock()
peer_locks = defaultdict(asyncio.Lock)
last_request: dict[tuple[str, int], float] = {}


def load_state() -> dict:
    os.makedirs("data", exist_ok=True)   # ← добавили
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
    os.makedirs("data", exist_ok=True)   # ← добавили
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[!] Не удалось сохранить {STATE_FILE}: {e}")


state = load_state()

# ================== КЛАВИАТУРА И ТЕКСТ ПОМОЩИ ==================

def make_keyboard() -> Keyboard:
    """
    Клавиатура с кнопками.
    one_time=True — после нажатия кнопки клавиатура сворачивается,
    открыть её заново можно вручную по иконке рядом с полем ввода.
    """
    return (
        Keyboard(one_time=True, inline=False)
        .add(Text("Новости"), color=KeyboardButtonColor.PRIMARY)
        .add(Text("Аниме"), color=KeyboardButtonColor.PRIMARY)
        .add(Text("Фото"), color=KeyboardButtonColor.PRIMARY)
        .row()
        .add(Text("Сброс"), color=KeyboardButtonColor.SECONDARY)
        .add(Text("Помощь"), color=KeyboardButtonColor.SECONDARY)
    )


HELP_TEXT = (
    "👋 Привет! Я показываю лучшие посты из заданных сообществ.\n\n"
    "📰 «Новости» — по одному лучшему посту из каждой новостной группы.\n"
    "🎌 «Аниме» — по одному лучшему посту из каждой аниме-группы.\n"
    "📷 «Фото» — по одному лучшему посту из каждой фотогруппы.\n\n"
    "Во всех трёх режимах:\n"
    "    • Сначала показываю новые посты (которых раньше не было).\n"
    "    • Если новых нет — следующий по популярности из старых.\n"
    "    • Если все посты из последних 10 уже показаны — пишу «Пусто».\n\n"
    "♻️ «Сброс» — очищаю память по всем трём разделам. После этого посты "
    "снова пойдут по кругу.\n\n"
    "⏱ Внутри одного раздела между запросами есть пауза — защита от спама. "
    "Разные разделы друг другу не мешают: можно запросить «Новости», затем "
    "сразу «Аниме», затем «Фото».\n\n"
    "Кнопки «Новости», «Аниме», «Фото» работают со своими списками групп "
    "и своей историей показов."
)

# ================== ЛОГИКА ==================
api = API(token=SERVICE_TOKEN)
bot = Bot(token=BOT_TOKEN)


async def get_candidate_for_group(group_id: int, peer_key: str):
    """
    peer_key — уникальный ключ в state, например 'news:123456'.
    """
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
    group_key = str(group_id)

    async with state_lock:
        seen = set(state["seen"].get(peer_key, []))
        known = set(state["known"].get(peer_key, {}).get(group_key, []))

        new_ids = current_ids - known
        old_ids = current_ids & known

        state["known"].setdefault(peer_key, {})[group_key] = list(current_ids)

        new_posts = [posts_by_id[i] for i in new_ids if f"{group_id}_{i}" not in seen]
        old_posts = [posts_by_id[i] for i in old_ids if f"{group_id}_{i}" not in seen]

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


async def process_category(message: Message, category_name: str):
    peer_id = message.peer_id
    cat = CATEGORIES[category_name]
    peer_key = f"{category_name}:{peer_id}"
    lock_key = (category_name, peer_id)

    async with peer_locks[lock_key]:
        now = time.monotonic()
        last = last_request.get(lock_key, 0.0)
        elapsed = now - last

        if elapsed < COOLDOWN_SECONDS:
            remaining = COOLDOWN_SECONDS - elapsed
            print(f"[!] {peer_id} / {category_name}: кулдаун, осталось {remaining:.1f} сек.")
            await message.answer(
                f"⏳ По разделу «{cat['label']}» подожди {remaining:.0f} сек.",
                keyboard=make_keyboard(),
            )
            return

        last_request[lock_key] = now

        print(f"[→] {message.from_id}: «{cat['label']}» в чат {peer_id}")
        await message.answer(f"Ищу «{cat['label']}»...", keyboard=make_keyboard())

        for group_id in cat["group_ids"]:
            post, status = await get_candidate_for_group(group_id, peer_key)

            if post:
                attachment = f"wall{post.owner_id}_{post.id}"
                try:
                    await message.answer(attachment=attachment)
                    likes = post.likes.count if post.likes else 0
                    print(f"[←] {peer_id} / {category_name}: {attachment} (лайков {likes})")
                except Exception as e:
                    print(f"[ОШИБКА] Отправка {attachment}: {e}")
                    await message.answer(
                        f"Не удалось отправить пост из группы {group_id}",
                        keyboard=make_keyboard(),
                    )
            else:
                if status == "empty":
                    await message.answer(
                        f"«{cat['label']}» / группа {group_id}: Пусто "
                        f"(все посты уже показаны). Сбрось память кнопкой «Сброс».",
                        keyboard=make_keyboard(),
                    )
                elif status == "no_posts":
                    await message.answer(
                        f"«{cat['label']}» / группа {group_id}: Пусто (нет постов).",
                        keyboard=make_keyboard(),
                    )
                elif status == "error":
                    await message.answer(
                        f"«{cat['label']}» / группа {group_id}: Ошибка чтения.",
                        keyboard=make_keyboard(),
                    )

        print(f"[←] {peer_id} / {category_name}: обработка завершена")


# ================== ОБРАБОТЧИКИ ==================

@bot.on.message(text=ALL_TRIGGERS)
async def category_handler(message: Message):
    category_name = detect_category(message.text)
    if not category_name:
        return
    await process_category(message, category_name)


@bot.on.message(text=["Сброс", "сброс", "/reset", "/сброс", "СБРОС"])
async def reset_handler(message: Message):
    peer_id = message.peer_id

    async with state_lock:
        total_seen = 0
        total_groups = 0
        for category_name in CATEGORIES.keys():
            peer_key = f"{category_name}:{peer_id}"
            total_seen += len(state["seen"].pop(peer_key, []))
            total_groups += len(state["known"].pop(peer_key, {}))
        save_state()

    print(
        f"[↺] {peer_id}: сброс памяти по всем разделам "
        f"(удалено seen={total_seen}, known_groups={total_groups})"
    )
    await message.answer(
        "♻️ Память по всем разделам («Новости», «Аниме», «Фото») сброшена. "
        "Посты снова будут показываться по кругу.",
        keyboard=make_keyboard(),
    )


@bot.on.message(text=[
    "Помощь", "помощь", "/help", "/помощь", "Help", "help",
    "Начать", "начать", "/start", "Старт", "старт", "Меню", "меню",
])
async def help_handler(message: Message):
    print(f"[?] {message.from_id} запросил помощь в чате {message.peer_id}")
    await message.answer(HELP_TEXT, keyboard=make_keyboard())


# ВАЖНО: должен идти последним
@bot.on.message()
async def fallback_handler(message: Message):
    print(f"[?] {message.from_id}: неизвестная команда «{message.text}»")
    await message.answer(
        "Не знаю такую команду. Вот что я умею:\n\n" + HELP_TEXT,
        keyboard=make_keyboard(),
    )


if __name__ == "__main__":
    print("=" * 50)
    print("Бот запускается... (Ctrl+C для остановки)")
    print(f"Новости: {NEWS_GROUP_IDS}")
    print(f"Аниме:   {ANIME_GROUP_IDS}")
    print(f"Фото:    {PHOTO_GROUP_IDS}")
    print(f"Кулдаун внутри одного раздела: {COOLDOWN_SECONDS:.0f} сек.")
    print("=" * 50)
    bot.run_forever()
