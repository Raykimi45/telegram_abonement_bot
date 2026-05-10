import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

TOKEN = os.getenv("TOKEN", "8159201968:AAHa7wHU5dsfSkbWUJMnpcd5D9hXfZQPhl8")

TIERS = {
    "premium": {
        "nom": "⭐ Premium",
        "prix": "9,99€/mois",
        "lien": "https://buy.stripe.com/test_9B6fZb2gTcy8b2p7tve3e00",
        "description": "Accès aux contenus premium"
    },
    "vip": {
        "nom": "👑 VIP Access",
        "prix": "Prix VIP",
        "lien": "https://buy.stripe.com/test_14AdR3bRt55G6M9cNPe3e01",
        "description": "Accès VIP complet"
    }
}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton(f"{TIERS['premium']['nom']} — {TIERS['premium']['prix']}", callback_data="premium")],
        [InlineKeyboardButton(f"{TIERS['vip']['nom']}", callback_data="vip")],
    ]
    await update.message.reply_text(
        "🔥 Passe à l'abonnement supérieur !\n\nChoisis ton offre 👇",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tier = TIERS.get(query.data)
    if not tier:
        return
    await query.edit_message_text(
        f"{tier['nom']}\n"
        f"💳 {tier['description']}\n\n"
        f"👉 Finalise ton abonnement ici :\n{tier['lien']}"
    )

app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(handle_choice))

print("✅ Bot démarré...")
app.run_polling()
