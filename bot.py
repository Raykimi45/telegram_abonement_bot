import os
import json
import time
import asyncio
import threading
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ChatMemberHandler, ContextTypes
from telegram.error import Conflict
import stripe

TOKEN = os.getenv("TOKEN")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/Help348848bot")

# Payment Links Stripe (format: "plink_xxx:premium")
_raw_payment_links = os.getenv("PAYMENT_LINKS", "")
PAYMENT_LINKS = {}
for entry in _raw_payment_links.split(","):
    entry = entry.strip()
    if ":" in entry:
        plink, tier = entry.split(":", 1)
        PAYMENT_LINKS[plink.strip()] = tier.strip()

# IDs des canaux Telegram
CANAUX = {
    "premium": int(os.getenv("CANAL_PREMIUM", "-1003947632446")),
}

TIERS = {
    "premium": {
        "nom": "🩷 KAYLA PRIVATE",
        "short": "PRIVATE",
        "emoji": "🩷",
        "prix": "9,99€/mois",
        "lien": os.getenv("PAYMENT_LINK_PREMIUM", ""),
    },
}

IMAGES = {
    "tarifs":  "AgACAgQAAxkBAAIBnGovvLL1RS7DyLCh8lWJxgHOuHwHAAJwDWsbECeBUcxC0FvOQDhSAQADAgADeQADPAQ",
    "premium": "AgACAgQAAxkBAAIBmmovvKbLyEOHZDIvip5Y-aUCmZDbAAJvDWsbECeBUQg2wxWX1j4pAQADAgADeQADPAQ",
}

SUBS_FILE = "/data/subscriptions.json"

# ── Data ──────────────────────────────────────────────────────────────────────

def load_data():
    with data_lock:
        os.makedirs("/data", exist_ok=True)
        if not os.path.exists(SUBS_FILE):
            return {
                "subscriptions": {},
                "customers": {},
                "invite_counts": {},
                "pending_msg": {},
                "pending_link": {},
                "tarifs_msg": {},
                "welcome_sent": {},
                "resilier_ctx": {},
            }
        try:
            with open(SUBS_FILE, "r") as f:
                data = json.load(f)
                for key in ("subscriptions", "customers", "invite_counts", "pending_msg",
                            "pending_link", "tarifs_msg", "welcome_sent", "resilier_ctx"):
                    if key not in data:
                        data[key] = {}
                return data
        except Exception:
            return {
                "subscriptions": {}, "customers": {}, "invite_counts": {},
                "pending_msg": {}, "pending_link": {}, "tarifs_msg": {}, "welcome_sent": {}, "resilier_ctx": {},
            }

def save_data(data):
    with data_lock:
        os.makedirs("/data", exist_ok=True)
        with open(SUBS_FILE, "w") as f:
            json.dump(data, f)

def get_subs_for_user(telegram_id: int) -> dict:
    data = load_data()
    return {
        sub_id: sub
        for sub_id, sub in data["subscriptions"].items()
        if sub["telegram_id"] == telegram_id
    }

def get_sub_by_tier(telegram_id: int, tier: str):
    for sub_id, sub in get_subs_for_user(telegram_id).items():
        if sub["tier"] == tier:
            return sub_id, sub
    return None, None

def get_invite_count(telegram_id: int, tier: str) -> int:
    data = load_data()
    return data["invite_counts"].get(f"{telegram_id}:{tier}", 0)

def increment_invite_count(telegram_id: int, tier: str) -> int:
    data = load_data()
    key = f"{telegram_id}:{tier}"
    data["invite_counts"][key] = data["invite_counts"].get(key, 0) + 1
    save_data(data)
    return data["invite_counts"][key]

async def is_user_in_canal(bot: Bot, telegram_id: int, tier: str) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CANAUX[tier], user_id=telegram_id)
        return member.status in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER)
    except Exception:
        return False

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

# ── Async loop (webhook) ──────────────────────────────────────────────────────

data_lock = threading.Lock()
webhook_loop = asyncio.new_event_loop()

def run_webhook_loop():
    asyncio.set_event_loop(webhook_loop)
    webhook_loop.run_forever()

# ── UI helpers ────────────────────────────────────────────────────────────────

def keyboard_espace_abo(subs: dict) -> InlineKeyboardMarkup:
    keyboard = []
    keyboard.append([InlineKeyboardButton("⚙️ Gérer mon abonnement", callback_data="menu_gerer_premium")])
    return InlineKeyboardMarkup(keyboard)

def texte_espace_abo(subs: dict) -> str:
    return (
        "✅ 🩷 KAYLA PRIVATE actif\n\n"
        "Ton abonnement est actif. 💕\n\n"
        "Que souhaites-tu faire ?"
    )

# ── Actions bot ───────────────────────────────────────────────────────────────

