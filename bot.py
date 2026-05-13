import os
import json
import time
import asyncio
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ChatMemberHandler, ContextTypes
from telegram.error import Conflict
import stripe

TOKEN = os.getenv("TOKEN")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/ton_support")

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

IMAGES = {
    "tarifs":  "AgACAgQAAxkBAAN9agMssUQeV1jLojb-69ij0iXD_awAAiwOaxvvlRhQuJV4QXsp0d4BAAMCAAN5AAM7BA",
    "private": "AgACAgQAAxkBAAN_agMsvW9juzcBzDzl_1nLWqBxXcAAAi0OaxvvlRhQS9mcR9IEb98BAAMCAAN5AAM7BA",
    "vip":     "AgACAgQAAxkBAAOBagMsxJkf9H1qXuIm5XQ9FfuXpYMAAi4OaxvvlRhQIaouXU2zf9oBAAMCAAN5AAM7BA",
}

SUBS_FILE = "/data/subscriptions.json"

# ── Data ──────────────────────────────────────────────────────────────────────

def load_data():
    os.makedirs("/data", exist_ok=True)
    if not os.path.exists(SUBS_FILE):
        return {"subscriptions": {}, "customers": {}, "invite_counts": {}, "pending_msg": {}, "tarifs_msg": {}}
    try:
        with open(SUBS_FILE, "r") as f:
            data = json.load(f)
            for key in ("subscriptions", "customers", "invite_counts", "pending_msg", "tarifs_msg"):
                if key not in data:
                    data[key] = {}
            return data
    except Exception:
        return {"subscriptions": {}, "customers": {}, "invite_counts": {}, "pending_msg": {}, "tarifs_msg": {}}

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

webhook_loop = asyncio.new_event_loop()

def run_webhook_loop():
    asyncio.set_event_loop(webhook_loop)
    webhook_loop.run_forever()

# ── Helpers UI ────────────────────────────────────────────────────────────────

def keyboard_espace_abo(tier: str):
    keyboard = []
    if tier == "premium":
        keyboard.append([InlineKeyboardButton("⬆️ Upgrader mon abonnement", callback_data="menu_upgrade")])
    keyboard.append([InlineKeyboardButton("⚙️ Gérer mon abonnement", callback_data="menu_gerer")])
    return InlineKeyboardMarkup(keyboard)

# ── Actions bot ───────────────────────────────────────────────────────────────

async def ajouter_membre(telegram_id: int, tier: str, subscription_id: str, period_end: int = None):
    bot = Bot(token=TOKEN)
    try:
        # Sauvegarder abonnement
        data = load_data()
        data["subscriptions"] = {k: v for k, v in data["subscriptions"].items() if v["telegram_id"] != telegram_id}
        data["subscriptions"][subscription_id] = {
            "telegram_id": telegram_id,
            "tier": tier,
            "period_end": period_end,
        }

        # Supprimer le message de sélection d'abonnement (tarifs_msg)
        tarifs_msg_id = data["tarifs_msg"].pop(str(telegram_id), None)
        save_data(data)

        if tarifs_msg_id:
            try:
                await bot.delete_message(chat_id=telegram_id, message_id=tarifs_msg_id)
                print(f"🗑️ Message tarifs supprimé pour {telegram_id}")
            except Exception as e:
                print(f"⚠️ Suppression tarifs_msg: {e}")

        print(f"💾 Sauvegardé: {subscription_id} → {telegram_id} ({tier})")
        print(f"📦 Paiement — telegram_id: {telegram_id}, tier: {tier}, sub: {subscription_id}")

        tier_nom = TIERS[tier]["nom"]
        tier_emoji = "🩷" if tier == "premium" else "💗"
        tier_short = "PRIVATE" if tier == "premium" else "VIP"

        # Créer lien d'invitation (ne compte PAS dans le compteur)
        invite = await bot.create_chat_invite_link(
            chat_id=CANAUX[tier],
            member_limit=1,
            creates_join_request=False
        )

        # Sauvegarder le message_id pour le supprimer quand l'user rejoint
        msg1 = await bot.send_message(
            chat_id=telegram_id,
            text=(
                f"✅ Paiement confirmé !\n\n"
                f"Rejoint ton canal {tier_emoji} {tier_short} ici (lien à usage unique) :\n"
                f"{invite.invite_link}"
            ),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔗 Générer un nouveau lien", callback_data="gen_lien_paiement")
            ]])
        )

        data = load_data()
        data["pending_msg"][str(telegram_id)] = msg1.message_id
        save_data(data)

        print(f"✅ Lien paiement envoyé à {telegram_id} pour {tier}, msg_id: {msg1.message_id}")
    except Exception as e:
        print(f"❌ Erreur ajout: {e}")
    finally:
        await bot.shutdown()

