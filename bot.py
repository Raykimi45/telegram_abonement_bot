import os
import json
import time
import asyncio
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import Conflict
import urllib.request

TOKEN = os.getenv("TOKEN")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")

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

SUBS_FILE = "/data/subscriptions.json"

def load_data():
    os.makedirs("/data", exist_ok=True)
    if not os.path.exists(SUBS_FILE):
        return {"subscriptions": {}, "users": {}}
    try:
        with open(SUBS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"subscriptions": {}, "users": {}}

def save_data(data):
    os.makedirs("/data", exist_ok=True)
    with open(SUBS_FILE, "w") as f:
        json.dump(data, f)

def get_sub_for_user(telegram_id: int):
    data = load_data()
    for sub_id, sub in data["subscriptions"].items():
        if sub["telegram_id"] == telegram_id:
            return sub_id, sub
    return None, None

def stripe_cancel_subscription(subscription_id: str):
    url = f"https://api.stripe.com/v1/subscriptions/{subscription_id}/cancel"
    req = urllib.request.Request(url, data=b"", method="POST")
    req.add_header("Authorization", f"Bearer {STRIPE_SECRET_KEY}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        urllib.request.urlopen(req)
        return True
    except Exception as e:
        print(f"❌ Erreur Stripe cancel: {e}")
        return False

def stripe_get_subscription(subscription_id: str):
    url = f"https://api.stripe.com/v1/subscriptions/{subscription_id}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {STRIPE_SECRET_KEY}")
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"❌ Erreur Stripe get: {e}")
        return None

webhook_loop = asyncio.new_event_loop()

def run_webhook_loop():
    asyncio.set_event_loop(webhook_loop)
    webhook_loop.run_forever()

async def ajouter_membre(telegram_id: int, tier: str, subscription_id: str):
    bot = Bot(token=TOKEN)
    try:
        data = load_data()
        data["subscriptions"][subscription_id] = {"telegram_id": telegram_id, "tier": tier}
        save_data(data)

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
    finally:
        await bot.shutdown()

async def retirer_membre(subscription_id: str):
    bot = Bot(token=TOKEN)
    try:
        data = load_data()
        sub = data["subscriptions"].get(subscription_id)
        if not sub:
            print(f"❌ Subscription inconnue: {subscription_id}")
            return

        telegram_id = sub["telegram_id"]
        tier = sub["tier"]

        await bot.ban_chat_member(chat_id=CANAUX[tier], user_id=telegram_id)
        await bot.unban_chat_member(chat_id=CANAUX[tier], user_id=telegram_id)

        await bot.send_message(
            chat_id=telegram_id,
            text=f"😔 Ton abonnement {TIERS[tier]['nom']} a expiré ou a été annulé.\n\nTu as été retiré du canal.\n\nTu peux te réabonner à tout moment 👇",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Se réabonner", url=TIERS[tier]["lien"])
            ]])
        )

        del data["subscriptions"][subscription_id]
        save_data(data)
        print(f"✅ {telegram_id} retiré du canal {tier}")
    except Exception as e:
        print(f"❌ Erreur retrait: {e}")
    finally:
        await bot.shutdown()

# ── Commandes bot ────────────────────────────────────────────────────────────

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

async def resilier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    sub_id, sub = get_sub_for_user(telegram_id)

    if not sub_id:
        await update.message.reply_text("❌ Tu n'as pas d'abonnement actif.")
        return

    print(f"🔍 sub_id trouvé: {sub_id}")
    tier = sub["tier"]

    # Récupérer la date de fin depuis Stripe
    stripe_sub = stripe_get_subscription(sub_id)
    date_fin = "inconnue"
    if stripe_sub:
        ts = stripe_sub.get("current_period_end")
        if ts:
            date_fin = datetime.fromtimestamp(ts).strftime("%d/%m/%Y")

    keyboard = [
        [InlineKeyboardButton("❌ Oui, perdre mon accès maintenant", callback_data=f"confirmer_resilier_{sub_id}")],
        [InlineKeyboardButton("✅ Non, garder mon accès", callback_data="annuler_resilier")],
    ]

    await update.message.reply_text(
        f"⚠️ *Attention — Résiliation de ton abonnement*\n\n"
        f"Tu es sur le point d'annuler ton abonnement {TIERS[tier]['nom']}.\n\n"
        f"Normalement ton accès était garanti jusqu'au *{date_fin}*.\n\n"
        f"❌ Si tu résilies maintenant, tu perds l'accès *IMMÉDIATEMENT*.\n"
        f"💸 Aucun remboursement ne sera effectué.\n\n"
        f"Es-tu vraiment sûr de vouloir perdre ton accès maintenant ?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_resilier_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id

    if query.data == "annuler_resilier":
        await query.edit_message_text("✅ Bonne décision ! Ton abonnement reste actif.")
        return

    if query.data.startswith("confirmer_resilier_"):
        sub_id = query.data.replace("confirmer_resilier_", "")
        sub_id_check, sub = get_sub_for_user(telegram_id)

        if sub_id_check != sub_id:
            await query.edit_message_text("❌ Erreur — abonnement introuvable.")
            return

        await query.edit_message_text("⏳ Résiliation en cours...")

        success = stripe_cancel_subscription(sub_id)
        if success:
            await retirer_membre(sub_id)
            await context.bot.send_message(
                chat_id=telegram_id,
                text="😔 Ton abonnement a été résilié. Tu as perdu ton accès immédiatement.\n\nTu peux te réabonner à tout moment avec /start."
            )
        else:
            await context.bot.send_message(
                chat_id=telegram_id,
                text="❌ Une erreur s'est produite. Contacte le support."
            )

# ── Webhook Stripe ────────────────────────────────────────────────────────────

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
        print(f"📨 Événement: {event_type}")

        if event_type == "checkout.session.completed":
            session = event["data"]["object"]
            telegram_id = session.get("client_reference_id")
            payment_link = session.get("payment_link")
            subscription_id = session.get("subscription")
            tier = PAYMENT_LINKS.get(payment_link)

            print(f"🔎 subscription_id brut: {subscription_id}")
            print(f"🔎 payment_link: {payment_link}")
            print(f"🔎 telegram_id: {telegram_id}")
            print(f"🔎 tier: {tier}")

            if telegram_id and tier and subscription_id:
                asyncio.run_coroutine_threadsafe(
                    ajouter_membre(int(telegram_id), tier, subscription_id),
                    webhook_loop
                )

        elif event_type in ("customer.subscription.deleted", "invoice.payment_failed"):
            obj = event["data"]["object"]
            subscription_id = obj.get("id") if event_type == "customer.subscription.deleted" else obj.get("subscription")
            if subscription_id:
                asyncio.run_coroutine_threadsafe(
                    retirer_membre(subscription_id),
                    webhook_loop
                )

def start_webhook_server():
    server = HTTPServer(("0.0.0.0", 8000), StripeWebhookHandler)
    server.serve_forever()

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
            app.add_handler(CommandHandler("resilier", resilier))
            app.add_handler(CallbackQueryHandler(handle_resilier_callback))
            print("✅ Bot démarré...")
            app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
        except Conflict:
            print("⚠️ Conflit, retry dans 15s...")
            time.sleep(15)
        except Exception as e:
            print(f"❌ Erreur: {e}, retry dans 15s...")
            time.sleep(15)