async def ajouter_membre(telegram_id: int, tier: str, subscription_id: str, period_end: int = None):
    bot = Bot(token=TOKEN)
    try:
        data = load_data()
        data["subscriptions"][subscription_id] = {
            "telegram_id": telegram_id,
            "tier": tier,
            "period_end": period_end,
        }
        tarifs_msg_id = data["tarifs_msg"].pop(str(telegram_id), None)
        main_msg_id = data["tarifs_msg"].pop(f"main_{telegram_id}", None)
        start_msg_id = data["tarifs_msg"].pop(f"start_{telegram_id}", None)
        save_data(data)
        for msg_id_to_del in [tarifs_msg_id, main_msg_id, start_msg_id]:
            if msg_id_to_del:
                try:
                    await bot.delete_message(chat_id=telegram_id, message_id=msg_id_to_del)
                except Exception:
                    pass  # Message déjà supprimé
        print(f"💾 Sauvegardé: {subscription_id} → {telegram_id} ({tier})")
        invite = await bot.create_chat_invite_link(
            chat_id=CANAUX[tier],
            member_limit=1,
            creates_join_request=False
        )
        tier_emoji = TIERS[tier]["emoji"]
        tier_short = TIERS[tier]["short"]
        msg1 = await bot.send_message(
            chat_id=telegram_id,
            text=(
                f"✅ Paiement confirmé !\n\n"
                f"Rejoint ton canal {tier_emoji} {tier_short} ici (lien à usage unique) :\n"
                f"{invite.invite_link}"
            ),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❓ Je n'arrive pas à rejoindre", callback_data=f"aide_rejoindre_{tier}")
            ]])
        )
        data = load_data()
        data["pending_msg"][f"{telegram_id}:{tier}"] = msg1.message_id
        data["pending_link"][f"{telegram_id}:{tier}"] = invite.invite_link
        save_data(data)
        print(f"✅ Lien paiement envoyé à {telegram_id} pour {tier}")
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
        try:
            await bot.ban_chat_member(chat_id=CANAUX[tier], user_id=telegram_id)
            await bot.unban_chat_member(chat_id=CANAUX[tier], user_id=telegram_id)
        except Exception as e:
            print(f"⚠️ Kick: {e}")
        msg_annule = (
            f"Ton abonnement {TIERS[tier]['nom']} a été annulé.\n\n"
            f"Tu n'as plus accès au canal privé. 🖤"
        )
        kb_reabo = InlineKeyboardMarkup([[
            InlineKeyboardButton("🩷 Se réabonner", callback_data="page_tarifs_new")
        ]])
        ctx = data["resilier_ctx"].pop(subscription_id, None)
        del data["subscriptions"][subscription_id]
        data["invite_counts"].pop(f"{telegram_id}:{tier}", None)
        data["pending_msg"].pop(f"{telegram_id}:{tier}", None)
        data["welcome_sent"].pop(f"{telegram_id}:{tier}", None)
        save_data(data)
        subs_restants = get_subs_for_user(telegram_id)
        if subs_restants:
            kb_annule = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🩷 Se réabonner", callback_data="page_tarifs_new")]]
                + [list(row) for row in keyboard_espace_abo(subs_restants).inline_keyboard]
            )
        else:
            kb_annule = kb_reabo
        main_msg_id = data["tarifs_msg"].pop(f"main_{telegram_id}", None)
        data["tarifs_msg"].pop(f"start_{telegram_id}", None)
        save_data(data)
        if main_msg_id:
            try:
                await bot.delete_message(chat_id=telegram_id, message_id=main_msg_id)
            except Exception:
                pass
        if ctx:
            try:
                await bot.edit_message_text(
                    chat_id=ctx["chat_id"],
                    message_id=ctx["msg_id"],
                    text=msg_annule,
                    reply_markup=kb_annule
                )
            except Exception as e:
                print(f"⚠️ Edit msg résiliation: {e}")
                await bot.send_message(chat_id=telegram_id, text=msg_annule, reply_markup=kb_annule)
        else:
            await bot.send_message(chat_id=telegram_id, text=msg_annule, reply_markup=kb_annule)
        print(f"✅ {telegram_id} retiré du canal {tier}")
    except Exception as e:
        print(f"❌ Erreur retrait: {e}")
    finally:
        await bot.shutdown()

# ── ChatMemberHandler ─────────────────────────────────────────────────────────

