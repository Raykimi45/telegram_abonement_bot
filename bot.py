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
        "short": "PRIVATE",
        "emoji": "🩷",
        "prix": "9,99€/mois",
        "lien": "https://buy.stripe.com/test_9B6fZb2gTcy8b2p7tve3e00",
    },
    "vip": {
        "nom": "💗 KAYLA VIP",
        "short": "VIP",
        "emoji": "💗",
        "prix": "19,99€/mois",
        "lien": "https://buy.stripe.com/test_14AdR3bRt55G6M9cNPe3e01",
    }
}

IMAGES = {
    "tarifs":  "AgACAgQAAxkBAAN9agMssUQeV1jLojb-69ij0iXD_awAAiwOaxvvlRhQuJV4QXsp0d4BAAMCAAN5AAM7BA",
    "premium": "AgACAgQAAxkBAAN_agMsvW9juzcBzDzl_1nLWqBxXcAAAi0OaxvvlRhQS9mcR9IEb98BAAMCAAN5AAM7BA",
    "vip":     "AgACAgQAAxkBAAOBagMsxJkf9H1qXuIm5XQ9FfuXpYMAAi4OaxvvlRhQIaouXU2zf9oBAAMCAAN5AAM7BA",
}

SUBS_FILE = "/data/subscriptions.json"

# ── Data ──────────────────────────────────────────────────────────────────────

