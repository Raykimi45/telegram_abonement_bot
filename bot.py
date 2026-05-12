import os
import json
import time
import asyncio
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import Conflict
import stripe

TOKEN = os.getenv("TOKEN")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/ton_support")  # ← à configurer

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
        "nom": "🩷 KAYLA PRIVATE",
        "prix": "9,99€/mois",
        "lien": "https://buy.stripe.com/test_9B6fZb2gTcy8b2p7tve3e00",
    },
    "vip": {
        "nom": "💗 KAYLA VIP",
        "prix": "19,99€/mois",
        "lien": "https://buy.stripe.com/test_14AdR3bRt55G6M9cNPe3e01",
    }
}

SUBS_FILE = "/data/subscriptions.json"

# ── Data ──────────────────────────────────────────────────────────────────────

def load_data():
    os.makedirs("/data", exist_ok=True)
    if not os.path.exists(SUBS_FILE):
        return {"subscriptions": {}, "users": {}, "customers": {}, "invite_counts": {}}
    try:
        with open(SUBS_FILE, "r") as f:
            data = json.load(f)
            for key in ("subscriptions", "users", "customers", "invite_counts"):
                if key not in data:
                    data[key] = {}
            return data
    except Exception:
        return {"subscriptions": {}, "users": {}, "customers": {}, "invite_counts": {}}

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

def get_invite_count(telegram_id: int) -> int:
    data = load_data()
    return data["invite_counts"].get(str(telegram_id), 0)

def increment_invite_count(telegram_id: int) -> int:
    data = load_data()
    key = str(telegram_id)
    data["invite_counts"][key] = data["invite_counts"].get(key, 0) + 1
    save_data(data)
    return data["invite_counts"][key]

# ── Stripe ────────────────────────────────────────────────────────────────────

def stripe_cancel_subscription(subscription_id: str):
    try:
        stripe.api_key = STRIPE_SECRET_KEY
        result = stripe.Subscription.cancel(subscription_id)
        print(f"✅ Stripe cancel OK: {result.status}")
        return True
    except Exception as e:
        print(f"❌ Erreur Stripe cancel: {e}")
        return False

def stripe_get_subscription(subscription_id: str):
    try:
        stripe.api_key = STRIPE_SECRET_KEY
        return stripe.Subscription.retrieve(subscription_id)
    except Exception as e:
        print(f"❌ Erreur Stripe get: {e}")
        return None

# ── Async loop (webhook) ──────────────────────────────────────────────────────

webhook_loop = asyncio.new_event_loop()

def run_webhook_loop():
    asyncio.set_event_loop(webhook_loop)
    webhook_loop.run_forever()

# ── Actions bot ───────────────────────────────────────────────────────────────

async def ajouter_membre(telegram_id: int, tier: str, subscription_id: str):
    bot = Bot(token=TOKEN)
    try:
        data = load_data()
        data["subscriptions"] = {k: v for k, v in data["subscriptions"].items() if v["telegram_id"] != telegram_id}
        data["subscriptions"][subscription_id] = {"telegram_id": telegram_id, "tier": tier}
        save_data(data)
        print(f"💾 Sauvegardé: {subscription_id} → {telegram_id} ({tier})")
        print(f"📦 Paiement — telegram_id: {telegram_id}, tier: {tier}, sub: {subscription_id}")

        invite = await bot.create_chat_invite_link(
            chat_id=CANAUX[tier],
            member_limit=1,
            creates_join_request=False
        )
        new_count = increment_invite_count(telegram_id)

        tier_nom = TIERS[tier]["nom"]

        # Message 1 — confirmation paiement
        await bot.send_message(
            chat_id=telegram_id,
            text=(
                "✅ Paiement confirmé !\n\n"
                "Ton accès est activé. Bienvenue de l'autre côté. 🖤🔥\n"
                "Kayla t'attend dans ton canal privé."
            )
        )

        # Pause 1,2s comme dans la simulation
        await asyncio.sleep(1.2)

        # Message 2 — lien + espace abonné
        keyboard = []
        if tier == "premium":
            keyboard.append([InlineKeyboardButton("⬆️ Upgrader mon abonnement", callback_data="menu_upgrade")])
        keyboard.append([InlineKeyboardButton("⚙️ Gérer mon abonnement", callback_data="menu_gerer")])

        await bot.send_message(
            chat_id=telegram_id,
            text=(
                f"✅ {tier_nom} actif\n\n"
                f"Ton abonnement est actif. 💕\n\n"
                f"🔗 Ton lien d'accès au canal (usage unique) :\n"
                f"{invite.invite_link}\n\n"
                f"⚠️ Ce lien est personnel. Ne le partage jamais — ton abonnement serait résilié immédiatement sans remboursement.\n\n"
                f"Que souhaites-tu faire ?"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

        print(f"✅ Lien envoyé à {telegram_id} pour {tier} ({new_count}/2)")
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
        tier_lien = TIERS[tier]["lien"]

        await bot.ban_chat_member(chat_id=CANAUX[tier], user_id=telegram_id)
        await bot.unban_chat_member(chat_id=CANAUX[tier], user_id=telegram_id)

        await bot.send_message(
            chat_id=telegram_id,
            text=(
                "⏳ Résiliation en cours…\n\n"
                "Ton abonnement a été annulé. Tu n'as plus accès au canal privé de Kayla."
            ),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🩷 Se réabonner", url=tier_lien)
            ]])
        )

        del data["subscriptions"][subscription_id]
        data["invite_counts"].pop(str(telegram_id), None)
        save_data(data)
        print(f"✅ {telegram_id} retiré du canal {tier}")
    except Exception as e:
        print(f"❌ Erreur retrait: {e}")
    finally:
        await bot.shutdown()

