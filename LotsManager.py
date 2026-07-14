from __future__ import annotations

from html import escape
from math import isfinite
from os import replace
from os.path import exists
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import SimpleNamespace
from typing import TYPE_CHECKING

import telebot
import threading

if TYPE_CHECKING:
    from cardinal import Cardinal

import FunPayAPI.types
from FunPayAPI.account import Account
from logging import getLogger
from telebot.types import Message
from tg_bot import static_keyboards as skb
import time
import json


NAME = "LotsManager"
VERSION = "1.0.0"
DESCRIPTION = "Плагин для копирования и управления лотами через inline кнопки."
CREDITS = "@woopertail, @sidor0912, @voterol (gpt5.6-sol)"
UUID = "5693f220-bcc6-4f6e-9745-9dee8664cbb2"
SETTINGS_PAGE = False


logger = getLogger("FPC.lots_copy_plugin")
RUNNING = False
LOTS_CACHE_TTL = 30


# Callback'и для копирования
CBT_COPY_LOTS = "lots_copy_plugin.copy"
CBT_CREATE_LOTS = "lots_copy_plugin.create"

# Callback'и для управления лотами
CB_LOT_VIEW = "ml_view"
CB_LOT_EDIT_PRICE = "ml_edit_price"
CB_LOT_EDIT_TITLE = "ml_edit_title"
CB_LOT_EDIT_DESC = "ml_edit_desc"
CB_LOT_TOGGLE = "ml_toggle"
CB_LOT_DELETE = "ml_delete"
CB_LOT_DELETE_CONFIRM = "ml_delete_confirm"
CB_EXPORT_MENU = "ml_export"
CB_EXPORT_JSON = "ml_exp_json"
CB_EXPORT_CSV = "ml_exp_csv"
CB_EXPORT_TXT = "ml_exp_txt"
CB_BACK_TO_LIST = "ml_back_list"
CB_PAGE = "ml_page"
CB_BULK_MENU = "ml_bulk_menu"
CB_BULK_ACTION = "ml_bulk_action"
CB_BULK_DELETE_STEP = "ml_bulk_del_step"
CB_ADD_LOTS = "ml_add_lots"
CB_SELECT_MODE = "ml_select_mode"
CB_SELECT_TOGGLE = "ml_select_toggle"
CB_SELECT_ALL_PAGE = "ml_select_all_page"
CB_SELECT_CLEAR = "ml_select_clear"
CB_SELECTED_MENU = "ml_selected_menu"
CB_SELECTED_ACTION = "ml_selected_action"
CB_SELECTED_DELETE_STEP = "ml_selected_del_step"
CB_SELECT_CANCEL = "ml_select_cancel"
CB_SUBCAT_MENU = "ml_subcat_menu"
CB_SUBCAT_ADD = "ml_subcat_add"
CB_SUBCAT_REMOVE = "ml_subcat_remove"
CB_SUBCAT_CLEAR_STEP = "ml_subcat_clear_step"
CB_SUBCAT_DISCOVERED_TOGGLE = "ml_subcat_discovered_toggle"
CB_IMPORT_MODE = "ml_import_mode"
CB_IMPORT_CANCEL = "ml_import_cancel"
CB_MENU_MAIN = "mm_main"
CB_MENU_LOTS = "mm_lots"
CB_MENU_TRANSFER = "mm_transfer"
CB_MENU_SETTINGS = "mm_settings"
CB_MENU_HISTORY = "mm_history"
CB_MENU_ACTION = "mm_action"
CB_MENU_HISTORY_CLEAR = "mm_history_clear"
CB_CACHE_MODE = "ml_cache_mode"

CBT_EDIT_LOT_PRICE = "manage_lots.edit_price"
CBT_EDIT_LOT_TITLE_RU = "manage_lots.edit_title_ru"
CBT_EDIT_LOT_TITLE_EN = "manage_lots.edit_title_en"
CBT_EDIT_LOT_TITLE_EN_AFTER_RU = "manage_lots.edit_title_en_after_ru"
CBT_EDIT_LOT_TITLE_RU_AFTER_EN = "manage_lots.edit_title_ru_after_en"
CBT_EDIT_LOT_DESC = "manage_lots.edit_desc"
CBT_EDIT_LOT_DESC_EN = "manage_lots.edit_desc_en"
CBT_ADD_SUBCATEGORY_ID = "manage_lots.add_subcategory_id"

# Файл для хранения отключенных лотов
DISABLED_LOTS_FILE = "storage/plugins/disabled_lots.json"

# Файл для хранения тегов лотов
LOT_TAGS_FILE = "storage/plugins/lot_tags.json"
SETTINGS_FILE = "storage/plugins/copy_lots_settings.json"
CACHE_DIR = Path("storage/cache")

# Лимиты FunPay
LIMITS = {
    "title_min": 5,
    "title_max": 100,
    "desc_max": 5000,
    "price_min": 0.01,
    "price_max": 999999.99
}

settings = {
    "with_secrets": False,
    "lot_search_subcategory_ids": [],
    "lot_search_use_discovered_ids": True
}

user_data = {}
lots_cache = {
    "items": None,
    "expires_at": 0.0
}


def html_text(value: object) -> str:
    """Escape untrusted text before embedding it in Telegram HTML."""
    return escape(str(value), quote=True)


def sanitize_lot_fields(fields: dict, *, include_delivery_secrets: bool = False) -> dict:
    """Return a transfer-safe copy without mutating FunPay API objects."""
    sanitized = dict(fields)
    for field_name in ("csrf_token", "golden_key", "offer_id"):
        sanitized.pop(field_name, None)
    if not include_delivery_secrets:
        sanitized.pop("secrets", None)
        sanitized.pop("auto_delivery", None)
    return sanitized


def parse_optional_bool(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "да", "on"}:
            return True
        if normalized in {"0", "false", "no", "нет", "off", ""}:
            return False
    raise ValueError("некорректное логическое значение")


def validate_price_value(price_str: str):
    try:
        price = float(str(price_str).strip().replace(",", "."))
    except (TypeError, ValueError):
        return False, "❌ Неверный формат цены. Используйте число (например: 100 или 99.99)", 0
    if not isfinite(price):
        return False, "❌ Цена должна быть конечным числом.", 0
    if price < LIMITS["price_min"]:
        return False, f"❌ Минимальная цена: {LIMITS['price_min']}", 0
    if price > LIMITS["price_max"]:
        return False, f"❌ Максимальная цена: {LIMITS['price_max']}", 0
    return True, "", price