async def retirer_membre(subscription_id: str, edit_msg_id: int = None, edit_chat_id: int = None):
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

        try:
            await bot.ban_chat_member(chat_id=CANAUX[tier], user_id=telegram_id)
            await bot.unban_chat_member(chat_id=CANAUX[tier], user_id=telegram_id)
        except Exception as e:
            print(f"⚠️ Kick: {e}")

        msg_annule = (
            "Ton abonnement a été annulé.\n\n"
            "Tu n'as plus accès au canal privé de Kayla. 🖤"
        )
        kb_reabo = InlineKeyboardMarkup([[
            InlineKeyboardButton("🩷 Se réabonner", callback_data="page_tarifs_new")
        ]])

        if edit_msg_id and edit_chat_id:
            try:
                await bot.edit_message_text(
                    chat_id=edit_chat_id,
                    message_id=edit_msg_id,
                    text=msg_annule,
                    reply_markup=kb_reabo
                )
            except Exception as e:
                print(f"⚠️ Edit msg résiliation: {e}")
                await bot.send_message(chat_id=telegram_id, text=msg_annule, reply_markup=kb_reabo)
        else:
            await bot.send_message(chat_id=telegram_id, text=msg_annule, reply_markup=kb_reabo)

        del data["subscriptions"][subscription_id]
        data["invite_counts"].pop(str(telegram_id), None)
        data["pending_msg"].pop(str(telegram_id), None)
        data["tarifs_msg"].pop(str(telegram_id), None)
        save_data(data)
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

    sub_id, sub = get_sub_for_user(telegram_id)
    if not sub_id:
        await context.bot.ban_chat_member(chat_id=chat_id_canal, user_id=telegram_id)
        await context.bot.unban_chat_member(chat_id=chat_id_canal, user_id=telegram_id)
        print(f"🚫 Intrus kické — telegram_id: {telegram_id}")
        return

    print(f"✅ Entrée canal — telegram_id: {telegram_id}, tier: {sub['tier']}")

    # Supprimer message 1 (lien de paiement)
    data = load_data()
    msg_id = data["pending_msg"].pop(str(telegram_id), None)
    save_data(data)
    if msg_id:
        try:
            await context.bot.delete_message(chat_id=telegram_id, message_id=msg_id)
        except Exception as e:
            print(f"⚠️ Suppression msg1: {e}")

    tier = sub["tier"]
    tier_nom = TIERS[tier]["nom"]

    # Message bienvenue
    await context.bot.send_message(
        chat_id=telegram_id,
        text="Ton accès est activé. Bienvenue de l'autre côté 🖤🔥"
    )
    await asyncio.sleep(1.2)

    # Message gestion
    await context.bot.send_message(
        chat_id=telegram_id,
        text=(
            f"✅ {tier_nom} actif\n\n"
            f"Ton abonnement est actif. 💕\n\n"
            f"Que souhaites-tu faire ?"
        ),
        reply_markup=keyboard_espace_abo(tier)
    )