async def membre_rejoint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    if not result or result.new_chat_member.status != "member":
        return
    telegram_id = result.new_chat_member.user.id
    chat_id_canal = result.chat.id
    tier = None
    for t, cid in CANAUX.items():
        if cid == chat_id_canal:
            tier = t
            break
    if not tier:
        return
    sub_id, sub = get_sub_by_tier(telegram_id, tier)
    if not sub_id:
        await context.bot.ban_chat_member(chat_id=chat_id_canal, user_id=telegram_id)
        await context.bot.unban_chat_member(chat_id=chat_id_canal, user_id=telegram_id)
        print(f"🚫 Intrus kické (pas d'abonnement local) — telegram_id: {telegram_id}, tier: {tier}")
        return

    # Vérification Stripe en temps réel — s'assurer que l'abonnement est toujours actif
    try:
        stripe.api_key = STRIPE_SECRET_KEY
        stripe_sub = stripe.Subscription.retrieve(sub_id)
        stripe_status = getattr(stripe_sub, "status", None)
        if stripe_status not in ("active", "trialing"):
            print(f"🚫 Abonnement Stripe inactif ({stripe_status}) — kick {telegram_id}, tier: {tier}")
            await context.bot.ban_chat_member(chat_id=chat_id_canal, user_id=telegram_id)
            await context.bot.unban_chat_member(chat_id=chat_id_canal, user_id=telegram_id)
            # Nettoyer les données locales
            data = load_data()
            data["subscriptions"].pop(sub_id, None)
            data["invite_counts"].pop(f"{telegram_id}:{tier}", None)
            data["pending_msg"].pop(f"{telegram_id}:{tier}", None)
            data["welcome_sent"].pop(f"{telegram_id}:{tier}", None)
            save_data(data)
            return
        print(f"✅ Abonnement Stripe vérifié ({stripe_status}) — telegram_id: {telegram_id}, tier: {tier}")
    except Exception as e:
        print(f"⚠️ Vérification Stripe échouée (on laisse entrer): {e}")
    print(f"✅ Entrée canal — telegram_id: {telegram_id}, tier: {tier}")
    data = load_data()
    welcome_key = f"{telegram_id}:{tier}"
    if data["welcome_sent"].get(welcome_key):
        print(f"⚠️ Bienvenue déjà envoyé à {telegram_id} pour {tier}, skip")
        return
    msg_id = data["pending_msg"].pop(f"{telegram_id}:{tier}", None)
    save_data(data)
    if msg_id:
        try:
            await context.bot.delete_message(chat_id=telegram_id, message_id=msg_id)
        except Exception as e:
            print(f"⚠️ Suppression msg lien: {e}")
    bvn_msg = await context.bot.send_message(
        chat_id=telegram_id,
        text="Ton accès est activé. Bienvenue de l'autre côté 🖤🔥"
    )
    data = load_data()
    data["tarifs_msg"][f"bvn_{telegram_id}:{tier}"] = bvn_msg.message_id
    save_data(data)
    await asyncio.sleep(1.2)
    subs = get_subs_for_user(telegram_id)
    main_msg = await context.bot.send_message(
        chat_id=telegram_id,
        text=texte_espace_abo(subs),
        reply_markup=keyboard_espace_abo(subs)
    )
    data = load_data()
    data["welcome_sent"][welcome_key] = True
    data["tarifs_msg"][f"main_{telegram_id}"] = main_msg.message_id
    save_data(data)