def atomic_write_json(file_path: str | Path, payload) -> None:
    """Write JSON atomically so interrupted writes keep the previous file valid."""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temp_file:
            json.dump(payload, temp_file, indent=4, ensure_ascii=False)
            temp_file.flush()
            temp_path = Path(temp_file.name)
        replace(temp_path, path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def get_user_state(user_id: int):
    if user_id not in user_data:
        user_data[user_id] = {}
    return user_data[user_id]


def is_user_busy(user_id: int) -> bool:
    return bool(get_user_state(user_id).get("busy"))


def set_user_busy(user_id: int):
    get_user_state(user_id)["busy"] = True


def clear_user_busy(user_id: int):
    get_user_state(user_id)["busy"] = False


def is_shared_operation_running() -> bool:
    return RUNNING


def set_shared_operation_running(value: bool):
    global RUNNING
    RUNNING = value


def get_selected_lot_ids(user_id: int) -> list[int]:
    return get_user_state(user_id).setdefault("selected_lot_ids", [])


def set_selection_mode(user_id: int, enabled: bool):
    get_user_state(user_id)["selection_mode"] = enabled


def is_selection_mode(user_id: int) -> bool:
    return bool(get_user_state(user_id).get("selection_mode"))


def toggle_selected_lot(user_id: int, lot_id: int):
    selected = get_selected_lot_ids(user_id)
    if lot_id in selected:
        selected.remove(lot_id)
    else:
        selected.append(lot_id)
    return selected


def clear_selected_lots(user_id: int):
    state = get_user_state(user_id)
    state["selected_lot_ids"] = []
    state["selection_mode"] = False


def get_pending_import(user_id: int):
    return get_user_state(user_id).get("pending_lots_import")


def set_pending_import(user_id: int, payload: dict):
    get_user_state(user_id)["pending_lots_import"] = payload


def clear_pending_import(user_id: int):
    get_user_state(user_id).pop("pending_lots_import", None)

def download_file(tg, msg: Message, file_name: str = "temp_file.txt"):
    """
    Скачивает выгруженный файл и сохраняет его в папку storage/cache/.

    :param tg: экземпляр TG бота.
    :param msg: экземпляр сообщения.
    :param file_name: название сохраненного файла.
    """
    tg.bot.send_message(msg.chat.id, "⏬ Загружаю файл...")
    try:
        file_info = tg.bot.get_file(msg.document.file_id)
        file = tg.bot.download_file(file_info.file_path)
    except:
        tg.bot.send_message(msg.chat.id, "❌ Произошла ошибка при загрузке файла.")
        logger.debug("TRACEBACK", exc_info=True)
        raise Exception

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / file_name
    with path.open("wb") as new_file:
        new_file.write(file)
    return path


def init_commands(cardinal: Cardinal):
    if not cardinal.telegram:
        return
    tg = cardinal.telegram
    bot = cardinal.telegram.bot

    def format_seconds(seconds: float) -> str:
        seconds = max(int(seconds), 0)
        minutes, sec = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{sec:02d}"
        return f"{minutes:02d}:{sec:02d}"

    def build_progress_text(title: str, started_at: float, current: int | None = None, total: int | None = None,
                            status: str | None = None, failed: int = 0) -> str:
        elapsed = time.time() - started_at
        lines = [f"⏳ {title}", ""]

        if total and total > 0:
            current = max(0, min(current or 0, total))
            percent = int((current / total) * 100)
            filled = min(10, int((current / total) * 10))
            bar = "█" * filled + "░" * (10 - filled)
            lines.append(f"[{bar}] {percent}%")
            lines.append(f"{current} / {total}")
            if failed:
                lines.append(f"Ошибок: {failed}")
            if current > 0 and current < total:
                eta = (elapsed / current) * (total - current)
                lines.append(f"Осталось: ~{format_seconds(eta)}")
        else:
            lines.append("[░░░░░░░░░░] ...")
            lines.append(f"Прошло: {format_seconds(elapsed)}")
            if failed:
                lines.append(f"Ошибок: {failed}")

        if status:
            lines.extend(["", status])

        return "\n".join(lines)

    def create_progress_tracker(chat_id: int, title: str, total: int | None = None, status: str | None = None):
        tracker = {
            "chat_id": chat_id,
            "title": title,
            "total": total,
            "current": 0,
            "failed": 0,
            "status": status,
            "started_at": time.time(),
            "message_id": None,
            "last_rendered_text": None,
            "last_edit_at": 0.0,
            "done": False,
            "lock": threading.Lock()
        }

        def show_later():
            with tracker["lock"]:
                if tracker["done"] or tracker["message_id"] is not None:
                    return
                text = build_progress_text(
                    tracker["title"],
                    tracker["started_at"],
                    tracker["current"],
                    tracker["total"],
                    tracker["status"],
                    tracker["failed"]
                )
            try:
                msg = bot.send_message(chat_id, text)
                with tracker["lock"]:
                    tracker["message_id"] = msg.message_id
                    tracker["last_rendered_text"] = text
                    tracker["last_edit_at"] = time.time()
            except Exception:
                logger.debug("TRACEBACK", exc_info=True)

        tracker["timer"] = threading.Timer(3.0, show_later)
        tracker["timer"].daemon = True
        tracker["timer"].start()
        return tracker

    def update_progress_tracker(tracker, current: int | None = None, total: int | None = None,
                                status: str | None = None, failed: int | None = None, force: bool = False):
        now = time.time()
        with tracker["lock"]:
            if tracker["done"]:
                return
            if current is not None:
                tracker["current"] = current
            if total is not None:
                tracker["total"] = total
            if status is not None:
                tracker["status"] = status
            if failed is not None:
                tracker["failed"] = failed

            message_id = tracker["message_id"]
            if message_id is None:
                return

            text = build_progress_text(
                tracker["title"],
                tracker["started_at"],
                tracker["current"],
                tracker["total"],
                tracker["status"],
                tracker["failed"]
            )
            if not force and text == tracker["last_rendered_text"]:
                return
            if not force and now - tracker["last_edit_at"] < 1.0:
                return

        try:
            bot.edit_message_text(text, tracker["chat_id"], message_id)
            with tracker["lock"]:
                tracker["last_rendered_text"] = text
                tracker["last_edit_at"] = time.time()
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)

    def finish_progress_tracker(tracker, final_text: str | None = None):
        with tracker["lock"]:
            tracker["done"] = True
            timer = tracker.get("timer")
            message_id = tracker["message_id"]
        if timer:
            timer.cancel()
        if message_id is None:
            return
        text = final_text or build_progress_text(
            tracker["title"],
            tracker["started_at"],
            tracker["current"],
            tracker["total"],
            tracker["status"],
            tracker["failed"]
        )
        try:
            bot.edit_message_text(text, tracker["chat_id"], message_id)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)

    # ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
    
    def load_disabled_lots():
        """Загружает список отключенных лотов из файла."""
        try:
            if exists(DISABLED_LOTS_FILE):
                with open(DISABLED_LOTS_FILE, "r", encoding="utf-8") as f:
                    return json.loads(f.read())
            return {}
        except:
            logger.error("[LOTS] Ошибка при загрузке списка отключенных лотов.")
            logger.debug("TRACEBACK", exc_info=True)
            return {}
    
    def save_disabled_lots(disabled_lots):
        """Сохраняет список отключенных лотов в файл."""
        try:
            atomic_write_json(DISABLED_LOTS_FILE, disabled_lots)
            logger.info("[LOTS] Список отключенных лотов сохранен.")
            return True
        except:
            logger.error("[LOTS] Ошибка при сохранении списка отключенных лотов.")
            logger.debug("TRACEBACK", exc_info=True)
            return False
    
    def load_lot_tags():
        """Загружает теги лотов из файла."""
        try:
            if exists(LOT_TAGS_FILE):
                with open(LOT_TAGS_FILE, "r", encoding="utf-8") as f:
                    return json.loads(f.read())
            return {}
        except:
            logger.error("[LOTS] Ошибка при загрузке тегов лотов.")
            logger.debug("TRACEBACK", exc_info=True)
            return {}
    
    def save_lot_tags(lot_tags):
        """Сохраняет теги лотов в файл."""
        try:
            atomic_write_json(LOT_TAGS_FILE, lot_tags)
            logger.info("[LOTS] Теги лотов сохранены.")
            return True
        except:
            logger.error("[LOTS] Ошибка при сохранении тегов лотов.")
            logger.debug("TRACEBACK", exc_info=True)
            return False
    
    def generate_tag_name(title: str, existing_tags: dict) -> str:
        """Генерирует уникальное имя тега на основе названия лота."""
        import re
        import random
        import string
        
        # Берем первые слова из названия (без эмодзи)
        clean_title = re.sub(r'[^\w\s]', '', title)
        words = clean_title.split()[:3]
        base_tag = '_'.join(words).lower()
        
        # Если пустой, генерируем случайный
        if not base_tag:
            base_tag = 'lot'
        
        # Добавляем случайные символы для уникальности
        tag = f"${base_tag}_{''.join(random.choices(string.ascii_lowercase + string.digits, k=4))}"
        
        # Проверяем уникальность
        counter = 1
        original_tag = tag
        while tag in [t['tag'] for t in existing_tags.values()]:
            tag = f"{original_tag}_{counter}"
            counter += 1
        
        return tag
    
    def get_lot_tag(lot_id: int, lot_tags: dict) -> str:
        """Получает тег лота по ID."""
        lot_id_str = str(lot_id)
        if lot_id_str in lot_tags:
            return lot_tags[lot_id_str]['tag']
        return None
    
    def find_lot_by_tag(tag: str, lot_tags: dict) -> int:
        """Находит ID лота по тегу."""
        for lot_id, data in lot_tags.items():
            if data['tag'] == tag:
                return int(lot_id)
        return None
    
    def add_disabled_lot(lot_id: int, lot_info: dict):
        """Добавляет лот в список отключенных."""
        disabled_lots = load_disabled_lots()
        disabled_lots[str(lot_id)] = {
            "id": lot_id,
            "title": lot_info.get("title", ""),
            "price": lot_info.get("price", ""),
            "disabled_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            **lot_info
        }
        save_disabled_lots(disabled_lots)
    
    def remove_disabled_lot(lot_id: int):
        """Удаляет лот из списка отключенных."""
        disabled_lots = load_disabled_lots()
        if str(lot_id) in disabled_lots:
            del disabled_lots[str(lot_id)]
            save_disabled_lots(disabled_lots)
    
    def delete_lot_from_funpay(lot_id: int, chat_id: int = None):
        """Удаляет лот с FunPay."""
        attempts = 3
        while attempts:
            try:
                cardinal.account.delete_lot(lot_id)
                invalidate_lots_cache()
                logger.info(f"[LOTS] Лот {lot_id} успешно удален.")
                return True
            except Exception as e:
                logger.error(f"[LOTS] Не удалось удалить лот {lot_id}. Ошибка: {str(e)}")
                logger.exception("TRACEBACK:")
                time.sleep(1)
                attempts -= 1
        else:
            if chat_id:
                bot.send_message(chat_id, f"❌ Не удалось удалить лот {lot_id}. Проверьте логи.")
            return False
    
    def invalidate_lots_cache():
        lots_cache["items"] = None
        lots_cache["expires_at"] = 0.0

    def save_settings():
        atomic_write_json(SETTINGS_FILE, settings)

    def get_configured_subcategory_ids() -> list[int]:
        raw_ids = settings.get("lot_search_subcategory_ids", [])
        if isinstance(raw_ids, str):
            raw_ids = raw_ids.replace("\n", ",").split(",")
        if not isinstance(raw_ids, (list, tuple, set)):
            raw_ids = [raw_ids]

        result = []
        seen = set()
        for value in raw_ids:
            if value in (None, ""):
                continue
            try:
                subcategory_id = int(str(value).strip())
            except (TypeError, ValueError):
                logger.warning(f"[LOTS] Пропущен некорректный ID подкатегории в настройках: {value}")
                continue
            if subcategory_id in seen:
                continue
            seen.add(subcategory_id)
            result.append(subcategory_id)
        return result

    def set_configured_subcategory_ids(ids: list[int]):
        clean_ids = []
        seen = set()
        for subcategory_id in ids:
            try:
                subcategory_id = int(subcategory_id)
            except (TypeError, ValueError):
                continue
            if subcategory_id in seen:
                continue
            seen.add(subcategory_id)
            clean_ids.append(subcategory_id)
        settings["lot_search_subcategory_ids"] = clean_ids
        save_settings()
        invalidate_lots_cache()

    def add_configured_subcategory_ids(ids: list[int]) -> list[int]:
        current_ids = get_configured_subcategory_ids()
        added = []
        seen = set(current_ids)
        for subcategory_id in ids:
            if subcategory_id in seen:
                continue
            current_ids.append(subcategory_id)
            seen.add(subcategory_id)
            added.append(subcategory_id)
        if added:
            set_configured_subcategory_ids(current_ids)
        return added

    def remove_configured_subcategory_id(subcategory_id: int) -> bool:
        current_ids = get_configured_subcategory_ids()
        if subcategory_id not in current_ids:
            return False
        current_ids.remove(subcategory_id)
        set_configured_subcategory_ids(current_ids)
        return True

    def toggle_discovered_subcategory_ids() -> bool:
        settings["lot_search_use_discovered_ids"] = not settings.get("lot_search_use_discovered_ids", True)
        save_settings()
        invalidate_lots_cache()
        return settings["lot_search_use_discovered_ids"]

    def get_subcategory_display_name(subcategory_id: int) -> str:
        try:
            subcategory = cardinal.account.get_sorted_subcategories()[FunPayAPI.types.SubCategoryTypes.COMMON].get(subcategory_id)
            if subcategory:
                return subcategory.ui_name
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)
        return f"ID {subcategory_id}"

    def format_subcategory_line(subcategory_id: int) -> str:
        name = get_subcategory_display_name(subcategory_id)
        return f"• <code>{subcategory_id}</code> — {name}"

    def format_subcategory_button_text(subcategory_id: int) -> str:
        name = get_subcategory_display_name(subcategory_id)
        text = f"➖ {subcategory_id} · {name}"
        return text if len(text) <= 60 else text[:57] + "..."

    def parse_subcategory_ids_input(text: str):
        payload = text.replace("\n", " ").replace(",", " ")
        parts = [part.strip() for part in payload.split() if part.strip()]
        parsed_ids = []
        invalid_parts = []

        for part in parts:
            try:
                parsed_ids.append(int(part))
            except ValueError:
                invalid_parts.append(part)

        return parsed_ids, invalid_parts

    def render_subcategory_settings_text() -> str:
        configured_ids = get_configured_subcategory_ids()
        discovered_text = "🟢 Вкл" if settings.get("lot_search_use_discovered_ids", True) else "🔴 Выкл"
        if configured_ids:
            configured_text = "\n".join(format_subcategory_line(i) for i in configured_ids)
        else:
            configured_text = "— нет"
        return (
            "🗂️ <b>Подкатегории для поиска лотов</b>\n\n"
            f"<b>ID из настроек:</b>\n{configured_text}\n\n"
            f"<b>Автопоиск по найденным лотам:</b> {discovered_text}\n\n"
            "Добавляй ID вручную, если надо искать лоты только в нужных подкатегориях."
        )

    def create_subcategory_settings_keyboard():
        keyboard = telebot.types.InlineKeyboardMarkup(row_width=1)
        keyboard.row(
            telebot.types.InlineKeyboardButton("➕ Добавить ID", callback_data=CB_SUBCAT_ADD),
            telebot.types.InlineKeyboardButton(
                f"🔄 Автопоиск: {'Вкл' if settings.get('lot_search_use_discovered_ids', True) else 'Выкл'}",
                callback_data=CB_SUBCAT_DISCOVERED_TOGGLE
            )
        )

        configured_ids = get_configured_subcategory_ids()
        for subcategory_id in configured_ids[:10]:
            keyboard.row(
                telebot.types.InlineKeyboardButton(
                    format_subcategory_button_text(subcategory_id),
                    callback_data=f"{CB_SUBCAT_REMOVE}:{subcategory_id}"
                )
            )
        if len(configured_ids) > 10:
            keyboard.row(telebot.types.InlineKeyboardButton(
                f"…ещё ID: {len(configured_ids) - 10}", callback_data="noop"
            ))

        keyboard.row(telebot.types.InlineKeyboardButton("🧹 Очистить все ID", callback_data=f"{CB_SUBCAT_CLEAR_STEP}:0"))
        keyboard.row(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data=CB_BACK_TO_LIST))
        return keyboard

    def get_relevant_subcategory_ids(chat_id: int = None):
        """Возвращает подкатегории из настроек и/или автообнаружения по лотам аккаунта."""
        attempts = 3
        while attempts:
            try:
                subcategory_ids = set(get_configured_subcategory_ids())

                if settings.get("lot_search_use_discovered_ids", True):
                    profile = cardinal.account.get_user(cardinal.account.id)

                    for lot in profile.get_lots():
                        if lot.subcategory.type == FunPayAPI.types.SubCategoryTypes.CURRENCY:
                            continue
                        subcategory_ids.add(lot.subcategory.id)

                    for lot_info in load_disabled_lots().values():
                        category_id = lot_info.get("category")
                        if category_id in (None, ""):
                            continue
                        try:
                            subcategory_ids.add(int(category_id))
                        except (TypeError, ValueError):
                            continue

                return sorted(subcategory_ids)
            except Exception as e:
                logger.error(f"[LOTS] Не удалось получить список моих подкатегорий. Ошибка: {str(e)}")
                logger.exception("TRACEBACK:")
                time.sleep(1)
                attempts -= 1

        if chat_id:
            bot.send_message(chat_id, "❌ Не удалось получить список ваших подкатегорий. Проверьте логи Cardinal.")
        raise Exception("Failed to get relevant subcategories after 3 attempts")

    def get_all_lots(chat_id: int = None, force_refresh: bool = False, progress_tracker=None, progress_status: str | None = None):
        """Получает все обычные лоты текущего аккаунта, включая деактивированные."""
        now = time.time()
        if not force_refresh and lots_cache["items"] is not None and lots_cache["expires_at"] > now:
            return list(lots_cache["items"])

        attempts = 3
        while attempts:
            try:
                relevant_subcategory_ids = get_relevant_subcategory_ids(chat_id)
                if not relevant_subcategory_ids:
                    lots_cache["items"] = []
                    lots_cache["expires_at"] = time.time() + LOTS_CACHE_TTL
                    if progress_tracker:
                        update_progress_tracker(progress_tracker, current=0, total=0,
                                                status="Лоты не найдены", force=True)
                    return []

                lots_map = {}
                total_subcategories = len(relevant_subcategory_ids)
                for index, subcategory_id in enumerate(relevant_subcategory_ids, start=1):
                    try:
                        sub_lots = cardinal.account.get_my_subcategory_lots(subcategory_id)
                        for lot in sub_lots:
                            lots_map[lot.id] = lot
                    except Exception as e:
                        logger.warning(f"[LOTS] Не удалось получить лоты подкатегории {subcategory_id}: {str(e)}")
                    if progress_tracker:
                        update_progress_tracker(
                            progress_tracker,
                            current=index,
                            total=total_subcategories,
                            status=progress_status or f"Сканирую категории: {index}/{total_subcategories}",
                            force=index == total_subcategories
                        )

                lots = list(lots_map.values())
                lots.sort(key=lambda x: (not x.active, (x.description or '').lower(), x.id))
                lots_cache["items"] = list(lots)
                lots_cache["expires_at"] = time.time() + LOTS_CACHE_TTL
                active_count = sum(1 for l in lots if l.active)
                logger.info(f"[LOTS] Получено {len(lots)} лотов. Активных: {active_count}, неактивных: {len(lots) - active_count}")
                return lots
            except Exception as e:
                logger.error(f"[LOTS] Не удалось получить список лотов. Ошибка: {str(e)}")
                logger.exception("TRACEBACK:")
                time.sleep(1)
                attempts -= 1
        else:
            if chat_id:
                bot.send_message(chat_id, "❌ Не удалось получить список лотов. Проверьте логи Cardinal.")
            raise Exception("Failed to get lots after 3 attempts")

    def get_lot_fields_by_id(lot_id: int, chat_id: int = None):
        """Получает детальную информацию о лоте."""
        attempts = 3
        while attempts:
            try:
                lot_fields = cardinal.account.get_lot_fields(lot_id)
                logger.info(f"[LOTS] Получены данные о лоте {lot_id}.")
                return lot_fields
            except Exception as e:
                logger.error(f"[LOTS] Не удалось получить данные о лоте {lot_id}. Ошибка: {str(e)}")
                logger.exception("TRACEBACK:")
                time.sleep(1)
                attempts -= 1
        else:
            if chat_id:
                bot.send_message(chat_id, f"❌ Не удалось получить данные о лоте {lot_id}. Проверьте логи.")
            raise Exception(f"Failed to get lot {lot_id} after 3 attempts")

    def save_lot_changes(lot_fields, chat_id: int = None):
        """Сохраняет изменения лота на месте без удаления и пересоздания."""
        attempts = 3
        while attempts:
            try:
                cardinal.account.save_lot(lot_fields)
                invalidate_lots_cache()
                logger.info(f"[LOTS] Лот {lot_fields.lot_id} успешно обновлен на месте.")
                return True
            except Exception as e:
                logger.error(f"[LOTS] Не удалось сохранить лот {lot_fields.lot_id}. Ошибка: {str(e)}")
                logger.exception("TRACEBACK:")
                time.sleep(1)
                attempts -= 1
        if chat_id:
            bot.send_message(chat_id, f"❌ Не удалось сохранить изменения лота #{lot_fields.lot_id}. Проверьте логи.")
        return False

    def update_cached_lots_for_user(user_id: int, chat_id: int, progress_tracker=None, progress_status: str | None = None):
        """Обновляет кэш списка лотов для пользователя."""
        lots = get_all_lots(chat_id, force_refresh=True, progress_tracker=progress_tracker, progress_status=progress_status)
        state = get_user_state(user_id)
        state['lots'] = lots
        state['page'] = 0
        return lots
    
    def validate_price(price_str: str):
        return validate_price_value(price_str)
    
    def validate_title(title: str):
        if len(title) < LIMITS["title_min"]:
            return False, f"❌ Минимальная длина названия: {LIMITS['title_min']} символов"
        if len(title) > LIMITS["title_max"]:
            return False, f"❌ Максимальная длина названия: {LIMITS['title_max']} символов"
        return True, ""
    
    def validate_desc(desc: str):
        if len(desc) > LIMITS["desc_max"]:
            return False, f"❌ Максимальная длина описания: {LIMITS['desc_max']} символов"
        return True, ""

    def render_manage_lots_text(lots, prefix_text: str | None = None):
        text = ""
        if prefix_text:
            text += f"{prefix_text}\n\n"
        text += (
            f"📋 <b>Управление лотами</b>\n\nВсего лотов: {len(lots)}\n"
            f"Активных: {sum(1 for l in lots if l.active)}\n"
            f"Неактивных: {sum(1 for l in lots if not l.active)}\n\n"
            f"Выберите лот для управления\nили используйте массовые действия ниже."
        )
        return text

    def create_bulk_actions_keyboard():
        keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
        keyboard.row(
            telebot.types.InlineKeyboardButton("🔴 Скрыть все", callback_data=f"{CB_BULK_ACTION}:hide"),
            telebot.types.InlineKeyboardButton("🟢 Открыть все", callback_data=f"{CB_BULK_ACTION}:show")
        )
        keyboard.row(
            telebot.types.InlineKeyboardButton("🗑️ Удалить все", callback_data=f"{CB_BULK_DELETE_STEP}:0")
        )
        keyboard.row(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data=CB_BACK_TO_LIST))
        return keyboard

    def refresh_manage_lots_message(call, notice: str | None = None, page: int | None = None):
        user_id = call.from_user.id
        lots = update_cached_lots_for_user(user_id, call.message.chat.id)
        if not lots:
            clear_selected_lots(user_id)
            bot.edit_message_text(
                "📭 Лотов нет.\n\nНет ни активных, ни скрытых лотов.",
                call.message.chat.id,
                call.message.message_id
            )
            return lots
        if page is None:
            page = user_data[user_id].get('page', 0)
        max_page = max((len(lots) - 1) // 8, 0)
        page = min(page, max_page)
        user_data[user_id]['page'] = page
        keyboard = create_lots_keyboard(lots, page, selection_mode=is_selection_mode(user_id), selected_ids=get_selected_lot_ids(user_id))
        bot.edit_message_text(
            render_manage_lots_text(lots, notice),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        return lots

    def start_create_lots_flow(chat_id: int, user_id: int):
        clear_pending_import(user_id)
        result = bot.send_message(
            chat_id,
            "Отправьте мне файл с лотами, полученный с помощью команды /cache_lots.\n"
            "После загрузки я покажу, сколько ID уже найдено, и дам выбрать режим: создать новые / заменить существующие / смешанный.",
            reply_markup=skb.CLEAR_STATE_BTN()
        )
        tg.set_state(chat_id, result.id, user_id, CBT_CREATE_LOTS)
        return result

    def perform_bulk_toggle(chat_id: int, user_id: int, activate: bool, progress_tracker=None):
        lots = get_all_lots(chat_id, progress_tracker=progress_tracker, progress_status="Получаю список лотов")
        targets = [lot for lot in lots if lot.active != activate]
        if not targets:
            return 0, 0, [], lots

        success = 0
        failed = []
        total_targets = len(targets)
        for index, lot in enumerate(targets, start=1):
            try:
                lot_fields = get_lot_fields_by_id(lot.id, chat_id)
                lot_fields.active = activate
                if save_lot_changes(lot_fields, chat_id):
                    success += 1
                    if activate:
                        remove_disabled_lot(lot.id)
                    else:
                        add_disabled_lot(lot.id, {
                            "title": lot_fields.fields.get('fields[summary][ru]', lot_fields.fields.get('fields[summary][en]', lot.description or 'Без названия')),
                            "price": lot_fields.fields.get('price', str(lot.price)),
                            "description": lot_fields.fields.get('fields[desc][ru]', lot_fields.fields.get('fields[desc][en]', '')),
                            "category": lot_fields.fields.get('node_id', '')
                        })
                else:
                    failed.append(lot.id)
            except Exception:
                logger.debug("TRACEBACK", exc_info=True)
                failed.append(lot.id)
            if progress_tracker:
                update_progress_tracker(progress_tracker, current=index, total=total_targets,
                                        status=f"Обрабатываю лоты: {index}/{total_targets}", failed=len(failed),
                                        force=index == total_targets)

        if progress_tracker:
            update_progress_tracker(progress_tracker, total=total_targets, current=total_targets,
                                    status="Обновляю список после изменения", failed=len(failed), force=True)
        updated_lots = update_cached_lots_for_user(user_id, chat_id, progress_tracker=progress_tracker,
                                                   progress_status="Обновляю список после изменения")
        return len(targets), success, failed, updated_lots

    def perform_bulk_delete(chat_id: int, user_id: int, progress_tracker=None):
        lots = get_all_lots(chat_id, progress_tracker=progress_tracker, progress_status="Получаю список лотов")
        if not lots:
            return 0, 0, [], lots

        success = 0
        failed = []
        total_lots = len(lots)
        for index, lot in enumerate(lots, start=1):
            if delete_lot_from_funpay(lot.id, chat_id):
                success += 1
                remove_disabled_lot(lot.id)
            else:
                failed.append(lot.id)
            if progress_tracker:
                update_progress_tracker(progress_tracker, current=index, total=total_lots,
                                        status=f"Удаляю лоты: {index}/{total_lots}", failed=len(failed),
                                        force=index == total_lots)

        if progress_tracker:
            update_progress_tracker(progress_tracker, current=total_lots, total=total_lots,
                                    status="Обновляю список после удаления", failed=len(failed), force=True)
        updated_lots = update_cached_lots_for_user(user_id, chat_id, progress_tracker=progress_tracker,
                                                   progress_status="Обновляю список после удаления")
        return len(lots), success, failed, updated_lots

    def perform_selected_toggle(chat_id: int, user_id: int, activate: bool, progress_tracker=None):
        selected_ids = list(get_selected_lot_ids(user_id))
        if not selected_ids:
            return 0, 0, [], get_all_lots(chat_id)

        lots_map = {lot.id: lot for lot in get_all_lots(chat_id, progress_tracker=progress_tracker, progress_status="Получаю список лотов")}
        success = 0
        failed = []
        total = 0
        total_selected = len(selected_ids)
        for index, lot_id in enumerate(selected_ids, start=1):
            lot = lots_map.get(lot_id)
            if not lot:
                failed.append(lot_id)
                if progress_tracker:
                    update_progress_tracker(progress_tracker, current=index, total=total_selected,
                                            status=f"Проверяю отмеченные лоты: {index}/{total_selected}", failed=len(failed),
                                            force=index == total_selected)
                continue
            if lot.active == activate:
                if progress_tracker:
                    update_progress_tracker(progress_tracker, current=index, total=total_selected,
                                            status=f"Проверяю отмеченные лоты: {index}/{total_selected}", failed=len(failed),
                                            force=index == total_selected)
                continue
            total += 1
            try:
                lot_fields = get_lot_fields_by_id(lot_id, chat_id)
                lot_fields.active = activate
                if save_lot_changes(lot_fields, chat_id):
                    success += 1
                    if activate:
                        remove_disabled_lot(lot_id)
                    else:
                        add_disabled_lot(lot_id, {
                            "title": lot_fields.fields.get('fields[summary][ru]', lot_fields.fields.get('fields[summary][en]', lot.description or 'Без названия')),
                            "price": lot_fields.fields.get('price', str(lot.price)),
                            "description": lot_fields.fields.get('fields[desc][ru]', lot_fields.fields.get('fields[desc][en]', '')),
                            "category": lot_fields.fields.get('node_id', '')
                        })
                else:
                    failed.append(lot_id)
            except Exception:
                logger.debug("TRACEBACK", exc_info=True)
                failed.append(lot_id)
            if progress_tracker:
                update_progress_tracker(progress_tracker, current=index, total=total_selected,
                                        status=f"Обрабатываю отмеченные лоты: {index}/{total_selected}", failed=len(failed),
                                        force=index == total_selected)

        if progress_tracker:
            update_progress_tracker(progress_tracker, current=total_selected, total=total_selected,
                                    status="Обновляю список после изменения", failed=len(failed), force=True)
        updated_lots = update_cached_lots_for_user(user_id, chat_id, progress_tracker=progress_tracker,
                                                   progress_status="Обновляю список после изменения")
        clear_selected_lots(user_id)
        return total, success, failed, updated_lots

    def perform_selected_delete(chat_id: int, user_id: int, progress_tracker=None):
        selected_ids = list(get_selected_lot_ids(user_id))
        if not selected_ids:
            return 0, 0, [], get_all_lots(chat_id)

        success = 0
        failed = []
        total_selected = len(selected_ids)
        for index, lot_id in enumerate(selected_ids, start=1):
            if delete_lot_from_funpay(lot_id, chat_id):
                success += 1
                remove_disabled_lot(lot_id)
            else:
                failed.append(lot_id)
            if progress_tracker:
                update_progress_tracker(progress_tracker, current=index, total=total_selected,
                                        status=f"Удаляю отмеченные лоты: {index}/{total_selected}", failed=len(failed),
                                        force=index == total_selected)

        if progress_tracker:
            update_progress_tracker(progress_tracker, current=total_selected, total=total_selected,
                                    status="Обновляю список после удаления", failed=len(failed), force=True)
        updated_lots = update_cached_lots_for_user(user_id, chat_id, progress_tracker=progress_tracker,
                                                   progress_status="Обновляю список после удаления")
        clear_selected_lots(user_id)
        return len(selected_ids), success, failed, updated_lots
    
    def create_lots_keyboard(lots, page: int = 0, per_page: int = 8, selection_mode: bool = False, selected_ids: list[int] | None = None):
        keyboard = telebot.types.InlineKeyboardMarkup(row_width=1)
        selected_ids = selected_ids or []
        start_idx = page * per_page
        end_idx = start_idx + per_page
        page_lots = lots[start_idx:end_idx]
        
        for lot in page_lots:
            status = "🟢" if lot.active else "🔴"
            prefix = "✅" if lot.id in selected_ids else "⬜"
            text = f"{status} {lot.description[:40]}... | {lot.price} {lot.currency}"
            if selection_mode:
                keyboard.add(telebot.types.InlineKeyboardButton(f"{prefix} {text}", callback_data=f"{CB_SELECT_TOGGLE}:{lot.id}"))
            else:
                keyboard.add(telebot.types.InlineKeyboardButton(text, callback_data=f"{CB_LOT_VIEW}:{lot.id}"))
        
        nav_buttons = []
        if page > 0:
            nav_buttons.append(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data=f"{CB_PAGE}:{page-1}"))
        if end_idx < len(lots):
            nav_buttons.append(telebot.types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"{CB_PAGE}:{page+1}"))
        
        if nav_buttons:
            keyboard.row(*nav_buttons)
        
        if selection_mode:
            keyboard.row(
                telebot.types.InlineKeyboardButton("☑️ Выделить страницу", callback_data=CB_SELECT_ALL_PAGE),
                telebot.types.InlineKeyboardButton("🧹 Очистить", callback_data=CB_SELECT_CLEAR)
            )
            keyboard.row(
                telebot.types.InlineKeyboardButton(f"⚙️ К отмеченным ({len(selected_ids)})", callback_data=CB_SELECTED_MENU),
                telebot.types.InlineKeyboardButton("❌ Отмена", callback_data=CB_SELECT_CANCEL)
            )
        else:
            keyboard.row(
                telebot.types.InlineKeyboardButton("⚙️ Массово", callback_data=CB_BULK_MENU),
                telebot.types.InlineKeyboardButton("✅ Выбрать", callback_data=CB_SELECT_MODE)
            )
            keyboard.row(
                telebot.types.InlineKeyboardButton("➕ Добавить", callback_data=CB_ADD_LOTS),
                telebot.types.InlineKeyboardButton("🗂️ Подкатегории", callback_data=CB_SUBCAT_MENU)
            )
            keyboard.row(telebot.types.InlineKeyboardButton("🔄 Обновить", callback_data=f"{CB_PAGE}:{page}"))
        return keyboard
    
    def create_lot_view_keyboard(lot_id: int, is_active: bool):
        keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
        keyboard.row(
            telebot.types.InlineKeyboardButton("💰 Изменить цену", callback_data=f"{CB_LOT_EDIT_PRICE}:{lot_id}"),
            telebot.types.InlineKeyboardButton("✏️ Изменить название", callback_data=f"{CB_LOT_EDIT_TITLE}:{lot_id}")
        )
        keyboard.row(
            telebot.types.InlineKeyboardButton("📝 Изменить описание", callback_data=f"{CB_LOT_EDIT_DESC}:{lot_id}")
        )
        toggle_text = "🔴 Отключить лот" if is_active else "🟢 Включить лот"
        keyboard.row(telebot.types.InlineKeyboardButton(toggle_text, callback_data=f"{CB_LOT_TOGGLE}:{lot_id}"))
        keyboard.row(
            telebot.types.InlineKeyboardButton("🗑️ Удалить лот", callback_data=f"{CB_LOT_DELETE}:{lot_id}")
        )
        keyboard.row(telebot.types.InlineKeyboardButton("⬅️ К списку лотов", callback_data=CB_BACK_TO_LIST))
        return keyboard
    
    def create_export_keyboard():
        keyboard = telebot.types.InlineKeyboardMarkup(row_width=1)
        keyboard.add(
            telebot.types.InlineKeyboardButton("📄 JSON формат", callback_data=CB_EXPORT_JSON),
            telebot.types.InlineKeyboardButton("📊 CSV формат (Excel)", callback_data=CB_EXPORT_CSV),
            telebot.types.InlineKeyboardButton("📝 Текстовый формат", callback_data=CB_EXPORT_TXT),
            telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data=CB_BACK_TO_LIST)
        )
        return keyboard

    def build_fake_message(chat_id: int, user_id: int, text: str = ""):
        return SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            from_user=SimpleNamespace(id=user_id),
            text=text,
            document=None
        )

    def render_manage_menu_text(section: str = "main") -> str:
        secrets_enabled = settings.get("with_secrets")
        texts = {
            "main": (
                "🧩 <b>Главное меню управления</b>\n\n"
                "Отсюда можно открыть все основные разделы плагина:\n"
                "• лоты и массовые действия\n"
                "• импорт / экспорт\n"
                "• настройки и теги\n"
                "• историю отключенных лотов"
            ),
            "lots": (
                "📦 <b>Раздел: Лоты</b>\n\n"
                "• открыть список и управлять лотами\n"
                "• загрузить лоты из JSON\n"
                "• настроить подкатегории поиска"
            ),
            "transfer": (
                "📥 <b>Раздел: Импорт / экспорт</b>\n\n"
                "• выгрузить лоты в JSON\n"
                "• загрузить лоты из JSON\n"
                "• скопировать лоты на другой аккаунт"
            ),
            "settings": (
                "⚙️ <b>Раздел: Настройки</b>\n\n"
                "• подкатегории поиска лотов\n"
                "• теги лотов\n"
                f"• автовыдача при копировании: {'🟢 включена' if secrets_enabled else '🔴 выключена'}"
            ),
            "history": (
                "🕘 <b>Раздел: История</b>\n\n"
                "• посмотреть отключенные лоты\n"
                "• очистить историю отключений"
            )
        }
        return texts.get(section, texts["main"])

    def create_manage_menu_keyboard(section: str = "main"):
        keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
        if section == "main":
            keyboard.row(
                telebot.types.InlineKeyboardButton("📦 Лоты", callback_data=CB_MENU_LOTS),
                telebot.types.InlineKeyboardButton("📥 Импорт / экспорт", callback_data=CB_MENU_TRANSFER)
            )
            keyboard.row(
                telebot.types.InlineKeyboardButton("⚙️ Настройки", callback_data=CB_MENU_SETTINGS),
                telebot.types.InlineKeyboardButton("🕘 История", callback_data=CB_MENU_HISTORY)
            )
            keyboard.row(telebot.types.InlineKeyboardButton("🔄 Обновить", callback_data=CB_MENU_MAIN))
            return keyboard

        if section == "lots":
            keyboard.row(
                telebot.types.InlineKeyboardButton("📋 Открыть список лотов", callback_data=f"{CB_MENU_ACTION}:manage_lots")
            )
            keyboard.row(
                telebot.types.InlineKeyboardButton("➕ Создать из файла", callback_data=f"{CB_MENU_ACTION}:create_lots"),
                telebot.types.InlineKeyboardButton("🗂️ Подкатегории", callback_data=CB_SUBCAT_MENU)
            )
        elif section == "transfer":
            keyboard.row(
                telebot.types.InlineKeyboardButton("💾 Кэшировать в JSON", callback_data=f"{CB_MENU_ACTION}:cache_lots"),
                telebot.types.InlineKeyboardButton("➕ Создать из JSON", callback_data=f"{CB_MENU_ACTION}:create_lots")
            )
            keyboard.row(
                telebot.types.InlineKeyboardButton("📤 Копировать на другой аккаунт", callback_data=f"{CB_MENU_ACTION}:copy_lots")
            )
        elif section == "settings":
            keyboard.row(
                telebot.types.InlineKeyboardButton("🗂️ Подкатегории лотов", callback_data=CB_SUBCAT_MENU),
                telebot.types.InlineKeyboardButton("🏷️ Теги лотов", callback_data=f"{CB_MENU_ACTION}:lots_tags")
            )
            keyboard.row(
                telebot.types.InlineKeyboardButton(
                    f"🔐 Автовыдача: {'🟢 Вкл' if settings.get('with_secrets') else '🔴 Выкл'}",
                    callback_data=f"{CB_MENU_ACTION}:copy_with_secrets"
                ),
                telebot.types.InlineKeyboardButton("❓ Справка по тегам", callback_data=f"{CB_MENU_ACTION}:tags_help")
            )
        elif section == "history":
            keyboard.row(
                telebot.types.InlineKeyboardButton("📄 Показать историю", callback_data=f"{CB_MENU_ACTION}:disabled_lots")
            )
            keyboard.row(
                telebot.types.InlineKeyboardButton("🧹 Очистить историю", callback_data=f"{CB_MENU_HISTORY_CLEAR}:0")
            )

        keyboard.row(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data=CB_MENU_MAIN))
        return keyboard

    def sanitize_lot_fields_for_transfer(fields: dict, *, include_delivery_secrets: bool = False) -> dict:
        return sanitize_lot_fields(fields, include_delivery_secrets=include_delivery_secrets)

    def build_export_lot_entry(lot_fields: FunPayAPI.types.LotFields) -> dict:
        # Files sent through Telegram must never contain delivery secrets.
        fields = sanitize_lot_fields_for_transfer(lot_fields.fields, include_delivery_secrets=False)
        node_id = fields.get("node_id")
        try:
            node_id = int(node_id) if node_id not in (None, "") else None
        except (TypeError, ValueError):
            node_id = None
        return {
            "schema_version": 2,
            "source_lot_id": lot_fields.lot_id,
            "exported_at": int(time.time()),
            "node_id": node_id,
            "active": bool(lot_fields.active),
            "fields": fields
        }

    def parse_import_lot_entry(item) -> tuple[dict | None, str | None]:
        if not isinstance(item, dict):
            return None, "элемент не является объектом"

        if isinstance(item.get("fields"), dict):
            fields = sanitize_lot_fields_for_transfer(item["fields"])
            source_lot_id = item.get("source_lot_id")
            node_id = item.get("node_id", fields.get("node_id"))
            active = item.get("active")
        else:
            fields = sanitize_lot_fields_for_transfer(item)
            source_lot_id = None
            node_id = fields.get("node_id")
            active = fields.get("active")

        if not isinstance(fields, dict) or not fields:
            return None, "нет данных лота"

        try:
            source_lot_id = int(source_lot_id) if source_lot_id not in (None, "") else None
        except (TypeError, ValueError):
            source_lot_id = None

        try:
            node_id = int(node_id) if node_id not in (None, "") else None
        except (TypeError, ValueError):
            node_id = None

        try:
            active = parse_optional_bool(active)
        except ValueError:
            return None, "некорректное значение active"

        if node_id is None or node_id <= 0:
            return None, "не указан корректный ID подкатегории"

        price = fields.get("price")
        if price not in (None, ""):
            valid, error, _ = validate_price_value(price)
            if not valid:
                return None, error.replace("❌ ", "")

        return {
            "source_lot_id": source_lot_id,
            "node_id": node_id,
            "active": active,
            "fields": fields,
            "title": fields.get('fields[summary][ru]', fields.get('fields[summary][en]', 'Без названия'))
        }, None

    def get_existing_lots_index(chat_id: int) -> dict[int, dict]:
        lots = get_all_lots(chat_id)
        index = {}
        for lot in lots:
            index[lot.id] = {
                "lot": lot,
                "node_id": getattr(getattr(lot, "subcategory", None), "id", None)
            }
        return index

    def analyze_import_entries(entries: list[dict], existing_index: dict[int, dict]):
        matched = 0
        unmatched = 0
        node_conflicts = 0
        duplicate_source_ids = 0
        seen_source_ids = set()

        for entry in entries:
            source_lot_id = entry.get("source_lot_id")
            if source_lot_id is None:
                unmatched += 1
                continue
            if source_lot_id in seen_source_ids:
                duplicate_source_ids += 1
            else:
                seen_source_ids.add(source_lot_id)
            existing = existing_index.get(source_lot_id)
            if existing:
                matched += 1
                existing_node_id = existing.get("node_id")
                if entry.get("node_id") and existing_node_id and entry.get("node_id") != existing_node_id:
                    node_conflicts += 1
            else:
                unmatched += 1

        return {
            "total": len(entries),
            "matched": matched,
            "unmatched": unmatched,
            "node_conflicts": node_conflicts,
            "duplicate_source_ids": duplicate_source_ids
        }

    def create_import_mode_keyboard(summary: dict, session_id: str):
        keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
        keyboard.row(
            telebot.types.InlineKeyboardButton("➕ Только новые", callback_data=f"{CB_IMPORT_MODE}:add_new_only:{session_id}"),
            telebot.types.InlineKeyboardButton("♻️ Только заменить", callback_data=f"{CB_IMPORT_MODE}:replace_matched_only:{session_id}")
        )
        keyboard.row(
            telebot.types.InlineKeyboardButton("🆕 Создать всё новым", callback_data=f"{CB_IMPORT_MODE}:create_all:{session_id}"),
            telebot.types.InlineKeyboardButton("⚡ Синхронизировать всё", callback_data=f"{CB_IMPORT_MODE}:sync_all:{session_id}")
        )
        keyboard.row(
            telebot.types.InlineKeyboardButton("❌ Отмена", callback_data=f"{CB_IMPORT_CANCEL}:{session_id}")
        )
        return keyboard

    def create_cache_mode_keyboard():
        keyboard = telebot.types.InlineKeyboardMarkup(row_width=1)
        keyboard.row(
            telebot.types.InlineKeyboardButton("🟢 Только активные", callback_data=f"{CB_CACHE_MODE}:active"),
            telebot.types.InlineKeyboardButton("📦 Активные + деактивированные", callback_data=f"{CB_CACHE_MODE}:all")
        )
        keyboard.row(telebot.types.InlineKeyboardButton("❌ Отмена", callback_data=f"{CB_CACHE_MODE}:cancel"))
        return keyboard

    def render_import_summary_text(summary: dict) -> str:
        lines = [
            "📦 <b>Файл лотов загружен</b>",
            "",
            f"Всего в файле: <b>{summary['total']}</b>",
            f"Совпадений по сохранённому ID: <b>{summary['matched']}</b>",
            f"Новых лотов: <b>{summary['unmatched']}</b>"
        ]
        if summary.get("node_conflicts"):
            lines.append(f"⚠️ Совпадений с другой подкатегорией: <b>{summary['node_conflicts']}</b>")
        if summary.get("duplicate_source_ids"):
            lines.append(f"⚠️ Повторов ID внутри файла: <b>{summary['duplicate_source_ids']}</b>")
        lines.extend([
            "",
            "Выберите режим импорта:",
            "• Только новые — создаст только отсутствующие",
            "• Только заменить — обновит только найденные",
            "• Создать всё новым — игнорирует совпадения",
            "• Синхронизировать всё — заменит найденные и добавит новые"
        ])
        return "\n".join(lines)

    def create_lot_from_import_entry(acc: Account, entry: dict):
        lot = FunPayAPI.types.LotFields(0, sanitize_lot_fields_for_transfer(entry["fields"]))
        if entry.get("active") is not None:
            lot.active = entry["active"]
        create_lot(acc, lot)
        return lot

    def apply_imported_fields_to_existing_lot(target_lot_fields, imported_entry: dict):
        source_fields = sanitize_lot_fields_for_transfer(imported_entry["fields"])
        preserved_offer_id = target_lot_fields.fields.get("offer_id")
        preserved_csrf_token = target_lot_fields.fields.get("csrf_token")
        was_active_before_replace = bool(target_lot_fields.active)

        new_fields = dict(source_fields)
        if preserved_offer_id is not None:
            new_fields["offer_id"] = preserved_offer_id
        if preserved_csrf_token is not None:
            new_fields["csrf_token"] = preserved_csrf_token

        target_lot_fields.set_fields(new_fields)

        target_lot_fields.title_ru = new_fields.get('fields[summary][ru]')
        target_lot_fields.title_en = new_fields.get('fields[summary][en]')
        target_lot_fields.description_ru = new_fields.get('fields[desc][ru]')
        target_lot_fields.description_en = new_fields.get('fields[desc][en]')

        price = new_fields.get("price")
        try:
            target_lot_fields.price = float(str(price).replace(",", ".")) if price not in (None, "") else 0.0
        except (TypeError, ValueError):
            target_lot_fields.price = 0.0

        amount = new_fields.get("amount")
        try:
            target_lot_fields.amount = int(amount) if amount not in (None, "") else None
        except (TypeError, ValueError):
            target_lot_fields.amount = None

        if imported_entry.get("active") is not None:
            target_lot_fields.active = bool(imported_entry["active"])
        else:
            active_field = new_fields.get("active")
            if isinstance(active_field, str):
                target_lot_fields.active = active_field.strip().lower() in ("1", "true", "yes", "да")
            elif active_field is None:
                target_lot_fields.active = target_lot_fields.active
            else:
                target_lot_fields.active = bool(active_field)

        if not was_active_before_replace:
            target_lot_fields.active = False

        deactivate_after_sale = new_fields.get("deactivate_after_sale")
        if deactivate_after_sale is None:
            deactivate_after_sale = new_fields.get("deactivate_after_sale[]")
        target_lot_fields.deactivate_after_sale = bool(parse_optional_bool(deactivate_after_sale))

    def import_lots_with_mode(chat_id: int, user_id: int, entries: list[dict], mode: str, progress_tracker=None):
        existing_index = get_existing_lots_index(chat_id)
        results = {
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "failed": 0,
            "node_conflicts": 0
        }
        total = len(entries)

        for index, entry in enumerate(entries, start=1):
            source_lot_id = entry.get("source_lot_id")
            existing = existing_index.get(source_lot_id) if source_lot_id is not None else None
            should_create = False
            should_update = False

            if mode == "create_all":
                should_create = True
            elif mode == "add_new_only":
                should_create = existing is None
            elif mode == "replace_matched_only":
                should_update = existing is not None
            elif mode == "sync_all":
                should_update = existing is not None
                should_create = existing is None

            if should_update and existing:
                existing_node_id = existing.get("node_id")
                if entry.get("node_id") and existing_node_id and entry["node_id"] != existing_node_id:
                    results["skipped"] += 1
                    results["node_conflicts"] += 1
                else:
                    try:
                        lot_fields = get_lot_fields_by_id(source_lot_id, chat_id)
                        apply_imported_fields_to_existing_lot(lot_fields, entry)
                        if save_lot_changes(lot_fields, chat_id):
                            results["updated"] += 1
                        else:
                            results["failed"] += 1
                    except Exception:
                        logger.debug("TRACEBACK", exc_info=True)
                        results["failed"] += 1
            elif should_create:
                try:
                    create_lot_from_import_entry(cardinal.account, entry)
                    results["created"] += 1
                except FunPayAPI.exceptions.LotSavingError as ex:
                    logger.error(f"[LOTS COPY] Не удалось создать лот при импорте: {ex.error_message}")
                    results["failed"] += 1
                except Exception:
                    logger.debug("TRACEBACK", exc_info=True)
                    results["failed"] += 1
            else:
                results["skipped"] += 1

            if progress_tracker:
                update_progress_tracker(
                    progress_tracker,
                    current=index,
                    total=total,
                    failed=results["failed"],
                    status=f"Импортирую лоты: {index}/{total}",
                    force=index == total
                )

        invalidate_lots_cache()
        return results

    # ========== ФУНКЦИИ ДЛЯ КОПИРОВАНИЯ ЛОТОВ ==========

    def get_current_account(tg_msg: Message) -> FunPayAPI.types.UserProfile:
        """
        Получает данные о текущем аккаунте.

        :param tg_msg: экземпляр Telegram-сообщения-триггера.

        :return: экземпляр текущего аккаунта.
        """
        attempts = 3
        while attempts:
            try:
                profile = cardinal.account.get_user(cardinal.account.id)
                return profile
            except:
                logger.error("[LOTS COPY] Не удалось получить данные о текущем профиле.")
                logger.debug("TRACEBACK", exc_info=True)
                time.sleep(1)
                attempts -= 1
        else:
            bot.send_message(tg_msg.chat.id, "❌ Не удалось получить данные текущего профиля.")
            raise Exception

    def get_second_account(tg_msg: Message, token: str) -> FunPayAPI.account.Account:
        """
        Получает данные об аккаунте, на который нужно скопировать лоты.

        :param tg_msg: экземпляр Telegram-сообщения-триггера.
        :param token: токен (golden_key) аккаунта, на который нужно скопировать лоты.

        :return: экземпляр аккаунта, на который необходимо скопировать лоты.
        """
        attempts = 3
        while attempts:
            try:
                acc = FunPayAPI.account.Account(token).get()
                return acc
            except:
                logger.error("[LOTS COPY] Не удалось получить данные об аккаунте для копирования лотов.")
                logger.debug("TRACEBACK", exc_info=True)
                time.sleep(1)
                attempts -= 1
        else:
            bot.send_message(tg_msg.chat.id, "❌ Не удалось получить данные об аккаунте для копирования лотов.")
            raise Exception

    def get_lots_info(tg_msg: Message, profile: FunPayAPI.types.UserProfile, progress_tracker=None,
                      progress_title: str = "Собираю данные лотов", include_deactivated: bool = False,
                      include_delivery_secrets: bool = False) -> list[FunPayAPI.types.LotFields]:
        """
        Получает данные о всех лотах (кроме валюты) на текущем аккаунте.

        :param tg_msg: экземпляр Telegram-сообщения-триггера.
        :param profile: экземпляр текущего аккаунта.

        :return: список экземпляров лотов.
        """
        result = []
        if include_deactivated:
            source_lots = get_all_lots(tg_msg.chat.id, force_refresh=True, progress_tracker=progress_tracker,
                                       progress_status="Сканирую подкатегории и собираю все лоты")
        else:
            source_lots = [i for i in profile.get_lots() if i.subcategory.type != FunPayAPI.types.SubCategoryTypes.CURRENCY]
        total_lots = len(source_lots)
        for index, i in enumerate(source_lots, start=1):
            if progress_tracker:
                update_progress_tracker(
                    progress_tracker,
                    current=index - 1,
                    total=total_lots,
                    status=f"{progress_title}: {index}/{total_lots}",
                    force=index == 1
                )
            if i.subcategory.type == FunPayAPI.types.SubCategoryTypes.CURRENCY:
                continue
            attempts = 3
            while attempts:
                try:
                    lot_fields = cardinal.account.get_lot_fields(i.id)
                    fields = sanitize_lot_fields_for_transfer(
                        lot_fields.fields,
                        include_delivery_secrets=include_delivery_secrets
                    )
                    lot_fields.set_fields(fields)
                    result.append(lot_fields)
                    logger.info(f"[LOTS COPY] Получил данные о лоте {i.id} {i.description}.")
                    break
                except:
                    logger.error(f"[LOTS COPY] Не удалось получить данные о лоте {i.id} {i.description}.")
                    logger.debug("TRACEBACK", exc_info=True)
                    time.sleep(2)
                    attempts -= 1
            else:
                bot.send_message(tg_msg.chat.id, f"❌ Не удалось получить данные о "
                                                 f"<a href=\"https://funpay.com/lots/offer?id={i.id}\">лоте {i.id} {i.description}</a>."
                                                 f" Пропускаю.")
                time.sleep(1)
                if progress_tracker:
                    update_progress_tracker(progress_tracker, current=index, total=total_lots,
                                            status=f"{progress_title}: {index}/{total_lots}", force=True)
                continue
            time.sleep(0.5)
            if progress_tracker:
                update_progress_tracker(progress_tracker, current=index, total=total_lots,
                                        status=f"{progress_title}: {index}/{total_lots}", force=index == total_lots)
        return result

    def create_lot(acc: Account, lot: FunPayAPI.types.LotFields):
        """
        Создает лот на переданном аккаунте.

        :param acc: экземпляр аккаунта, на котором нужно создать лот.
        :param lot: экземпляр лота.
        """
        fields = dict(lot.fields)
        fields["offer_id"] = "0"
        fields["csrf_token"] = acc.csrf_token
        lot.set_fields(fields)
        lot.lot_id = 0

        attempts = 3
        while attempts:
            try:
                acc.save_lot(lot)
                if getattr(acc, "id", None) == getattr(cardinal.account, "id", None):
                    invalidate_lots_cache()
                logger.info(f"[LOTS COPY] Создал лот {lot.title_ru}.")
                return
            except FunPayAPI.exceptions.LotSavingError as e:
                raise e
            except Exception as e:
                logger.error(f"[LOTS COPY] Не удалось создать лот {lot.title_ru}.")
                logger.debug("TRACEBACK", exc_info=True)
                if isinstance(e, FunPayAPI.exceptions.RequestFailedError):
                    logger.debug(e.response.content.decode())
                time.sleep(2)
                attempts -= 1
        else:
            raise Exception

    def act_copy_lots(m: Message):
        """
        Активирует режим ожидания ввода токена для копирования лотов.
        """
        if is_shared_operation_running() or is_user_busy(m.from_user.id):
            bot.send_message(m.chat.id,
                             "❌ Операция уже выполняется. Дождитесь завершения текущего процесса.")
            return
        result = bot.send_message(m.chat.id, "Отправьте токен (golden_key) аккаунта, на который нужно скопировать лоты.\n"
                                             "Копировать встроенную автовыдачу FunPay: "
                                             f"{'🟢Вкл.' if settings.get('with_secrets') else '🔴Выкл.'} (изменить - /copy_with_secrets)",
                                  reply_markup=skb.CLEAR_STATE_BTN())
        tg.set_state(m.chat.id, result.id, m.from_user.id, CBT_COPY_LOTS)

    def copy_lots(m: Message):
        """
        Копирует лоты.
        """
        token = m.text.strip()
        if len(token) != 32:
            bot.send_message(m.chat.id, "❌ Неверный формат токена.")
            return
        tg.clear_state(m.chat.id, m.from_user.id, True)
        try:
            bot.delete_message(m.chat.id, m.message_id)
        except Exception:
            logger.warning("[LOTS COPY] Не удалось удалить сообщение с golden_key.")

        if is_shared_operation_running():
            bot.send_message(m.chat.id, "❌ Сейчас уже выполняется общий процесс. Попробуйте позже.")
            return

        if is_user_busy(m.from_user.id):
            bot.send_message(m.chat.id, "❌ Операция уже выполняется. Подождите.")
            return

        set_user_busy(m.from_user.id)
        set_shared_operation_running(True)
        progress = create_progress_tracker(m.chat.id, "Копирование лотов", status="Проверяю аккаунты")
        try:
            update_progress_tracker(progress, status="Получаю данные текущего аккаунта", force=True)
            profile = get_current_account(m)

            update_progress_tracker(progress, status="Проверяю второй аккаунт", force=True)
            second_account = get_second_account(m, token)

            update_progress_tracker(progress, current=0, total=None, status="Собираю данные текущих лотов", force=True)
            lots = get_lots_info(
                m,
                profile,
                progress_tracker=progress,
                progress_title="Собираю лоты",
                include_delivery_secrets=bool(settings.get("with_secrets"))
            )

            total_lots = len(lots)
            update_progress_tracker(progress, current=0, total=total_lots, status="Начинаю копирование лотов", force=True)
            failed_count = 0
            for index, i in enumerate(lots, start=1):
                lot_id = i.lot_id
                time.sleep(1)
                try:
                    create_lot(second_account, i)
                except:
                    failed_count += 1
                    bot.send_message(m.chat.id, f"❌ Не удалось скопировать лот "
                                                f"https://funpay.com/lots/offer?id={lot_id}\n"
                                                f"Пропускаю.")
                update_progress_tracker(progress, current=index, total=total_lots,
                                        status=f"Копирую лоты: {index}/{total_lots}", failed=failed_count,
                                        force=index == total_lots)

            set_shared_operation_running(False)
            clear_user_busy(m.from_user.id)
            finish_progress_tracker(progress, f"✅ Копирование завершено\n\nСкопировано: {total_lots - failed_count}/{total_lots}\nОшибок: {failed_count}")
            bot.send_message(m.chat.id, "✅ Копирование активных лотов завершено!")
        except:
            set_shared_operation_running(False)
            clear_user_busy(m.from_user.id)
            finish_progress_tracker(progress, "❌ Копирование остановлено из-за ошибки.")
            logger.error("[LOTS COPY] Не удалось скопировать лоты.")
            logger.debug("TRACEBACK", exc_info=True)
            bot.send_message(m.chat.id, "❌ Не удалось скопировать лоты.")
            return

    def act_cache_lots(m: Message):
        bot.send_message(
            m.chat.id,
            "💾 <b>Кэширование лотов</b>\n\nВыбери, что выгружать в JSON:",
            reply_markup=create_cache_mode_keyboard(),
            parse_mode="HTML"
        )

    def cache_lots(m: Message, include_deactivated: bool = False):
        """
        Кэширует лоты в файл и отправляет его в Telegram чат.
        """
        if is_shared_operation_running() or is_user_busy(m.from_user.id):
            bot.send_message(m.chat.id, "❌ Операция уже выполняется. Дождитесь завершения текущего процесса.")
            return
        set_user_busy(m.from_user.id)
        set_shared_operation_running(True)
        progress = create_progress_tracker(m.chat.id, "Кэширование лотов", status="Проверяю аккаунт")
        try:
            update_progress_tracker(progress, status="Получаю профиль. Экспорт выполняется без секретов", force=True)
            profile = get_current_account(m)

            update_progress_tracker(progress, current=0, total=None, status="Собираю данные лотов", force=True)
            result = []
            lots_info = get_lots_info(
                m,
                profile,
                progress_tracker=progress,
                progress_title="Собираю лоты",
                include_deactivated=include_deactivated,
                include_delivery_secrets=False
            )
            total_lots = len(lots_info)
            update_progress_tracker(progress, current=0, total=total_lots, status="Сохраняю файл", force=True)
            for index, i in enumerate(lots_info, start=1):
                result.append(build_export_lot_entry(i))
                update_progress_tracker(progress, current=index, total=total_lots,
                                        status=f"Подготавливаю файл: {index}/{total_lots}", force=index == total_lots)

            update_progress_tracker(progress, current=total_lots, total=total_lots, status="Отправляю файл в чат", force=True)
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            export_path = None
            try:
                with NamedTemporaryFile("w", encoding="utf-8", suffix=".json", prefix="lots_", dir=CACHE_DIR, delete=False) as f:
                    json.dump(result, f, indent=4, ensure_ascii=False)
                    export_path = Path(f.name)
                with export_path.open("rb") as f:
                    bot.send_document(m.chat.id, f)
            finally:
                if export_path and export_path.exists():
                    export_path.unlink()
            set_shared_operation_running(False)
            clear_user_busy(m.from_user.id)
            finish_progress_tracker(progress, f"✅ Кэширование завершено\n\nПодготовлено лотов: {len(result)}")
        except:
            set_shared_operation_running(False)
            clear_user_busy(m.from_user.id)
            finish_progress_tracker(progress, "❌ Кэширование остановлено из-за ошибки.")
            logger.error("[LOTS COPY] Не удалось кэшировать лоты.")
            logger.debug("TRACEBACK", exc_info=True)
            bot.send_message(m.chat.id, "❌ Не удалось кэшировать лоты.")
            return

    def act_create_lots(m: Message):
        """
        Активирует режим ожидания файла с лотами для создания лотов на текущем аккаунте.
        """
        if is_shared_operation_running() or is_user_busy(m.from_user.id):
            bot.send_message(m.chat.id,
                             "❌ Операция уже выполняется. Дождитесь завершения текущего процесса.")
            return
        clear_pending_import(m.from_user.id)
        result = bot.send_message(m.chat.id,
                                  "Отправьте мне файл с лотами, полученный с помощью команды /cache_lots.\n"
                                  "Я сохраню ID исходных товаров и перед импортом спрошу, что делать с совпадениями.",
                                  reply_markup=skb.CLEAR_STATE_BTN())
        tg.set_state(m.chat.id, result.id, m.from_user.id, CBT_CREATE_LOTS)

    def create_lots(m: Message):
        if not m.document.file_name.endswith(".json"):
            bot.send_message(m.chat.id, "❌ Это не файл с лотами.")
            return
        if m.document.file_size >= 20971520:
            bot.send_message(m.chat.id, "❌ Размер файла не должен превышать 20МБ.")
            return
        tg.clear_state(m.chat.id, m.from_user.id, True)

        if is_shared_operation_running():
            bot.send_message(m.chat.id, "❌ Сейчас уже выполняется общий процесс. Попробуйте позже.")
            return

        if is_user_busy(m.from_user.id):
            bot.send_message(m.chat.id, "❌ Операция уже выполняется. Подождите.")
            return

        set_user_busy(m.from_user.id)
        set_shared_operation_running(True)
        progress = create_progress_tracker(m.chat.id, "Подготовка импорта", status="Загружаю файл")
        import_path = None
        try:
            import_path = download_file(tg, m, f"lots_import_{m.from_user.id}.json")

            with import_path.open("r", encoding="utf-8") as f:
                data = json.loads(f.read())
            if not isinstance(data, list):
                raise ValueError("Файл должен содержать JSON-массив лотов.")

            normalized_entries = []
            invalid_entries = []
            total_items = len(data)
            update_progress_tracker(progress, current=0, total=total_items,
                                    status=f"Проверяю файл: 0/{total_items}", force=True)

            for index, item in enumerate(data, start=1):
                entry, error = parse_import_lot_entry(item)
                if entry is None:
                    invalid_entries.append((index, error or "неизвестная ошибка"))
                else:
                    normalized_entries.append(entry)
                update_progress_tracker(progress, current=index, total=total_items,
                                        status=f"Проверяю файл: {index}/{total_items}",
                                        failed=len(invalid_entries), force=index == total_items)

            existing_index = get_existing_lots_index(m.chat.id)
            summary = analyze_import_entries(normalized_entries, existing_index)
            summary["invalid"] = len(invalid_entries)
            session_id = str(int(time.time() * 1000))
            set_pending_import(m.from_user.id, {
                "session_id": session_id,
                "chat_id": m.chat.id,
                "entries": normalized_entries,
                "summary": summary,
                "invalid_entries": invalid_entries,
                "created_at": time.time()
            })
            set_shared_operation_running(False)
            clear_user_busy(m.from_user.id)
            finish_progress_tracker(progress, f"✅ Файл подготовлен\n\nЛотов в файле: {summary['total']}\nСовпадений: {summary['matched']}\nНовых: {summary['unmatched']}")
            bot.send_message(
                m.chat.id,
                render_import_summary_text(summary),
                parse_mode="HTML",
                reply_markup=create_import_mode_keyboard(summary, session_id)
            )
            if invalid_entries:
                preview = "\n".join(f"• #{idx}: {err}" for idx, err in invalid_entries[:10])
                more = "" if len(invalid_entries) <= 10 else f"\n…и ещё {len(invalid_entries) - 10}"
                bot.send_message(m.chat.id, f"⚠️ Пропущены некорректные элементы: {len(invalid_entries)}\n{preview}{more}")
        except Exception as e:
            set_shared_operation_running(False)
            clear_user_busy(m.from_user.id)
            finish_progress_tracker(progress, "❌ Создание лотов остановлено из-за ошибки.")
            logger.error("[LOTS COPY] Не удалось создать лоты.")
            logger.debug("TRACEBACK", exc_info=True)
            bot.send_message(m.chat.id, f"❌ Не удалось подготовить импорт лотов.\n\nОшибка: {str(e)}")
            return
        finally:
            if import_path and import_path.exists():
                import_path.unlink()
    def copy_with_secrets (m: telebot.types.Message):
        try:
            if is_shared_operation_running() or is_user_busy(m.from_user.id):
                bot.send_message(m.chat.id,
                                 "❌ Операция уже выполняется. Дождитесь завершения текущего процесса.")
                return
            global settings
            settings["with_secrets"] = not(settings.get("with_secrets"))
            save_settings()
            bot.send_message(m.chat.id, f"Изменено успешно.\nКопировать встроенную автовыдачу FunPay: "
                                        f"{'🟢Вкл.' if settings.get('with_secrets') else '🔴Выкл.'}")
        except:
            logger.debug("TRACEBACK", exc_info=True)
            bot.send_message(m.chat.id, "Произошла ошибка.")

    # ========== КОМАНДЫ УПРАВЛЕНИЯ ЛОТАМИ ==========
    
    def manage_lots(m: telebot.types.Message):
        """Главная команда управления лотами."""
        if is_user_busy(m.from_user.id):
            bot.send_message(m.chat.id, "❌ Операция уже выполняется. Подождите.")
            return
        try:
            logger.info(f"[LOTS] Пользователь {m.from_user.id} запросил список лотов.")
            progress = create_progress_tracker(m.chat.id, "Загрузка списка лотов", status="Получаю категории и лоты")
            lots = get_all_lots(m.chat.id, progress_tracker=progress, progress_status="Получаю категории и лоты")
            
            if not lots:
                finish_progress_tracker(progress, "📭 Лотов нет\n\nНет ни активных, ни скрытых лотов.")
                bot.send_message(
                    m.chat.id,
                    "📭 Лотов нет\n\nНет ни активных, ни скрытых лотов.\n"
                    "Если нужно, укажи ID подкатегорий в storage/plugins/copy_lots_settings.json → lot_search_subcategory_ids."
                )
                return
            
            state = get_user_state(m.from_user.id)
            state['lots'] = lots
            state['page'] = 0
            clear_selected_lots(m.from_user.id)
            
            keyboard = create_lots_keyboard(lots, 0, selection_mode=False, selected_ids=[])
            bot.send_message(
                m.chat.id,
                render_manage_lots_text(lots),
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            finish_progress_tracker(progress, f"✅ Список лотов загружен\n\nВсего: {len(lots)}\nАктивных: {sum(1 for l in lots if l.active)}")
            logger.info(f"[LOTS] Отправлен список из {len(lots)} лотов пользователю {m.from_user.id}.")
        except Exception as e:
            try:
                finish_progress_tracker(progress, "❌ Не удалось загрузить список лотов.")
            except Exception:
                pass
            logger.error(f"[LOTS] Ошибка при получении списка лотов для пользователя {m.from_user.id}. Ошибка: {str(e)}")
            logger.exception("TRACEBACK:")
            bot.send_message(m.chat.id, f"❌ Произошла ошибка при получении списка лотов.\n\n"
                                        f"Ошибка: {str(e)}\n\n"
                                        f"Проверьте логи Cardinal для подробностей.")

    def manage_menu(m: telebot.types.Message):
        bot.send_message(
            m.chat.id,
            render_manage_menu_text("main"),
            reply_markup=create_manage_menu_keyboard("main"),
            parse_mode="HTML"
        )

    def show_lot_subcats(m: telebot.types.Message):
        keyboard = telebot.types.InlineKeyboardMarkup().add(
            telebot.types.InlineKeyboardButton("🗂️ Открыть inline-меню", callback_data=CB_SUBCAT_MENU)
        )
        bot.send_message(
            m.chat.id,
            render_subcategory_settings_text() + "\n\n"
            "Команды:\n"
            "<code>/add_lot_subcat 12345, 67890</code>\n"
            "<code>/remove_lot_subcat 12345</code>",
            parse_mode="HTML",
            reply_markup=keyboard
        )

    def add_lot_subcat(m: telebot.types.Message):
        payload = m.text.partition(" ")[2].strip()
        if not payload:
            bot.send_message(m.chat.id, "❌ Укажи ID. Пример: /add_lot_subcat 12345, 67890")
            return

        parsed_ids, invalid_parts = parse_subcategory_ids_input(payload)
        if not parsed_ids:
            bot.send_message(m.chat.id, "❌ Не найдено ни одного корректного ID.")
            return

        added_ids = add_configured_subcategory_ids(parsed_ids)
        duplicates = []
        seen = set()
        for lot_id in parsed_ids:
            if lot_id in added_ids or lot_id in seen:
                continue
            seen.add(lot_id)
            duplicates.append(lot_id)

        lines = [f"✅ Добавлено ID: <code>{', '.join(str(i) for i in added_ids) if added_ids else '—'}</code>"]
        if duplicates:
            lines.append(f"ℹ️ Уже были в списке: <code>{', '.join(str(i) for i in duplicates)}</code>")
        if invalid_parts:
            lines.append(f"⚠️ Пропущены некорректные значения: <code>{', '.join(invalid_parts)}</code>")
        bot.send_message(m.chat.id, "\n".join(lines), parse_mode="HTML")

    def remove_lot_subcat(m: telebot.types.Message):
        payload = m.text.partition(" ")[2].strip()
        if not payload:
            bot.send_message(m.chat.id, "❌ Укажи ID. Пример: /remove_lot_subcat 12345")
            return

        parsed_ids, invalid_parts = parse_subcategory_ids_input(payload)
        if not parsed_ids:
            bot.send_message(m.chat.id, "❌ Не найдено ни одного корректного ID.")
            return

        removed = []
        missing = []
        seen = set()
        for subcategory_id in parsed_ids:
            if subcategory_id in seen:
                continue
            seen.add(subcategory_id)
            if remove_configured_subcategory_id(subcategory_id):
                removed.append(subcategory_id)
            else:
                missing.append(subcategory_id)

        lines = []
        if removed:
            lines.append(f"✅ Удалено ID: <code>{', '.join(str(i) for i in removed)}</code>")
        if missing:
            lines.append(f"ℹ️ Не найдены в списке: <code>{', '.join(str(i) for i in missing)}</code>")
        if invalid_parts:
            lines.append(f"⚠️ Пропущены некорректные значения: <code>{', '.join(invalid_parts)}</code>")
        bot.send_message(m.chat.id, "\n".join(lines) if lines else "ℹ️ Нечего удалять.", parse_mode="HTML")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(CB_PAGE))
    def callback_page(call):
        try:
            page = int(call.data.split(":")[1])
            user_id = call.from_user.id
            if user_id not in user_data or 'lots' not in user_data[user_id]:
                bot.answer_callback_query(call.id, "❌ Данные устарели. Используйте /manage_lots")
                return
            lots = user_data[user_id]['lots']
            user_data[user_id]['page'] = page
            keyboard = create_lots_keyboard(lots, page, selection_mode=is_selection_mode(user_id), selected_ids=get_selected_lot_ids(user_id))
            bot.edit_message_text(
                render_manage_lots_text(lots),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            bot.answer_callback_query(call.id)
        except:
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(CB_LOT_VIEW))
    def callback_lot_view(call):
        try:
            lot_id = int(call.data.split(":")[1])
            bot.answer_callback_query(call.id, "⏳ Загружаю данные лота...")
            lot_fields = get_lot_fields_by_id(lot_id, call.message.chat.id)
            fields = lot_fields.fields
            
            # Правильное получение полей
            title = fields.get('fields[summary][ru]', fields.get('fields[summary][en]', 'Без названия'))
            desc = fields.get('fields[desc][ru]', fields.get('fields[desc][en]', 'Без описания'))
            price = fields.get('price', '0')
            
            active = lot_fields.active
            
            logger.info(f"[LOTS] Лот {lot_id}: active={active}, title='{title[:50]}...', price={price}")
            
            if len(desc) > 500:
                desc = desc[:500] + "..."
            status = "🟢 Активен" if active else "🔴 Неактивен"
            message = (
                f"📦 <b>Лот #{lot_id}</b>\n\n<b>Статус:</b> {status}\n"
                f"<b>Название:</b> {html_text(title)}\n<b>Цена:</b> {html_text(price)} ₽\n"
                f"<b>Описание:</b>\n{html_text(desc)}\n\n"
                f"<a href=\"https://funpay.com/lots/offer?id={lot_id}\">🔗 Открыть на FunPay</a>"
            )
            keyboard = create_lot_view_keyboard(lot_id, active)
            bot.edit_message_text(
                message,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.error(f"[LOTS] Ошибка при просмотре лота: {str(e)}")
            logger.exception("TRACEBACK:")
            bot.answer_callback_query(call.id, "❌ Ошибка загрузки лота")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(CB_LOT_TOGGLE))
    def callback_lot_toggle(call):
        if is_user_busy(call.from_user.id):
            bot.answer_callback_query(call.id, "❌ Операция уже выполняется")
            return
        set_user_busy(call.from_user.id)
        try:
            lot_id = int(call.data.split(":")[1])
            bot.answer_callback_query(call.id, "⏳ Изменяю статус...")
            lot_fields = get_lot_fields_by_id(lot_id, call.message.chat.id)
            current_active = lot_fields.active
            lot_fields.active = not current_active
            new_active = lot_fields.active
            logger.info(f"[LOTS] Переключение статуса лота {lot_id}: {current_active} -> {new_active}")
            
            if save_lot_changes(lot_fields, call.message.chat.id):
                fields = lot_fields.fields
                if not new_active:
                    lot_info = {
                        "title": fields.get('fields[summary][ru]', fields.get('fields[summary][en]', 'Без названия')),
                        "price": fields.get('price', '0'),
                        "description": fields.get('fields[desc][ru]', fields.get('fields[desc][en]', '')),
                        "category": fields.get('node_id', '')
                    }
                    add_disabled_lot(lot_id, lot_info)
                    bot.send_message(
                        call.message.chat.id,
                        f"💾 Информация о лоте #{lot_id} сохранена в файл:\n"
                        f"<code>{DISABLED_LOTS_FILE}</code>",
                        parse_mode="HTML"
                    )
                else:
                    remove_disabled_lot(lot_id)

                update_cached_lots_for_user(call.from_user.id, call.message.chat.id)
                callback_lot_view(call)
                status_text = "отключен" if not new_active else "включен"
                bot.answer_callback_query(call.id, f"✅ Лот {status_text}!")
            else:
                bot.answer_callback_query(call.id, "❌ Ошибка сохранения")
            
            clear_user_busy(call.from_user.id)
        except Exception as e:
            clear_user_busy(call.from_user.id)
            logger.error(f"[LOTS] Ошибка при переключении статуса: {str(e)}")
            logger.exception("TRACEBACK:")
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(CB_LOT_DELETE) and not c.data.startswith(CB_LOT_DELETE_CONFIRM))
    def callback_lot_delete(call):
        """Запрашивает подтверждение удаления лота."""
        try:
            lot_id = int(call.data.split(":")[1])
            
            # Получаем информацию о лоте
            lot_fields = get_lot_fields_by_id(lot_id, call.message.chat.id)
            fields = lot_fields.fields
            title = fields.get('fields[summary][ru]', fields.get('fields[summary][en]', 'Без названия'))
            
            keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
            keyboard.row(
                telebot.types.InlineKeyboardButton("✅ Да, удалить", callback_data=f"{CB_LOT_DELETE_CONFIRM}:{lot_id}"),
                telebot.types.InlineKeyboardButton("❌ Отмена", callback_data=f"{CB_LOT_VIEW}:{lot_id}")
            )
            
            bot.edit_message_text(
                f"⚠️ <b>Подтверждение удаления</b>\n\n"
                f"Вы уверены, что хотите удалить лот #{lot_id}?\n"
                f"<b>Название:</b> {html_text(title)}\n\n"
                f"⚠️ <b>Это действие необратимо!</b>\n"
                f"Лот будет полностью удален с FunPay.",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            bot.answer_callback_query(call.id)
        except:
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(CB_LOT_DELETE_CONFIRM))
    def callback_lot_delete_confirm(call):
        """Удаляет лот после подтверждения."""
        if is_user_busy(call.from_user.id):
            bot.answer_callback_query(call.id, "❌ Операция уже выполняется")
            return
        
        set_user_busy(call.from_user.id)
        try:
            lot_id = int(call.data.split(":")[1])
            bot.answer_callback_query(call.id, "⏳ Удаляю лот...")
            
            # Получаем информацию о лоте перед удалением
            lot_fields = get_lot_fields_by_id(lot_id, call.message.chat.id)
            fields = lot_fields.fields
            title = fields.get('fields[summary][ru]', fields.get('fields[summary][en]', 'Без названия'))
            
            # Удаляем лот
            if delete_lot_from_funpay(lot_id, call.message.chat.id):
                # Удаляем из списка отключенных если там был
                remove_disabled_lot(lot_id)
                
                # Обновляем список лотов пользователя
                user_id = call.from_user.id
                if user_id in user_data and 'lots' in user_data[user_id]:
                    try:
                        # Обновляем список
                        lots = update_cached_lots_for_user(user_id, call.message.chat.id)
                        if not lots:
                            clear_selected_lots(user_id)
                            bot.edit_message_text(
                                f"✅ Лот #{lot_id} удален.\n\n📭 Лотов больше нет — ни активных, ни скрытых.",
                                call.message.chat.id,
                                call.message.message_id
                            )
                            bot.answer_callback_query(call.id, "✅ Лот удален!")
                            clear_user_busy(call.from_user.id)
                            return
                        page = user_data[user_id].get('page', 0)
                        
                        keyboard = create_lots_keyboard(lots, page, selection_mode=is_selection_mode(user_id), selected_ids=get_selected_lot_ids(user_id))
                        bot.edit_message_text(
                            f"✅ <b>Лот #{lot_id} успешно удален!</b>\n\n"
                            f"Название: {html_text(title)}\n\n"
                            f"📋 <b>Управление лотами</b>\n\n"
                            f"Всего лотов: {len(lots)}\n"
                            f"Активных: {sum(1 for l in lots if l.active)}\n"
                            f"Неактивных: {sum(1 for l in lots if not l.active)}\n\n"
                            f"Выберите лот для управления:",
                            call.message.chat.id,
                            call.message.message_id,
                            reply_markup=keyboard,
                            parse_mode="HTML"
                        )
                    except:
                        bot.send_message(
                            call.message.chat.id,
                            f"✅ Лот #{lot_id} (<b>{html_text(title)}</b>) успешно удален!",
                            parse_mode="HTML"
                        )
                else:
                    bot.send_message(
                        call.message.chat.id,
                        f"✅ Лот #{lot_id} (<b>{html_text(title)}</b>) успешно удален!",
                        parse_mode="HTML"
                    )
                
                bot.answer_callback_query(call.id, "✅ Лот удален!")
            else:
                bot.answer_callback_query(call.id, "❌ Ошибка удаления")
            
            clear_user_busy(call.from_user.id)
        except:
            clear_user_busy(call.from_user.id)
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(CB_LOT_EDIT_PRICE))
    def callback_edit_price(call):
        try:
            lot_id = int(call.data.split(":")[1])
            lot_fields = get_lot_fields_by_id(lot_id, call.message.chat.id)
            current_price = lot_fields.fields.get('price', '0')
            bot.answer_callback_query(call.id)
            result = bot.send_message(
                call.message.chat.id,
                f"💰 <b>Изменение цены лота #{lot_id}</b>\n\n"
                f"Текущая цена: <b>{current_price} ₽</b>\n\n"
                f"📝 Отправьте новую цену:\n"
                f"• Минимум: {LIMITS['price_min']} ₽\n"
                f"• Максимум: {LIMITS['price_max']} ₽\n"
                f"• Формат: число (например: 100 или 99.99)\n\n"
                f"Или нажмите кнопку для отмены:",
                parse_mode="HTML",
                reply_markup=telebot.types.InlineKeyboardMarkup().add(
                    telebot.types.InlineKeyboardButton("⬅️ Назад к лоту", callback_data=f"{CB_LOT_VIEW}:{lot_id}")
                )
            )
            if call.from_user.id not in user_data:
                user_data[call.from_user.id] = {}
            user_data[call.from_user.id]['editing_lot_id'] = lot_id
            tg.set_state(call.message.chat.id, result.id, call.from_user.id, CBT_EDIT_LOT_PRICE)
        except:
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data.startswith("ml_edit_title_ru:"))
    def callback_edit_title_ru(call):
        try:
            lot_id = int(call.data.split(":")[1])
            lot_fields = get_lot_fields_by_id(lot_id, call.message.chat.id)
            current_title_ru = lot_fields.fields.get('fields[summary][ru]', 'Без названия')
            bot.answer_callback_query(call.id)
            result = bot.send_message(
                call.message.chat.id,
                f"✏️ <b>Изменение русского названия лота #{lot_id}</b>\n\n"
                f"Текущее название (RU):\n<b>{current_title_ru}</b>\n\n"
                f"📝 Отправьте новое русское название:\n"
                f"• Минимум: {LIMITS['title_min']} символов\n"
                f"• Максимум: {LIMITS['title_max']} символов\n\n"
                f"Или нажмите кнопку для отмены:",
                parse_mode="HTML",
                reply_markup=telebot.types.InlineKeyboardMarkup().add(
                    telebot.types.InlineKeyboardButton("⬅️ Назад к лоту", callback_data=f"{CB_LOT_VIEW}:{lot_id}")
                )
            )
            if call.from_user.id not in user_data:
                user_data[call.from_user.id] = {}
            user_data[call.from_user.id]['editing_lot_id'] = lot_id
            tg.set_state(call.message.chat.id, result.id, call.from_user.id, CBT_EDIT_LOT_TITLE_RU)
        except:
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")
    
    @bot.callback_query_handler(func=lambda c: c.data.startswith(CB_LOT_EDIT_TITLE) and not c.data.startswith("ml_edit_title_ru:") and not c.data.startswith("ml_edit_title_en:"))
    def callback_edit_title(call):
        try:
            lot_id = int(call.data.split(":")[1])
            lot_fields = get_lot_fields_by_id(lot_id, call.message.chat.id)
            fields = lot_fields.fields
            current_title_ru = fields.get('fields[summary][ru]', 'Без названия')
            current_title_en = fields.get('fields[summary][en]', 'Без названия')
            bot.answer_callback_query(call.id)
            
            keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
            keyboard.row(
                telebot.types.InlineKeyboardButton("🇷🇺 Русское", callback_data=f"ml_edit_title_ru:{lot_id}"),
                telebot.types.InlineKeyboardButton("🇬🇧 Английское", callback_data=f"ml_edit_title_en:{lot_id}")
            )
            keyboard.row(
                telebot.types.InlineKeyboardButton("⬅️ Назад к лоту", callback_data=f"{CB_LOT_VIEW}:{lot_id}")
            )
            
            bot.edit_message_text(
                f"✏️ <b>Изменение названия лота #{lot_id}</b>\n\n"
                f"<b>Текущее название (RU):</b>\n{current_title_ru}\n\n"
                f"<b>Текущее название (EN):</b>\n{current_title_en}\n\n"
                f"Выберите язык для редактирования:",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except:
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")
    
    @bot.callback_query_handler(func=lambda c: c.data.startswith("ml_edit_title_en:"))
    def callback_edit_title_en(call):
        try:
            lot_id = int(call.data.split(":")[1])
            lot_fields = get_lot_fields_by_id(lot_id, call.message.chat.id)
            current_title_en = lot_fields.fields.get('fields[summary][en]', 'Без названия')
            bot.answer_callback_query(call.id)
            result = bot.send_message(
                call.message.chat.id,
                f"✏️ <b>Изменение английского названия лота #{lot_id}</b>\n\n"
                f"Текущее название (EN):\n<b>{current_title_en}</b>\n\n"
                f"📝 Отправьте новое английское название:\n"
                f"• Минимум: {LIMITS['title_min']} символов\n"
                f"• Максимум: {LIMITS['title_max']} символов\n\n"
                f"Или нажмите кнопку для отмены:",
                parse_mode="HTML",
                reply_markup=telebot.types.InlineKeyboardMarkup().add(
                    telebot.types.InlineKeyboardButton("⬅️ Назад к лоту", callback_data=f"{CB_LOT_VIEW}:{lot_id}")
                )
            )
            if call.from_user.id not in user_data:
                user_data[call.from_user.id] = {}
            user_data[call.from_user.id]['editing_lot_id'] = lot_id
            tg.set_state(call.message.chat.id, result.id, call.from_user.id, CBT_EDIT_LOT_TITLE_EN)
        except:
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(CB_LOT_EDIT_DESC))
    def callback_edit_desc(call):
        try:
            lot_id = int(call.data.split(":")[1])
            lot_fields = get_lot_fields_by_id(lot_id, call.message.chat.id)
            current_desc = lot_fields.fields.get('fields[desc][ru]', lot_fields.fields.get('fields[desc][en]', 'Без описания'))
            if len(current_desc) > 300:
                current_desc = current_desc[:300] + "..."
            bot.answer_callback_query(call.id)
            result = bot.send_message(
                call.message.chat.id,
                f"📝 <b>Изменение описания лота #{lot_id}</b>\n\n"
                f"Текущее описание:\n{current_desc}\n\n"
                f"📝 Отправьте новое описание:\n"
                f"• Максимум: {LIMITS['desc_max']} символов\n\n"
                f"Или нажмите кнопку для отмены:",
                parse_mode="HTML",
                reply_markup=telebot.types.InlineKeyboardMarkup().add(
                    telebot.types.InlineKeyboardButton("⬅️ Назад к лоту", callback_data=f"{CB_LOT_VIEW}:{lot_id}")
                )
            )
            if call.from_user.id not in user_data:
                user_data[call.from_user.id] = {}
            user_data[call.from_user.id]['editing_lot_id'] = lot_id
            tg.set_state(call.message.chat.id, result.id, call.from_user.id, CBT_EDIT_LOT_DESC)
        except:
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data == CB_BACK_TO_LIST)
    def callback_back_to_list(call):
        try:
            user_id = call.from_user.id
            if user_id not in user_data or 'lots' not in user_data[user_id]:
                bot.answer_callback_query(call.id, "❌ Данные устарели. Используйте /manage_lots")
                return
            lots = user_data[user_id]['lots']
            page = user_data[user_id].get('page', 0)
            keyboard = create_lots_keyboard(lots, page, selection_mode=is_selection_mode(user_id), selected_ids=get_selected_lot_ids(user_id))
            bot.edit_message_text(
                render_manage_lots_text(lots),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            bot.answer_callback_query(call.id)
        except:
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data == CB_EXPORT_MENU)
    def callback_export_menu(call):
        try:
            keyboard = create_export_keyboard()
            bot.edit_message_text(
                "📊 <b>Экспорт лотов</b>\n\nВыберите формат для экспорта:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            bot.answer_callback_query(call.id)
        except:
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data == CB_BULK_MENU)
    def callback_bulk_menu(call):
        try:
            bot.edit_message_text(
                "⚙️ <b>Массовые действия</b>\n\n"
                "Здесь можно изменить статус сразу у всех лотов\n"
                "или удалить все лоты.\n\n"
                "🔴 Скрыть все — только деактивирует\n"
                "🟢 Открыть все — только активирует\n"
                "🗑️ Удалить все — удаляет без возможности восстановления",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=create_bulk_actions_keyboard(),
                parse_mode="HTML"
            )
            bot.answer_callback_query(call.id)
        except:
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data == "noop")
    def callback_noop(call):
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda c: c.data.startswith(CB_IMPORT_CANCEL))
    def callback_import_cancel(call):
        pending = get_pending_import(call.from_user.id)
        if not pending:
            bot.answer_callback_query(call.id, "Импорт уже неактуален")
            return
        session_id = call.data.split(":", 1)[1]
        if pending.get("session_id") != session_id:
            bot.answer_callback_query(call.id, "Это старое окно импорта")
            return
        clear_pending_import(call.from_user.id)
        bot.edit_message_text(
            "❌ Импорт лотов отменён.",
            call.message.chat.id,
            call.message.message_id
        )
        bot.answer_callback_query(call.id, "Импорт отменён")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(CB_IMPORT_MODE))
    def callback_import_mode(call):
        if is_shared_operation_running() or is_user_busy(call.from_user.id):
            bot.answer_callback_query(call.id, "❌ Другая операция уже идёт")
            return
        pending = get_pending_import(call.from_user.id)
        if not pending:
            bot.answer_callback_query(call.id, "Импорт уже неактуален")
            return
        try:
            _, mode, session_id = call.data.split(":", 2)
        except ValueError:
            bot.answer_callback_query(call.id, "❌ Неизвестный режим")
            return
        if pending.get("session_id") != session_id:
            bot.answer_callback_query(call.id, "Это старое окно импорта")
            return

        mode_titles = {
            "add_new_only": "Импорт: только новые",
            "replace_matched_only": "Импорт: только замена",
            "create_all": "Импорт: создать всё новым",
            "sync_all": "Импорт: синхронизация"
        }
        if mode not in mode_titles:
            bot.answer_callback_query(call.id, "❌ Неизвестный режим")
            return

        set_user_busy(call.from_user.id)
        set_shared_operation_running(True)
        progress = create_progress_tracker(call.message.chat.id, mode_titles[mode], status="Готовлю импорт")
        try:
            update_progress_tracker(progress, current=0, total=len(pending.get("entries", [])), status="Запускаю импорт", force=True)
            result = import_lots_with_mode(call.message.chat.id, call.from_user.id, pending.get("entries", []), mode, progress_tracker=progress)
            clear_pending_import(call.from_user.id)
            finish_progress_tracker(
                progress,
                f"✅ Импорт завершён\n\nСоздано: {result['created']}\nЗаменено: {result['updated']}\nПропущено: {result['skipped']}\nОшибок: {result['failed']}"
            )
            bot.edit_message_text(
                "✅ <b>Импорт завершён</b>\n\n"
                f"Создано: <b>{result['created']}</b>\n"
                f"Заменено: <b>{result['updated']}</b>\n"
                f"Пропущено: <b>{result['skipped']}</b>\n"
                f"Ошибок: <b>{result['failed']}</b>\n"
                f"Конфликтов подкатегорий: <b>{result['node_conflicts']}</b>",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML"
            )
            bot.answer_callback_query(call.id, "Импорт выполнен")
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)
            finish_progress_tracker(progress, "❌ Импорт остановлен из-за ошибки.")
            bot.answer_callback_query(call.id, "❌ Ошибка импорта")
            bot.send_message(call.message.chat.id, "❌ Не удалось импортировать лоты. Проверьте логи Cardinal.")
        finally:
            set_shared_operation_running(False)
            clear_user_busy(call.from_user.id)

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{CB_CACHE_MODE}:"))
    def callback_cache_mode(call):
        mode = call.data.split(":", 1)[1]
        if mode == "cancel":
            bot.edit_message_text(
                "❌ Кэширование отменено.",
                call.message.chat.id,
                call.message.message_id
            )
            bot.answer_callback_query(call.id, "Отменено")
            return

        include_deactivated = mode == "all"
        bot.answer_callback_query(call.id, "Запускаю кэширование")
        cache_lots(build_fake_message(call.message.chat.id, call.from_user.id, "/cache_lots"), include_deactivated=include_deactivated)

    @bot.callback_query_handler(func=lambda c: c.data in {CB_MENU_MAIN, CB_MENU_LOTS, CB_MENU_TRANSFER, CB_MENU_SETTINGS, CB_MENU_HISTORY})
    def callback_manage_menu_sections(call):
        section_map = {
            CB_MENU_MAIN: "main",
            CB_MENU_LOTS: "lots",
            CB_MENU_TRANSFER: "transfer",
            CB_MENU_SETTINGS: "settings",
            CB_MENU_HISTORY: "history"
        }
        section = section_map.get(call.data, "main")
        bot.edit_message_text(
            render_manage_menu_text(section),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=create_manage_menu_keyboard(section),
            parse_mode="HTML"
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{CB_MENU_ACTION}:"))
    def callback_manage_menu_action(call):
        action = call.data.split(":", 1)[1]
        fake_message = build_fake_message(call.message.chat.id, call.from_user.id, f"/{action}")
        try:
            if action == "manage_lots":
                bot.answer_callback_query(call.id, "Открываю список лотов")
                manage_lots(fake_message)
            elif action == "create_lots":
                bot.answer_callback_query(call.id, "Открываю загрузку файла")
                start_create_lots_flow(call.message.chat.id, call.from_user.id)
            elif action == "cache_lots":
                bot.answer_callback_query(call.id, "Открываю выбор режима")
                bot.send_message(
                    call.message.chat.id,
                    "💾 <b>Кэширование лотов</b>\n\nВыбери, что выгружать в JSON:",
                    reply_markup=create_cache_mode_keyboard(),
                    parse_mode="HTML"
                )
            elif action == "copy_lots":
                bot.answer_callback_query(call.id, "Открываю ввод токена")
                act_copy_lots(fake_message)
            elif action == "lots_tags":
                bot.answer_callback_query(call.id, "Открываю теги")
                manage_lot_tags(fake_message)
            elif action == "tags_help":
                bot.answer_callback_query(call.id, "Показываю справку")
                show_lot_tags_help(fake_message)
            elif action == "disabled_lots":
                bot.answer_callback_query(call.id, "Открываю историю")
                view_disabled_lots(fake_message)
            elif action == "copy_with_secrets":
                bot.answer_callback_query(call.id, "Переключаю настройку")
                copy_with_secrets(fake_message)
                try:
                    bot.edit_message_text(
                        render_manage_menu_text("settings"),
                        call.message.chat.id,
                        call.message.message_id,
                        reply_markup=create_manage_menu_keyboard("settings"),
                        parse_mode="HTML"
                    )
                except Exception:
                    logger.debug("TRACEBACK", exc_info=True)
            else:
                bot.answer_callback_query(call.id, "Неизвестное действие")
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{CB_MENU_HISTORY_CLEAR}:"))
    def callback_manage_menu_history_clear(call):
        step = int(call.data.split(":", 1)[1])
        if step == 0:
            keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
            keyboard.row(
                telebot.types.InlineKeyboardButton("✅ Да, очистить", callback_data=f"{CB_MENU_HISTORY_CLEAR}:1"),
                telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data=CB_MENU_HISTORY)
            )
            bot.edit_message_text(
                "⚠️ <b>Очистить историю отключенных лотов?</b>\n\nЭто удалит сохранённую историю из файла.",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            bot.answer_callback_query(call.id)
            return

        fake_message = build_fake_message(call.message.chat.id, call.from_user.id, "/clear_disabled_history")
        try:
            clear_disabled_lots_history(fake_message)
            bot.edit_message_text(
                render_manage_menu_text("history"),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=create_manage_menu_keyboard("history"),
                parse_mode="HTML"
            )
            bot.answer_callback_query(call.id, "История очищена")
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data == CB_SUBCAT_MENU)
    def callback_subcategory_menu(call):
        try:
            bot.edit_message_text(
                render_subcategory_settings_text(),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=create_subcategory_settings_keyboard(),
                parse_mode="HTML"
            )
            bot.answer_callback_query(call.id)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data == CB_SUBCAT_ADD)
    def callback_subcategory_add(call):
        try:
            bot.answer_callback_query(call.id)
            result = bot.send_message(
                call.message.chat.id,
                "📝 Отправьте ID подкатегории числом.\n"
                "Можно один или несколько через запятую.\n\n"
                "Пример: <code>12345, 67890</code>",
                parse_mode="HTML",
                reply_markup=skb.CLEAR_STATE_BTN()
            )
            tg.set_state(call.message.chat.id, result.id, call.from_user.id, CBT_ADD_SUBCATEGORY_ID)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{CB_SUBCAT_REMOVE}:"))
    def callback_subcategory_remove(call):
        try:
            subcategory_id = int(call.data.split(":")[1])
            removed = remove_configured_subcategory_id(subcategory_id)
            bot.edit_message_text(
                render_subcategory_settings_text(),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=create_subcategory_settings_keyboard(),
                parse_mode="HTML"
            )
            bot.answer_callback_query(call.id, "✅ ID удалён" if removed else "ℹ️ ID уже отсутствует")
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{CB_SUBCAT_CLEAR_STEP}:"))
    def callback_subcategory_clear(call):
        try:
            step = call.data.split(":")[1]
            if step == "0":
                keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
                keyboard.row(
                    telebot.types.InlineKeyboardButton("🗑️ Да, очистить", callback_data=f"{CB_SUBCAT_CLEAR_STEP}:1"),
                    telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data=CB_SUBCAT_MENU)
                )
                bot.edit_message_text(
                    "🧹 <b>Очистить все ID подкатегорий?</b>\n\nЭто удалит только вручную добавленные ID.",
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id)
                return

            set_configured_subcategory_ids([])
            bot.edit_message_text(
                render_subcategory_settings_text(),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=create_subcategory_settings_keyboard(),
                parse_mode="HTML"
            )
            bot.answer_callback_query(call.id, "✅ Все ID очищены")
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data == CB_SUBCAT_DISCOVERED_TOGGLE)
    def callback_subcategory_discovered_toggle(call):
        try:
            enabled = toggle_discovered_subcategory_ids()
            bot.edit_message_text(
                render_subcategory_settings_text(),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=create_subcategory_settings_keyboard(),
                parse_mode="HTML"
            )
            bot.answer_callback_query(call.id, f"Автопоиск {'включён' if enabled else 'выключен'}")
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data == CB_ADD_LOTS)
    def callback_add_lots(call):
        try:
            bot.answer_callback_query(call.id)
            start_create_lots_flow(call.message.chat.id, call.from_user.id)
        except:
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data == CB_SELECT_MODE)
    def callback_select_mode(call):
        try:
            user_id = call.from_user.id
            if user_id not in user_data or 'lots' not in user_data[user_id]:
                bot.answer_callback_query(call.id, "❌ Данные устарели. Используйте /manage_lots")
                return
            set_selection_mode(user_id, True)
            user_data[user_id]['selected_lot_ids'] = []
            lots = user_data[user_id]['lots']
            page = user_data[user_id].get('page', 0)
            keyboard = create_lots_keyboard(lots, page, selection_mode=True, selected_ids=[])
            bot.edit_message_text(
                render_manage_lots_text(lots, "✅ <b>Режим выбора включен</b>\n\nНажимайте на лоты, чтобы отметить их."),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            bot.answer_callback_query(call.id)
        except:
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{CB_SELECT_TOGGLE}:"))
    def callback_select_toggle(call):
        try:
            user_id = call.from_user.id
            lot_id = int(call.data.split(":", 1)[1])
            if user_id not in user_data or 'lots' not in user_data[user_id]:
                bot.answer_callback_query(call.id, "❌ Данные устарели. Используйте /manage_lots")
                return
            selected = toggle_selected_lot(user_id, lot_id)
            lots = user_data[user_id]['lots']
            page = user_data[user_id].get('page', 0)
            keyboard = create_lots_keyboard(lots, page, selection_mode=True, selected_ids=selected)
            bot.edit_message_text(
                render_manage_lots_text(lots, f"✅ <b>Выбрано лотов:</b> {len(selected)}"),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            bot.answer_callback_query(call.id)
        except:
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data == CB_SELECT_ALL_PAGE)
    def callback_select_all_page(call):
        try:
            user_id = call.from_user.id
            lots = user_data[user_id]['lots']
            page = user_data[user_id].get('page', 0)
            page_lots = lots[page * 8:(page + 1) * 8]
            state = get_user_state(user_id)
            selected = state.setdefault('selected_lot_ids', [])
            for lot in page_lots:
                if lot.id not in selected:
                    selected.append(lot.id)
            keyboard = create_lots_keyboard(lots, page, selection_mode=True, selected_ids=selected)
            bot.edit_message_text(
                render_manage_lots_text(lots, f"✅ <b>Выбрано лотов:</b> {len(selected)}"),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            bot.answer_callback_query(call.id, "✅ Страница выделена")
        except:
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data == CB_SELECT_CLEAR)
    def callback_select_clear(call):
        try:
            user_id = call.from_user.id
            get_user_state(user_id)['selected_lot_ids'] = []
            lots = user_data[user_id]['lots']
            page = user_data[user_id].get('page', 0)
            keyboard = create_lots_keyboard(lots, page, selection_mode=True, selected_ids=[])
            bot.edit_message_text(
                render_manage_lots_text(lots, "🧹 <b>Выделение очищено</b>"),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            bot.answer_callback_query(call.id)
        except:
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data == CB_SELECT_CANCEL)
    def callback_select_cancel(call):
        try:
            clear_selected_lots(call.from_user.id)
            refresh_manage_lots_message(call, "❌ <b>Режим выбора выключен</b>")
            bot.answer_callback_query(call.id)
        except:
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data == CB_SELECTED_MENU)
    def callback_selected_menu(call):
        try:
            user_id = call.from_user.id
            count = len(get_selected_lot_ids(user_id))
            keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
            keyboard.row(
                telebot.types.InlineKeyboardButton("🔴 Скрыть отмеченные", callback_data=f"{CB_SELECTED_ACTION}:hide"),
                telebot.types.InlineKeyboardButton("🟢 Открыть отмеченные", callback_data=f"{CB_SELECTED_ACTION}:show")
            )
            keyboard.row(
                telebot.types.InlineKeyboardButton("🗑️ Удалить отмеченные", callback_data=f"{CB_SELECTED_DELETE_STEP}:0")
            )
            keyboard.row(telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data=f"{CB_PAGE}:{user_data[user_id].get('page', 0)}"))
            bot.edit_message_text(
                f"⚙️ <b>Действия с отмеченными</b>\n\nВыбрано лотов: {count}",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            bot.answer_callback_query(call.id)
        except:
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{CB_SELECTED_ACTION}:"))
    def callback_selected_action(call):
        if is_user_busy(call.from_user.id):
            bot.answer_callback_query(call.id, "❌ Операция уже выполняется")
            return
        set_user_busy(call.from_user.id)
        try:
            action = call.data.split(":", 1)[1]
            activate = action == "show"
            progress = create_progress_tracker(call.message.chat.id, "Обработка отмеченных лотов", status="Готовлю список")
            total, success, failed, _ = perform_selected_toggle(call.message.chat.id, call.from_user.id, activate, progress_tracker=progress)
            if total == 0 and not failed:
                finish_progress_tracker(progress, "ℹ️ Для отмеченных лотов изменений не потребовалось.")
                clear_user_busy(call.from_user.id)
                bot.answer_callback_query(call.id, "Нечего менять")
                return
            notice = (
                f"✅ <b>Массовое действие по отмеченным завершено</b>\n\n"
                f"Обработано: {success}\nОшибок: {len(failed)}"
            )
            if failed:
                notice += "\nНе удалось: " + ", ".join(map(lambda x: f"#{x}", failed[:10]))
            refresh_manage_lots_message(call, notice)
            finish_progress_tracker(progress, f"✅ Обработка отмеченных завершена\n\nУспешно: {success}\nОшибок: {len(failed)}")
            clear_user_busy(call.from_user.id)
            bot.answer_callback_query(call.id, "✅ Готово")
        except:
            try:
                finish_progress_tracker(progress, "❌ Обработка отмеченных остановлена из-за ошибки.")
            except Exception:
                pass
            clear_user_busy(call.from_user.id)
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{CB_SELECTED_DELETE_STEP}:"))
    def callback_selected_delete_step(call):
        try:
            user_id = call.from_user.id
            selected = get_selected_lot_ids(user_id)
            step = int(call.data.split(":", 1)[1])
            if not selected:
                bot.answer_callback_query(call.id, "❌ Ничего не выбрано")
                return
            if step == 0:
                keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
                keyboard.row(
                    telebot.types.InlineKeyboardButton("Да, продолжить", callback_data=f"{CB_SELECTED_DELETE_STEP}:1"),
                    telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data=CB_SELECTED_MENU)
                )
                bot.edit_message_text(
                    f"⚠️ <b>Удаление отмеченных лотов</b>\n\nБудет удалено: {len(selected)} лотов.",
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id)
                return

            if is_user_busy(user_id):
                bot.answer_callback_query(call.id, "❌ Операция уже выполняется")
                return
            set_user_busy(user_id)
            progress = create_progress_tracker(call.message.chat.id, "Удаление отмеченных лотов", status="Готовлю список")
            total, success, failed, _ = perform_selected_delete(call.message.chat.id, user_id, progress_tracker=progress)
            notice = f"✅ <b>Удаление отмеченных завершено</b>\n\nУдалено: {success}\nОшибок: {len(failed)}"
            if failed:
                notice += "\nНе удалось: " + ", ".join(map(lambda x: f"#{x}", failed[:10]))
            refresh_manage_lots_message(call, notice)
            finish_progress_tracker(progress, f"✅ Удаление отмеченных завершено\n\nУдалено: {success}\nОшибок: {len(failed)}")
            clear_user_busy(user_id)
            bot.answer_callback_query(call.id, "✅ Удаление завершено")
        except:
            try:
                finish_progress_tracker(progress, "❌ Удаление отмеченных остановлено из-за ошибки.")
            except Exception:
                pass
            clear_user_busy(call.from_user.id)
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{CB_BULK_ACTION}:"))
    def callback_bulk_action(call):
        if is_user_busy(call.from_user.id):
            bot.answer_callback_query(call.id, "❌ Операция уже выполняется")
            return
        set_user_busy(call.from_user.id)
        try:
            action = call.data.split(":", 1)[1]
            progress = create_progress_tracker(call.message.chat.id, "Массовое действие с лотами", status="Готовлю список")
            if action == "hide":
                total, success, failed, _ = perform_bulk_toggle(call.message.chat.id, call.from_user.id, False, progress_tracker=progress)
                if total == 0:
                    if not get_user_state(call.from_user.id).get('lots'):
                        finish_progress_tracker(progress, "📭 Лотов нет\n\nНет ни активных, ни скрытых лотов.")
                        bot.answer_callback_query(call.id, "Лотов нет")
                    else:
                        finish_progress_tracker(progress, "ℹ️ Все лоты уже скрыты.")
                        bot.answer_callback_query(call.id, "Все лоты уже скрыты")
                    clear_user_busy(call.from_user.id)
                    return
                notice = f"✅ <b>Массовое скрытие завершено</b>\n\nСкрыто: {success}\nОшибок: {len(failed)}"
            else:
                total, success, failed, _ = perform_bulk_toggle(call.message.chat.id, call.from_user.id, True, progress_tracker=progress)
                if total == 0:
                    if not get_user_state(call.from_user.id).get('lots'):
                        finish_progress_tracker(progress, "📭 Лотов нет\n\nНет ни активных, ни скрытых лотов.")
                        bot.answer_callback_query(call.id, "Лотов нет")
                    else:
                        finish_progress_tracker(progress, "ℹ️ Все лоты уже открыты.")
                        bot.answer_callback_query(call.id, "Все лоты уже открыты")
                    clear_user_busy(call.from_user.id)
                    return
                notice = f"✅ <b>Массовая активация завершена</b>\n\nАктивировано: {success}\nОшибок: {len(failed)}"

            if failed:
                notice += "\nНе удалось: " + ", ".join(map(lambda x: f"#{x}", failed[:10]))
            refresh_manage_lots_message(call, notice)
            finish_progress_tracker(progress, f"✅ Массовое действие завершено\n\nУспешно: {success}\nОшибок: {len(failed)}")
            bot.answer_callback_query(call.id, "✅ Готово")
            clear_user_busy(call.from_user.id)
        except Exception:
            try:
                finish_progress_tracker(progress, "❌ Массовое действие остановлено из-за ошибки.")
            except Exception:
                pass
            clear_user_busy(call.from_user.id)
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{CB_BULK_DELETE_STEP}:"))
    def callback_bulk_delete_step(call):
        try:
            step = int(call.data.split(":", 1)[1])
            lots = get_all_lots(call.message.chat.id)
            count = len(lots)
            if count == 0:
                bot.edit_message_text(
                    "📭 Лотов нет.\n\nНет ни активных, ни скрытых лотов.",
                    call.message.chat.id,
                    call.message.message_id
                )
                bot.answer_callback_query(call.id, "Лотов нет")
                return
            texts = {
                0: f"⚠️ <b>Удаление всех лотов</b>\n\nБудут удалены все лоты: {count} шт.\n\nЭто необратимо.",
                1: "⚠️ <b>Подтвердите ещё раз</b>\n\nУдалятся и активные, и скрытые лоты.\nПосле этого восстановить их нельзя.",
                2: "🚨 <b>Последствия удаления</b>\n\nБудет очищен весь текущий список лотов аккаунта.",
                3: f"🗑️ <b>Последнее подтверждение</b>\n\nТочно удалить все {count} лотов?"
            }
            keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
            if step < 3:
                keyboard.row(
                    telebot.types.InlineKeyboardButton("Да, продолжить", callback_data=f"{CB_BULK_DELETE_STEP}:{step + 1}"),
                    telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data=CB_BULK_MENU)
                )
            else:
                keyboard.row(
                    telebot.types.InlineKeyboardButton("✅ Да, удалить все", callback_data=f"{CB_BULK_DELETE_STEP}:4"),
                    telebot.types.InlineKeyboardButton("❌ Отмена", callback_data=CB_BULK_MENU)
                )

            if step < 4:
                bot.edit_message_text(
                    texts[step],
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id)
                return

            if is_user_busy(call.from_user.id):
                bot.answer_callback_query(call.id, "❌ Операция уже выполняется")
                return
            set_user_busy(call.from_user.id)
            progress = create_progress_tracker(call.message.chat.id, "Удаление всех лотов", status="Готовлю список")
            total, success, failed, _ = perform_bulk_delete(call.message.chat.id, call.from_user.id, progress_tracker=progress)
            notice = f"✅ <b>Массовое удаление завершено</b>\n\nУдалено: {success}\nОшибок: {len(failed)}"
            if failed:
                notice += "\nНе удалось: " + ", ".join(map(lambda x: f"#{x}", failed[:10]))
            refresh_manage_lots_message(call, notice)
            finish_progress_tracker(progress, f"✅ Массовое удаление завершено\n\nУдалено: {success}\nОшибок: {len(failed)}")
            clear_user_busy(call.from_user.id)
            bot.answer_callback_query(call.id, "✅ Удаление завершено")
        except Exception:
            try:
                finish_progress_tracker(progress, "❌ Массовое удаление остановлено из-за ошибки.")
            except Exception:
                pass
            clear_user_busy(call.from_user.id)
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка")

    @bot.callback_query_handler(func=lambda c: c.data == CB_EXPORT_JSON)
    def callback_export_json(call):
        if is_user_busy(call.from_user.id):
            bot.answer_callback_query(call.id, "❌ Операция уже выполняется")
            return
        set_user_busy(call.from_user.id)
        try:
            import csv
            bot.answer_callback_query(call.id, "⏳ Экспортирую...")
            progress = create_progress_tracker(call.message.chat.id, "Экспорт в JSON", status="Получаю список лотов")
            lots = get_all_lots(call.message.chat.id, progress_tracker=progress, progress_status="Получаю список лотов")
            lots_data = []
            total_lots = len(lots)
            for index, lot in enumerate(lots, start=1):
                lots_data.append({
                    "id": lot.id,
                    "title": lot.description,
                    "price": lot.price,
                    "currency": lot.currency,
                    "active": lot.active,
                    "category": lot.subcategory.fullname,
                    "url": f"https://funpay.com/lots/offer?id={lot.id}"
                })
                update_progress_tracker(progress, current=index, total=total_lots,
                                        status=f"Готовлю JSON: {index}/{total_lots}", force=index == total_lots)
            filename = f"storage/cache/lots_export_{int(time.time())}.json"
            update_progress_tracker(progress, current=total_lots, total=total_lots, status="Отправляю файл", force=True)
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(lots_data, f, indent=4, ensure_ascii=False)
            with open(filename, "rb") as f:
                bot.send_document(call.message.chat.id, f, caption="✅ Экспорт в JSON завершен!")
            finish_progress_tracker(progress, f"✅ Экспорт в JSON завершен\n\nЛотов: {len(lots_data)}")
            clear_user_busy(call.from_user.id)
        except:
            try:
                finish_progress_tracker(progress, "❌ Экспорт в JSON остановлен из-за ошибки.")
            except Exception:
                pass
            clear_user_busy(call.from_user.id)
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка экспорта")

    @bot.callback_query_handler(func=lambda c: c.data == CB_EXPORT_CSV)
    def callback_export_csv(call):
        if is_user_busy(call.from_user.id):
            bot.answer_callback_query(call.id, "❌ Операция уже выполняется")
            return
        set_user_busy(call.from_user.id)
        try:
            import csv
            bot.answer_callback_query(call.id, "⏳ Экспортирую...")
            progress = create_progress_tracker(call.message.chat.id, "Экспорт в CSV", status="Получаю список лотов")
            lots = get_all_lots(call.message.chat.id, progress_tracker=progress, progress_status="Получаю список лотов")
            filename = f"storage/cache/lots_export_{int(time.time())}.csv"
            total_lots = len(lots)
            with open(filename, "w", encoding="utf-8-sig", newline='') as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow(["ID", "Название", "Цена", "Валюта", "Активен", "Категория", "URL"])
                for index, lot in enumerate(lots, start=1):
                    writer.writerow([
                        lot.id,
                        lot.description,
                        lot.price,
                        lot.currency,
                        "Да" if lot.active else "Нет",
                        lot.subcategory.fullname,
                        f"https://funpay.com/lots/offer?id={lot.id}"
                    ])
                    update_progress_tracker(progress, current=index, total=total_lots,
                                            status=f"Готовлю CSV: {index}/{total_lots}", force=index == total_lots)
            update_progress_tracker(progress, current=total_lots, total=total_lots, status="Отправляю файл", force=True)
            with open(filename, "rb") as f:
                bot.send_document(call.message.chat.id, f, caption="✅ Экспорт в CSV завершен!")
            finish_progress_tracker(progress, f"✅ Экспорт в CSV завершен\n\nЛотов: {total_lots}")
            clear_user_busy(call.from_user.id)
        except:
            try:
                finish_progress_tracker(progress, "❌ Экспорт в CSV остановлен из-за ошибки.")
            except Exception:
                pass
            clear_user_busy(call.from_user.id)
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка экспорта")

    @bot.callback_query_handler(func=lambda c: c.data == CB_EXPORT_TXT)
    def callback_export_txt(call):
        if is_user_busy(call.from_user.id):
            bot.answer_callback_query(call.id, "❌ Операция уже выполняется")
            return
        set_user_busy(call.from_user.id)
        try:
            bot.answer_callback_query(call.id, "⏳ Экспортирую...")
            progress = create_progress_tracker(call.message.chat.id, "Экспорт в TXT", status="Получаю список лотов")
            lots = get_all_lots(call.message.chat.id, progress_tracker=progress, progress_status="Получаю список лотов")
            filename = f"storage/cache/lots_export_{int(time.time())}.txt"
            total_lots = len(lots)
            with open(filename, "w", encoding="utf-8") as f:
                f.write("=" * 60 + "\n")
                f.write("СПИСОК ЛОТОВ\n")
                f.write("=" * 60 + "\n\n")
                for i, lot in enumerate(lots, 1):
                    f.write(f"Лот #{i}\n")
                    f.write(f"ID: {lot.id}\n")
                    f.write(f"Название: {lot.description}\n")
                    f.write(f"Цена: {lot.price} {lot.currency}\n")
                    f.write(f"Статус: {'Активен' if lot.active else 'Неактивен'}\n")
                    f.write(f"Категория: {lot.subcategory.fullname}\n")
                    f.write(f"URL: https://funpay.com/lots/offer?id={lot.id}\n")
                    f.write("-" * 60 + "\n\n")
                    update_progress_tracker(progress, current=i, total=total_lots,
                                            status=f"Готовлю TXT: {i}/{total_lots}", force=i == total_lots)
            update_progress_tracker(progress, current=total_lots, total=total_lots, status="Отправляю файл", force=True)
            with open(filename, "rb") as f:
                bot.send_document(call.message.chat.id, f, caption="✅ Экспорт в TXT завершен!")
            finish_progress_tracker(progress, f"✅ Экспорт в TXT завершен\n\nЛотов: {total_lots}")
            clear_user_busy(call.from_user.id)
        except:
            try:
                finish_progress_tracker(progress, "❌ Экспорт в TXT остановлен из-за ошибки.")
            except Exception:
                pass
            clear_user_busy(call.from_user.id)
            logger.debug("TRACEBACK", exc_info=True)
            bot.answer_callback_query(call.id, "❌ Ошибка экспорта")

    def handle_edit_price(m: telebot.types.Message):
        if m.from_user.id not in user_data or 'editing_lot_id' not in user_data[m.from_user.id]:
            bot.send_message(m.chat.id, "❌ Ошибка: данные лота не найдены.")
            return
        lot_id = user_data[m.from_user.id]['editing_lot_id']
        new_price = m.text.strip()
        valid, error_msg, price_value = validate_price(new_price)
        if not valid:
            bot.send_message(m.chat.id, error_msg)
            return
        tg.clear_state(m.chat.id, m.from_user.id, True)
        if is_user_busy(m.from_user.id):
            bot.send_message(m.chat.id, "❌ Операция уже выполняется")
            return
        set_user_busy(m.from_user.id)
        try:
            bot.send_message(m.chat.id, f"⏳ Изменяю цену лота #{lot_id}...")
            lot_fields = get_lot_fields_by_id(lot_id, m.chat.id)
            lot_fields.price = price_value

            if save_lot_changes(lot_fields, m.chat.id):
                update_cached_lots_for_user(m.from_user.id, m.chat.id)
                user_data[m.from_user.id].pop('editing_lot_id', None)
                keyboard = telebot.types.InlineKeyboardMarkup().add(
                    telebot.types.InlineKeyboardButton("🔄 Обновить список", callback_data=f"{CB_PAGE}:0")
                )
                bot.send_message(
                    m.chat.id,
                    f"✅ Цена изменена на <b>{price_value} ₽</b>",
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
            else:
                bot.send_message(m.chat.id, "❌ Не удалось сохранить изменения.")
            clear_user_busy(m.from_user.id)
        except:
            clear_user_busy(m.from_user.id)
            logger.debug("TRACEBACK", exc_info=True)
            bot.send_message(m.chat.id, "❌ Произошла ошибка при изменении цены.")

    def handle_edit_title_ru(m: telebot.types.Message):
        if m.from_user.id not in user_data or 'editing_lot_id' not in user_data[m.from_user.id]:
            bot.send_message(m.chat.id, "❌ Ошибка: данные лота не найдены.")
            return
        lot_id = user_data[m.from_user.id]['editing_lot_id']
        new_title_ru = m.text.strip()
        valid, error_msg = validate_title(new_title_ru)
        if not valid:
            bot.send_message(m.chat.id, error_msg)
            return
        tg.clear_state(m.chat.id, m.from_user.id, True)
        
        if is_user_busy(m.from_user.id):
            bot.send_message(m.chat.id, "❌ Операция уже выполняется")
            return
        set_user_busy(m.from_user.id)
        try:
            bot.send_message(m.chat.id, f"⏳ Изменяю русское название лота #{lot_id}...")
            lot_fields = get_lot_fields_by_id(lot_id, m.chat.id)
            lot_fields.title_ru = new_title_ru

            if save_lot_changes(lot_fields, m.chat.id):
                update_cached_lots_for_user(m.from_user.id, m.chat.id)
                user_data[m.from_user.id].pop('editing_lot_id', None)
                keyboard = telebot.types.InlineKeyboardMarkup().add(
                    telebot.types.InlineKeyboardButton("🔄 Обновить список", callback_data=f"{CB_PAGE}:0")
                )
                bot.send_message(
                    m.chat.id,
                    f"✅ Русское название изменено!\n\nНовое название (RU): <b>{html_text(new_title_ru)}</b>",
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
            else:
                bot.send_message(m.chat.id, "❌ Не удалось сохранить изменения.")
            clear_user_busy(m.from_user.id)
        except Exception as e:
            clear_user_busy(m.from_user.id)
            logger.error(f"[LOTS] Ошибка: {str(e)}")
            logger.exception("TRACEBACK:")
            bot.send_message(m.chat.id, f"❌ Произошла ошибка при изменении названия: {str(e)}")

    def handle_edit_title_en(m: telebot.types.Message):
        if m.from_user.id not in user_data or 'editing_lot_id' not in user_data[m.from_user.id]:
            bot.send_message(m.chat.id, "❌ Ошибка: данные лота не найдены.")
            return
        lot_id = user_data[m.from_user.id]['editing_lot_id']
        new_title = m.text.strip()
        valid, error_msg = validate_title(new_title)
        if not valid:
            bot.send_message(m.chat.id, error_msg)
            return
        tg.clear_state(m.chat.id, m.from_user.id, True)
        if is_user_busy(m.from_user.id):
            bot.send_message(m.chat.id, "❌ Операция уже выполняется")
            return
        set_user_busy(m.from_user.id)
        try:
            bot.send_message(m.chat.id, f"⏳ Изменяю английское название лота #{lot_id}...")
            lot_fields = get_lot_fields_by_id(lot_id, m.chat.id)
            lot_fields.title_en = new_title

            if save_lot_changes(lot_fields, m.chat.id):
                update_cached_lots_for_user(m.from_user.id, m.chat.id)
                user_data[m.from_user.id].pop('editing_lot_id', None)
                keyboard = telebot.types.InlineKeyboardMarkup().add(
                    telebot.types.InlineKeyboardButton("🔄 Обновить список", callback_data=f"{CB_PAGE}:0")
                )
                bot.send_message(
                    m.chat.id,
                    f"✅ Английское название изменено!\n\n"
                    f"Новое название (EN): <b>{html_text(new_title)}</b>",
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
            else:
                bot.send_message(m.chat.id, "❌ Не удалось сохранить изменения.")
            clear_user_busy(m.from_user.id)
        except Exception as e:
            clear_user_busy(m.from_user.id)
            logger.error(f"[LOTS] Ошибка: {str(e)}")
            logger.exception("TRACEBACK:")
            bot.send_message(m.chat.id, f"❌ Произошла ошибка при изменении названия: {str(e)}")

    def handle_edit_desc(m: telebot.types.Message):
        if m.from_user.id not in user_data or 'editing_lot_id' not in user_data[m.from_user.id]:
            bot.send_message(m.chat.id, "❌ Ошибка: данные лота не найдены.")
            return
        lot_id = user_data[m.from_user.id]['editing_lot_id']
        new_desc = m.text.strip()
        valid, error_msg = validate_desc(new_desc)
        if not valid:
            bot.send_message(m.chat.id, error_msg)
            return
        tg.clear_state(m.chat.id, m.from_user.id, True)
        if is_user_busy(m.from_user.id):
            bot.send_message(m.chat.id, "❌ Операция уже выполняется")
            return
        set_user_busy(m.from_user.id)
        try:
            bot.send_message(m.chat.id, f"⏳ Изменяю описание лота #{lot_id}...")
            lot_fields = get_lot_fields_by_id(lot_id, m.chat.id)
            lot_fields.description_ru = new_desc
            lot_fields.description_en = new_desc

            if save_lot_changes(lot_fields, m.chat.id):
                update_cached_lots_for_user(m.from_user.id, m.chat.id)
                user_data[m.from_user.id].pop('editing_lot_id', None)
                keyboard = telebot.types.InlineKeyboardMarkup().add(
                    telebot.types.InlineKeyboardButton("🔄 Обновить список", callback_data=f"{CB_PAGE}:0")
                )
                desc_preview = new_desc[:200] + "..." if len(new_desc) > 200 else new_desc
                bot.send_message(
                    m.chat.id,
                    f"✅ Описание изменено!\n\nПревью:\n{html_text(desc_preview)}",
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
            else:
                bot.send_message(m.chat.id, "❌ Не удалось сохранить изменения.")
            clear_user_busy(m.from_user.id)
        except:
            clear_user_busy(m.from_user.id)
            logger.debug("TRACEBACK", exc_info=True)
            bot.send_message(m.chat.id, "❌ Произошла ошибка при изменении описания.")

    def handle_add_subcategory_id(m: telebot.types.Message):
        tg.clear_state(m.chat.id, m.from_user.id, True)
        parsed_ids, invalid_parts = parse_subcategory_ids_input(m.text)

        if not parsed_ids:
            bot.send_message(m.chat.id, "❌ Не найдено ни одного корректного ID. Отправьте числа, например: 12345, 67890")
            return

        added_ids = add_configured_subcategory_ids(parsed_ids)
        duplicates = [lot_id for lot_id in parsed_ids if lot_id not in added_ids]

        message = [f"✅ Добавлено ID: <code>{', '.join(str(i) for i in added_ids) if added_ids else '—'}</code>"]
        if duplicates:
            unique_duplicates = []
            seen = set()
            for lot_id in duplicates:
                if lot_id in seen:
                    continue
                seen.add(lot_id)
                unique_duplicates.append(lot_id)
            message.append(f"ℹ️ Уже были в списке: <code>{', '.join(str(i) for i in unique_duplicates)}</code>")
        if invalid_parts:
            message.append(f"⚠️ Пропущены некорректные значения: <code>{', '.join(invalid_parts)}</code>")

        keyboard = telebot.types.InlineKeyboardMarkup().add(
            telebot.types.InlineKeyboardButton("🗂️ Открыть список ID", callback_data=CB_SUBCAT_MENU)
        )
        bot.send_message(m.chat.id, "\n".join(message), parse_mode="HTML", reply_markup=keyboard)

    def view_disabled_lots(m: telebot.types.Message):
        """Показывает список отключенных лотов."""
        try:
            disabled_lots = load_disabled_lots()
            
            if not disabled_lots:
                bot.send_message(m.chat.id, "📭 Нет отключенных лотов в истории.")
                return
            
            message = "📋 <b>История отключенных лотов</b>\n\n"
            message += f"Всего отключенных: {len(disabled_lots)}\n"
            message += f"Файл: <code>{DISABLED_LOTS_FILE}</code>\n\n"
            
            for lot_id, lot_info in disabled_lots.items():
                message += f"🔴 <b>Лот #{lot_id}</b>\n"
                message += f"   Название: {lot_info.get('title', 'Без названия')}\n"
                message += f"   Цена: {lot_info.get('price', '0')} ₽\n"
                message += f"   Отключен: {lot_info.get('disabled_at', 'Неизвестно')}\n"
                message += f"   <a href=\"https://funpay.com/lots/offer?id={lot_id}\">Открыть на FunPay</a>\n\n"
            
            # Разбиваем на части если слишком длинное
            if len(message) > 4096:
                parts = [message[i:i+4096] for i in range(0, len(message), 4096)]
                for part in parts:
                    bot.send_message(m.chat.id, part, parse_mode="HTML", disable_web_page_preview=True)
            else:
                bot.send_message(m.chat.id, message, parse_mode="HTML", disable_web_page_preview=True)
        except:
            logger.error("[LOTS] Ошибка при просмотре отключенных лотов.")
            logger.debug("TRACEBACK", exc_info=True)
            bot.send_message(m.chat.id, "❌ Произошла ошибка при получении списка отключенных лотов.")

    def clear_disabled_lots_history(m: telebot.types.Message):
        """Очищает историю отключенных лотов."""
        try:
            disabled_lots = load_disabled_lots()
            count = len(disabled_lots)
            
            if count == 0:
                bot.send_message(m.chat.id, "📭 История отключенных лотов уже пуста.")
                return
            
            # Очищаем файл
            save_disabled_lots({})
            bot.send_message(
                m.chat.id,
                f"✅ История отключенных лотов очищена!\n"
                f"Удалено записей: {count}"
            )
        except:
            logger.error("[LOTS] Ошибка при очистке истории отключенных лотов.")
            logger.debug("TRACEBACK", exc_info=True)
            bot.send_message(m.chat.id, "❌ Произошла ошибка при очистке истории.")

    def manage_lot_tags(m: telebot.types.Message):
        """Управление тегами лотов - создание/просмотр."""
        if is_user_busy(m.from_user.id):
            bot.send_message(m.chat.id, "❌ Операция уже выполняется. Подождите.")
            return
        
        set_user_busy(m.from_user.id)
        progress = create_progress_tracker(m.chat.id, "Генерация тегов лотов", status="Получаю список лотов")
        try:
            # Получаем все лоты
            lots = get_all_lots(m.chat.id, progress_tracker=progress, progress_status="Получаю список лотов")
            
            if not lots:
                finish_progress_tracker(progress, "📭 У вас нет лотов.")
                bot.send_message(m.chat.id, "📭 У вас нет лотов.")
                clear_user_busy(m.from_user.id)
                return
            
            # Загружаем существующие теги
            lot_tags = load_lot_tags()
            
            # Генерируем теги для новых лотов
            new_tags_count = 0
            updated_tags_count = 0
            total_lots = len(lots)
            
            for index, lot in enumerate(lots, start=1):
                lot_id_str = str(lot.id)
                
                # Если тег уже есть, обновляем информацию о лоте
                if lot_id_str in lot_tags:
                    lot_tags[lot_id_str]['title'] = lot.description
                    lot_tags[lot_id_str]['price'] = lot.price
                    lot_tags[lot_id_str]['url'] = f"https://funpay.com/lots/offer?id={lot.id}"
                    lot_tags[lot_id_str]['updated_at'] = time.strftime("%Y-%m-%d %H:%M:%S")
                    updated_tags_count += 1
                else:
                    # Генерируем новый тег
                    tag = generate_tag_name(lot.description, lot_tags)
                    lot_tags[lot_id_str] = {
                        'tag': tag,
                        'lot_id': lot.id,
                        'title': lot.description,
                        'price': lot.price,
                        'url': f"https://funpay.com/lots/offer?id={lot.id}",
                        'created_at': time.strftime("%Y-%m-%d %H:%M:%S"),
                        'updated_at': time.strftime("%Y-%m-%d %H:%M:%S")
                    }
                    new_tags_count += 1
                update_progress_tracker(progress, current=index, total=total_lots,
                                        status=f"Обновляю теги: {index}/{total_lots}", force=index == total_lots)
            
            # Сохраняем теги
            update_progress_tracker(progress, current=total_lots, total=total_lots, status="Сохраняю теги", force=True)
            save_lot_tags(lot_tags)
            
            # Формируем сообщение со списком тегов
            message = f"✅ <b>Теги лотов обновлены!</b>\n\n"
            message += f"📊 Статистика:\n"
            message += f"• Новых тегов: {new_tags_count}\n"
            message += f"• Обновлено: {updated_tags_count}\n"
            message += f"• Всего тегов: {len(lot_tags)}\n\n"
            message += f"🏷️ <b>Список тегов:</b>\n\n"
            
            # Сортируем по ID лота
            sorted_tags = sorted(lot_tags.items(), key=lambda x: int(x[0]))
            
            for lot_id, data in sorted_tags:
                title_preview = data['title'][:50] + "..." if len(data['title']) > 50 else data['title']
                message += f"<code>{data['tag']}</code>\n"
                message += f"  └ {title_preview}\n"
                message += f"  └ {data['price']} ₽ | ID: {lot_id}\n\n"
            
            message += f"\n💡 <b>Как использовать:</b>\n"
            message += f"Вставьте тег в любое сообщение, например:\n"
            message += f"<code>Привет! Посмотри {list(lot_tags.values())[0]['tag']}</code>\n\n"
            message += f"При отправке тег автоматически заменится на ссылку на лот.\n\n"
            message += f"📁 Теги сохранены в:\n<code>{LOT_TAGS_FILE}</code>"
            
            # Разбиваем на части если слишком длинное
            if len(message) > 4096:
                parts = [message[i:i+4096] for i in range(0, len(message), 4096)]
                for part in parts:
                    bot.send_message(m.chat.id, part, parse_mode="HTML")
            else:
                bot.send_message(m.chat.id, message, parse_mode="HTML")
            
            # Отправляем файл с тегами
            with open(LOT_TAGS_FILE, "rb") as f:
                bot.send_document(
                    m.chat.id, 
                    f, 
                    caption="📄 Файл с тегами лотов (для резервной копии)"
                )
            
            finish_progress_tracker(progress, f"✅ Теги обновлены\n\nНовых: {new_tags_count}\nОбновлено: {updated_tags_count}\nВсего: {len(lot_tags)}")
            clear_user_busy(m.from_user.id)
            logger.info(f"[LOTS] Теги обновлены: {new_tags_count} новых, {updated_tags_count} обновлено")
            
        except Exception as e:
            finish_progress_tracker(progress, "❌ Генерация тегов остановлена из-за ошибки.")
            clear_user_busy(m.from_user.id)
            logger.error(f"[LOTS] Ошибка при управлении тегами: {str(e)}")
            logger.exception("TRACEBACK:")
            bot.send_message(m.chat.id, f"❌ Произошла ошибка при генерации тегов.\n\nОшибка: {str(e)}")

    def show_lot_tags_help(m: telebot.types.Message):
        """Показывает справку по использованию тегов."""
        lot_tags = load_lot_tags()
        
        if not lot_tags:
            bot.send_message(
                m.chat.id,
                "❌ Теги еще не созданы.\n\n"
                "Используйте команду /lots_tags для создания тегов."
            )
            return
        
        message = f"📖 <b>Справка по тегам лотов</b>\n\n"
        message += f"🏷️ <b>Что это?</b>\n"
        message += f"Теги - это короткие переменные для быстрой вставки ссылок на лоты.\n\n"
        message += f"💡 <b>Как использовать:</b>\n"
        message += f"1. Скопируйте нужный тег из списка\n"
        message += f"2. Вставьте его в приветственное сообщение или любой текст\n"
        message += f"3. При отправке тег заменится на ссылку\n\n"
        message += f"📝 <b>Пример:</b>\n"
        message += f"<code>Привет! Посмотри мой лот {list(lot_tags.values())[0]['tag']}</code>\n\n"
        message += f"Превратится в:\n"
        message += f"Привет! Посмотри мой лот https://funpay.com/lots/offer?id={list(lot_tags.values())[0]['lot_id']}\n\n"
        message += f"📊 <b>Всего тегов:</b> {len(lot_tags)}\n\n"
        message += f"🔄 Обновить теги: /lots_tags\n"
        message += f"📋 Список всех тегов: /lots_tags"
        
        bot.send_message(m.chat.id, message, parse_mode="HTML")

    def edit_lot_tag(m: telebot.types.Message):
        """Изменить тег конкретного лота."""
        args = m.text.split(maxsplit=2)
        
        if len(args) < 3:
            bot.send_message(
                m.chat.id,
                "❌ Неверный формат команды.\n\n"
                "Использование:\n"
                "<code>/edit_lot_tag [ID лота] [новый_тег]</code>\n\n"
                "Пример:\n"
                "<code>/edit_lot_tag 12345 $my_cool_lot</code>",
                parse_mode="HTML"
            )
            return
        
        try:
            lot_id = int(args[1])
            new_tag = args[2].strip()
            
            # Проверяем формат тега
            if not new_tag.startswith('$'):
                bot.send_message(m.chat.id, "❌ Тег должен начинаться с символа $")
                return
            
            # Загружаем теги
            lot_tags = load_lot_tags()
            lot_id_str = str(lot_id)
            
            if lot_id_str not in lot_tags:
                bot.send_message(m.chat.id, f"❌ Лот с ID {lot_id} не найден в системе тегов.\n\nИспользуйте /lots_tags для создания тегов.")
                return
            
            # Проверяем что новый тег уникален
            for lid, data in lot_tags.items():
                if data['tag'] == new_tag and lid != lot_id_str:
                    bot.send_message(m.chat.id, f"❌ Тег {new_tag} уже используется для лота #{lid}")
                    return
            
            # Сохраняем старый тег
            old_tag = lot_tags[lot_id_str]['tag']
            
            # Обновляем тег
            lot_tags[lot_id_str]['tag'] = new_tag
            lot_tags[lot_id_str]['updated_at'] = time.strftime("%Y-%m-%d %H:%M:%S")
            
            # Сохраняем
            save_lot_tags(lot_tags)
            
            bot.send_message(
                m.chat.id,
                f"✅ Тег лота #{lot_id} изменен!\n\n"
                f"Старый тег: <code>{old_tag}</code>\n"
                f"Новый тег: <code>{new_tag}</code>\n\n"
                f"Название лота: {lot_tags[lot_id_str]['title'][:50]}...",
                parse_mode="HTML"
            )
            
        except ValueError:
            bot.send_message(m.chat.id, "❌ ID лота должен быть числом.")
        except Exception as e:
            logger.error(f"[LOTS] Ошибка при изменении тега: {str(e)}")
            logger.exception("TRACEBACK:")
            bot.send_message(m.chat.id, f"❌ Произошла ошибка: {str(e)}")

    # ========== СТАРЫЕ ДВУХШАГОВЫЕ ОБРАБОТЧИКИ ОТКЛЮЧЕНЫ ==========

    # ========== РЕГИСТРАЦИЯ КОМАНД ==========

    cardinal.add_telegram_commands(UUID, [
        ("copy_lots", "копирует активные лоты с текущего аккаунта на другой", True),
        ("cache_lots", "кэширует лоты в файл с выбором режима", True),
        ("create_lots", "создает лоты на текущем аккаунте", True),
        ("copy_with_secrets", "Копировать ли встроенную автовыдачу FunPay?", True),
        ("manage_menu", "главное меню управления плагином", True),
        ("manage_lots", "управление лотами (inline меню)", True),
        ("lot_subcats", "показать подкатегории для поиска лотов", True),
        ("add_lot_subcat", "добавить ID подкатегорий для поиска", True),
        ("remove_lot_subcat", "удалить ID подкатегорий из поиска", True),
        ("disabled_lots", "показать историю отключенных лотов", True),
        ("clear_disabled_history", "очистить историю отключенных лотов", True),
        ("lots_tags", "создать/обновить теги для всех лотов", True),
        ("tags_help", "справка по использованию тегов", True),
        ("edit_lot_tag", "изменить тег конкретного лота", True)
    ])

    tg.msg_handler(act_copy_lots, commands=["copy_lots"])
    tg.msg_handler(copy_lots, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, CBT_COPY_LOTS))
    tg.msg_handler(act_cache_lots, commands=["cache_lots"])
    tg.msg_handler(act_create_lots, commands=["create_lots"])
    tg.msg_handler(copy_with_secrets, commands=["copy_with_secrets"])
    tg.msg_handler(manage_menu, commands=["manage_menu"])
    tg.msg_handler(manage_lots, commands=["manage_lots"])
    tg.msg_handler(show_lot_subcats, commands=["lot_subcats"])
    tg.msg_handler(add_lot_subcat, commands=["add_lot_subcat"])
    tg.msg_handler(remove_lot_subcat, commands=["remove_lot_subcat"])
    tg.msg_handler(view_disabled_lots, commands=["disabled_lots"])
    tg.msg_handler(clear_disabled_lots_history, commands=["clear_disabled_history"])
    tg.msg_handler(manage_lot_tags, commands=["lots_tags"])
    tg.msg_handler(show_lot_tags_help, commands=["tags_help"])
    tg.msg_handler(edit_lot_tag, commands=["edit_lot_tag"])
    tg.msg_handler(handle_edit_price, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, CBT_EDIT_LOT_PRICE))
    tg.msg_handler(handle_edit_title_ru, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, CBT_EDIT_LOT_TITLE_RU))
    tg.msg_handler(handle_edit_title_en, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, CBT_EDIT_LOT_TITLE_EN))
    tg.msg_handler(handle_edit_desc, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, CBT_EDIT_LOT_DESC))
    tg.msg_handler(handle_add_subcategory_id, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, CBT_ADD_SUBCATEGORY_ID))
    tg.file_handler(CBT_CREATE_LOTS, create_lots)
    
    if exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                loaded_settings = json.load(f)
            if not isinstance(loaded_settings, dict):
                raise ValueError("настройки должны быть JSON-объектом")
            if isinstance(loaded_settings.get("with_secrets"), bool):
                settings["with_secrets"] = loaded_settings["with_secrets"]
            if isinstance(loaded_settings.get("lot_search_subcategory_ids"), list):
                settings["lot_search_subcategory_ids"] = loaded_settings["lot_search_subcategory_ids"]
            if isinstance(loaded_settings.get("lot_search_use_discovered_ids"), bool):
                settings["lot_search_use_discovered_ids"] = loaded_settings["lot_search_use_discovered_ids"]
            logger.info("[LOTS COPY] Настройки копирования лотов загружены.")
        except (OSError, ValueError, json.JSONDecodeError):
            logger.error("[LOTS COPY] Настройки повреждены; используются безопасные значения по умолчанию.")
            logger.debug("TRACEBACK", exc_info=True)
    
    logger.info("[LOTS COPY & MANAGE] Плагин инициализирован.")


BIND_TO_PRE_INIT = [init_commands]
BIND_TO_DELETE = None
