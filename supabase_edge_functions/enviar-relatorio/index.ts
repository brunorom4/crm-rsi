// Edge Function: enviar-relatorio
// Recebe o texto do relatório do CRM e manda pro Telegram, de dentro dos
// servidores do Supabase (por isso não esbarra no bloqueio de rede da empresa).
//
// Como configurar:
// 1. No painel do Supabase, vá em "Edge Functions" -> "Deploy a new function"
// 2. Nome da function: enviar-relatorio
// 3. Cole esse código no editor
// 4. Antes de fazer o deploy, vá em "Edge Functions" -> "Secrets" (ou
//    Settings -> Edge Functions) e cadastre duas variáveis:
//      TELEGRAM_BOT_TOKEN = o token do seu bot (o que começa com números:AA...)
//      TELEGRAM_CHAT_ID   = o chat_id de destino (descubra com a outra function)
// 5. Faça o deploy

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";

serve(async (req) => {
  if (req.method !== "POST") {
    return new Response(JSON.stringify({ ok: false, erro: "Use POST" }), {
      status: 405,
      headers: { "Content-Type": "application/json" },
    });
  }

  try {
    const corpo = await req.json();
    const texto = corpo.texto;

    if (!texto) {
      return new Response(JSON.stringify({ ok: false, erro: "Campo 'texto' não veio no corpo da requisição" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    const token = Deno.env.get("TELEGRAM_BOT_TOKEN");
    const chatId = Deno.env.get("TELEGRAM_CHAT_ID");

    if (!token || !chatId) {
      return new Response(
        JSON.stringify({ ok: false, erro: "TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID não configurados nas Secrets do Supabase" }),
        { status: 500, headers: { "Content-Type": "application/json" } }
      );
    }

    const respostaTelegram = await fetch("https://api.telegram.org/bot" + token + "/sendMessage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text: texto,
        parse_mode: "Markdown",
      }),
    });

    const resultado = await respostaTelegram.json();

    return new Response(JSON.stringify(resultado), {
      status: respostaTelegram.ok ? 200 : 400,
      headers: { "Content-Type": "application/json" },
    });
  } catch (erro) {
    return new Response(JSON.stringify({ ok: false, erro: String(erro) }), {
      status: 500,
      headers: { "Content-Type": "application/json" },
    });
  }
});
