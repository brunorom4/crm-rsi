// Edge Function: salvar-relatorio
// O CRM chama essa function de hora em hora (das 8h às 18h) pra guardar a
// versão mais recente do relatório. Ela só GUARDA -- não manda nada pro
// Telegram. Quem responde consultas é a "webhook-telegram", lendo o que
// ficou salvo aqui.
//
// Precisa da tabela relatorio_cache -- rode o arquivo
// criar_tabela_relatorio_cache.sql no SQL Editor do Supabase antes de usar.

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

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

    const supabase = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
    );

    const { error } = await supabase
      .from("relatorio_cache")
      .upsert({ id: 1, texto: texto, atualizado_em: new Date().toISOString() });

    if (error) {
      return new Response(JSON.stringify({ ok: false, erro: error.message }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      });
    }

    return new Response(JSON.stringify({ ok: true }), {
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
