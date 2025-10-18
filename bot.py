import os
import logging
from telegram import Update, Poll, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, PollAnswerHandler, CallbackQueryHandler, filters, ContextTypes
import json
from datetime import datetime, timedelta
import sqlite3
from groq import Groq

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== CONFIGURATION ====================
# 🔑 METTEZ VOS TOKENS ICI:
TELEGRAM_BOT_TOKEN = "VOTRE_TOKEN_TELEGRAM_ICI"
GROQ_API_KEY = "VOTRE_CLE_GROQ_ICI"
ADMIN_ID = 123456789  # Remplacez par votre ID Telegram

groq_client = Groq(api_key=GROQ_API_KEY)

# ==================== BASE DE DONNÉES ====================
def init_db():
    conn = sqlite3.connect('medical_bot.db')
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        approved INTEGER DEFAULT 0,
        request_time TEXT,
        total_points INTEGER DEFAULT 0,
        correct_answers INTEGER DEFAULT 0,
        total_answers INTEGER DEFAULT 0,
        streak INTEGER DEFAULT 0,
        last_activity TEXT,
        specialty TEXT DEFAULT 'Toutes',
        difficulty TEXT DEFAULT 'Moyen'
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS cases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        specialty TEXT,
        difficulty TEXT,
        case_text TEXT,
        questions TEXT,
        created_by TEXT,
        created_at TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS answers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        case_id INTEGER,
        question_num INTEGER,
        is_correct INTEGER,
        answered_at TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS pending_requests (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        request_time TEXT
    )''')
    
    conn.commit()
    conn.close()

# ==================== FONCTIONS UTILITAIRES ====================
def is_admin(user_id):
    return user_id == ADMIN_ID

def is_approved(user_id):
    if is_admin(user_id):
        return True
    conn = sqlite3.connect('medical_bot.db')
    c = conn.cursor()
    c.execute("SELECT approved FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result and result[0] == 1

def can_request_again(user_id):
    conn = sqlite3.connect('medical_bot.db')
    c = conn.cursor()
    c.execute("SELECT request_time FROM pending_requests WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    
    if not result:
        return True
    
    last_request = datetime.fromisoformat(result[0])
    return datetime.now() - last_request > timedelta(hours=8)

def add_user(user_id, username, first_name):
    conn = sqlite3.connect('medical_bot.db')
    c = conn.cursor()
    approved = 1 if is_admin(user_id) else 0
    c.execute('''INSERT OR IGNORE INTO users (user_id, username, first_name, approved, last_activity) 
                 VALUES (?, ?, ?, ?, ?)''', 
              (user_id, username, first_name, approved, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_user_stats(user_id):
    conn = sqlite3.connect('medical_bot.db')
    c = conn.cursor()
    c.execute("SELECT total_points, correct_answers, total_answers, streak FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result if result else (0, 0, 0, 0)

def update_user_stats(user_id, is_correct):
    conn = sqlite3.connect('medical_bot.db')
    c = conn.cursor()
    points = 10 if is_correct else 2
    c.execute('''UPDATE users SET 
                 total_points = total_points + ?,
                 correct_answers = correct_answers + ?,
                 total_answers = total_answers + 1,
                 last_activity = ?
                 WHERE user_id = ?''',
              (points, 1 if is_correct else 0, datetime.now().isoformat(), user_id))
    conn.commit()
    conn.close()

def get_user_settings(user_id):
    conn = sqlite3.connect('medical_bot.db')
    c = conn.cursor()
    c.execute("SELECT specialty, difficulty FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result if result else ("Toutes", "Moyen")

# ==================== GÉNÉRATION DE CAS AVEC GROQ ====================
def generate_case(specialty="Toutes", difficulty="Moyen"):
    specialties_map = {
        "Toutes": "toutes spécialités médicales",
        "Cardiologie": "cardiologie",
        "Pédiatrie": "pédiatrie",
        "Neurologie": "neurologie",
        "Gastro": "gastro-entérologie",
        "Pneumologie": "pneumologie",
        "Néphrologie": "néphrologie",
        "Endocrinologie": "endocrinologie"
    }
    
    difficulty_map = {
        "Facile": "niveau externe (simple)",
        "Moyen": "niveau interne (moyennement difficile)",
        "Difficile": "niveau ECN/Résidanat (très difficile)"
    }
    
    specialty_text = specialties_map.get(specialty, "toutes spécialités")
    difficulty_text = difficulty_map.get(difficulty, "niveau moyen")
    
    prompt = f"""Tu es un professeur de médecine expert. Crée un cas clinique réaliste en français pour un étudiant en médecine.

**Spécialité**: {specialty_text}
**Difficulté**: {difficulty_text}

**Instructions strictes**:
1. Écris un cas clinique RÉALISTE et COMPLET avec:
   - Présentation du patient (âge, sexe, motif de consultation)
   - Histoire de la maladie (détaillée)
   - Antécédents médicaux pertinents
   - Examen clinique complet
   - Signes vitaux

2. Crée EXACTEMENT 4 questions QCM. Pour CHAQUE question:
   - Une question claire et précise
   - 4 options de réponse (A, B, C, D)
   - UNE SEULE réponse correcte
   - Les autres réponses doivent être plausibles mais incorrectes

3. Pour chaque question, fournis:
   - L'explication détaillée de la bonne réponse
   - Pourquoi les autres sont incorrectes
   - Points clés à retenir

**Format de réponse EXACT** (respecte ce format JSON):
```json
{{
  "cas": "Description complète du cas clinique ici...",
  "questions": [
    {{
      "question": "Quel est le diagnostic le plus probable?",
      "options": ["A) Option 1", "B) Option 2", "C) Option 3", "D) Option 4"],
      "correct": 0,
      "explication": "Explication détaillée...",
      "points_cles": ["Point 1", "Point 2", "Point 3"]
    }},
    ... (3 autres questions)
  ]
}}
```

**Important**: 
- Le champ "correct" est l'INDEX (0, 1, 2 ou 3) de la bonne réponse
- Sois précis médicalement
- Utilise la terminologie médicale française correcte
- Crée des distracteurs réalistes"""

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.1-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=2500
        )
        
        content = response.choices[0].message.content
        
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        
        case_data = json.loads(content)
        return case_data
    
    except Exception as e:
        logger.error(f"Erreur génération: {e}")
        return None

# ==================== COMMANDES TELEGRAM ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    
    add_user(user_id, user.username, user.first_name)
    
    if is_admin(user_id):
        await update.message.reply_text(
            f"🩺 **Bienvenue Docteur {user.first_name}!**\n\n"
            f"👨‍⚕️ *Mode Administrateur activé*\n\n"
            f"🎯 **Commandes principales:**\n"
            f"📋 /case - Nouveau cas clinique\n"
            f"⚙️ /settings - Paramètres\n"
            f"📊 /stats - Vos statistiques\n"
            f"➕ /addcase - Ajouter un cas (Admin)\n"
            f"👥 /requests - Demandes en attente (Admin)\n"
            f"❓ /help - Aide complète\n\n"
            f"🔬 Prêt à tester vos connaissances médicales?",
            parse_mode='Markdown'
        )
        return
    
    if is_approved(user_id):
        await update.message.reply_text(
            f"🩺 **Bienvenue {user.first_name}!**\n\n"
            f"✅ *Votre compte est activé*\n\n"
            f"🎯 **Commandes:**\n"
            f"📋 /case - Nouveau cas clinique\n"
            f"⚙️ /settings - Paramètres\n"
            f"📊 /stats - Vos statistiques\n"
            f"❓ /help - Aide\n\n"
            f"💊 Commencez avec /case !",
            parse_mode='Markdown'
        )
        return
    
    if not can_request_again(user_id):
        await update.message.reply_text(
            f"⏰ **Demande déjà envoyée**\n\n"
            f"Votre demande est en attente d'approbation.\n"
            f"Vous pourrez renvoyer une demande dans 8 heures si nécessaire.\n\n"
            f"⏳ Merci de votre patience!",
            parse_mode='Markdown'
        )
        return
    
    keyboard = [[InlineKeyboardButton("✅ Demander l'accès", callback_data=f"request_access_{user_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"🩺 **Bot Médical - Cas Cliniques**\n\n"
        f"👋 Bonjour {user.first_name}!\n\n"
        f"🔒 Ce bot est à accès restreint.\n"
        f"📨 Cliquez ci-dessous pour demander l'accès.\n\n"
        f"⏱️ L'administrateur sera notifié instantanément.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def request_access_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    user_id = user.id
    
    conn = sqlite3.connect('medical_bot.db')
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO pending_requests (user_id, username, first_name, request_time)
                 VALUES (?, ?, ?, ?)''',
              (user_id, user.username, user.first_name, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    
    await query.edit_message_text(
        f"✅ **Demande envoyée!**\n\n"
        f"📬 L'administrateur a été notifié.\n"
        f"⏳ Vous recevrez une réponse bientôt.\n\n"
        f"🔄 Si refusée, vous pourrez redemander dans 8h.",
        parse_mode='Markdown'
    )
    
    keyboard = [[
        InlineKeyboardButton("✅ Approuver", callback_data=f"approve_{user_id}"),
        InlineKeyboardButton("❌ Refuser", callback_data=f"reject_{user_id}")
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"🔔 **Nouvelle demande d'accès**\n\n"
             f"👤 Nom: {user.first_name}\n"
             f"🆔 ID: `{user_id}`\n"
             f"📱 Username: @{user.username if user.username else 'N/A'}\n"
             f"🕐 Date: {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def approve_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data.split("_")
    action = data[0]
    target_user_id = int(data[1])
    
    conn = sqlite3.connect('medical_bot.db')
    c = conn.cursor()
    
    if action == "approve":
        c.execute("UPDATE users SET approved = 1 WHERE user_id = ?", (target_user_id,))
        c.execute("DELETE FROM pending_requests WHERE user_id = ?", (target_user_id,))
        conn.commit()
        conn.close()
        
        await query.edit_message_text(
            f"✅ **Utilisateur approuvé!**\n\n"
            f"🆔 ID: `{target_user_id}`\n"
            f"👤 L'utilisateur a été notifié.",
            parse_mode='Markdown'
        )
        
        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"🎉 **Accès approuvé!**\n\n"
                 f"✅ Votre demande a été acceptée.\n"
                 f"🩺 Vous pouvez maintenant utiliser le bot!\n\n"
                 f"📋 Tapez /case pour commencer!",
            parse_mode='Markdown'
        )
    
    else:
        c.execute("DELETE FROM pending_requests WHERE user_id = ?", (target_user_id,))
        conn.commit()
        conn.close()
        
        await query.edit_message_text(
            f"❌ **Utilisateur refusé**\n\n"
            f"🆔 ID: `{target_user_id}`\n"
            f"⏰ Il pourra redemander dans 8h.",
            parse_mode='Markdown'
        )
        
        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"❌ **Demande refusée**\n\n"
                 f"Votre demande d'accès n'a pas été acceptée.\n"
                 f"⏰ Vous pouvez réessayer dans 8 heures.\n\n"
                 f"📧 Contactez l'administrateur si nécessaire.",
            parse_mode='Markdown'
        )

async def case_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not is_approved(user_id):
        await update.message.reply_text("❌ Vous devez être approuvé pour utiliser cette commande.")
        return
    
    await update.message.reply_text("🔬 **Génération du cas clinique...**\n⏳ Patientez quelques secondes...", parse_mode='Markdown')
    
    specialty, difficulty = get_user_settings(user_id)
    
    case_data = generate_case(specialty, difficulty)
    
    if not case_data:
        await update.message.reply_text("❌ Erreur lors de la génération. Réessayez avec /case")
        return
    
    conn = sqlite3.connect('medical_bot.db')
    c = conn.cursor()
    c.execute('''INSERT INTO cases (specialty, difficulty, case_text, questions, created_by, created_at)
                 VALUES (?, ?, ?, ?, ?, ?)''',
              (specialty, difficulty, case_data['cas'], json.dumps(case_data['questions'], ensure_ascii=False), 
               'AI', datetime.now().isoformat()))
    case_id = c.lastrowid
    conn.commit()
    conn.close()
    
    specialty_emoji = {
        "Cardiologie": "🫀",
        "Pédiatrie": "👶",
        "Neurologie": "🧠",
        "Gastro": "🫁",
        "Pneumologie": "🫁",
        "Néphrologie": "💧",
        "Endocrinologie": "🧬"
    }
    emoji = specialty_emoji.get(specialty, "🩺")
    
    await update.message.reply_text(
        f"{emoji} **CAS CLINIQUE #{case_id}**\n"
        f"🏥 Spécialité: *{specialty}*\n"
        f"⭐ Difficulté: *{difficulty}*\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"{case_data['cas']}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📝 *{len(case_data['questions'])} questions à suivre...*",
        parse_mode='Markdown'
    )
    
    context.user_data['current_case_id'] = case_id
    context.user_data['current_questions'] = case_data['questions']
    context.user_data['question_index'] = 0
    context.user_data['correct_count'] = 0
    
    await send_question(update, context)

async def send_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    questions = context.user_data.get('current_questions', [])
    index = context.user_data.get('question_index', 0)
    
    if index >= len(questions):
        return
    
    question_data = questions[index]
    
    message = await update.effective_chat.send_poll(
        question=f"❓ **Question {index + 1}/{len(questions)}**\n\n{question_data['question']}",
        options=question_data['options'],
        type=Poll.QUIZ,
        correct_option_id=question_data['correct'],
        is_anonymous=False,
        allows_multiple_answers=False,
        explanation=f"📖 {question_data['explication']}\n\n💡 Points clés:\n" + "\n".join([f"• {p}" for p in question_data['points_cles']]),
        parse_mode='Markdown'
    )
    
    context.user_data[f'poll_{message.poll.id}'] = {
        'case_id': context.user_data['current_case_id'],
        'question_num': index,
        'correct': question_data['correct']
    }

async def receive_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    user_id = answer.user.id
    poll_id = answer.poll_id
    
    poll_data = context.user_data.get(f'poll_{poll_id}')
    if not poll_data:
        return
    
    selected = answer.option_ids[0] if answer.option_ids else None
    is_correct = selected == poll_data['correct']
    
    update_user_stats(user_id, is_correct)
    
    conn = sqlite3.connect('medical_bot.db')
    c = conn.cursor()
    c.execute('''INSERT INTO answers (user_id, case_id, question_num, is_correct, answered_at)
                 VALUES (?, ?, ?, ?, ?)''',
              (user_id, poll_data['case_id'], poll_data['question_num'], 1 if is_correct else 0, 
               datetime.now().isoformat()))
    conn.commit()
    conn.close()
    
    if is_correct:
        context.user_data['correct_count'] = context.user_data.get('correct_count', 0) + 1
    
    context.user_data['question_index'] += 1
    
    import asyncio
    await asyncio.sleep(3)
    
    if context.user_data['question_index'] >= len(context.user_data.get('current_questions', [])):
        questions = context.user_data.get('current_questions', [])
        correct = context.user_data.get('correct_count', 0)
        total = len(questions)
        percentage = (correct / total * 100) if total > 0 else 0
        
        if percentage >= 75:
            result_emoji = "🏆"
            message = "Excellent travail!"
        elif percentage >= 50:
            result_emoji = "✅"
            message = "Bien joué!"
        else:
            result_emoji = "📚"
            message = "Continuez à réviser!"
        
        points_earned = correct * 10 + (total - correct) * 2
        total_points, _, _, streak = get_user_stats(user_id)
        
        await context.bot.send_message(
            chat_id=user_id,
            text=f"{result_emoji} **RÉSULTATS**\n\n"
                 f"✅ Bonnes réponses: {correct}/{total}\n"
                 f"📊 Taux de réussite: {percentage:.1f}%\n"
                 f"🎯 Points gagnés: +{points_earned}\n"
                 f"💰 Total: {total_points} points\n"
                 f"🔥 Série: {streak} jours\n\n"
                 f"💬 *{message}*\n\n"
                 f"📋 /case - Nouveau cas\n"
                 f"📊 /stats - Statistiques",
            parse_mode='Markdown'
        )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not is_approved(user_id):
        await update.message.reply_text("❌ Accès non autorisé.")
        return
    
    points, correct, total, streak = get_user_stats(user_id)
    percentage = (correct / total * 100) if total > 0 else 0
    
    if points < 500:
        level = "🥉 Bronze"
    elif points < 1500:
        level = "🥈 Argent"
    elif points < 3000:
        level = "🥇 Or"
    else:
        level = "💎 Platine"
    
    await update.message.reply_text(
        f"📊 **VOS STATISTIQUES**\n\n"
        f"🏅 Niveau: {level}\n"
        f"💰 Points totaux: {points}\n"
        f"✅ Bonnes réponses: {correct}/{total}\n"
        f"📈 Taux de réussite: {percentage:.1f}%\n"
        f"🔥 Série: {streak} jours\n\n"
        f"🎯 Continue comme ça! 💪",
        parse_mode='Markdown'
    )

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not is_approved(user_id):
        await update.message.reply_text("❌ Accès non autorisé.")
        return
    
    keyboard = [
        [InlineKeyboardButton("🏥 Spécialité", callback_data="settings_specialty")],
        [InlineKeyboardButton("⭐ Difficulté", callback_data="settings_difficulty")],
        [InlineKeyboardButton("🔙 Retour", callback_data="settings_close")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    specialty, difficulty = get_user_settings(user_id)
    
    await update.message.reply_text(
        f"⚙️ **PARAMÈTRES**\n\n"
        f"🏥 Spécialité actuelle: *{specialty}*\n"
        f"⭐ Difficulté: *{difficulty}*\n\n"
        f"Choisissez ce que vous voulez modifier:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data.split("_")
    
    if data[1] == "specialty":
        keyboard = [
            [InlineKeyboardButton("🫀 Cardiologie", callback_data="set_specialty_Cardiologie")],
            [InlineKeyboardButton("👶 Pédiatrie", callback_data="set_specialty_Pédiatrie")],
            [InlineKeyboardButton("🧠 Neurologie", callback_data="set_specialty_Neurologie")],
            [InlineKeyboardButton("🫁 Gastro", callback_data="set_specialty_Gastro")],
            [InlineKeyboardButton("🫁 Pneumologie", callback_data="set_specialty_Pneumologie")],
            [InlineKeyboardButton("💧 Néphrologie", callback_data="set_specialty_Néphrologie")],
            [InlineKeyboardButton("🧬 Endocrinologie", callback_data="set_specialty_Endocrinologie")],
            [InlineKeyboardButton("🩺 Toutes", callback_data="set_specialty_Toutes")],
            [InlineKeyboardButton("🔙 Retour", callback_data="settings_back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("🏥 Choisissez une spécialité:", reply_markup=reply_markup)
    
    elif data[1] == "difficulty":
        keyboard = [
            [InlineKeyboardButton("😊 Facile", callback_data="set_difficulty_Facile")],
            [InlineKeyboardButton("😐 Moyen", callback_data="set_difficulty_Moyen")],
            [InlineKeyboardButton("😰 Difficile", callback_data="set_difficulty_Difficile")],
            [InlineKeyboardButton("🔙 Retour", callback_data="settings_back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("⭐ Choisissez la difficulté:", reply_markup=reply_markup)
    
    elif data[1] == "close":
        await query.edit_message_text("⚙️ Paramètres fermés.")
    
    elif data[1] == "back":
        keyboard = [
            [InlineKeyboardButton("🏥 Spécialité", callback_data="settings_specialty")],
            [InlineKeyboardButton("⭐ Difficulté", callback_data="settings_difficulty")],
            [InlineKeyboardButton("🔙 Retour", callback_data="settings_close")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        specialty, difficulty = get_user_settings(query.from_user.id)
        await query.edit_message_text(
            f"⚙️ **PARAMÈTRES**\n\n"
            f"🏥 Spécialité: *{specialty}*\n"
            f"⭐ Difficulté: *{difficulty}*",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

async def set_preference_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data.split("_")
    pref_type = data[1]
    value = data[2]
    user_id = query.from_user.id
    
    conn = sqlite3.connect('medical_bot.db')
    c = conn.cursor()
    
    if pref_type == "specialty":
        c.execute("UPDATE users SET specialty = ? WHERE user_id = ?", (value, user_id))
        emoji = "🏥"
        text = "Spécialité"
    else:
        c.execute("UPDATE users SET difficulty = ? WHERE user_id = ?", (value, user_id))
        emoji = "⭐"
        text = "Difficulté"
    
    conn.commit()
    conn.close()
    
    await query.edit_message_text(
        f"✅ **Paramètre modifié!**\n\n"
        f"{emoji} {text}: *{value}*\n\n"
        f"📋 Utilisez /case pour générer un nouveau cas!",
        parse_mode='Markdown'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    admin_help = ""
    if is_admin(user_id):
        admin_help = (
            f"\n\n👨‍⚕️ **COMMANDES ADMIN:**\n"
            f"➕ /addcase - Ajouter un cas manuellement\n"
            f"👥 /requests - Voir demandes en attente"
        )
    
    await update.message.reply_text(
        f"❓ **AIDE - BOT MÉDICAL**\n\n"
        f"🎯 **Commandes principales:**\n"
        f"📋 /case - Générer un nouveau cas clinique\n"
        f"⚙️ /settings - Modifier spécialité/difficulté\n"
        f"📊 /stats - Voir vos statistiques\n"
        f"📚 /history - Historique des cas\n"
        f"❓ /help - Cette aide\n"
        f"{admin_help}\n\n"
        f"🩺 **Comment ça marche?**\n"
        f"1️⃣ Configurez vos préférences avec /settings\n"
        f"2️⃣ Demandez un cas avec /case\n"
        f"3️⃣ Lisez le cas clinique attentivement\n"
        f"4️⃣ Répondez aux 4 questions QCM\n"
        f"5️⃣ Recevez explications et votre score!\n\n"
        f"💡 **Système de points:**\n"
        f"✅ Bonne réponse: +10 points\n"
        f"❌ Mauvaise réponse: +2 points\n\n"
        f"🏅 **Niveaux:**\n"
        f"🥉 Bronze: 0-500 pts\n"
        f"🥈 Argent: 500-1500 pts\n"
        f"🥇 Or: 1500-3000 pts\n"
        f"💎 Platine: 3000+ pts",
        parse_mode='Markdown'
    )

async def addcase_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ Commande réservée aux administrateurs.")
        return
    
    await update.message.reply_text(
        f"➕ **AJOUTER UN CAS MANUELLEMENT**\n\n"
        f"📝 Envoyez votre cas au format suivant:\n\n"
        f"```\n"
        f"SPÉCIALITÉ: Cardiologie\n"
        f"DIFFICULTÉ: Moyen\n\n"
        f"CAS:\n"
        f"[Décrivez le cas clinique complet...]\n\n"
        f"Q1: [Question 1?]\n"
        f"A) Option 1\n"
        f"B) Option 2\n"
        f"C) Option 3\n"
        f"D) Option 4\n"
        f"CORRECT: A\n"
        f"EXPLICATION: [Explication...]\n"
        f"POINTS: Point 1; Point 2; Point 3\n\n"
        f"Q2: [Question 2?]\n"
        f"...(même format)\n"
        f"```\n\n"
        f"⚠️ Format strict requis!",
        parse_mode='Markdown'
    )
    
    context.user_data['awaiting_manual_case'] = True

async def receive_manual_case(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not is_admin(user_id) or not context.user_data.get('awaiting_manual_case'):
        return
    
    text = update.message.text
    
    try:
        lines = text.strip().split('\n')
        specialty = ""
        difficulty = ""
        case_text = ""
        questions = []
        
        current_question = None
        in_case = False
        
        for line in lines:
            line = line.strip()
            
            if line.startswith("SPÉCIALITÉ:"):
                specialty = line.split(":", 1)[1].strip()
            elif line.startswith("DIFFICULTÉ:"):
                difficulty = line.split(":", 1)[1].strip()
            elif line.startswith("CAS:"):
                in_case = True
                continue
            elif line.startswith("Q") and ":" in line:
                if current_question:
                    questions.append(current_question)
                in_case = False
                current_question = {
                    "question": line.split(":", 1)[1].strip(),
                    "options": [],
                    "correct": 0,
                    "explication": "",
                    "points_cles": []
                }
            elif line.startswith(("A)", "B)", "C)", "D)")) and current_question:
                current_question["options"].append(line)
            elif line.startswith("CORRECT:") and current_question:
                correct_letter = line.split(":", 1)[1].strip().upper()
                current_question["correct"] = ord(correct_letter) - ord('A')
            elif line.startswith("EXPLICATION:") and current_question:
                current_question["explication"] = line.split(":", 1)[1].strip()
            elif line.startswith("POINTS:") and current_question:
                points_text = line.split(":", 1)[1].strip()
                current_question["points_cles"] = [p.strip() for p in points_text.split(";")]
            elif in_case and line:
                case_text += line + "\n"
        
        if current_question:
            questions.append(current_question)
        
        if not specialty or not difficulty or not case_text or len(questions) < 4:
            raise ValueError("Format invalide")
        
        conn = sqlite3.connect('medical_bot.db')
        c = conn.cursor()
        c.execute('''INSERT INTO cases (specialty, difficulty, case_text, questions, created_by, created_at)
                     VALUES (?, ?, ?, ?, ?, ?)''',
                  (specialty, difficulty, case_text.strip(), json.dumps(questions, ensure_ascii=False),
                   'ADMIN', datetime.now().isoformat()))
        case_id = c.lastrowid
        conn.commit()
        conn.close()
        
        await update.message.reply_text(
            f"✅ **Cas ajouté avec succès!**\n\n"
            f"🆔 ID du cas: #{case_id}\n"
            f"🏥 Spécialité: {specialty}\n"
            f"⭐ Difficulté: {difficulty}\n"
            f"❓ Questions: {len(questions)}\n\n"
            f"🎯 Le cas est maintenant disponible!",
            parse_mode='Markdown'
        )
        
        context.user_data['awaiting_manual_case'] = False
    
    except Exception as e:
        await update.message.reply_text(
            f"❌ **Erreur de format!**\n\n"
            f"Détails: {str(e)}\n\n"
            f"Vérifiez le format et réessayez.",
            parse_mode='Markdown'
        )

async def requests_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ Commande admin uniquement.")
        return
    
    conn = sqlite3.connect('medical_bot.db')
    c = conn.cursor()
    c.execute("SELECT user_id, username, first_name, request_time FROM pending_requests ORDER BY request_time DESC")
    requests = c.fetchall()
    conn.close()
    
    if not requests:
        await update.message.reply_text("✅ Aucune demande en attente!")
        return
    
    for req in requests:
        user_id_req, username, first_name, req_time = req
        dt = datetime.fromisoformat(req_time)
        
        keyboard = [[
            InlineKeyboardButton("✅ Approuver", callback_data=f"approve_{user_id_req}"),
            InlineKeyboardButton("❌ Refuser", callback_data=f"reject_{user_id_req}")
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"👤 **{first_name}**\n"
            f"🆔 ID: `{user_id_req}`\n"
            f"📱 @{username if username else 'N/A'}\n"
            f"🕐 {dt.strftime('%d/%m/%Y %H:%M')}",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not is_approved(user_id):
        await update.message.reply_text("❌ Accès non autorisé.")
        return
    
    conn = sqlite3.connect('medical_bot.db')
    c = conn.cursor()
    c.execute('''SELECT c.id, c.specialty, c.difficulty, c.created_at,
                 COUNT(a.is_correct) as total,
                 SUM(a.is_correct) as correct
                 FROM cases c
                 LEFT JOIN answers a ON c.id = a.case_id AND a.user_id = ?
                 WHERE a.user_id = ?
                 GROUP BY c.id
                 ORDER BY c.created_at DESC
                 LIMIT 10''', (user_id, user_id))
    history = c.fetchall()
    conn.close()
    
    if not history:
        await update.message.reply_text("📚 Aucun historique. Commencez avec /case !")
        return
    
    text = f"📚 **HISTORIQUE (10 derniers cas)**\n\n"
    
    for h in history:
        case_id, specialty, difficulty, created_at, total, correct = h
        percentage = (correct / total * 100) if total > 0 else 0
        
        if percentage >= 75:
            emoji = "🏆"
        elif percentage >= 50:
            emoji = "✅"
        else:
            emoji = "📖"
        
        text += f"{emoji} **Cas #{case_id}**\n"
        text += f"   🏥 {specialty} | ⭐ {difficulty}\n"
        text += f"   📊 {correct}/{total} ({percentage:.0f}%)\n\n"
    
    await update.message.reply_text(text, parse_mode='Markdown')

# ==================== MAIN ====================
def main():
    init_db()
    
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("case", case_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("addcase", addcase_command))
    application.add_handler(CommandHandler("requests", requests_command))
    application.add_handler(CommandHandler("history", history_command))
    
    application.add_handler(CallbackQueryHandler(request_access_callback, pattern="^request_access_"))
    application.add_handler(CallbackQueryHandler(approve_reject_callback, pattern="^(approve|reject)_"))
    application.add_handler(CallbackQueryHandler(settings_callback, pattern="^settings_"))
    application.add_handler(CallbackQueryHandler(set_preference_callback, pattern="^set_"))
    
    application.add_handler(PollAnswerHandler(receive_poll_answer))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_manual_case))
    
    print("🩺 Bot médical démarré!")
    print(f"👨‍⚕️ Admin ID: {ADMIN_ID}")
    application.run_polling()

if __name__ == '__main__':
    main()