# ── /start ────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    print(f"▶ /start — telegram_id: {telegram_id}")

    sub_id, sub = get_sub_for_user(telegram_id)
    if sub_id:
        tier = sub["tier"]
        tier_nom = TIERS[tier]["nom"]
        await update.message.reply_text(
            f"✅ {tier_nom} actif\n\n"
            f"Ton abonnement est actif. 💕\n\n"
            f"Que souhaites-tu faire ?",
            reply_markup=keyboard_espace_abo(tier)
        )
        return

    keyboard = [
        [InlineKeyboardButton("🩷 PRIVATE — 9,99€/mois", callback_data="page_private")],
        [InlineKeyboardButton("💗 VIP — 19,99€/mois", callback_data="page_vip")],
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

    # Sauvegarder le message_id du message tarifs pour le supprimer au paiement
    data = load_data()
    data["tarifs_msg"][str(telegram_id)] = msg.message_id
    save_data(data)

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
        await query.delete_message()
        msg = await context.bot.send_photo(
            chat_id=telegram_id,
            photo=IMAGES["private"],
            caption=(
                "🩷 PRIVATE — 9,99€/mois\n\n"
                "Canal KAYLA PRIVATE\n\n"
                "🩷 Photos & vidéos en lingerie\n"
                "🩷 Topless exclusifs\n"
                "🩷 Contenu inédit, jamais publié ailleurs\n"
                "🩷 Nouveau contenu chaque semaine\n"
                "🩷 Accès à mes archives privées\n"
                "❤️‍🔥 Un mois de plaisir rien que pour toi\n\n"
                "La plupart ne restent pas longtemps au PRIVATE. Une fois qu'ils découvrent le VIP… ils upgradent. 👀"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        # Mettre à jour tarifs_msg avec le nouveau message
        data = load_data()
        data["tarifs_msg"][str(telegram_id)] = msg.message_id
        save_data(data)

    # ── PAGE VIP ──
    elif data_cb == "page_vip":
        keyboard = [
            [InlineKeyboardButton(
                "🔓 Accéder au canal VIP",
                url=f"{TIERS['vip']['lien']}?client_reference_id={telegram_id}"
            )],
            [InlineKeyboardButton("👈🏽 Retour", callback_data="page_tarifs")],
        ]
        await query.delete_message()
        msg = await context.bot.send_photo(
            chat_id=telegram_id,
            photo=IMAGES["vip"],
            caption=(
                "💗 VIP — 19,99€/mois\n\n"
                "Canal KAYLA VIP\n\n"
                "💗 Tout le contenu PRIVATE inclus\n"
                "💗 Full nude & vidéos exclusives\n"
                "💗 2x plus de contenu que le PRIVATE\n"
                "💗 Accès en avant-première à toutes mes nouveautés\n"
                "💗 Contenu réservé uniquement aux VIP\n"
                "❤️‍🔥 Une expérience unique & inoubliable\n\n"
                "Ceux qui ont le VIP ne regardent plus jamais en arrière. 🖤"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        data = load_data()
        data["tarifs_msg"][str(telegram_id)] = msg.message_id
        save_data(data)

    # ── PAGE TARIFS (retour) ──
    elif data_cb == "page_tarifs":
        keyboard = [
            [InlineKeyboardButton("🩷 PRIVATE — 9,99€/mois", callback_data="page_private")],
            [InlineKeyboardButton("💗 VIP — 19,99€/mois", callback_data="page_vip")],
        ]
        await query.delete_message()
        msg = await context.bot.send_photo(
            chat_id=telegram_id,
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
        save_data(data)

    # ── SE RÉABONNER → page tarifs (depuis message annulation) ──
    elif data_cb == "page_tarifs_new":
        keyboard = [
            [InlineKeyboardButton("🩷 PRIVATE — 9,99€/mois", callback_data="page_private")],
            [InlineKeyboardButton("💗 VIP — 19,99€/mois", callback_data="page_vip")],
        ]
        await query.delete_message()
        msg = await context.bot.send_photo(
            chat_id=telegram_id,
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
        save_data(data)

    # ── GÉNÉRER LIEN (depuis message paiement confirmé) ──
    elif data_cb == "gen_lien_paiement":
        sub_id, sub = get_sub_for_user(telegram_id)
        if not sub_id:
            await query.answer("❌ Abonnement introuvable.", show_alert=True)
            return

        tier = sub["tier"]

        # Vérifier si déjà dans le canal
        in_canal = await is_user_in_canal(context.bot, telegram_id, tier)
        if in_canal:
            await query.answer("✅ Tu es déjà dans le canal !", show_alert=True)
            return

        count = get_invite_count(telegram_id)
        if count >= 2:
            keyboard = [
                [InlineKeyboardButton("💬 Contacter le support", url=SUPPORT_URL)],
            ]
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
            await query.answer("⛔ Limite de 2 liens atteinte.", show_alert=True)
            return

        try:
            invite = await context.bot.create_chat_invite_link(
                chat_id=CANAUX[tier],
                member_limit=1,
                creates_join_request=False
            )
            new_count = increment_invite_count(telegram_id)
            tier_emoji = "🩷" if tier == "premium" else "💗"
            tier_short = "PRIVATE" if tier == "premium" else "VIP"
            print(f"🔗 Nouveau lien généré (paiement) — telegram_id: {telegram_id}, {new_count}/2")

            kb = []
            if new_count < 2:
                kb.append([InlineKeyboardButton("🔗 Générer un nouveau lien", callback_data="gen_lien_paiement")])
            else:
                kb.append([InlineKeyboardButton("⛔ Limite atteinte — 2/2", callback_data="noop")])

            await query.edit_message_text(
                f"✅ Paiement confirmé !\n\n"
                f"Rejoint ton canal {tier_emoji} {tier_short} ici (lien à usage unique) :\n"
                f"{invite.invite_link}\n\n"
                f"⚠️ Ce lien est personnel. Ne le partage jamais — ton abonnement serait résilié immédiatement sans remboursement.\n\n"
                f"🔗 {new_count}/2 liens générés",
                reply_markup=InlineKeyboardMarkup(kb)
            )

            # Mettre à jour pending_msg
            data = load_data()
            data["pending_msg"][str(telegram_id)] = query.message.message_id
            save_data(data)

        except Exception as e:
            print(f"❌ Erreur génération lien paiement: {e}")
            await query.answer("❌ Erreur. Contacte le support.", show_alert=True)

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
        await query.edit_message_text(
            f"✅ {tier_nom} actif\n\n"
            f"Ton abonnement est actif. 💕\n\n"
            f"Que souhaites-tu faire ?",
            reply_markup=keyboard_espace_abo(tier)
        )

    # ── ACCÉDER AU CANAL ──
    elif data_cb == "menu_canal":
        sub_id, sub = get_sub_for_user(telegram_id)
        if not sub_id:
            await query.edit_message_text("❌ Tu n'as pas d'abonnement actif.")
            return

        tier = sub["tier"]
        count = get_invite_count(telegram_id)
        print(f"🔗 Canal — telegram_id: {telegram_id}, liens: {count}/2")

        # Vérifier si déjà dans le canal
        in_canal = await is_user_in_canal(context.bot, telegram_id, tier)
        if in_canal:
            keyboard = [[InlineKeyboardButton("👈🏽 Retour", callback_data="menu_gerer")]]
            await query.edit_message_text(
                "✅ Tu es déjà dans le canal !\n\n"
                "Si tu as un problème, contacte le support.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💬 Contacter le support", url=SUPPORT_URL)
                ], [
                    InlineKeyboardButton("👈🏽 Retour", callback_data="menu_gerer")
                ]])
            )
            return

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

    # ── GÉNÉRER LIEN (depuis gestion) ──
    elif data_cb == "gen_lien":
        sub_id, sub = get_sub_for_user(telegram_id)
        if not sub_id:
            await query.edit_message_text("❌ Tu n'as pas d'abonnement actif.")
            return

        tier = sub["tier"]

        # Vérifier si déjà dans le canal
        in_canal = await is_user_in_canal(context.bot, telegram_id, tier)
        if in_canal:
            await query.edit_message_text(
                "✅ Tu es déjà dans le canal !\n\n"
                "Si tu as un problème, contacte le support.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💬 Contacter le support", url=SUPPORT_URL)
                ], [
                    InlineKeyboardButton("👈🏽 Retour", callback_data="menu_gerer")
                ]])
            )
            return

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
        period_end = sub.get("period_end")
        date_fin = datetime.fromtimestamp(period_end).strftime("%d/%m/%Y") if period_end else "inconnue"

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
            "✅ Bonne décision ! Ton abonnement reste actif. 💕",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ── RÉSILIER OUI → confirmation finale ──
    elif data_cb.startswith("resilier_oui_"):
        sub_id = data_cb.replace("resilier_oui_", "")
        sub_id_check, sub = get_sub_for_user(telegram_id)
        if sub_id_check != sub_id:
            await query.edit_message_text("❌ Erreur — abonnement introuvable.")
            return

        tier = sub["tier"]
        tier_nom = TIERS[tier]["nom"]
        period_end = sub.get("period_end")
        date_fin = datetime.fromtimestamp(period_end).strftime("%d/%m/%Y") if period_end else "inconnue"

        # Étape de confirmation finale
        keyboard = [
            [InlineKeyboardButton("✅ Non, je reste", callback_data="resilier_non")],
            [InlineKeyboardButton("❌ Oui, confirmer la résiliation", callback_data=f"resilier_confirmer_{sub_id}")],
        ]
        await query.edit_message_text(
            f"⛔ Dernière confirmation\n\n"
            f"Tu es sur le point de résilier définitivement ton abonnement {tier_nom}.\n\n"
            f"Ton accès au canal privé sera supprimé *immédiatement*.\n"
            f"Accès garanti jusqu'au : *{date_fin}*\n\n"
            f"Cette action est irréversible. Es-tu sûr ?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ── RÉSILIER CONFIRMER (exécution) ──
    elif data_cb.startswith("resilier_confirmer_"):
        sub_id = data_cb.replace("resilier_confirmer_", "")
        sub_id_check, sub = get_sub_for_user(telegram_id)
        if sub_id_check != sub_id:
            await query.edit_message_text("❌ Erreur — abonnement introuvable.")
            return

        await query.edit_message_text("⏳ Résiliation en cours…")
        msg_id = query.message.message_id

        success = stripe_cancel_subscription(sub_id)
        if success:
            data = load_data()
            if sub_id in data["subscriptions"]:
                data["subscriptions"][sub_id]["resilier_msg_id"] = msg_id
                data["subscriptions"][sub_id]["resilier_chat_id"] = telegram_id
                save_data(data)
        else:
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
                period_end = None
                try:
                    stripe.api_key = STRIPE_SECRET_KEY
                    stripe_sub = stripe.Subscription.retrieve(subscription_id)
                    period_end = getattr(stripe_sub, "current_period_end", None)
                except Exception as e:
                    print(f"⚠️ Impossible de récupérer period_end: {e}")

                asyncio.run_coroutine_threadsafe(
                    ajouter_membre(int(telegram_id), tier, subscription_id, period_end),
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

        elif event_type == "invoice.payment_succeeded":
            obj = event["data"]["object"]
            subscription_id = obj.get("subscription")
            if subscription_id:
                try:
                    stripe.api_key = STRIPE_SECRET_KEY
                    stripe_sub = stripe.Subscription.retrieve(subscription_id)
                    period_end = getattr(stripe_sub, "current_period_end", None)
                    if period_end:
                        data = load_data()
                        if subscription_id in data["subscriptions"]:
                            data["subscriptions"][subscription_id]["period_end"] = period_end
                            save_data(data)
                            print(f"🔄 Renouvellement — sub: {subscription_id}, date: {datetime.fromtimestamp(period_end).strftime('%d/%m/%Y')}")
                except Exception as e:
                    print(f"⚠️ Erreur update period_end: {e}")

        elif event_type in ("customer.subscription.deleted", "invoice.payment_failed"):
            obj = event["data"]["object"]
            subscription_id = obj.get("id") if event_type == "customer.subscription.deleted" else obj.get("subscription")
            if subscription_id:
                data = load_data()
                sub = data["subscriptions"].get(subscription_id, {})
                edit_msg_id = sub.get("resilier_msg_id")
                edit_chat_id = sub.get("resilier_chat_id")
                asyncio.run_coroutine_threadsafe(
                    retirer_membre(subscription_id, edit_msg_id, edit_chat_id),
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
            app.add_handler(ChatMemberHandler(membre_rejoint, ChatMemberHandler.CHAT_MEMBER))
            print("✅ Bot démarré...")
            app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
        except Conflict:
            print("⚠️ Conflit, retry dans 15s...")
            time.sleep(15)
        except Exception as e:
            print(f"❌ Erreur: {e}, retry dans 15s...")
            time.sleep(15)
