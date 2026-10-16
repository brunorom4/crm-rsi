// Edge Function: webhook-telegram
// O Telegram chama essa function SOZINHO, direto, toda vez que alguém manda
// uma mensagem pro bot (não é o CRM nem o Supabase que ficam "escutando" --
// é o Telegram que avisa, assim que a pessoa manda a mensagem).
//
// Só responde quando a mensagem for exatamente "/status" -- qualquer outra
// coisa, ela ignora.
//
// IMPORTANTE: essa function precisa ficar com a verificação de JWT do
// Supabase DESLIGADA (o Telegram não sabe se autenticar com o Supabase) --
// no painel, depois de criar a function, vá em "Details" e desmarque
// "Enforce JWT Verification" só pra essa function específica.
//
// Depois de fazer o deploy, registre o webhook no Telegram (uma vez só --
// pode ser feito colando essa URL no navegador do CELULAR, já que o
// Telegram não é bloqueado lá):
//
// https://api.telegram.org/bot<SEU_TOKEN>/setWebhook?url=https://<SEU_PROJETO>.supabase.co/functions/v1/webhook-telegram

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

serve(async (req) => {
  try {
    const atualizacao = await req.json();
    const mensagem = atualizacao.message;
    const texto = (mensagem?.text || "").trim().toLowerCase();
    const chatId = mensagem?.chat?.id;

    // Ignora silenciosamente qualquer coisa que não seja o comando esperado
    if (texto !== "/status" || !chatId) {
      return new Response("ok", { status: 200 });
    }

    const supabase = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
    );

    const { data, error } = await supabase
      .from("relatorio_cache")
      .select("texto, atualizado_em")
      .eq("id", 1)
      .single();

    const textoParaEnviar = error || !data
      ? "Ainda não tem nenhum relatório salvo. Tenta de novo daqui a pouco."
      : data.texto;

    const token = Deno.env.get("TELEGRAM_BOT_TOKEN");
    await fetch("https://api.telegram.org/bot" + token + "/sendMessage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text: textoParaEnviar,
        parse_mode: "Markdown",
      }),
    });

    return new Response("ok", { status: 200 });
  } catch (erro) {
    console.error(erro);
    // Sempre responde 200 pro Telegram, mesmo com erro interno -- senão o
    // Telegram fica tentando reenviar a mesma mensagem repetidamente.
    return new Response("ok", { status: 200 });
  }
});
