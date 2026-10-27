import os
import sqlite3
import datetime
from openai import AsyncOpenAI
import asyncio
import logging
from pathlib import Path
from html import escape
from contextlib import closing
from seed_data import seed_players, seed_clubs
from understatapi import UnderstatClient
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, filters, CallbackQueryHandler
)

from config import BOT_TOKEN, ADMIN_ID, BOT_USERNAME, OPENROUTER_API_KEY, DB_NAME
from texts import TEXTS

ai_client = None


# ==================== БД ====================

def db_connect():
    conn = sqlite3.connect(DB_NAME, timeout=10)
    conn.create_function('CASEFOLD', 1, lambda value: (value or '').casefold())
    return conn


def db_query(sql, params=(), *, one=False):
    with closing(db_connect()) as conn:
        cursor = conn.execute(sql, params)
        return cursor.fetchone() if one else cursor.fetchall()


def db_execute(sql, params=()):
    with closing(db_connect()) as conn, conn:
        conn.execute(sql, params)


def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS players (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, name_latin TEXT,
        birth_year INTEGER, position TEXT, current_club TEXT,
        total_goals INTEGER DEFAULT 0, total_games INTEGER DEFAULT 0,
        description TEXT, description_en TEXT, photo_id TEXT,
        understat_id TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS clubs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, city TEXT,
        founded INTEGER, stadium TEXT, trophies TEXT, legends TEXT, history TEXT)''')
    try:
        c.execute('ALTER TABLE players ADD COLUMN understat_id TEXT')
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def get_all_clubs():
    return db_query('SELECT id, name, city, founded, stadium, trophies, legends, history FROM clubs ORDER BY name', (), one=False)


def get_club_by_id(cid):
    return db_query('SELECT * FROM clubs WHERE id = ?', (cid,), one=True)


def get_players_by_club(club_name):
    return db_query('SELECT id, name, position, total_goals, total_games FROM players WHERE current_club = ? ORDER BY name', (club_name,), one=False)


def add_player(name, name_latin, birth_year, position, club, goals, games, desc, desc_en="", understat_id=None):
    db_execute('INSERT INTO players (name, name_latin, birth_year, position, current_club, total_goals, total_games, description, description_en, understat_id) VALUES (?,?,?,?,?,?,?,?,?,?)',
               (name, name_latin, birth_year, position, club, goals, games, desc, desc_en, understat_id))


def save_photo(surname, photo_id):
    players = search_players(surname)
    if len(players) != 1:
        return False
    db_execute('UPDATE players SET photo_id = ? WHERE id = ?', (photo_id, players[0][0]))
    return True


def delete_photo(surname):
    players = search_players(surname)
    if len(players) != 1:
        return False
    db_execute('UPDATE players SET photo_id = ? WHERE id = ?', (None, players[0][0]))
    return True


def search_players(q):
    return db_query('SELECT * FROM players WHERE CASEFOLD(name) LIKE CASEFOLD(?) OR CASEFOLD(name_latin) LIKE CASEFOLD(?) LIMIT 10', (f'%{q}%', f'%{q}%'), one=False)


def get_top_scorers(limit=10):
    return db_query('SELECT name, total_goals, total_games, current_club FROM players WHERE total_goals > 0 ORDER BY total_goals DESC LIMIT ?', (limit,), one=False)


def get_top_by_matches(limit=10):
    return db_query('SELECT name, total_games, total_goals, current_club FROM players WHERE total_games > 0 ORDER BY total_games DESC LIMIT ?', (limit,), one=False)


def get_oldest(limit=10):
    return db_query('SELECT name, birth_year, position, current_club FROM players WHERE birth_year > 0 ORDER BY birth_year ASC LIMIT ?', (limit,), one=False)


def get_youngest(limit=10):
    return db_query('SELECT name, birth_year, position, current_club FROM players WHERE birth_year > 0 ORDER BY birth_year DESC LIMIT ?', (limit,), one=False)


def get_all_count():
    return db_query('SELECT COUNT(*) FROM players', one=True)[0]


def get_random_player():
    return db_query('SELECT * FROM players ORDER BY RANDOM() LIMIT 1', (), one=True)


def get_random_player_for_game():
    return db_query("SELECT * FROM players WHERE name IS NOT NULL AND name != '' AND birth_year > 0 ORDER BY RANDOM() LIMIT 1", (), one=True)


def get_players_by_position(position=None):
    sql = 'SELECT id, name, position, current_club FROM players'
    if position:
        return db_query(sql + ' WHERE position = ? ORDER BY name', (position,))
    return db_query(sql + ' ORDER BY name')


def get_player_by_id(player_id):
    return db_query('SELECT * FROM players WHERE id = ?', (player_id,), one=True)


def update_player_understat(player_id, goals, games, understat_id):
    db_execute('UPDATE players SET total_goals = ?, total_games = ?, understat_id = ? WHERE id = ?',
               (goals, games, understat_id, player_id))


# ==================== ЯЗЫК ====================

def get_lang(context):
    return context.user_data.get('lang', 'ru')


def t(context, key):
    language = TEXTS.get(get_lang(context), TEXTS['ru'])
    return language.get(key, TEXTS['ru'][key])


def get_desc(context, p):
    if get_lang(context) == 'en' and p[9]:
        return p[9]
    return p[8] if p[8] else t(context, 'no_desc')


# ==================== СИДЫ ====================


# ==================== КЛАВИАТУРЫ ====================

def lang_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
         InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")]
    ])


def reply_help_kb(context):
    return ReplyKeyboardMarkup(
        [[KeyboardButton(t(context, 'reply_help_btn'))]],
        resize_keyboard=True,
        is_persistent=True
    )


def main_menu(context):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(context, 'search'), callback_data="search"),
         InlineKeyboardButton(t(context, 'top'), callback_data="top")],
        [InlineKeyboardButton(t(context, 'list'), callback_data="list"),
         InlineKeyboardButton(t(context, 'compare'), callback_data="compare")],
        [InlineKeyboardButton(t(context, 'clubs'), callback_data="clubs_menu"),
         InlineKeyboardButton(t(context, 'random'), callback_data="random_player")],
        [InlineKeyboardButton(t(context, 'game'), callback_data="game_start")],
        [InlineKeyboardButton(t(context, 'add'), callback_data="add")]
    ])


def back_menu_kb(context):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t(context, 'back_menu'), callback_data="main_menu")]])


def top_menu(context):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(context, 'top_goals'), callback_data="top_by_goals"),
         InlineKeyboardButton(t(context, 'top_matches'), callback_data="top_by_matches")],
        [InlineKeyboardButton(t(context, 'top_age_old'), callback_data="top_oldest"),
         InlineKeyboardButton(t(context, 'top_age_young'), callback_data="top_youngest")],
        [InlineKeyboardButton(t(context, 'back_menu'), callback_data="main_menu")]
    ])


def filter_menu(context):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(context, 'filter_all'), callback_data="filter_all"),
         InlineKeyboardButton(t(context, 'filter_forwards'), callback_data="filter_Forward")],
        [InlineKeyboardButton(t(context, 'filter_midfielders'), callback_data="filter_Midfielder"),
         InlineKeyboardButton(t(context, 'filter_defenders'), callback_data="filter_Defender")],
        [InlineKeyboardButton(t(context, 'filter_goalkeepers'), callback_data="filter_Goalkeeper")],
        [InlineKeyboardButton(t(context, 'back_menu'), callback_data="main_menu")]
    ])


def clubs_menu(context, page=0):
    clubs = get_all_clubs()
    per_page = 10
    start = page * per_page
    end = start + per_page
    page_clubs = clubs[start:end]
    keyboard = []
    for club in page_clubs:
        keyboard.append([InlineKeyboardButton(f"🏟️ {club[1]}", callback_data=f"club_{club[0]}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(t(context, 'prev'), callback_data=f"clubs_page_{page - 1}"))
    if end < len(clubs):
        nav.append(InlineKeyboardButton(t(context, 'next'), callback_data=f"clubs_page_{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(t(context, 'back_menu'), callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)


def game_kb(context):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(context, 'game_next_hint'), callback_data="game_next_hint")],
        [InlineKeyboardButton(t(context, 'game_give_up'), callback_data="game_give_up")],
        [InlineKeyboardButton(t(context, 'game_end'), callback_data="game_end")],
    ])


# ==================== ХЕЛПЕРЫ ИГРЫ ====================

def game_hint_text(context, player, hint_num):
    if hint_num == 1:
        return t(context, 'game_hint_pos').format(player[4] or t(context, 'unknown'))
    elif hint_num == 2:
        return t(context, 'game_hint_year').format(player[3] or t(context, 'unknown'))
    elif hint_num == 3:
        return t(context, 'game_hint_club').format(player[5] or t(context, 'free_agent'))
    elif hint_num == 4:
        return t(context, 'game_hint_goals').format(player[6])


async def start_game(query, context):
    if context.user_data.get('game_active'):
        await query.message.reply_text(t(context, 'game_already'))
        return
    player = get_random_player_for_game()
    if not player:
        await query.message.reply_text(t(context, 'game_no_players'), reply_markup=main_menu(context))
        return

    context.user_data['game_player'] = player
    context.user_data['game_hints_shown'] = 1
    context.user_data['game_active'] = True
    if 'game_score' not in context.user_data:
        context.user_data['game_score'] = 0

    text = t(context, 'game_start') + game_hint_text(context, player, 1)
    await query.message.reply_text(text, parse_mode='HTML', reply_markup=game_kb(context))


async def next_hint(query, context):
    if not context.user_data.get('game_active'):
        await query.message.reply_text(t(context, 'use_menu'), reply_markup=main_menu(context))
        return
    player = context.user_data['game_player']
    shown = context.user_data.get('game_hints_shown', 1)
    if shown >= 4:
        await query.message.reply_text(t(context, 'game_no_more_hints'))
        return
    shown += 1
    context.user_data['game_hints_shown'] = shown
    text = t(context, 'game_hint_num').format(shown) + game_hint_text(context, player, shown)
    await query.message.reply_text(text, parse_mode='HTML', reply_markup=game_kb(context))


async def give_up(query, context):
    if not context.user_data.get('game_active'):
        await query.message.reply_text(t(context, 'use_menu'), reply_markup=main_menu(context))
        return
    player = context.user_data['game_player']
    context.user_data.pop('game_player', None)
    context.user_data.pop('game_hints_shown', None)
    context.user_data.pop('game_active', None)
    await query.message.reply_text(
        t(context, 'game_give_up_text').format(player[1]),
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(t(context, 'game_again'), callback_data="game_start")],
            [InlineKeyboardButton(t(context, 'back_menu'), callback_data="main_menu")]
        ])
    )


# ==================== UNDERSTAT ОБНОВЛЕНИЕ ====================

def load_understat_players():
    with UnderstatClient() as understat:
        return understat.league(league='RFPL').get_player_data(season=os.getenv('UNDERSTAT_SEASON', '2025'))


async def refresh_from_understat(query, context, player_id):
    player = get_player_by_id(player_id)
    if not player or not player[2]:
        await query.message.reply_text(t(context, 'refresh_no_latin'))
        return
    try:
        players = await asyncio.to_thread(load_understat_players)
        matches = [p for p in players if
                   (player[11] and str(p.get('id')) == str(player[11])) or
                   p.get('player_name', '').casefold() == player[2].casefold()]
        if len(matches) != 1:
            await query.message.reply_text(t(context, 'refresh_no_understat'))
            return
        found = matches[0]
        season = os.getenv('UNDERSTAT_SEASON', '2025')
        await query.message.reply_text(
            f"Understat • {player[1]} • {season}\n"
            f"⚽ {int(found.get('goals', 0))}  📊 {int(found.get('games', 0))}\n"
            + ('Данные за сезон. Карьерная статистика в базе сохранена.' if get_lang(context) == 'ru'
               else 'Season statistics. Career totals in the database are unchanged.'))
    except Exception as error:
        logging.warning('Understat: %s', type(error).__name__)
        await query.message.reply_text(t(context, 'refresh_error'))


# ==================== ИИ (OPENROUTER) ====================

NORMALIZED_DB = Path(__file__).resolve().parent / 'football_normalized.db'


def _norm_text(value):
    return ' '.join((value or '').casefold().replace('ё', 'е').split())


def get_extended_player_data(player_name):
    """Дополнительный AI-контекст из расширенной football_normalized.db."""
    if not NORMALIZED_DB.exists():
        return None
    conn = sqlite3.connect(str(NORMALIZED_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        profiles = conn.execute("""SELECT player_name, full_name, birth_date, citizenship, height_cm,
            weight_kg, preferred_foot, current_club, position, shirt_number FROM player_profiles""").fetchall()
        target = _norm_text(player_name)
        profile = next((r for r in profiles if _norm_text(r['player_name']) == target), None)
        if profile is None:
            profile = next((r for r in profiles if target in _norm_text(r['player_name']) or _norm_text(r['player_name']) in target), None)
        if profile is None:
            return None
        canonical = profile['player_name']
        career = conn.execute("""SELECT club_name, date_from, date_to, appearances, goals, assists
            FROM player_career WHERE player_name = ?
            ORDER BY COALESCE(date_to, '9999') DESC, COALESCE(date_from, '') DESC LIMIT 20""", (canonical,)).fetchall()
        seasons = conn.execute("""SELECT season, competition, club_name, appearances, starts, minutes,
            goals, assists, yellow_cards, red_cards, shots, shots_on_target, penalties_scored,
            penalties_missed, winning_goals, xg, xa FROM player_season_stats WHERE player_name = ?
            ORDER BY season DESC, competition LIMIT 30""", (canonical,)).fetchall()
        totals = conn.execute("""SELECT COALESCE(SUM(appearances),0), COALESCE(SUM(goals),0),
            COALESCE(SUM(assists),0) FROM player_career WHERE player_name = ?""", (canonical,)).fetchone()
        return {'profile': dict(profile), 'career': [dict(r) for r in career], 'seasons': [dict(r) for r in seasons],
                'career_totals': {'appearances': totals[0], 'goals': totals[1], 'assists': totals[2]}}
    finally:
        conn.close()


def build_ai_context(player, extended):
    lines = ['ДАННЫЕ ИЗ ЛОКАЛЬНОЙ БАЗЫ PLAYERS HISTORY:', f'Имя: {player[1]}',
             f'Позиция (старая карточка): {player[4] or "нет данных"}', f'Клуб (старая карточка): {player[5] or "нет данных"}']
    if not extended:
        lines += [f'Матчи (старая карточка): {player[7] or 0}', f'Голы (старая карточка): {player[6] or 0}']
        return '\n'.join(lines)
    p = extended['profile']
    lines += ['', 'ПРОФИЛЬ:', f"Полное имя: {p.get('full_name') or p.get('player_name')}",
              f"Дата рождения: {p.get('birth_date') or 'нет данных'}", f"Гражданство: {p.get('citizenship') or 'нет данных'}",
              f"Рост: {p.get('height_cm') or 'нет данных'} см", f"Вес: {p.get('weight_kg') or 'нет данных'} кг",
              f"Рабочая нога: {p.get('preferred_foot') or 'нет данных'}", f"Текущий клуб: {p.get('current_club') or 'нет данных'}",
              f"Позиция: {p.get('position') or 'нет данных'}", f"Номер: {p.get('shirt_number') or 'нет данных'}"]
    totals = extended['career_totals']
    lines += ['', 'ИТОГО ПО ЗАПИСЯМ КАРЬЕРЫ:', f"Матчи: {totals['appearances']}; голы: {totals['goals']}; ассисты: {totals['assists']}"]
    if extended['career']:
        lines.append('\nКАРЬЕРА (до 20 последних записей):')
        for r in extended['career']:
            lines.append(f"- {r['club_name']}: {r.get('date_from') or '?'} — {r.get('date_to') or 'н.в.'}; матчи {r.get('appearances') or 0}, голы {r.get('goals') or 0}, ассисты {r.get('assists') or 0}")
    if extended['seasons']:
        lines.append('\nСЕЗОННАЯ СТАТИСТИКА (до 30 последних записей):')
        for r in extended['seasons']:
            extra=[]
            if r.get('xg') is not None: extra.append(f"xG {r['xg']}")
            if r.get('xa') is not None: extra.append(f"xA {r['xa']}")
            lines.append(f"- {r['season']} | {r['competition']} | {r['club_name']}: матчи {r.get('appearances') or 0}, старты {r.get('starts') or 0}, минуты {r.get('minutes') or 0}, голы {r.get('goals') or 0}, ассисты {r.get('assists') or 0}" + (f"; {', '.join(extra)}" if extra else ''))
    return '\n'.join(lines)


async def ask_deepseek(query, context, player_id):
    global ai_client
    if not OPENROUTER_API_KEY:
        await query.message.reply_text('🤖 ИИ пока не подключён на сервере. Добавь OPENROUTER_API_KEY в Variables сервиса бота на Railway.')
        return
    if ai_client is None:
        ai_client = AsyncOpenAI(api_key=OPENROUTER_API_KEY, base_url='https://openrouter.ai/api/v1', timeout=45, max_retries=2)
    player = get_player_by_id(player_id)
    if not player:
        await query.message.reply_text(t(context, 'ai_error')); return
    extended = get_extended_player_data(player[1])
    database_context = build_ai_context(player, extended)
    try:
        await context.bot.send_chat_action(chat_id=query.message.chat_id, action='typing')
    except Exception:
        pass
    if get_lang(context) == 'en':
        system_prompt = ('You are Players History AI, a football analyst specializing in Russian football. The database context is the primary source of truth for numbers. Never invent statistics, clubs, dates, achievements, injuries or transfers. If a fact is absent, say the database does not confirm it. Distinguish database facts from analysis. Answer with: profile, career/statistics, style/analysis, conclusion.')
        user_prompt = f'Analyze this player using the Players History database below.\n\n{database_context}'
    else:
        system_prompt = ('Ты — Players History AI, футбольный аналитик по российскому футболу. Контекст базы Players History — главный источник истины для цифр. Никогда не выдумывай статистику, клубы, даты, достижения, травмы или трансферы. Если факта нет в базе, прямо скажи об этом. Отделяй факты базы от аналитической оценки. Ответ: профиль, карьера/статистика, стиль и аналитика, вывод. Пиши понятно и компактно на русском.')
        user_prompt = f'Проанализируй футболиста по данным Players History ниже.\n\n{database_context}'
    try:
        response = await ai_client.chat.completions.create(
            model=os.getenv('OPENROUTER_MODEL', 'openrouter/free'),
            messages=[{'role':'system','content':system_prompt},{'role':'user','content':user_prompt}],
            stream=False, max_tokens=1100, temperature=0.25)
        answer=(response.choices[0].message.content or '').strip()
        if not answer:
            await query.message.reply_text(t(context, 'ai_empty')); return
        full_text=f"🤖 {player[1]} • Players History AI\n\n{answer}"
        for offset in range(0,len(full_text),4000):
            await query.message.reply_text(full_text[offset:offset+4000])
    except Exception as e:
        logging.exception('OpenRouter AI error: %s', e)
        await query.message.reply_text(t(context, 'ai_error'))


# ==================== ХЕНДЛЕРЫ ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args and args[0].startswith("player_"):
        try:
            player_id = int(args[0].split("_")[1])
        except (IndexError, ValueError):
            player_id = None

        if player_id:
            player = get_player_by_id(player_id)
            if player:
                await update.message.reply_text(
                    TEXTS['ru']['welcome'],
                    reply_markup=reply_help_kb(context)
                )
                await send_player_card(update.message, context, player, show_actions=True)
                return

    await update.message.reply_text(
        TEXTS['ru']['choose_lang'],
        reply_markup=lang_menu()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        t(context, 'help_text'),
        parse_mode='HTML',
        reply_markup=reply_help_kb(context)
    )


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(TEXTS['ru']['reset_done'], reply_markup=lang_menu())


async def savephoto_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not update.message.photo:
        await update.message.reply_text(TEXTS['ru']['savephoto_no_photo'])
        return
    caption = update.message.caption
    if not caption:
        await update.message.reply_text(TEXTS['ru']['savephoto_no_args'])
        return
    parts = caption.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text(TEXTS['ru']['savephoto_no_args'])
        return
    surname = parts[1].strip()
    photo_id = update.message.photo[-1].file_id
    ok = save_photo(surname, photo_id)
    if ok:
        await update.message.reply_text(TEXTS['ru']['savephoto_ok'].format(surname), parse_mode='HTML')
    else:
        await update.message.reply_text(TEXTS['ru']['savephoto_not_found'].format(surname))


async def deletephoto_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text(TEXTS['ru']['deletephoto_no_args'])
        return
    surname = parts[1].strip()
    ok = delete_photo(surname)
    if ok:
        await update.message.reply_text(TEXTS['ru']['deletephoto_ok'].format(surname), parse_mode='HTML')
    else:
        await update.message.reply_text(TEXTS['ru']['savephoto_not_found'].format(surname))


async def setclub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("❌ Использование: /setclub Батраков Galatasaray")
        return
    surname = parts[1].strip()
    new_club = parts[2].strip()

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('SELECT id, name FROM players WHERE name LIKE ?', (f'%{surname}%',))
    row = c.fetchone()
    if not row:
        conn.close()
        await update.message.reply_text(f"❌ Игрок '{surname}' не найден")
        return
    c.execute('UPDATE players SET current_club = ? WHERE id = ?', (new_club, row[0]))
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"✅ Клуб <b>{row[1]}</b> изменён на <b>{new_club}</b>",
        parse_mode='HTML'
    )


async def lang_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception as e:
        print(f"query.answer error: {e}")
    lang = 'ru' if query.data == 'lang_ru' else 'en'
    context.user_data['lang'] = lang
    await query.message.reply_text(
        TEXTS[lang]['welcome'],
        reply_markup=reply_help_kb(context)
    )
    await query.message.reply_text(
        TEXTS[lang]['main_menu'],
        reply_markup=main_menu(context)
    )


async def send_player_card(message, context, p, show_actions=False):
    club = p[5] if p[5] else t(context, 'free_agent')
    desc = get_desc(context, p)

    age_str = ""
    if p[3] and p[3] > 0:
        age = datetime.datetime.now().year - p[3]
        age_str = f" ({age} {t(context, 'player_age')})"

    gpm_str = ""
    if p[7] and p[7] > 0:
        gpm = p[6] / p[7]
        gpm_str = f"\n{t(context, 'player_goals_per_match')} {gpm:.2f}"

    text = (f"⚽ <b>{p[1]}</b>\n"
            f"{t(context, 'player_year')} {p[3] or t(context, 'unknown')}{age_str}\n"
            f"{t(context, 'player_position')} {p[4] or t(context, 'unknown')}\n"
            f"{t(context, 'player_club')} {club}\n"
            f"{t(context, 'player_goals')} {p[6]}\n"
            f"{t(context, 'player_games')} {p[7]}{gpm_str}\n"
            f"📖 {desc}")
    photo_id = p[10] if len(p) > 10 else None

    kb = None
    if show_actions:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(t(context, 'refresh_btn'), callback_data=f"refresh_{p[0]}")],
            [InlineKeyboardButton(t(context, 'ai_btn'), callback_data=f"ai_{p[0]}")],
            [InlineKeyboardButton(t(context, 'share_btn'), callback_data=f"share_{p[0]}")]
        ])

    if photo_id:
        await message.reply_photo(photo=photo_id)
        await message.reply_text(text, parse_mode='HTML', reply_markup=kb)
    else:
        await message.reply_text(text, parse_mode='HTML', reply_markup=kb)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception as e:
        print(f"query.answer error: {e}")

    if query.data.startswith("lang_"):
        await lang_choice(update, context)
        return

    if query.data in ("main_menu", "search", "compare", "game_start"):
        if query.data != "game_start":
            clear_modes(context)
        else:
            context.user_data.pop("search_mode", None)
            context.user_data.pop("compare_mode", None)

    if query.data == "main_menu":
        await query.message.reply_text(t(context, 'main_menu'), reply_markup=main_menu(context))

    elif query.data == "compare":
        await query.message.reply_text(t(context, 'compare_input'), parse_mode='HTML',
                                       reply_markup=back_menu_kb(context))
        context.user_data['compare_mode'] = True

    elif query.data == "top":
        await query.message.reply_text(t(context, 'top_choose'), parse_mode='HTML', reply_markup=top_menu(context))

    elif query.data == "top_by_goals":
        top_players = get_top_scorers(10)
        if not top_players:
            await query.message.reply_text(t(context, 'no_scorers'), reply_markup=top_menu(context))
            return
        text = t(context, 'top_by_goals_title')
        for i, p in enumerate(top_players, 1):
            club = p[3] if p[3] else t(context, 'free_agent')
            text += f"{i}. <b>{p[0]}</b> — {p[1]} ⚽ ({p[2]})\n   🏟️ {club}\n\n"
        await query.message.reply_text(text, parse_mode='HTML', reply_markup=top_menu(context))

    elif query.data == "top_by_matches":
        top_players = get_top_by_matches(10)
        if not top_players:
            await query.message.reply_text("📊", reply_markup=top_menu(context))
            return
        text = t(context, 'top_by_matches_title')
        for i, p in enumerate(top_players, 1):
            club = p[3] if p[3] else t(context, 'free_agent')
            text += f"{i}. <b>{p[0]}</b> — {p[1]} 📊 ({p[2]} ⚽)\n   🏟️ {club}\n\n"
        await query.message.reply_text(text, parse_mode='HTML', reply_markup=top_menu(context))

    elif query.data == "top_oldest":
        players = get_oldest(10)
        if not players:
            await query.message.reply_text("📊", reply_markup=top_menu(context))
            return
        text = t(context, 'top_oldest_title')
        for i, p in enumerate(players, 1):
            club = p[3] if p[3] else t(context, 'free_agent')
            text += f"{i}. <b>{p[0]}</b> — {p[1]} 📅 ({p[2]})\n   🏟️ {club}\n\n"
        await query.message.reply_text(text, parse_mode='HTML', reply_markup=top_menu(context))

    elif query.data == "top_youngest":
        players = get_youngest(10)
        if not players:
            await query.message.reply_text("📊", reply_markup=top_menu(context))
            return
        text = t(context, 'top_youngest_title')
        for i, p in enumerate(players, 1):
            club = p[3] if p[3] else t(context, 'free_agent')
            text += f"{i}. <b>{p[0]}</b> — {p[1]} 📅 ({p[2]})\n   🏟️ {club}\n\n"
        await query.message.reply_text(text, parse_mode='HTML', reply_markup=top_menu(context))

    elif query.data == "list":
        await query.message.reply_text(t(context, 'filter_choose'), parse_mode='HTML',
                                       reply_markup=filter_menu(context))

    elif query.data.startswith("filter_"):
        pos = query.data.split("_")[1]
        if pos == "all":
            players = get_players_by_position(None)
            title = t(context, 'filter_all')
        else:
            players = get_players_by_position(pos)
            title = {
                'Forward': t(context, 'filter_forwards'),
                'Midfielder': t(context, 'filter_midfielders'),
                'Defender': t(context, 'filter_defenders'),
                'Goalkeeper': t(context, 'filter_goalkeepers'),
            }.get(pos, pos)

        if not players:
            await query.message.reply_text("📭", reply_markup=filter_menu(context))
            return

        text = t(context, 'filter_result').format(title, len(players))
        for p in players:
            club = p[3] if p[3] else t(context, 'free_agent')
            text += f"🔹 {p[1]} — {p[2]} ({club})\n"

        await query.message.reply_text(text[:4000], parse_mode='HTML', reply_markup=filter_menu(context))

    elif query.data == "search":
        await query.message.reply_text(t(context, 'enter_name'), parse_mode='HTML',
                                       reply_markup=back_menu_kb(context))
        context.user_data['search_mode'] = True

    elif query.data == "random_player":
        player = get_random_player()
        if player:
            await send_player_card(query.message, context, player, show_actions=True)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(t(context, 'again'), callback_data="random_player")],
                [InlineKeyboardButton(t(context, 'back_menu'), callback_data="main_menu")]
            ])
            await query.message.reply_text(t(context, 'choose_action'), reply_markup=kb)
        else:
            await query.message.reply_text("📭", reply_markup=back_menu_kb(context))

    elif query.data == "game_start":
        await start_game(query, context)

    elif query.data == "game_next_hint":
        await next_hint(query, context)

    elif query.data == "game_give_up":
        await give_up(query, context)

    elif query.data == "game_end":
        score = context.user_data.get('game_score', 0)
        context.user_data.pop('game_player', None)
        context.user_data.pop('game_hints_shown', None)
        context.user_data.pop('game_active', None)
        await query.message.reply_text(
            t(context, 'game_end').format(score),
            reply_markup=main_menu(context)
        )

    elif query.data.startswith("refresh_"):
        player_id = int(query.data.split("_")[1])
        await refresh_from_understat(query, context, player_id)

    elif query.data.startswith("ai_"):
        player_id = int(query.data.split("_")[1])
        await ask_deepseek(query, context, player_id)

    elif query.data.startswith("share_"):
        player_id = int(query.data.split("_")[1])
        player = get_player_by_id(player_id)
        if player:
            await query.message.reply_text(
                t(context, 'share_text').format(player[1], BOT_USERNAME, player_id),
                parse_mode='HTML'
            )

    elif query.data == "clubs_menu":
        await query.message.reply_text(t(context, 'choose_club'), parse_mode='HTML',
                                       reply_markup=clubs_menu(context, 0))

    elif query.data.startswith("clubs_page_"):
        page = int(query.data.split("_")[2])
        await query.message.reply_text(t(context, 'choose_club_page').format(page + 1), parse_mode='HTML',
                                       reply_markup=clubs_menu(context, page))

    elif query.data.startswith("clubplayers_"):
        club_name = query.data.split("_", 1)[1]
        players = get_players_by_club(club_name)
        if not players:
            await query.message.reply_text(
                t(context, 'club_players_empty'),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(t(context, 'back_clubs'), callback_data="clubs_menu")],
                    [InlineKeyboardButton(t(context, 'back_menu'), callback_data="main_menu")]
                ])
            )
        else:
            text = t(context, 'club_players_title').format(club_name)
            for p in players:
                text += f"🔹 <b>{p[1]}</b> — {p[2]} (⚽ {p[3]}, 📊 {p[4]})\n"
            await query.message.reply_text(text, parse_mode='HTML',
                                           reply_markup=InlineKeyboardMarkup([
                                               [InlineKeyboardButton(t(context, 'back_clubs'),
                                                                     callback_data="clubs_menu")],
                                               [InlineKeyboardButton(t(context, 'back_menu'),
                                                                     callback_data="main_menu")]
                                           ]))

    elif query.data.startswith("club_"):
        club_id = int(query.data.split("_")[1])
        club = get_club_by_id(club_id)
        if club:
            text = (f"🏟️ <b>{club[1]}</b>\n"
                    f"{t(context, 'club_city')} {club[2]}\n"
                    f"{t(context, 'club_founded')} {club[3]}\n"
                    f"{t(context, 'club_stadium')} {club[4]}\n\n"
                    f"{t(context, 'club_trophies')}\n{club[5]}\n\n"
                    f"{t(context, 'club_legends')}\n{club[6]}\n\n"
                    f"{t(context, 'club_history')}\n{club[7]}")
            await query.message.reply_text(text, parse_mode='HTML',
                                           reply_markup=InlineKeyboardMarkup([
                                               [InlineKeyboardButton(t(context, 'club_players_btn'),
                                                                     callback_data=f"clubplayers_{club[1]}")],
                                               [InlineKeyboardButton(t(context, 'back_clubs'),
                                                                     callback_data="clubs_menu")],
                                               [InlineKeyboardButton(t(context, 'back_menu'),
                                                                     callback_data="main_menu")]
                                           ]))


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text_msg = update.message.text

    if text_msg in [TEXTS['ru']['reply_help_btn'], TEXTS['en']['reply_help_btn']]:
        await update.message.reply_text(
            t(context, 'help_text'),
            parse_mode='HTML',
            reply_markup=reply_help_kb(context)
        )
        return

    if context.user_data.get('game_active'):
        text = text_msg.strip().lower()
        player = context.user_data['game_player']
        correct_names = [player[1].lower(), (player[2] or '').lower()]
        surname_ru = player[1].split()[-1].lower()
        surname_lat = (player[2] or '').split()[-1].lower() if player[2] else ''

        if text in correct_names or text == surname_ru or text == surname_lat:
            hints_used = context.user_data.get('game_hints_shown', 1)
            points = 5 - hints_used
            score = context.user_data.get('game_score', 0) + points
            context.user_data['game_score'] = score

            context.user_data.pop('game_player', None)
            context.user_data.pop('game_hints_shown', None)
            context.user_data.pop('game_active', None)

            await update.message.reply_text(
                t(context, 'game_correct').format(player[1], points, score),
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(t(context, 'game_again'), callback_data="game_start")],
                    [InlineKeyboardButton(t(context, 'back_menu'), callback_data="main_menu")]
                ])
            )
        else:
            await update.message.reply_text(t(context, 'game_wrong'), reply_markup=game_kb(context))
        return

    if context.user_data.get('compare_mode'):
        text = text_msg
        context.user_data['compare_mode'] = False

        parts = [x.strip() for x in text.split(',')]
        if len(parts) != 2:
            await update.message.reply_text(t(context, 'compare_not_found'), reply_markup=back_menu_kb(context))
            return

        p1_list = search_players(parts[0])
        p2_list = search_players(parts[1])

        if not p1_list or not p2_list:
            await update.message.reply_text(t(context, 'compare_not_found'), reply_markup=back_menu_kb(context))
            return

        p1 = p1_list[0]
        p2 = p2_list[0]

        current_year = datetime.datetime.now().year
        age1 = current_year - p1[3] if p1[3] else 0
        age2 = current_year - p2[3] if p2[3] else 0

        avg1 = p1[6] / p1[7] if p1[7] > 0 else 0
        avg2 = p2[6] / p2[7] if p2[7] > 0 else 0

        club1 = p1[5] if p1[5] else t(context, 'free_agent')
        club2 = p2[5] if p2[5] else t(context, 'free_agent')

        criteria = [
            ('compare_by_goals', p1[6], p2[6]),
            ('compare_by_matches', p1[7], p2[7]),
            ('compare_by_avg', avg1, avg2),
            ('compare_by_age', -age1 if age1 > 0 else None, -age2 if age2 > 0 else None),
        ]
        winners = []
        for label, first, second in criteria:
            winner = 0
            if first is not None and second is not None:
                winner = 1 if first > second else 2 if second > first else 0
            winners.append((label, winner))
        p1_wins = sum(winner == 1 for _, winner in winners)
        p2_wins = sum(winner == 2 for _, winner in winners)

        text_out = t(context, 'compare_title')
        text_out += "━━━━━━━━━━━━━━━━━━━━━\n"
        text_out += f"⚽ <b>{p1[1]}</b>  {t(context, 'compare_vs')}  ⚽ <b>{p2[1]}</b>\n"
        text_out += "━━━━━━━━━━━━━━━━━━━━━\n\n"
        text_out += f"{t(context, 'compare_age')} {age1} {t(context, 'compare_years')} | {age2} {t(context, 'compare_years')}\n"
        text_out += f"📍 {p1[4] or '?'} | {p2[4] or '?'}\n"
        text_out += f"🏟️ {club1} | {club2}\n"
        text_out += f"⚡ <b>{p1[6]}</b> | ⚡ <b>{p2[6]}</b>\n"
        text_out += f"📊 <b>{p1[7]}</b> | 📊 <b>{p2[7]}</b>\n"
        text_out += f"{t(context, 'compare_goals_per_match')} {avg1:.2f} | {avg2:.2f}\n\n"
        text_out += "━━━━━━━━━━━━━━━━━━━━━\n"
        text_out += f"{t(context, 'compare_results')}\n"
        text_out += "━━━━━━━━━━━━━━━━━━━━━\n"

        for label, winner in winners:
            name = p1[1] if winner == 1 else p2[1] if winner == 2 else '🤝'
            text_out += f"{t(context, label)} {name}\n"
        text_out += "\n"

        total = p1_wins + p2_wins
        if p1_wins > p2_wins:
            text_out += t(context, 'compare_winner_total').format(p1[1], p1_wins, total)
        elif p2_wins > p1_wins:
            text_out += t(context, 'compare_winner_total').format(p2[1], p2_wins, total)
        else:
            text_out += t(context, 'compare_tie')

        await update.message.reply_text(text_out, parse_mode='HTML', reply_markup=main_menu(context))
        return

    if context.user_data.get('search_mode'):
        q = text_msg
        players = search_players(q)
        context.user_data['search_mode'] = False
        if not players:
            await update.message.reply_text(t(context, 'not_found').format(q), reply_markup=back_menu_kb(context))
            return
        for p in players:
            await send_player_card(update.message, context, p, show_actions=True)
        await update.message.reply_text(t(context, 'choose_action'), reply_markup=main_menu(context))
        return
    await update.message.reply_text(t(context, 'use_menu'), reply_markup=main_menu(context))


ADD_FIELDS = (
    ('name', 'enter_player_name'), ('name_latin', 'enter_latin'),
    ('year', 'enter_year'), ('position', 'enter_position'),
    ('club', 'enter_club'), ('goals', 'enter_goals'),
    ('games', 'enter_games'), ('description', 'enter_desc'),
)


def clear_modes(context):
    for key in ('search_mode', 'compare_mode', 'game_active', 'game_player', 'game_hints_shown'):
        context.user_data.pop(key, None)


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if update.effective_user.id != ADMIN_ID:
        await message.reply_text(t(context, 'no_rights'))
        return ConversationHandler.END
    if update.callback_query:
        await update.callback_query.answer()
    clear_modes(context)
    context.user_data['new_player'] = {}
    await message.reply_text(t(context, ADD_FIELDS[0][1]))
    return 0


async def add_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    draft = context.user_data['new_player']
    step = len(draft)
    field, _ = ADD_FIELDS[step]
    value = update.message.text.strip()
    if not value:
        await update.message.reply_text('Введите непустое значение / Enter a value.')
        return step
    if field in ('year', 'goals', 'games'):
        try:
            value = int(value)
            if value < 0 or (field == 'year' and value > datetime.datetime.now().year):
                raise ValueError
        except ValueError:
            await update.message.reply_text('Введите корректное неотрицательное число / Enter a valid nonnegative number.')
            return step
    if field == 'position' and value not in ('Forward', 'Midfielder', 'Defender', 'Goalkeeper'):
        await update.message.reply_text('Forward / Midfielder / Defender / Goalkeeper')
        return step
    draft[field] = value
    if step + 1 < len(ADD_FIELDS):
        await update.message.reply_text(t(context, ADD_FIELDS[step + 1][1]))
        return step + 1
    add_player(draft['name'], draft['name_latin'], draft['year'], draft['position'],
               draft['club'], draft['goals'], draft['games'], draft['description'])
    context.user_data.pop('new_player', None)
    await update.message.reply_text(t(context, 'added').format(escape(draft['name'])),
                                    parse_mode='HTML', reply_markup=main_menu(context))
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('new_player', None)
    clear_modes(context)
    await update.effective_message.reply_text(t(context, 'cancelled'), reply_markup=main_menu(context))
    return ConversationHandler.END


async def on_error(update, context):
    logging.error('Ошибка обработки обновления: %s', type(context.error).__name__)


def main():
    if not BOT_TOKEN:
        raise SystemExit('Укажите BOT_TOKEN в .env')
    init_db()
    if not get_all_count():
        seed_players()
    if not get_all_clubs():
        seed_clubs()
    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(False).build()
    conversation = ConversationHandler(
        entry_points=[CommandHandler('add', add_start), CallbackQueryHandler(add_start, pattern=r'^add$')],
        states={i: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_step)] for i in range(len(ADD_FIELDS))},
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True,
    )
    app.add_handler(conversation)
    for command, handler in [('start', start), ('help', help_command), ('reset', reset_command),
                              ('cancel', cancel), ('deletephoto', deletephoto_command), ('setclub', setclub_command)]:
        app.add_handler(CommandHandler(command, handler))
    app.add_handler(MessageHandler(filters.PHOTO & filters.CaptionRegex(r'^/savephoto'), savephoto_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)
    print(f'Бот запущен. Игроков: {get_all_count()}')
    app.run_polling()


if __name__ == '__main__':
    main()
