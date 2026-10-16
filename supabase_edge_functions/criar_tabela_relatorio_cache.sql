-- Rode isso uma vez, no SQL Editor do painel do Supabase.
-- Cria a tabela onde o CRM guarda a versão mais recente do relatório
-- (atualizada de hora em hora, das 8h às 18h), pra responder consultas
-- do Telegram a qualquer momento sem precisar do CRM estar "acordado" na hora.

create table if not exists relatorio_cache (
    id integer primary key default 1,
    texto text not null,
    atualizado_em timestamptz not null default now(),
    constraint linha_unica check (id = 1)
);

-- Deixa a tabela só com a MAIS RECENTE (sempre sobrescreve a linha 1, nunca acumula histórico)
insert into relatorio_cache (id, texto) values (1, 'Ainda sem relatório salvo.')
on conflict (id) do nothing;
