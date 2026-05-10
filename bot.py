import os
import hmac
import hashlib
import json
import time
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.error import Conflict

TOKEN = os.getenv("TOKEN")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

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
        sig_header = self.headers.get("Stripe-Signature", "")

        if STRIPE_WEBHOOK_SECRET:
            try:
                timestamp = sig_header.split("t=")[1].split(",")[0]
                sig = sig_header.split("v1=")[1].split(",")[0]
                signed_payload = f"{timestamp}.{body.decode()}"
                expected = hmac.new(
                    STRIPE_WEBHOOK_SECRET.encode(),
                    signed_payload.encode(),
                    hashlib.sha256
                ).hexdigest()
                if not hmac.compare_digest(expected, sig):
                    self.send_response(400)
                    self.end_headers()
                    return
            except Exception:
                self.send_response(400)
                self.end_headers()
                return

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
            metadata = session.get("metadata", {})
            telegram_id = metadata.get("telegram_id")
            tier = metadata.get("tier")
            if telegram_id and tier and tier in CANAUX:
                asyncio.run(ajouter_membre(int(telegram_id), tier))

async def ajouter_membre(telegram_id: int, tier: str):
    try:
        await bot.add_chat_member(chat_id=CANAUX[tier], user_id=telegram_id)
        await bot.send_message(
            chat_id=telegram_id,
            text=f"✅ Paiement confirmé ! Tu as été ajouté au canal {TIERS[tier]['nom']}."
        )
    except Exception:
        await bot.send_message(
            chat_id=telegram_id,
            text="✅ Paiement reçu ! Contacte le support si tu n'as pas encore accès."
        )

def start_webhook_server():
    server = HTTPServer(("0.0.0.0", 8000), StripeWebhookHandler)
    server.serve_forever()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    keyboard = [
        [InlineKeyboardButton(
            f"{TIERS['premium']['nom']} — {TIERS['premium']['prix']}",
            url=f"{TIERS['premium']['lien']}?client_reference_id={telegram_id}&metadata[telegram_id]={telegram_id}&metadata[tier]=premium"
        )],
        [InlineKeyboardButton(
            f"{TIERS['vip']['nom']}",
            url=f"{TIERS['vip']['lien']}?client_reference_id={telegram_id}&metadata[telegram_id]={telegram_id}&metadata[tier]=vip"
        )],
    ]
    await update.message.reply_text(
        "🔥 Passe à l'abonnement supérieur !\n\nChoisis ton offre 👇",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

if __name__ == "__main__":
    # Attendre que l'ancienne instance soit complètement arrêtée
    time.sleep(5)

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
            print("⚠️ Conflit détecté, nouvelle tentative dans 5 secondes...")
            time.sleep(5)
        except Exception as e:
            print(f"❌ Erreur: {e}, redémarrage dans 5 secondes...")
            time.sleep(5)
