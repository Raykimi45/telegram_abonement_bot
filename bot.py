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

# Fichier de stockage sur le volume Railway
SUBS_FILE = "/data/subscriptions.json"

def load_subs():
    os.makedirs("/data", exist_ok=True)
    if not os.path.exists(SUBS_FILE):
        return {}
    try:
        with open(SUBS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_subs(subs):
    os.makedirs("/data", exist_ok=True)
    with open(SUBS_FILE, "w") as f:
        json.dump(subs, f)

# Event loop dédié pour les appels Telegram depuis le webhook
webhook_loop = asyncio.new_event_loop()

def run_webhook_loop():
    asyncio.set_event_loop(webhook_loop)
    webhook_loop.run_forever()

async def ajouter_membre(telegram_id: int, tier: str, subscription_id: str):
    bot = Bot(token=TOKEN)
    try:
        # Sauvegarder la correspondance subscription → telegram
        subs = load_subs()
        subs[subscription_id] = {"telegram_id": telegram_id, "tier": tier}
        save_subs(subs)
        print(f"💾 Sauvegardé: {subscription_id} → {telegram_id} ({tier})")

        # Envoyer lien d'invitation à usage unique
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
        print(f"❌ Erreur ajout: {e}")
        try:
            await bot.send_message(
                chat_id=telegram_id,
                text="✅ Paiement reçu ! Contacte le support si tu n'as pas encore accès."
            )
        except Exception:
            pass
    finally:
        await bot.shutdown()

async def retirer_membre(subscription_id: str):
    bot = Bot(token=TOKEN)
    try:
        subs = load_subs()
        sub = subs.get(subscription_id)

        if not sub:
            print(f"❌ Subscription inconnue: {subscription_id}")
            return

        telegram_id = sub["telegram_id"]
        tier = sub["tier"]

        # Kick du canal
        await bot.ban_chat_member(chat_id=CANAUX[tier], user_id=telegram_id)
        await bot.unban_chat_member(chat_id=CANAUX[tier], user_id=telegram_id)

        # Message à l'utilisateur
        await bot.send_message(
            chat_id=telegram_id,
            text=f"😔 Ton abonnement {TIERS[tier]['nom']} a expiré ou a été annulé.\n\nTu as été retiré du canal.\n\nTu peux te réabonner à tout moment 👇"
        )

        # Supprimer de la base
        del subs[subscription_id]
        save_subs(subs)
        print(f"✅ {telegram_id} retiré du canal {tier}")

    except Exception as e:
        print(f"❌ Erreur retrait: {e}")
    finally:
        await bot.shutdown()

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

        event_type = event.get("type")
        print(f"📨 Événement reçu: {event_type}")

        if event_type == "checkout.session.completed":
            session = event["data"]["object"]
            telegram_id = session.get("client_reference_id")
            payment_link = session.get("payment_link")
            subscription_id = session.get("subscription")
            tier = PAYMENT_LINKS.get(payment_link)

            print(f"📦 Paiement — telegram_id: {telegram_id}, tier: {tier}, sub: {subscription_id}")

            if telegram_id and tier and subscription_id:
                asyncio.run_coroutine_threadsafe(
                    ajouter_membre(int(telegram_id), tier, subscription_id),
                    webhook_loop
                )
            else:
                print(f"❌ Manquant — telegram_id: {telegram_id}, tier: {tier}, sub: {subscription_id}")

        elif event_type in ("customer.subscription.deleted", "invoice.payment_failed"):
            subscription_id = event["data"]["object"].get("id") or event["data"]["object"].get("subscription")
            print(f"🚫 Résiliation/échec — sub: {subscription_id}")
            if subscription_id:
                asyncio.run_coroutine_threadsafe(
                    retirer_membre(subscription_id),
                    webhook_loop
                )

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

    loop_thread = threading.Thread(target=run_webhook_loop, daemon=True)
    loop_thread.start()

    webhook_thread = threading.Thread(target=start_webhook_server, daemon=True)
    webhook_thread.start()
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