def load_data():
    os.makedirs("/data", exist_ok=True)
    if not os.path.exists(SUBS_FILE):
        return {
            "subscriptions": {},   # sub_id → {telegram_id, tier, period_end}
            "customers": {},       # customer_id → telegram_id
            "invite_counts": {},   # "telegram_id:tier" → count
            "pending_msg": {},     # "telegram_id:tier" → msg_id (lien paiement)
            "pending_link": {},    # "telegram_id:tier" → lien invite
            "tarifs_msg": {},      # telegram_id → msg_id (page tarifs)
            "welcome_sent": {},    # "telegram_id:tier" → bool (bienvenue déjà envoyé)
            "resilier_ctx": {},    # sub_id → {msg_id, chat_id}
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
    os.makedirs("/data", exist_ok=True)
    with open(SUBS_FILE, "w") as f:
        json.dump(data, f)

def get_subs_for_user(telegram_id: int) -> dict:
    """Retourne {sub_id: sub} pour tous les abonnements actifs de l'user."""
    data = load_data()
    return {
        sub_id: sub
        for sub_id, sub in data["subscriptions"].items()
        if sub["telegram_id"] == telegram_id
    }

def get_sub_by_tier(telegram_id: int, tier: str):
    """Retourne (sub_id, sub) pour un tier précis."""
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

webhook_loop = asyncio.new_event_loop()

def run_webhook_loop():
    asyncio.set_event_loop(webhook_loop)
    webhook_loop.run_forever()

# ── UI helpers ────────────────────────────────────────────────────────────────

def keyboard_espace_abo(subs: dict) -> InlineKeyboardMarkup:
    """Génère le clavier de l'espace abonné selon les tiers actifs."""
    keyboard = []
    tiers_actifs = [sub["tier"] for sub in subs.values()]

    # Bouton upgrade uniquement si PRIVATE sans VIP
    if "premium" in tiers_actifs and "vip" not in tiers_actifs:
        keyboard.append([InlineKeyboardButton("⬆️ Upgrader vers le VIP", callback_data="menu_upgrade")])

    if len(tiers_actifs) == 1:
        keyboard.append([InlineKeyboardButton("⚙️ Gérer mon abonnement", callback_data=f"menu_gerer_{tiers_actifs[0]}")])
    else:
        keyboard.append([InlineKeyboardButton("⚙️ Gérer PRIVATE", callback_data="menu_gerer_premium")])
        keyboard.append([InlineKeyboardButton("⚙️ Gérer VIP", callback_data="menu_gerer_vip")])

    return InlineKeyboardMarkup(keyboard)

def texte_espace_abo(subs: dict) -> str:
    tiers_actifs = [sub["tier"] for sub in subs.values()]
    if len(tiers_actifs) == 2:
        return (
            "✅ 🩷 KAYLA PRIVATE actif\n"
            "✅ 💗 KAYLA VIP actif\n\n"
            "Tes 2 abonnements sont actifs. 💕\n\n"
            "Que souhaites-tu faire ?"
        )
    tier = tiers_actifs[0]
    tier_nom = TIERS[tier]["nom"]
    return (
        f"✅ {tier_nom} actif\n\n"
        f"Ton abonnement est actif. 💕\n\n"
        f"Que souhaites-tu faire ?"
    )

# ── Actions bot ───────────────────────────────────────────────────────────────

async def ajouter_membre(telegram_id: int, tier: str, subscription_id: str, period_end: int = None):
    bot = Bot(token=TOKEN)
    try:
        data = load_data()

        # Sauvegarder le nouvel abonnement (sans écraser les autres)
        data["subscriptions"][subscription_id] = {
            "telegram_id": telegram_id,
            "tier": tier,
            "period_end": period_end,
        }

        # Supprimer message page tarifs ET message espace abonné précédent
        tarifs_msg_id = data["tarifs_msg"].pop(str(telegram_id), None)
        main_msg_id = data["tarifs_msg"].pop(f"main_{telegram_id}", None)
        start_msg_id = data["tarifs_msg"].pop(f"start_{telegram_id}", None)
        save_data(data)

        for msg_id_to_del in [tarifs_msg_id, main_msg_id, start_msg_id]:
            if msg_id_to_del:
                try:
                    await bot.delete_message(chat_id=telegram_id, message_id=msg_id_to_del)
                except Exception as e:
                    print(f"⚠️ Suppression msg: {e}")

        print(f"💾 Sauvegardé: {subscription_id} → {telegram_id} ({tier})")
        print(f"📦 Paiement — telegram_id: {telegram_id}, tier: {tier}, sub: {subscription_id}")

        # Créer lien d'invitation (ne compte PAS dans le compteur)
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
                InlineKeyboardButton("🔗 Générer un nouveau lien", callback_data=f"gen_lien_paiement_{tier}")
            ]])
        )

        data = load_data()
        data["pending_msg"][f"{telegram_id}:{tier}"] = msg1.message_id
        data["pending_link"][f"{telegram_id}:{tier}"] = invite.invite_link
        save_data(data)

        print(f"✅ Lien paiement envoyé à {telegram_id} pour {tier}, msg_id: {msg1.message_id}")
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

        # Récupérer ctx résiliation manuelle
        ctx = data["resilier_ctx"].pop(subscription_id, None)

        # Supprimer le sub avant de recalculer les subs restants
        del data["subscriptions"][subscription_id]
        data["invite_counts"].pop(f"{telegram_id}:{tier}", None)
        data["pending_msg"].pop(f"{telegram_id}:{tier}", None)
        data["welcome_sent"].pop(f"{telegram_id}:{tier}", None)
        save_data(data)

        # Vérifier s'il reste des abonnements actifs
        subs_restants = get_subs_for_user(telegram_id)

        if subs_restants:
            # Ajouter boutons gestion abonnement(s) restant(s)
            kb_annule = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🩷 Se réabonner", callback_data="page_tarifs_new")]]
                + keyboard_espace_abo(subs_restants).inline_keyboard
            )
        else:
            kb_annule = kb_reabo

        # Supprimer message espace abonné si présent
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

    # Identifier le tier depuis le canal
    tier = None
    for t, cid in CANAUX.items():
        if cid == chat_id_canal:
            tier = t
            break
    if not tier:
        return

    # Vérifier abonnement
    sub_id, sub = get_sub_by_tier(telegram_id, tier)
    if not sub_id:
        await context.bot.ban_chat_member(chat_id=chat_id_canal, user_id=telegram_id)
        await context.bot.unban_chat_member(chat_id=chat_id_canal, user_id=telegram_id)
        print(f"🚫 Intrus kické — telegram_id: {telegram_id}, tier: {tier}")
        return

    print(f"✅ Entrée canal — telegram_id: {telegram_id}, tier: {tier}")

    # Éviter doublon bienvenue
    data = load_data()
    welcome_key = f"{telegram_id}:{tier}"
    if data["welcome_sent"].get(welcome_key):
        print(f"⚠️ Bienvenue déjà envoyé à {telegram_id} pour {tier}, skip")
        return

    # Supprimer message lien paiement
    msg_id = data["pending_msg"].pop(f"{telegram_id}:{tier}", None)
    save_data(data)

    if msg_id:
        try:
            await context.bot.delete_message(chat_id=telegram_id, message_id=msg_id)
        except Exception as e:
            print(f"⚠️ Suppression msg lien: {e}")

    tier_nom = TIERS[tier]["nom"]

    bvn_msg = await context.bot.send_message(
        chat_id=telegram_id,
        text="Ton accès est activé. Bienvenue de l'autre côté 🖤🔥"
    )
    # Sauvegarder le msg_id bienvenue pour le supprimer quand l'user clique
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

    # Marquer bienvenue envoyé + sauvegarder main_msg_id
    data = load_data()
    data["welcome_sent"][welcome_key] = True
    data["tarifs_msg"][f"main_{telegram_id}"] = main_msg.message_id
    save_data(data)

