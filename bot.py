import os
import json
import time
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.error import Conflict

TOKEN = os.getenv("TOKEN")

PAYMENT_LINKS = {
    "plink_1TVXET7U8dMyWbthflB8Pxox": "premium",
    "plink_1TVXMR7U8dMyWbth22E7uF3n": "vip",
}

CANAUX = {
    "premium": -1003947632446,
    "vip":     -1003769970686,
}

TIERS = {
    "premium": {
        "nom": "⭐ Premium",
        "prix": "9,99€/mois",
        "lien": "https://buy.stripe.com/test_9B6fZb2gTcy8b2p7tve3e00",
    },
    "vip": {
        "nom": "👑 VIP Access",
        "lien": "https://buy.stripe.com/test_14AdR3bRt55G6M9cNPe3e01",
    }
}

bot = Bot(token=TOKEN)

class StripeWebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        if self.path != "/webhook":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        try:
            event = json.loads(body)
        except Exception:
            self.send_response(400)
            self.end_headers()
            return

        self.send_response(200)
        self.end_headers()

        if event.get("type") == "checkout.session.completed":
            session = event["data"]["object"]
            telegram_id = session.get("client_reference_id")
            payment_link = session.get("payment_link")
            tier = PAYMENT_LINKS.get(payment_link)

            print(f"📦 Paiement — telegram_id: {telegram_id}, tier: {tier}")

            if telegram_id and tier:
                asyncio.run(ajouter_membre(int(telegram_id), tier))
            else:
                print(f"❌ Manquant — telegram_id: {telegram_id}, payment_link: {payment_link}, tier: {tier}")

async def ajouter_membre(telegram_id: int, tier: str):
    try:
        invite = await bot.create_chat_invite_link(
            chat_id=CANAUX[tier],
            member_limit=1,
            creates_join_request=False
        )
        await bot.send_message(
            chat_id=telegram_id,
            text=f"✅ Paiement confirmé !\n\nRejoint ton canal {TIERS[tier]['nom']} ici (lien à usage unique) :\n{invite.invite_link}"
        )
        print(f"✅ Lien envoyé à {telegram_id} pour {tier}")
    except Exception as e:
        print(f"❌ Erreur: {e}")
        try:
            await bot.send_message(
                chat_id=telegram_id,
                text="✅ Paiement reçu ! Contacte le support si tu n'as pas encore accès."
            )
        except Exception:
            pass

def start_webhook_server():
    server = HTTPServer(("0.0.0.0", 8000), StripeWebhookHandler)
    server.serve_forever()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    keyboard = [
        [InlineKeyboardButton(
            f"{TIERS['premium']['nom']} — {TIERS['premium']['prix']}",
            url=f"{TIERS['premium']['lien']}?client_reference_id={telegram_id}"
        )],
        [InlineKeyboardButton(
            f"{TIERS['vip']['nom']}",
            url=f"{TIERS['vip']['lien']}?client_reference_id={telegram_id}"
        )],
    ]
    await update.message.reply_text(
        "🔥 Passe à l'abonnement supérieur !\n\nChoisis ton offre 👇",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

if __name__ == "__main__":
    time.sleep(15)
    t = threading.Thread(target=start_webhook_server, daemon=True)
    t.start()
    print("✅ Serveur webhook démarré sur le port 8000")

    while True:
        try:
            app = ApplicationBuilder().token(TOKEN).build()
            app.add_handler(CommandHandler("start", start))
            print("✅ Bot démarré...")
            app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
        except Conflict:
            print("⚠️ Conflit, retry dans 15s...")
            time.sleep(15)
        except Exception as e:
            print(f"❌ Erreur: {e}, retry dans 15s...")
            time.sleep(15)