# ── Commandes bot ─────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    print(f"▶ /start — telegram_id: {telegram_id}")

    sub_id, sub = get_sub_for_user(telegram_id)
    if sub_id:
        tier = sub["tier"]
        tier_nom = TIERS[tier]["nom"]
        keyboard = []
        if tier == "premium":
            keyboard.append([InlineKeyboardButton("⬆️ Upgrader mon abonnement", callback_data="menu_upgrade")])
        keyboard.append([InlineKeyboardButton("⚙️ Gérer mon abonnement", callback_data="menu_gerer")])
        await update.message.reply_text(
            f"✅ {tier_nom} actif\n\n"
            f"Ton abonnement est actif. 💕\n\n"
            f"Que souhaites-tu faire ?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    keyboard = [
        [InlineKeyboardButton("🩷 PRIVATE — 9,99€/mois", callback_data="page_private")],
        [InlineKeyboardButton("💗 VIP — 19,99€/mois", callback_data="page_vip")],
    ]
    await update.message.reply_text(
        "Tu sais déjà pourquoi t'es là. 🔥\n\n"
        "Après paiement, tu rejoins mon canal privé instantanément 💕\n"
        "Aucune attente. Accès immédiat.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ── Callbacks ─────────────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    data_cb = query.data

    # ── PAGE PRIVATE ──
    if data_cb == "page_private":
        keyboard = [
            [InlineKeyboardButton(
                "🔓 Accéder au canal PRIVATE",
                url=f"{TIERS['premium']['lien']}?client_reference_id={telegram_id}"
            )],
            [InlineKeyboardButton("💗 Voir le VIP", callback_data="page_vip")],
            [InlineKeyboardButton("👈🏽 Retour", callback_data="page_tarifs")],
        ]
        await query.edit_message_text(
            "🩷 PRIVATE — 9,99€/mois\n\n"
            "Canal KAYLA PRIVATE\n\n"
            "🩷 Photos & vidéos en lingerie\n"
            "🩷 Topless exclusifs\n"
            "🩷 Contenu inédit, jamais publié ailleurs\n"
            "🩷 Nouveau contenu chaque semaine\n"
            "🩷 Accès à mes archives privées\n"
            "❤️‍🔥 Un mois de plaisir rien que pour toi\n\n"
            "La plupart ne restent pas longtemps au PRIVATE. Une fois qu'ils découvrent le VIP… ils upgradent. 👀",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ── PAGE VIP ──
    elif data_cb == "page_vip":
        keyboard = [
            [InlineKeyboardButton(
                "🔓 Accéder au canal VIP",
                url=f"{TIERS['vip']['lien']}?client_reference_id={telegram_id}"
            )],
            [InlineKeyboardButton("👈🏽 Retour", callback_data="page_tarifs")],
        ]
        await query.edit_message_text(
            "💗 VIP — 19,99€/mois\n\n"
            "Canal KAYLA VIP\n\n"
            "💗 Tout le contenu PRIVATE inclus\n"
            "💗 Full nude & vidéos exclusives\n"
            "💗 2x plus de contenu que le PRIVATE\n"
            "💗 Accès en avant-première à toutes mes nouveautés\n"
            "💗 Contenu réservé uniquement aux VIP\n"
            "❤️‍🔥 Une expérience unique & inoubliable\n\n"
            "Ceux qui ont le VIP ne regardent plus jamais en arrière. 🖤",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ── PAGE TARIFS (retour) ──
    elif data_cb == "page_tarifs":
        keyboard = [
            [InlineKeyboardButton("🩷 PRIVATE — 9,99€/mois", callback_data="page_private")],
            [InlineKeyboardButton("💗 VIP — 19,99€/mois", callback_data="page_vip")],
        ]
        await query.edit_message_text(
            "Tu sais déjà pourquoi t'es là. 🔥\n\n"
            "Après paiement, tu rejoins mon canal privé instantanément 💕\n"
            "Aucune attente. Accès immédiat.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ── MENU GÉRER ──
    elif data_cb == "menu_gerer":
        print(f"⚙️ Gestion — telegram_id: {telegram_id}")
        keyboard = [
            [InlineKeyboardButton("🔗 Accéder à mon canal", callback_data="menu_canal")],
            [InlineKeyboardButton("💬 Support", callback_data="menu_support")],
            [InlineKeyboardButton("❌ Résilier", callback_data="menu_resilier")],
            [InlineKeyboardButton("👈🏽 Retour", callback_data="menu_retour_abo")],
        ]
        await query.edit_message_text(
            "⚙️ Gestion de mon abonnement\n\n"
            "Que souhaites-tu faire ?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ── RETOUR ESPACE ABONNÉ ──
    elif data_cb == "menu_retour_abo":
        sub_id, sub = get_sub_for_user(telegram_id)
        if not sub_id:
            await query.edit_message_text("❌ Tu n'as pas d'abonnement actif.")
            return
        tier = sub["tier"]
        tier_nom = TIERS[tier]["nom"]
        keyboard = []
        if tier == "premium":
            keyboard.append([InlineKeyboardButton("⬆️ Upgrader mon abonnement", callback_data="menu_upgrade")])
        keyboard.append([InlineKeyboardButton("⚙️ Gérer mon abonnement", callback_data="menu_gerer")])
        await query.edit_message_text(
            f"✅ {tier_nom} actif\n\n"
            f"Ton abonnement est actif. 💕\n\n"
            f"Que souhaites-tu faire ?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ── ACCÉDER AU CANAL ──
    elif data_cb == "menu_canal":
        count = get_invite_count(telegram_id)
        print(f"🔗 Canal — telegram_id: {telegram_id}, liens: {count}/2")

        if count >= 2:
            keyboard = [
                [InlineKeyboardButton("💬 Contacter le support", url=SUPPORT_URL)],
                [InlineKeyboardButton("👈🏽 Retour", callback_data="menu_gerer")],
            ]
            await query.edit_message_text(
                "⛔ Limite atteinte\n\n"
                "Tu as déjà généré 2 liens d'invitation.\n\n"
                "Si tu as un problème d'accès, contacte le support.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

        dots = "🟢" * count + "⚪" * (2 - count)
        keyboard = [
            [InlineKeyboardButton("🔗 Générer mon lien d'invitation", callback_data="gen_lien")],
            [InlineKeyboardButton("👈🏽 Retour", callback_data="menu_gerer")],
        ]
        await query.edit_message_text(
            "🔗 Accéder à mon canal\n\n"
            "Tu as quitté le groupe sans faire exprès ou tu n'as pas réussi à t'abonner avec le premier lien ?\n\n"
            "Tu peux en générer un nouveau ici.\n\n"
            f"⚠️ Maximum 2 générations possibles. {dots} {count}/2\n"
            "Si tu partages ton lien, ton abonnement sera résilié immédiatement sans remboursement.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ── GÉNÉRER LIEN ──
    elif data_cb == "gen_lien":
        count = get_invite_count(telegram_id)
        if count >= 2:
            keyboard = [
                [InlineKeyboardButton("💬 Contacter le support", url=SUPPORT_URL)],
                [InlineKeyboardButton("👈🏽 Retour", callback_data="menu_gerer")],
            ]
            await query.edit_message_text(
                "⛔ Limite atteinte\n\n"
                "Tu as déjà généré 2 liens d'invitation.\n\n"
                "Si tu as un problème d'accès, contacte le support.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

        sub_id, sub = get_sub_for_user(telegram_id)
        if not sub_id:
            await query.edit_message_text("❌ Tu n'as pas d'abonnement actif.")
            return

        tier = sub["tier"]
        try:
            invite = await context.bot.create_chat_invite_link(
                chat_id=CANAUX[tier],
                member_limit=1,
                creates_join_request=False
            )
            new_count = increment_invite_count(telegram_id)
            dots = "🟢" * new_count + "⚪" * (2 - new_count)
            print(f"🔗 Lien généré — telegram_id: {telegram_id}, {new_count}/2")

            keyboard = []
            if new_count < 2:
                keyboard.append([InlineKeyboardButton("🔗 Générer un autre lien", callback_data="gen_lien")])
            else:
                keyboard.append([InlineKeyboardButton("⛔ Limite atteinte — 2/2 liens générés", callback_data="noop")])
            keyboard.append([InlineKeyboardButton("👈🏽 Retour", callback_data="menu_gerer")])

            await query.edit_message_text(
                f"✅ Ton lien d'invitation :\n\n"
                f"{invite.invite_link}\n\n"
                f"⚠️ Ce lien est personnel et à usage unique. Ne le partage jamais — ton abonnement serait résilié immédiatement sans remboursement.\n\n"
                f"{dots} {new_count}/2 liens générés",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            print(f"❌ Erreur génération lien: {e}")
            await query.edit_message_text("❌ Une erreur s'est produite. Contacte le support.")

    # ── SUPPORT ──
    elif data_cb == "menu_support":
        print(f"💬 Support — telegram_id: {telegram_id}")
        keyboard = [
            [InlineKeyboardButton("✉️ Contacter le support", url=SUPPORT_URL)],
            [InlineKeyboardButton("👈🏽 Retour", callback_data="menu_gerer")],
        ]
        await query.edit_message_text(
            "💬 Support\n\n"
            "Un problème ? Une question ?\n\n"
            "Contacte-moi directement ici 👇\n"
            "Je réponds dans les plus brefs délais. 💕",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ── UPGRADE ──
    elif data_cb == "menu_upgrade":
        print(f"⬆️ Upgrade — telegram_id: {telegram_id}")
        keyboard = [
            [InlineKeyboardButton(
                "🔓 Passer au VIP maintenant",
                url=f"{TIERS['vip']['lien']}?client_reference_id={telegram_id}"
            )],
            [InlineKeyboardButton("👈🏽 Retour", callback_data="menu_retour_abo")],
        ]
        await query.edit_message_text(
            "⬆️ Upgrader vers le VIP\n\n"
            "Tu es actuellement en PRIVATE.\n"
            "Passe au VIP et accède à tout le contenu exclusif. 🔥\n\n"
            "💗 Full nude & vidéos longues\n"
            "💗 2x plus de contenu\n"
            "💗 Accès prioritaire aux nouveautés\n\n"
            "+10€/mois seulement",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ── RÉSILIER ──
    elif data_cb == "menu_resilier":
        print(f"❌ Résiliation — telegram_id: {telegram_id}")
        sub_id, sub = get_sub_for_user(telegram_id)
        if not sub_id:
            await query.edit_message_text("❌ Tu n'as pas d'abonnement actif.")
            return

        tier = sub["tier"]
        tier_nom = TIERS[tier]["nom"]
        stripe_sub = stripe_get_subscription(sub_id)
        date_fin = "inconnue"
        if stripe_sub:
            ts = getattr(stripe_sub, "current_period_end", None)
            if ts:
                date_fin = datetime.fromtimestamp(ts).strftime("%d/%m/%Y")

        keyboard = [
            [InlineKeyboardButton("✅ Non, garder mon accès", callback_data="resilier_non")],
            [InlineKeyboardButton("❌ Oui, perdre mon accès maintenant", callback_data=f"resilier_oui_{sub_id}")],
        ]
        await query.edit_message_text(
            f"⚠️ Attention — Résiliation de ton abonnement\n\n"
            f"Tu es sur le point d'annuler ton abonnement {tier_nom}.\n\n"
            f"Normalement ton accès était garanti jusqu'au {date_fin}.\n\n"
            f"❌ Si tu résilies maintenant, tu perds l'accès IMMÉDIATEMENT.\n"
            f"💸 Aucun remboursement ne sera effectué.\n\n"
            f"Es-tu vraiment sûr de vouloir perdre ton accès maintenant ?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ── RÉSILIER NON ──
    elif data_cb == "resilier_non":
        keyboard = [[InlineKeyboardButton("🔓 Retour à mon abonnement", callback_data="menu_gerer")]]
        await query.edit_message_text(
            "✅\n\nBonne décision ! Ton abonnement reste actif. 💕",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ── RÉSILIER OUI ──
    elif data_cb.startswith("resilier_oui_"):
        sub_id = data_cb.replace("resilier_oui_", "")
        sub_id_check, sub = get_sub_for_user(telegram_id)
        if sub_id_check != sub_id:
            await query.edit_message_text("❌ Erreur — abonnement introuvable.")
            return
        await query.edit_message_text("⏳ Résiliation en cours…")
        success = stripe_cancel_subscription(sub_id)
        if not success:
            await context.bot.send_message(
                chat_id=telegram_id,
                text="❌ Une erreur s'est produite. Contacte le support.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✉️ Contacter le support", url=SUPPORT_URL)
                ]])
            )

    # ── NOOP ──
    elif data_cb == "noop":
        pass

# ── Handler temporaire file_id ────────────────────────────────────────────────

async def get_file_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        print(f"📸 file_id reçu: {file_id}")
        await update.message.reply_text(
            f"📸 Ton file\_id :\n\n`{file_id}`",
            parse_mode="Markdown"
        )

# ── Webhook Stripe ─────────────────────────────────────────────────────────────

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
            customer_id = session.get("customer")
            payment_link = session.get("payment_link")
            subscription_id = session.get("subscription")
            tier = PAYMENT_LINKS.get(payment_link)

            print(f"🔎 subscription_id: {subscription_id}, customer: {customer_id}, telegram_id: {telegram_id}, tier: {tier}")

            if telegram_id and customer_id:
                data = load_data()
                data["customers"][customer_id] = int(telegram_id)
                save_data(data)

            if telegram_id and tier and subscription_id:
                asyncio.run_coroutine_threadsafe(
                    ajouter_membre(int(telegram_id), tier, subscription_id),
                    webhook_loop
                )

        elif event_type == "customer.subscription.created":
            obj = event["data"]["object"]
            subscription_id = obj.get("id")
            customer_id = obj.get("customer")
            data = load_data()
            telegram_id = data["customers"].get(customer_id)
            existing_tier = None
            for sub in data["subscriptions"].values():
                if sub.get("customer_id") == customer_id:
                    existing_tier = sub["tier"]
                    break
            print(f"🔎 subscription.created: {subscription_id}, customer: {customer_id}, telegram_id: {telegram_id}")
            if telegram_id and subscription_id and existing_tier:
                data["subscriptions"] = {k: v for k, v in data["subscriptions"].items() if v["telegram_id"] != telegram_id}
                data["subscriptions"][subscription_id] = {"telegram_id": telegram_id, "tier": existing_tier, "customer_id": customer_id}
                save_data(data)
                print(f"✅ sub_id mis à jour: {subscription_id}")

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

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import subprocess
    print(subprocess.run(["df", "-h"], capture_output=True, text=True).stdout)
    print("LS DATA:", subprocess.run(["ls", "-la", "/data"], capture_output=True, text=True).stdout)
    print("LS APP:", subprocess.run(["ls", "-la", "/app"], capture_output=True, text=True).stdout)
    print("SUBS:", subprocess.run(["cat", "/data/subscriptions.json"], capture_output=True, text=True).stdout)
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
            app.add_handler(CallbackQueryHandler(handle_callback))
            app.add_handler(MessageHandler(filters.PHOTO, get_file_id))  # ← TEMPORAIRE
            print("✅ Bot démarré...")
            app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
        except Conflict:
            print("⚠️ Conflit, retry dans 15s...")
            time.sleep(15)
        except Exception as e:
            print(f"❌ Erreur: {e}, retry dans 15s...")
            time.sleep(15)
