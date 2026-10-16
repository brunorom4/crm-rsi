// Edge Function: descobrir-chat-id
// Ajuda a descobrir o chat_id do Telegram: manda uma mensagem pro bot pelo
// app do Telegram, chama essa function, e ela te devolve os chats recentes.
//
// Como configurar: mesmo processo da "enviar-relatorio" (crie a function com
// esse nome, cole esse código, e ela usa a mesma Secret TELEGRAM_BOT_TOKEN).

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";

serve(async (req) => {
  try {
    const token = Deno.env.get("TELEGRAM_BOT_TOKEN");
    if (!token) {
      return new Response(JSON.stringify({ ok: false, erro: "TELEGRAM_BOT_TOKEN não configurado nas Secrets do Supabase" }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      });
    }

    const respostaTelegram = await fetch("https://api.telegram.org/bot" + token + "/getUpdates");
    const corpo = await respostaTelegram.json();

    if (!corpo.ok) {
      return new Response(JSON.stringify({ ok: false, erro: corpo.description || "erro desconhecido do Telegram" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    const encontrados: Record<string, string> = {};
    for (const update of corpo.result || []) {
      const mensagem = update.message || update.channel_post;
      const chat = mensagem?.chat;
      if (chat) {
        const nome = chat.title || [chat.first_name, chat.last_name].filter(Boolean).join(" ") || chat.username || "(sem nome)";
        encontrados[chat.id] = nome + " — tipo: " + chat.type;
      }
    }

    return new Response(JSON.stringify({ ok: true, chats: encontrados }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  } catch (erro) {
    return new Response(JSON.stringify({ ok: false, erro: String(erro) }), {
      status: 500,
      headers: { "Content-Type": "application/json" },
    });
  }
});
