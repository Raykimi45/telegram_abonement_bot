export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/telegram") return handleTelegram(request, env);
    if (url.pathname === "/stripe") return handleStripe(request, env);

    return new Response("Fanselli Bot actif ✅");
  }
};

async function handleTelegram(request, env) {
  const body = await request.json();
  const message = body.message;
  const callbackQuery = body.callback_query;

  if (message?.text?.startsWith("/start")) {
    const telegramId = message.from.id;
    const username = message.from.username || "inconnu";

    await env.FANSELLI_KV.put(`lead:${telegramId}`, JSON.stringify({
      telegramId,
      username,
      status: "pending",
      createdAt: new Date().toISOString()
    }));

    await sendTelegram(env, telegramId, "🔥 Choisis ton accès Premium :", {
      inline_keyboard: [
        [{ text: "⭐ Silver — 9.99€/mois", callback_data: "tier_silver" }],
        [{ text: "💎 Gold — 19.99€/mois", callback_data: "tier_gold" }],
      ]
    });
  }

  if (callbackQuery?.data?.startsWith("tier_")) {
    const telegramId = callbackQuery.from.id;
    const tier = callbackQuery.data;
    const session = await createStripeSession(env, telegramId, tier);

    await sendTelegram(env, telegramId, "👇 Finalise ton paiement ici :", {
      inline_keyboard: [[
        { text: "💳 Payer maintenant", url: session.url }
      ]]
    });
  }

  if (message?.text === "/subscription") {
    const telegramId = message.from.id;
    const data = await env.FANSELLI_KV.get(`lead:${telegramId}`, "json");

    if (!data || data.status !== "active") {
      await sendTelegram(env, telegramId, "❌ Tu n'as pas d'abonnement actif.");
      return new Response("ok");
    }

    const portal = await createStripePortal(env, data.stripeCustomerId);
    await sendTelegram(env, telegramId, "⚙️ Gère ton abonnement ici :", {
      inline_keyboard: [[
        { text: "🔧 Gérer / Annuler", url: portal.url }
      ]]
    });
  }

  return new Response("ok");
}

async function handleStripe(request, env) {
  const body = await request.text();
  const sig = request.headers.get("stripe-signature");

  let event;
  try {
    event = await verifyStripeWebhook(body, sig, env.STRIPE_WEBHOOK_SECRET);
  } catch (e) {
    return new Response("Webhook invalide", { status: 400 });
  }

  const telegramId = event.data.object.metadata?.telegramId;

  switch (event.type) {

    case "checkout.session.completed": {
      const stripeCustomerId = event.data.object.customer;
      const existing = await env.FANSELLI_KV.get(`lead:${telegramId}`, "json");

      await env.FANSELLI_KV.put(`lead:${telegramId}`, JSON.stringify({
        ...existing,
        status: "active",
        stripeCustomerId,
        subscriptionId: event.data.object.subscription
      }));

      const invite = await createInviteLink(env);

      await sendTelegram(env, telegramId,
        "✅ Paiement confirmé ! Bienvenue dans le canal Premium 🔥\n\nCe lien expire dans 24h :",
        {
          inline_keyboard: [[
            { text: "🚀 Rejoindre le canal", url: invite }
          ]]
        }
      );
      break;
    }

    case "customer.subscription.deleted": {
      const lead = await env.FANSELLI_KV.get(`lead:${telegramId}`, "json");

      await kickFromChannel(env, lead.telegramId);

      await env.FANSELLI_KV.put(`lead:${telegramId}`, JSON.stringify({
        ...lead,
        status: "canceled"
      }));

      await sendTelegram(env, lead.telegramId,
        "😔 Ton accès a expiré.\nReviens quand tu veux avec /start 👋"
      );
      break;
    }
  }

  return new Response("ok");
}

// ── HELPERS ──────────────────────────────

async function sendTelegram(env, chatId, text, inline_keyboard = null) {
  const body = { chat_id: chatId, text };
  if (inline_keyboard) body.reply_markup = { inline_keyboard };

  await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
}

async function createStripeSession(env, telegramId, tier) {
  const priceId = tier === "tier_gold" ? env.PRICE_ID_GOLD : env.PRICE_ID_SILVER;

  const res = await fetch("https://api.stripe.com/v1/checkout/sessions", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.STRIPE_SECRET_KEY}`,
      "Content-Type": "application/x-www-form-urlencoded"
    },
    body: new URLSearchParams({
      mode: "subscription",
      "line_items[0][price]": priceId,
      "line_items[0][quantity]": "1",
      success_url: `https://t.me/${env.BOT_USERNAME}?start=success`,
      cancel_url: `https://t.me/${env.BOT_USERNAME}?start=cancel`,
      "metadata[telegramId]": String(telegramId),
      "metadata[tier]": tier
    })
  });
  return res.json();
}

async function createStripePortal(env, customerId) {
  const res = await fetch("https://api.stripe.com/v1/billing_portal/sessions", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.STRIPE_SECRET_KEY}`,
      "Content-Type": "application/x-www-form-urlencoded"
    },
    body: new URLSearchParams({
      customer: customerId,
      return_url: `https://t.me/${env.BOT_USERNAME}`
    })
  });
  return res.json();
}

async function createInviteLink(env) {
  const res = await fetch(
    `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/createChatInviteLink`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: env.CHANNEL_ID,
        member_limit: 1,
        expire_date: Math.floor(Date.now() / 1000) + 86400
      })
    }
  );
  const data = await res.json();
  return data.result.invite_link;
}

async function kickFromChannel(env, telegramId) {
  await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/banChatMember`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: env.CHANNEL_ID, user_id: telegramId })
  });

  await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/unbanChatMember`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: env.CHANNEL_ID, user_id: telegramId })
  });
}

async function verifyStripeWebhook(payload, sig, secret) {
  const encoder = new TextEncoder();
  const parts = sig.split(",");
  const timestamp = parts.find(p => p.startsWith("t=")).split("=")[1];
  const v1 = parts.find(p => p.startsWith("v1=")).split("=")[1];

  const signedPayload = `${timestamp}.${payload}`;
  const key = await crypto.subtle.importKey(
    "raw", encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false, ["sign"]
  );
  const signature = await crypto.subtle.sign("HMAC", key, encoder.encode(signedPayload));
  const computed = Array.from(new Uint8Array(signature))
    .map(b => b.toString(16).padStart(2, "0")).join("");

  if (computed !== v1) throw new Error("Signature invalide");
  return JSON.parse(payload);
}