# ── /start ────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    print(f"▶ /start — telegram_id: {telegram_id}")
    data = load_data()
    old_start_id = data["tarifs_msg"].pop(f"start_{telegram_id}", None)
    save_data(data)
    if old_start_id:
        try:
            await context.bot.delete_message(chat_id=telegram_id, message_id=old_start_id)
        except Exception:
            pass
    subs = get_subs_for_user(telegram_id)
    if subs:
        msg = await update.message.reply_text(
            texte_espace_abo(subs),
            reply_markup=keyboard_espace_abo(subs)
        )
        data = load_data()
        data["tarifs_msg"][f"start_{telegram_id}"] = msg.message_id
        save_data(data)
        return
    keyboard = [
        [InlineKeyboardButton("🩷 PRIVATE — 9,99€/mois", callback_data="page_premium")],
    ]
    msg = await update.message.reply_photo(
        photo=IMAGES["tarifs"],
        caption=(
            "Tu sais déjà pourquoi t'es là. 🔥\n\n"
            "Après paiement, tu rejoins mon canal privé instantanément 💕\n"
            "Aucune attente. Accès immédiat."
        ),
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    data = load_data()
    data["tarifs_msg"][str(telegram_id)] = msg.message_id
    data["tarifs_msg"][f"start_{telegram_id}"] = msg.message_id
    save_data(data)

# ── Callbacks ─────────────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    data_cb = query.data

    if data_cb == "page_premium":
        keyboard = [
            [InlineKeyboardButton("🔓 Accéder au canal PRIVATE", url=f"{TIERS['premium']['lien']}?client_reference_id={telegram_id}")],
            [InlineKeyboardButton("👈🏽 Retour", callback_data="page_tarifs")],
        ]
        await query.delete_message()
        msg = await context.bot.send_photo(
            chat_id=telegram_id, photo=IMAGES["premium"],
            caption=(
                "🩷 PRIVATE — 9,99€/mois\n\nCanal KAYLA PRIVATE\n\n"
                "🩷 Photos & vidéos en lingerie\n🩷 Topless exclusifs\n"
                "🩷 Contenu inédit, jamais publié ailleurs\n🩷 Nouveau contenu chaque semaine\n"
                "🩷 Accès à mes archives privées\n❤️‍🔥 Un mois de plaisir rien que pour toi"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        data = load_data()
        data["tarifs_msg"][str(telegram_id)] = msg.message_id
        save_data(data)

    elif data_cb in ("page_tarifs", "page_tarifs_new"):
        keyboard = [
            [InlineKeyboardButton("🩷 PRIVATE — 9,99€/mois", callback_data="page_premium")],
        ]
        await query.delete_message()
        msg = await context.bot.send_photo(
            chat_id=telegram_id, photo=IMAGES["tarifs"],
            caption=(
                "Tu sais déjà pourquoi t'es là. 🔥\n\n"
                "Après paiement, tu rejoins mon canal privé instantanément 💕\n"
                "Aucune attente. Accès immédiat."
            ),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        data = load_data()
        data["tarifs_msg"][str(telegram_id)] = msg.message_id
        save_data(data)

    elif data_cb.startswith("gen_lien_paiement_") and not data_cb.startswith("gen_lien_paiement_ok_") and not data_cb.startswith("gen_lien_paiement_cancel_"):
        tier = data_cb.replace("gen_lien_paiement_", "")
        sub_id, sub = get_sub_by_tier(telegram_id, tier)
        if not sub_id:
            await query.answer("❌ Abonnement introuvable.", show_alert=True)
            return
        in_canal = await is_user_in_canal(context.bot, telegram_id, tier)
        if in_canal:
            await query.edit_message_text(
                "✅ Tu es déjà dans le canal !\n\nSi tu as un problème d'accès, contacte le support.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Contacter le support", url=SUPPORT_URL)]])
            )
            return
        count = get_invite_count(telegram_id, tier)
        if count >= 2:
            await query.edit_message_text(
                "⛔ Limite atteinte\n\nTu as déjà généré 2 liens d'invitation.\n\nContacte le support.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Contacter le support", url=SUPPORT_URL)]])
            )
            return
        await query.edit_message_text(
            f"🔗 Générer un nouveau lien d'invitation\n\n"
            f"⚠️ Ce lien sera à usage unique et personnel.\n"
            f"Ne le partage jamais — ton abonnement serait résilié immédiatement sans remboursement.\n\n"
            f"Tu as {count}/2 liens générés. Confirmes-tu ?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Oui, générer le lien", callback_data=f"gen_lien_paiement_ok_{tier}")],
                [InlineKeyboardButton("❌ Annuler", callback_data=f"gen_lien_paiement_cancel_{tier}")],
            ])
        )

    elif data_cb.startswith("gen_lien_paiement_ok_"):
        tier = data_cb.replace("gen_lien_paiement_ok_", "")
        sub_id, sub = get_sub_by_tier(telegram_id, tier)
        if not sub_id:
            await query.edit_message_text("❌ Abonnement introuvable.")
            return
        in_canal = await is_user_in_canal(context.bot, telegram_id, tier)
        if in_canal:
            await query.edit_message_text(
                f"✅ Tu es déjà dans le canal !\n\nContacte le support si problème.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Contacter le support", url=SUPPORT_URL)]])
            )
            return
        count = get_invite_count(telegram_id, tier)
        if count >= 2:
            await query.edit_message_text(
                "⛔ Limite atteinte\n\nContacte le support.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Support", url=SUPPORT_URL)]])
            )
            return
        try:
            invite = await context.bot.create_chat_invite_link(chat_id=CANAUX[tier], member_limit=1, creates_join_request=False)
            new_count = increment_invite_count(telegram_id, tier)
            dots = "🟢" * new_count + "⚪" * (2 - new_count)
            tier_emoji = TIERS[tier]["emoji"]
            tier_short = TIERS[tier]["short"]
            kb = []
            if new_count < 2:
                kb.append([InlineKeyboardButton("🔗 Générer un nouveau lien", callback_data=f"gen_lien_paiement_{tier}")])
            else:
                kb.append([InlineKeyboardButton("⛔ Limite atteinte — 2/2", callback_data="noop")])
            await query.edit_message_text(
                f"✅ Paiement confirmé !\n\n"
                f"Rejoint ton canal {tier_emoji} {tier_short} ici (lien à usage unique) :\n"
                f"{invite.invite_link}\n\n"
                f"⚠️ Ce lien est personnel. Ne le partage jamais.\n\n"
                f"{dots} {new_count}/2 liens générés",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            data = load_data()
            data["pending_msg"][f"{telegram_id}:{tier}"] = query.message.message_id
            data["pending_link"][f"{telegram_id}:{tier}"] = invite.invite_link
            data["welcome_sent"].pop(f"{telegram_id}:{tier}", None)
            save_data(data)
        except Exception as e:
            print(f"❌ Erreur génération lien paiement: {e}")
            await query.answer("❌ Erreur. Contacte le support.", show_alert=True)

    elif data_cb.startswith("gen_lien_paiement_cancel_"):
        tier = data_cb.replace("gen_lien_paiement_cancel_", "")
        count = get_invite_count(telegram_id, tier)
        tier_emoji = TIERS[tier]["emoji"]
        tier_short = TIERS[tier]["short"]
        data = load_data()
        pending_link = data.get("pending_link", {}).get(f"{telegram_id}:{tier}", None)
        kb = []
        if count < 2:
            kb.append([InlineKeyboardButton("🔗 Générer un nouveau lien", callback_data=f"gen_lien_paiement_{tier}")])
        else:
            kb.append([InlineKeyboardButton("⛔ Limite atteinte — 2/2", callback_data="noop")])
        if pending_link:
            texte = (
                f"✅ Paiement confirmé !\n\n"
                f"Rejoint ton canal {tier_emoji} {tier_short} ici :\n"
                f"{pending_link}\n\n🔗 {count}/2 liens générés"
            )
        else:
            texte = (
                f"✅ Paiement confirmé !\n\n"
                f"Utilise le bouton ci-dessous pour générer ton lien.\n\n🔗 {count}/2 liens générés"
            )
        await query.edit_message_text(texte, reply_markup=InlineKeyboardMarkup(kb))

    elif data_cb.startswith("aide_rejoindre_"):
        tier = data_cb.replace("aide_rejoindre_", "")
        sub_id, sub = get_sub_by_tier(telegram_id, tier)
        if not sub_id:
            await query.answer("❌ Abonnement introuvable.", show_alert=True)
            return
        in_canal = await is_user_in_canal(context.bot, telegram_id, tier)
        if in_canal:
            await query.edit_message_text(
                "✅ Tu es déjà dans le canal !\n\nSi tu as un problème, contacte le support.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Contacter le support", url=SUPPORT_URL)]])
            )
            return
        count = get_invite_count(telegram_id, tier)
        if count >= 2:
            await query.edit_message_text(
                "⛔ Limite atteinte\n\nTu as déjà généré 2 liens.\n\nContacte le support.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Contacter le support", url=SUPPORT_URL)]])
            )
            return
        dots = "🟢" * count + "⚪" * (2 - count)
        await query.edit_message_text(
            f"❓ Tu n'arrives pas à rejoindre ?\n\n"
            f"Le lien a peut-être expiré ou a déjà été utilisé.\n\n"
            f"Tu peux en générer un nouveau ici.\n\n"
            f"⚠️ Maximum 2 générations possibles. {dots} {count}/2\n"
            f"Ne partage jamais ton lien.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Générer un nouveau lien", callback_data=f"gen_lien_depuis_paiement_{tier}")],
                [InlineKeyboardButton("👈🏽 Retour", callback_data=f"retour_paiement_{tier}")],
            ])
        )

    elif data_cb.startswith("gen_lien_depuis_paiement_"):
        tier = data_cb.replace("gen_lien_depuis_paiement_", "")
        sub_id, sub = get_sub_by_tier(telegram_id, tier)
        if not sub_id:
            await query.edit_message_text("❌ Abonnement introuvable.")
            return
        in_canal = await is_user_in_canal(context.bot, telegram_id, tier)
        if in_canal:
            await query.edit_message_text(
                "✅ Tu es déjà dans le canal !\n\nContacte le support si problème.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Contacter le support", url=SUPPORT_URL)]])
            )
            return
        count = get_invite_count(telegram_id, tier)
        if count >= 2:
            await query.edit_message_text(
                "⛔ Limite atteinte\n\nContacte le support.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Support", url=SUPPORT_URL)]])
            )
            return
        try:
            invite = await context.bot.create_chat_invite_link(chat_id=CANAUX[tier], member_limit=1, creates_join_request=False)
            new_count = increment_invite_count(telegram_id, tier)
            dots = "🟢" * new_count + "⚪" * (2 - new_count)
            tier_emoji = TIERS[tier]["emoji"]
            tier_short = TIERS[tier]["short"]
            kb = []
            if new_count < 2:
                kb.append([InlineKeyboardButton("🔗 Générer un autre lien", callback_data=f"gen_lien_depuis_paiement_{tier}")])
            else:
                kb.append([InlineKeyboardButton("⛔ Limite atteinte — 2/2", callback_data="noop")])
            kb.append([InlineKeyboardButton("👈🏽 Retour", callback_data=f"retour_paiement_{tier}")])
            await query.edit_message_text(
                f"✅ Ton nouveau lien d'invitation :\n\n"
                f"{invite.invite_link}\n\n"
                f"⚠️ Lien personnel à usage unique. Ne le partage jamais.\n\n"
                f"{dots} {new_count}/2 liens générés",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            data = load_data()
            data["pending_link"][f"{telegram_id}:{tier}"] = invite.invite_link
            data["welcome_sent"].pop(f"{telegram_id}:{tier}", None)
            save_data(data)
        except Exception as e:
            print(f"❌ Erreur génération lien aide: {e}")
            await query.answer("❌ Erreur. Contacte le support.", show_alert=True)

    elif data_cb.startswith("retour_paiement_"):
        tier = data_cb.replace("retour_paiement_", "")
        count = get_invite_count(telegram_id, tier)
        tier_emoji = TIERS[tier]["emoji"]
        tier_short = TIERS[tier]["short"]
        data = load_data()
        pending_link = data.get("pending_link", {}).get(f"{telegram_id}:{tier}", None)
        if pending_link:
            texte = (
                f"✅ Paiement confirmé !\n\n"
                f"Rejoint ton canal {tier_emoji} {tier_short} ici (lien à usage unique) :\n"
                f"{pending_link}"
            )
        else:
            texte = (
                f"✅ Paiement confirmé !\n\n"
                f"Utilise le bouton ci-dessous si tu n'arrives pas à rejoindre le canal {tier_emoji} {tier_short}."
            )
        await query.edit_message_text(
            texte,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❓ Je n'arrive pas à rejoindre", callback_data=f"aide_rejoindre_{tier}")
            ]])
        )

    elif data_cb.startswith("menu_gerer_"):
        tier = data_cb.replace("menu_gerer_", "")
        tier_nom = TIERS[tier]["nom"]
        data = load_data()
        bvn_id = data["tarifs_msg"].pop(f"bvn_{telegram_id}:{tier}", None)
        save_data(data)
        if bvn_id:
            try:
                await context.bot.delete_message(chat_id=telegram_id, message_id=bvn_id)
            except Exception:
                pass
        keyboard = [
            [InlineKeyboardButton("🔗 Accéder à mon canal", callback_data=f"menu_canal_{tier}")],
            [InlineKeyboardButton("💬 Support", callback_data="menu_support")],
            [InlineKeyboardButton("❌ Résilier", callback_data=f"menu_resilier_{tier}")],
            [InlineKeyboardButton("👈🏽 Retour", callback_data="menu_retour_abo")],
        ]
        await query.edit_message_text(
            f"⚙️ Gestion — {tier_nom}\n\nQue souhaites-tu faire ?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data_cb == "menu_retour_abo":
        subs = get_subs_for_user(telegram_id)
        if not subs:
            await query.edit_message_text("❌ Tu n'as pas d'abonnement actif.")
            return
        await query.edit_message_text(
            texte_espace_abo(subs),
            reply_markup=keyboard_espace_abo(subs)
        )

    elif data_cb.startswith("menu_canal_"):
        tier = data_cb.replace("menu_canal_", "")
        sub_id, sub = get_sub_by_tier(telegram_id, tier)
        if not sub_id:
            await query.edit_message_text("❌ Tu n'as pas d'abonnement actif.")
            return
        count = get_invite_count(telegram_id, tier)
        in_canal = await is_user_in_canal(context.bot, telegram_id, tier)
        if in_canal:
            await query.edit_message_text(
                "✅ Tu es déjà dans le canal !\n\nSi tu as un problème, contacte le support.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💬 Contacter le support", url=SUPPORT_URL)
                ], [InlineKeyboardButton("👈🏽 Retour", callback_data=f"menu_gerer_{tier}")]])
            )
            return
        if count >= 2:
            await query.edit_message_text(
                "⛔ Limite atteinte\n\nTu as déjà généré 2 liens.\n\nContacte le support.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💬 Contacter le support", url=SUPPORT_URL)
                ], [InlineKeyboardButton("👈🏽 Retour", callback_data=f"menu_gerer_{tier}")]])
            )
            return
        dots = "🟢" * count + "⚪" * (2 - count)
        await query.edit_message_text(
            f"🔗 Accéder à mon canal\n\n"
            f"Tu as quitté le groupe sans faire exprès ?\n\n"
            f"Tu peux générer un nouveau lien ici.\n\n"
            f"⚠️ Maximum 2 générations possibles. {dots} {count}/2\n"
            f"Si tu partages ton lien, ton abonnement sera résilié immédiatement.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Générer mon lien d'invitation", callback_data=f"gen_lien_{tier}")],
                [InlineKeyboardButton("👈🏽 Retour", callback_data=f"menu_gerer_{tier}")],
            ])
        )

    elif data_cb.startswith("gen_lien_") and not data_cb.startswith("gen_lien_paiement") and not data_cb.startswith("gen_lien_depuis"):
        tier = data_cb.replace("gen_lien_", "")
        sub_id, sub = get_sub_by_tier(telegram_id, tier)
        if not sub_id:
            await query.edit_message_text("❌ Tu n'as pas d'abonnement actif.")
            return
        in_canal = await is_user_in_canal(context.bot, telegram_id, tier)
        if in_canal:
            await query.edit_message_text(
                "✅ Tu es déjà dans le canal !\n\nContacte le support si problème.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💬 Support", url=SUPPORT_URL)
                ], [InlineKeyboardButton("👈🏽 Retour", callback_data=f"menu_gerer_{tier}")]])
            )
            return
        count = get_invite_count(telegram_id, tier)
        if count >= 2:
            await query.edit_message_text(
                "⛔ Limite atteinte\n\nTu as déjà généré 2 liens. Contacte le support.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💬 Support", url=SUPPORT_URL)
                ], [InlineKeyboardButton("👈🏽 Retour", callback_data=f"menu_gerer_{tier}")]])
            )
            return
        try:
            invite = await context.bot.create_chat_invite_link(chat_id=CANAUX[tier], member_limit=1, creates_join_request=False)
            new_count = increment_invite_count(telegram_id, tier)
            dots = "🟢" * new_count + "⚪" * (2 - new_count)
            kb = []
            if new_count < 2:
                kb.append([InlineKeyboardButton("🔗 Générer un autre lien", callback_data=f"gen_lien_{tier}")])
            else:
                kb.append([InlineKeyboardButton("⛔ Limite atteinte — 2/2", callback_data="noop")])
            kb.append([InlineKeyboardButton("👈🏽 Retour", callback_data=f"menu_gerer_{tier}")])
            await query.edit_message_text(
                f"✅ Ton lien d'invitation :\n\n"
                f"{invite.invite_link}\n\n"
                f"⚠️ Lien personnel à usage unique. Ne le partage jamais.\n\n"
                f"{dots} {new_count}/2 liens générés",
                reply_markup=InlineKeyboardMarkup(kb)
            )
        except Exception as e:
            print(f"❌ Erreur génération lien gestion: {e}")
            await query.edit_message_text("❌ Une erreur s'est produite. Contacte le support.")

    elif data_cb == "menu_support":
        await query.edit_message_text(
            "💬 Support\n\nUn problème ? Une question ?\n\nContacte-moi directement ici 👇\nJe réponds dans les plus brefs délais. 💕",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✉️ Contacter le support", url=SUPPORT_URL)],
                [InlineKeyboardButton("👈🏽 Retour", callback_data="menu_retour_abo")],
            ])
        )

    elif data_cb.startswith("menu_resilier_"):
        tier = data_cb.replace("menu_resilier_", "")
        sub_id, sub = get_sub_by_tier(telegram_id, tier)
        if not sub_id:
            await query.edit_message_text("❌ Tu n'as pas d'abonnement actif.")
            return
        tier_nom = TIERS[tier]["nom"]
        period_end = sub.get("period_end")
        date_fin = datetime.fromtimestamp(period_end).strftime("%d/%m/%Y") if period_end else (datetime.now() + timedelta(days=30)).strftime("%d/%m/%Y")
        await query.edit_message_text(
            f"⚠️ Attention — Résiliation de ton abonnement\n\n"
            f"Tu es sur le point d'annuler ton abonnement {tier_nom}.\n\n"
            f"Normalement ton accès était garanti jusqu'au {date_fin}.\n\n"
            f"❌ Si tu résilies maintenant, tu perds l'accès IMMÉDIATEMENT.\n"
            f"💸 Aucun remboursement ne sera effectué.\n\n"
            f"Es-tu vraiment sûr de vouloir perdre ton accès maintenant ?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Non, garder mon accès", callback_data=f"resilier_non_{tier}")],
                [InlineKeyboardButton("❌ Oui, perdre mon accès", callback_data=f"resilier_oui_{tier}_{sub_id}")],
            ])
        )

    elif data_cb.startswith("resilier_non_"):
        tier = data_cb.replace("resilier_non_", "")
        await query.edit_message_text(
            "✅ Bonne décision ! Ton abonnement reste actif. 💕",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔓 Retour à mon abonnement", callback_data=f"menu_gerer_{tier}")
            ]])
        )

    elif data_cb.startswith("resilier_oui_"):
        parts = data_cb.replace("resilier_oui_", "").split("_", 1)
        tier = parts[0]
        sub_id = parts[1] if len(parts) > 1 else ""
        sub_id_check, sub = get_sub_by_tier(telegram_id, tier)
        if sub_id_check != sub_id:
            await query.edit_message_text("❌ Erreur — abonnement introuvable.")
            return
        tier_nom = TIERS[tier]["nom"]
        period_end = sub.get("period_end")
        date_fin = datetime.fromtimestamp(period_end).strftime("%d/%m/%Y") if period_end else (datetime.now() + timedelta(days=30)).strftime("%d/%m/%Y")
        await query.edit_message_text(
            f"⛔ Dernière confirmation\n\n"
            f"Tu es sur le point de résilier définitivement ton abonnement {tier_nom}.\n\n"
            f"Ton accès au canal privé sera supprimé *immédiatement*.\n"
            f"Accès garanti jusqu'au : *{date_fin}*\n\n"
            f"Cette action est irréversible. Es-tu sûr ?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Non, je reste", callback_data=f"resilier_non_{tier}")],
                [InlineKeyboardButton("❌ Oui, confirmer", callback_data=f"resilier_confirmer_{tier}_{sub_id}")],
            ])
        )

    elif data_cb.startswith("resilier_confirmer_"):
        parts = data_cb.replace("resilier_confirmer_", "").split("_", 1)
        tier = parts[0]
        sub_id = parts[1] if len(parts) > 1 else ""
        sub_id_check, sub = get_sub_by_tier(telegram_id, tier)
        if sub_id_check != sub_id:
            await query.edit_message_text("❌ Erreur — abonnement introuvable.")
            return
        await query.edit_message_text("⏳ Résiliation en cours…")
        msg_id = query.message.message_id
        success = stripe_cancel_subscription(sub_id)
        if success:
            data = load_data()
            data["resilier_ctx"][sub_id] = {"msg_id": msg_id, "chat_id": telegram_id}
            save_data(data)
        else:
            await context.bot.send_message(
                chat_id=telegram_id,
                text="❌ Une erreur s'est produite. Contacte le support.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✉️ Support", url=SUPPORT_URL)]])
            )

    elif data_cb == "noop":
        pass