# ── /start ────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    print(f"▶ /start — telegram_id: {telegram_id}")

    # Supprimer l'ancien message /start si existant
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

    # ── PAGE PRIVATE ──
    if data_cb == "page_premium":
        keyboard = [
            [InlineKeyboardButton("🔓 Accéder au canal PRIVATE", url=f"{TIERS['premium']['lien']}?client_reference_id={telegram_id}")],
            [InlineKeyboardButton("💗 Voir le VIP", callback_data="page_vip")],
            [InlineKeyboardButton("👈🏽 Retour", callback_data="page_tarifs")],
        ]
        await query.delete_message()
        msg = await context.bot.send_photo(
            chat_id=telegram_id, photo=IMAGES["premium"],
            caption=(
                "🩷 PRIVATE — 9,99€/mois\n\nCanal KAYLA PRIVATE\n\n"
                "🩷 Photos & vidéos en lingerie\n🩷 Topless exclusifs\n"
                "🩷 Contenu inédit, jamais publié ailleurs\n🩷 Nouveau contenu chaque semaine\n"
                "🩷 Accès à mes archives privées\n❤️‍🔥 Un mois de plaisir rien que pour toi\n\n"
                "La plupart ne restent pas longtemps au PRIVATE. Une fois qu'ils découvrent le VIP… ils upgradent. 👀"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        data = load_data()
        data["tarifs_msg"][str(telegram_id)] = msg.message_id
        save_data(data)

    # ── PAGE VIP ──
    elif data_cb == "page_vip":
        keyboard = [
            [InlineKeyboardButton("🔓 Accéder au canal VIP", url=f"{TIERS['vip']['lien']}?client_reference_id={telegram_id}")],
            [InlineKeyboardButton("👈🏽 Retour", callback_data="page_tarifs")],
        ]
        await query.delete_message()
        msg = await context.bot.send_photo(
            chat_id=telegram_id, photo=IMAGES["vip"],
            caption=(
                "💗 VIP — 19,99€/mois\n\nCanal KAYLA VIP\n\n"
                "💗 Tout le contenu PRIVATE inclus\n💗 Full nude & vidéos exclusives\n"
                "💗 2x plus de contenu que le PRIVATE\n💗 Accès en avant-première à toutes mes nouveautés\n"
                "💗 Contenu réservé uniquement aux VIP\n❤️‍🔥 Une expérience unique & inoubliable\n\n"
                "Ceux qui ont le VIP ne regardent plus jamais en arrière. 🖤"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        data = load_data()
        data["tarifs_msg"][str(telegram_id)] = msg.message_id
        save_data(data)

    # ── PAGE TARIFS (retour) ──
    elif data_cb in ("page_tarifs", "page_tarifs_new"):
        keyboard = [
            [InlineKeyboardButton("🩷 PRIVATE — 9,99€/mois", callback_data="page_premium")],
            [InlineKeyboardButton("💗 VIP — 19,99€/mois", callback_data="page_vip")],
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

    # ── GÉNÉRER LIEN PAIEMENT — confirmation ──
    elif data_cb.startswith("gen_lien_paiement_"):
        tier = data_cb.replace("gen_lien_paiement_", "")
        sub_id, sub = get_sub_by_tier(telegram_id, tier)
        if not sub_id:
            await query.answer("❌ Abonnement introuvable.", show_alert=True)
            return

        in_canal = await is_user_in_canal(context.bot, telegram_id, tier)
        if in_canal:
            # Déjà dans le canal — supprimer message paiement + afficher espace abonné
            await query.delete_message()
            subs = get_subs_for_user(telegram_id)
            new_msg = await context.bot.send_message(
                chat_id=telegram_id,
                text=texte_espace_abo(subs),
                reply_markup=keyboard_espace_abo(subs)
            )
            data = load_data()
            data["tarifs_msg"][f"main_{telegram_id}"] = new_msg.message_id
            save_data(data)
            return

        count = get_invite_count(telegram_id, tier)
        if count >= 2:
            await query.delete_message()
            await context.bot.send_message(
                chat_id=telegram_id,
                text="⛔ Limite atteinte\n\nTu as déjà généré 2 liens d'invitation.\n\nContacte le support.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Contacter le support", url=SUPPORT_URL)]])
            )
            return

        # Supprimer message paiement + envoyer confirmation
        await query.delete_message()
        keyboard = [
            [InlineKeyboardButton("✅ Oui, générer le lien", callback_data=f"gen_lien_paiement_ok_{tier}")],
            [InlineKeyboardButton("❌ Annuler", callback_data=f"gen_lien_paiement_cancel_{tier}")],
        ]
        await context.bot.send_message(
            chat_id=telegram_id,
            text=(
                f"🔗 Générer un nouveau lien d'invitation\n\n"
                f"⚠️ Ce lien sera à usage unique et personnel.\n"
                f"Ne le partage jamais — ton abonnement serait résilié immédiatement sans remboursement.\n\n"
                f"Tu as {count}/2 liens générés. Confirmes-tu ?"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ── GÉNÉRER LIEN PAIEMENT — exécution ──
    elif data_cb.startswith("gen_lien_paiement_ok_"):
        tier = data_cb.replace("gen_lien_paiement_ok_", "")
        sub_id, sub = get_sub_by_tier(telegram_id, tier)
        if not sub_id:
            await query.delete_message()
            await context.bot.send_message(chat_id=telegram_id, text="❌ Abonnement introuvable.")
            return

        in_canal = await is_user_in_canal(context.bot, telegram_id, tier)
        if in_canal:
            tier_nom = TIERS[tier]["nom"]
            await query.delete_message()
            await context.bot.send_message(
                chat_id=telegram_id,
                text=(
                    f"✅ Tu es déjà dans le canal {tier_nom} !\n\n"
                    f"Si tu as un problème d'accès, contacte le support."
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💬 Contacter le support", url=SUPPORT_URL)
                ]])
            )
            return

        count = get_invite_count(telegram_id, tier)
        if count >= 2:
            await query.delete_message()
            await context.bot.send_message(
                chat_id=telegram_id,
                text="⛔ Limite atteinte\n\nContacte le support.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Support", url=SUPPORT_URL)]])
            )
            return

        try:
            invite = await context.bot.create_chat_invite_link(chat_id=CANAUX[tier], member_limit=1, creates_join_request=False)
            new_count = increment_invite_count(telegram_id, tier)
            dots = "🟢" * new_count + "⚪" * (2 - new_count)
            tier_emoji = TIERS[tier]["emoji"]
            tier_short = TIERS[tier]["short"]
            print(f"🔗 Lien généré (paiement) — telegram_id: {telegram_id}, tier: {tier}, {new_count}/2")

            kb = []
            if new_count < 2:
                kb.append([InlineKeyboardButton("🔗 Générer un nouveau lien", callback_data=f"gen_lien_paiement_{tier}")])
            else:
                kb.append([InlineKeyboardButton("⛔ Limite atteinte — 2/2", callback_data="noop")])

            await query.delete_message()
            new_msg = await context.bot.send_message(
                chat_id=telegram_id,
                text=(
                    f"✅ Paiement confirmé !\n\n"
                    f"Rejoint ton canal {tier_emoji} {tier_short} ici (lien à usage unique) :\n"
                    f"{invite.invite_link}\n\n"
                    f"⚠️ Ce lien est personnel. Ne le partage jamais — ton abonnement serait résilié immédiatement sans remboursement.\n\n"
                    f"{dots} {new_count}/2 liens générés"
                ),
                reply_markup=InlineKeyboardMarkup(kb)
            )
            data = load_data()
            data["pending_msg"][f"{telegram_id}:{tier}"] = new_msg.message_id
            data["pending_link"][f"{telegram_id}:{tier}"] = invite.invite_link
            data["welcome_sent"].pop(f"{telegram_id}:{tier}", None)
            save_data(data)
        except Exception as e:
            print(f"❌ Erreur génération lien paiement: {e}")
            await query.answer("❌ Erreur. Contacte le support.", show_alert=True)

    # ── ANNULER GÉNÉRATION LIEN PAIEMENT ──
    elif data_cb.startswith("gen_lien_paiement_cancel_"):
        tier = data_cb.replace("gen_lien_paiement_cancel_", "")
        count = get_invite_count(telegram_id, tier)
        tier_emoji = TIERS[tier]["emoji"]
        tier_short = TIERS[tier]["short"]
        # Retour simple — supprimer message confirmation et réafficher le message paiement original
        await query.delete_message()
        kb = []
        if count < 2:
            kb.append([InlineKeyboardButton("🔗 Générer un nouveau lien", callback_data=f"gen_lien_paiement_{tier}")])
        else:
            kb.append([InlineKeyboardButton("⛔ Limite atteinte — 2/2", callback_data="noop")])
        # Récupérer le lien stocké dans pending_link si disponible, sinon message neutre
        data = load_data()
        pending_link = data.get("pending_link", {}).get(f"{telegram_id}:{tier}", None)
        if pending_link:
            texte = (
                f"✅ Paiement confirmé !\n\n"
                f"Rejoint ton canal {tier_emoji} {tier_short} ici (lien à usage unique) :\n"
                f"{pending_link}\n\n"
                f"⚠️ Ce lien est personnel. Ne le partage jamais — ton abonnement serait résilié immédiatement sans remboursement.\n\n"
                f"🔗 {count}/2 liens générés"
            )
        else:
            texte = (
                f"✅ Paiement confirmé !\n\n"
                f"Utilise le bouton ci-dessous pour générer ton lien d'accès au canal {tier_emoji} {tier_short}.\n\n"
                f"🔗 {count}/2 liens générés"
            )
        new_msg = await context.bot.send_message(
            chat_id=telegram_id,
            text=texte,
            reply_markup=InlineKeyboardMarkup(kb)
        )
        data["pending_msg"][f"{telegram_id}:{tier}"] = new_msg.message_id
        save_data(data)

    # ── MENU GÉRER (tier précis) ──
    elif data_cb.startswith("menu_gerer_"):
        tier = data_cb.replace("menu_gerer_", "")
        tier_nom = TIERS[tier]["nom"]
        print(f"⚙️ Gestion {tier} — telegram_id: {telegram_id}")
        # Supprimer message bienvenue si présent
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

    # ── RETOUR ESPACE ABONNÉ ──
    elif data_cb == "menu_retour_abo":
        subs = get_subs_for_user(telegram_id)
        if not subs:
            await query.edit_message_text("❌ Tu n'as pas d'abonnement actif.")
            return
        await query.edit_message_text(
            texte_espace_abo(subs),
            reply_markup=keyboard_espace_abo(subs)
        )

    # ── ACCÉDER AU CANAL ──
    elif data_cb.startswith("menu_canal_"):
        tier = data_cb.replace("menu_canal_", "")
        sub_id, sub = get_sub_by_tier(telegram_id, tier)
        if not sub_id:
            await query.edit_message_text("❌ Tu n'as pas d'abonnement actif.")
            return

        count = get_invite_count(telegram_id, tier)
        print(f"🔗 Canal {tier} — telegram_id: {telegram_id}, liens: {count}/2")

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
                "⛔ Limite atteinte\n\nTu as déjà généré 2 liens d'invitation.\n\nContacte le support.",
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
            f"Si tu partages ton lien, ton abonnement sera résilié immédiatement sans remboursement.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Générer mon lien d'invitation", callback_data=f"gen_lien_{tier}")],
                [InlineKeyboardButton("👈🏽 Retour", callback_data=f"menu_gerer_{tier}")],
            ])
        )

    # ── GÉNÉRER LIEN (gestion) ──
    elif data_cb.startswith("gen_lien_") and not data_cb.startswith("gen_lien_paiement"):
        tier = data_cb.replace("gen_lien_", "")
        sub_id, sub = get_sub_by_tier(telegram_id, tier)
        if not sub_id:
            await query.edit_message_text("❌ Tu n'as pas d'abonnement actif.")
            return

        in_canal = await is_user_in_canal(context.bot, telegram_id, tier)
        if in_canal:
            await query.edit_message_text(
                "✅ Tu es déjà dans le canal !\n\nSi tu as un problème, contacte le support.",
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
            print(f"🔗 Lien généré (gestion) — telegram_id: {telegram_id}, tier: {tier}, {new_count}/2")

            kb = []
            if new_count < 2:
                kb.append([InlineKeyboardButton("🔗 Générer un autre lien", callback_data=f"gen_lien_{tier}")])
            else:
                kb.append([InlineKeyboardButton("⛔ Limite atteinte — 2/2", callback_data="noop")])
            kb.append([InlineKeyboardButton("👈🏽 Retour", callback_data=f"menu_gerer_{tier}")])

            await query.edit_message_text(
                f"✅ Ton lien d'invitation :\n\n"
                f"{invite.invite_link}\n\n"
                f"⚠️ Ce lien est personnel et à usage unique. Ne le partage jamais — ton abonnement serait résilié immédiatement sans remboursement.\n\n"
                f"{dots} {new_count}/2 liens générés",
                reply_markup=InlineKeyboardMarkup(kb)
            )
        except Exception as e:
            print(f"❌ Erreur génération lien gestion: {e}")
            await query.edit_message_text("❌ Une erreur s'est produite. Contacte le support.")

    # ── SUPPORT ──
    elif data_cb == "menu_support":
        print(f"💬 Support — telegram_id: {telegram_id}")
        await query.edit_message_text(
            "💬 Support\n\nUn problème ? Une question ?\n\nContacte-moi directement ici 👇\nJe réponds dans les plus brefs délais. 💕",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✉️ Contacter le support", url=SUPPORT_URL)],
                [InlineKeyboardButton("👈🏽 Retour", callback_data="menu_retour_abo")],
            ])
        )

    # ── UPGRADE ──
    elif data_cb == "menu_upgrade":
        print(f"⬆️ Upgrade — telegram_id: {telegram_id}")
        # Supprimer message bienvenue PRIVATE si présent
        data = load_data()
        bvn_id = data["tarifs_msg"].pop(f"bvn_{telegram_id}:premium", None)
        save_data(data)
        if bvn_id:
            try:
                await context.bot.delete_message(chat_id=telegram_id, message_id=bvn_id)
            except Exception:
                pass
        await query.edit_message_text(
            "⬆️ Upgrader vers le VIP\n\n"
            "Tu es actuellement en PRIVATE.\n"
            "Passe au VIP et accède à tout le contenu exclusif. 🔥\n\n"
            "💗 Full nude & vidéos longues\n💗 2x plus de contenu\n💗 Accès prioritaire aux nouveautés\n\n"
            "+10€/mois seulement",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔓 Passer au VIP maintenant", url=f"{TIERS['vip']['lien']}?client_reference_id={telegram_id}")],
                [InlineKeyboardButton("👈🏽 Retour", callback_data="menu_retour_abo")],
            ])
        )

    # ── RÉSILIER (tier précis) ──
    elif data_cb.startswith("menu_resilier_"):
        tier = data_cb.replace("menu_resilier_", "")
        sub_id, sub = get_sub_by_tier(telegram_id, tier)
        if not sub_id:
            await query.edit_message_text("❌ Tu n'as pas d'abonnement actif.")
            return

        tier_nom = TIERS[tier]["nom"]
        period_end = sub.get("period_end")
        if period_end:
            date_fin = datetime.fromtimestamp(period_end).strftime("%d/%m/%Y")
        else:
            date_fin = (datetime.now() + timedelta(days=30)).strftime("%d/%m/%Y")

        print(f"❌ Résiliation {tier} — telegram_id: {telegram_id}")
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

    # ── RÉSILIER NON ──
    elif data_cb.startswith("resilier_non_"):
        tier = data_cb.replace("resilier_non_", "")
        await query.edit_message_text(
            "✅ Bonne décision ! Ton abonnement reste actif. 💕",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔓 Retour à mon abonnement", callback_data=f"menu_gerer_{tier}")
            ]])
        )

    # ── RÉSILIER OUI → confirmation finale ──
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

    # ── RÉSILIER CONFIRMER ──
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

    # ── NOOP ──
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

        try:
            event = json.loads(body)
        except Exception:
            self.send_response(400); self.end_headers(); return

        self.send_response(200); self.end_headers()
        event_type = event.get("type")
        print(f"📨 Événement: {event_type}")

        if event_type == "checkout.session.completed":
            session = event["data"]["object"]
            telegram_id = session.get("client_reference_id")
            customer_id = session.get("customer")
            payment_link = session.get("payment_link")
            subscription_id = session.get("subscription")
            tier = PAYMENT_LINKS.get(payment_link)

            print(f"🔎 sub: {subscription_id}, customer: {customer_id}, telegram_id: {telegram_id}, tier: {tier}")

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
                # Fallback +30j
                if not period_end:
                    period_end = int((datetime.now() + timedelta(days=30)).timestamp())

                asyncio.run_coroutine_threadsafe(
                    ajouter_membre(int(telegram_id), tier, subscription_id, period_end),
                    webhook_loop
                )

        elif event_type == "invoice.payment_succeeded":
            obj = event["data"]["object"]
            subscription_id = obj.get("subscription")
            lines = obj.get("lines", {}).get("data", [])
            period_end = None
            for line in lines:
                pe = line.get("period", {}).get("end")
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
                    print(f"🔄 Renouvellement — sub: {subscription_id}, date: {datetime.fromtimestamp(period_end).strftime('%d/%m/%Y')}")

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