# ── Webhook Stripe ─────────────────────────────────────────────────────────────

class StripeWebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        if self.path != "/webhook":
            self.send_response(404); self.end_headers(); return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        stripe_signature = self.headers.get("Stripe-Signature")

        try:
            stripe.api_key = STRIPE_SECRET_KEY
            event = stripe.Webhook.construct_event(
                body, stripe_signature, STRIPE_WEBHOOK_SECRET
            )
        except ValueError:
            print("❌ Webhook payload invalide")
            self.send_response(400); self.end_headers(); return
        except stripe.error.SignatureVerificationError:
            print("❌ Signature webhook invalide — requête rejetée")
            self.send_response(400); self.end_headers(); return

        self.send_response(200); self.end_headers()
        event_type = event["type"]
        print(f"📨 Événement: {event_type}")

        if event_type == "checkout.session.completed":
            session = event["data"]["object"]
            telegram_id = getattr(session, "client_reference_id", None)
            customer_id = getattr(session, "customer", None)
            payment_link = getattr(session, "payment_link", None)
            subscription_id = getattr(session, "subscription", None)
            tier = PAYMENT_LINKS.get(payment_link)

            # Validation du telegram_id
            if not telegram_id or not str(telegram_id).isdigit():
                print(f"❌ telegram_id invalide: {telegram_id}")
                return

            print(f"🔎 sub: {subscription_id}, tier: {tier}")

            if telegram_id and customer_id:
                data = load_data()
                data["customers"][customer_id] = int(telegram_id)
                save_data(data)

            if telegram_id and tier and subscription_id:
                period_end = None
                try:
                    stripe.api_key = STRIPE_SECRET_KEY
                    stripe_sub = stripe.Subscription.retrieve(subscription_id)
                    period_end = getattr(stripe_sub, "current_period_end", None)
                except Exception as e:
                    print(f"⚠️ period_end: {e}")
                if not period_end:
                    period_end = int((datetime.now() + timedelta(days=30)).timestamp())
                asyncio.run_coroutine_threadsafe(
                    ajouter_membre(int(telegram_id), tier, subscription_id, period_end),
                    webhook_loop
                )

        elif event_type == "invoice.payment_succeeded":
            obj = event["data"]["object"]
            subscription_id = getattr(obj, "subscription", None)
            lines_obj = getattr(obj, "lines", None)
            if lines_obj is not None:
                lines = lines_obj.data if hasattr(lines_obj, "data") else []
            else:
                lines = []
            period_end = None
            for line in lines:
                period_obj = getattr(line, "period", None)
                pe = getattr(period_obj, "end", None) if period_obj else None
                if pe:
                    period_end = pe
                    break
            if not period_end and subscription_id:
                try:
                    stripe.api_key = STRIPE_SECRET_KEY
                    stripe_sub = stripe.Subscription.retrieve(subscription_id)
                    period_end = getattr(stripe_sub, "current_period_end", None)
                except Exception as e:
                    print(f"⚠️ Erreur fallback period_end: {e}")
            if not period_end:
                period_end = int((datetime.now() + timedelta(days=30)).timestamp())
            if subscription_id:
                data = load_data()
                if subscription_id in data["subscriptions"]:
                    data["subscriptions"][subscription_id]["period_end"] = period_end
                    save_data(data)
                    print(f"🔄 Renouvellement — sub: {subscription_id}")

        elif event_type == "charge.dispute.created":
            # Anti-fraude chargeback : kick immédiat
            obj = event["data"]["object"]
            customer_id = getattr(obj, "customer", None)
            if customer_id:
                data = load_data()
                telegram_id = data["customers"].get(customer_id)
                if telegram_id:
                    # Trouver tous les subs de cet user et les annuler
                    subs_user = {
                        sid: sub for sid, sub in data["subscriptions"].items()
                        if sub["telegram_id"] == telegram_id
                    }
                    for sub_id in subs_user:
                        print(f"⚠️ Chargeback détecté — kick {telegram_id}, sub: {sub_id}")
                        asyncio.run_coroutine_threadsafe(
                            retirer_membre(sub_id),
                            webhook_loop
                        )

        elif event_type in ("customer.subscription.deleted", "invoice.payment_failed"):
            obj = event["data"]["object"]
            subscription_id = getattr(obj, "id", None) if event_type == "customer.subscription.deleted" else getattr(obj, "subscription", None)
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

    # Check variables critiques au démarrage
    if not PAYMENT_LINKS:
        print("❌ FATAL: variable d'env PAYMENT_LINKS non définie ou vide — le bot ne pourra pas traiter les paiements")
    else:
        print(f"✅ PAYMENT_LINKS chargés: {PAYMENT_LINKS}")
    if not TOKEN:
        print("❌ FATAL: TOKEN non défini")
    if not STRIPE_SECRET_KEY:
        print("❌ FATAL: STRIPE_SECRET_KEY non défini")
    if not STRIPE_WEBHOOK_SECRET:
        print("❌ FATAL: STRIPE_WEBHOOK_SECRET non défini")

    print(subprocess.run(["df", "-h"], capture_output=True, text=True).stdout)
    print("LS DATA:", subprocess.run(["ls", "-la", "/data"], capture_output=True, text=True).stdout)
    print("SUBS:", subprocess.run(["cat", "/data/subscriptions.json"], capture_output=True, text=True).stdout)

    # Démarrer webhook avant le sleep pour ne pas rater d'événements Stripe
    loop_thread = threading.Thread(target=run_webhook_loop, daemon=True)
    loop_thread.start()

    webhook_thread = threading.Thread(target=start_webhook_server, daemon=True)
    webhook_thread.start()
    print("✅ Serveur webhook démarré sur le port 8000")

    time.sleep(15)  # Laisser Railway terminer le déploiement avant de poller Telegram

    while True:
        try:
            app = ApplicationBuilder().token(TOKEN).build()
            app.add_handler(CommandHandler("start", start))
            app.add_handler(CallbackQueryHandler(handle_callback))
            app.add_handler(ChatMemberHandler(membre_rejoint, ChatMemberHandler.CHAT_MEMBER))
            print("✅ Bot démarré...")
            app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
        except Conflict:
            print("⚠️ Conflit, retry dans 15s...")
            time.sleep(15)
        except Exception as e:
            print(f"❌ Erreur: {e}, retry dans 15s...")
            time.sleep(15)
