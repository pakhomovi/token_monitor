import argparse
import logging
import sys
import time
from dataclasses import dataclass, replace

import httpx

from .active import with_active_liquidity
from .config import Settings, load_settings
from .dev import dev_report, enrich_launches
from .dexscreener import PairStats, pair_stats
from .models import Network, PoolSnapshot
from .onchain import ChainReader, Earned, parse_position_url
from .pipeline import Scored, screen
from .positions import PositionView, quote_reference
from .scoring import Params, RangeStress, min_fee_for, stress_model
from .screener import CodexError, CodexScreener, ScreenerQuery, is_stable, parse_pair
from .strategy import RangePosition, Shape


def _days(d: float) -> str:
    return f"{d:>5.1f}d" if d < 1e3 else "    ∞"


def _row(s: Scored) -> str:
    p, m = s.pool, s.market.metrics
    covers = (f"cover ¼ {_days(m['cover_25'])} ½ {_days(m['cover_50'])} bottom {_days(m['cover_100'])}  "
              if "cover_25" in m else "")
    eff = f" (eff ${p.effective_tvl_usd:,.0f})" if p.effective_tvl_usd is not None else ""
    return (f"{s.market.verdict.value.upper():<5} {p.network.value:<9} {p.symbol[:12]:<12} "
            f"{p.exchange[:16]:<16} fee {p.fee_bps:>5.1f}bp  liq ${p.liquidity_usd:>12,.0f}{eff}  "
            f"vol24 ${p.vol_24h_usd:>13,.0f}  pers {m.get('persistence', 0):>5.2f}  {covers}"
            f"{'; '.join(s.market.reasons)}  {p.address}")


def _params(st: Settings, args: argparse.Namespace) -> Params:
    overrides = {k: v for k, v in (("strategy", args.strategy), ("strategy_depth", args.depth),
                                   ("strategy_size", args.size)) if v is not None}
    return replace(st.params, **overrides)


def cmd_screen(args: argparse.Namespace) -> int:
    st = load_settings()
    query = ScreenerQuery(
        networks=st.networks,
        limit=args.limit or st.screen_limit,
        min_liquidity=st.params.min_liquidity,
        min_volume_24h=st.screen_min_volume_24h,
        # Сервер не знает версию пула: берём мягкий порог, точный применяет скоринг
        min_fee_bps=min(st.params.min_fee_bps, st.params.min_fee_bps_v4),
        rank_by=st.screen_rank_by,
    )
    try:
        with CodexScreener(st.codex_api_key or "") as screener:
            pools = screener.fetch(query)
            if not args.no_onchain:
                pools = with_active_liquidity(pools, screener)
    except CodexError as e:
        print(f"codex: {e}", file=sys.stderr)
        return 1

    results = screen(pools, _params(st, args), keep_skipped=args.all)
    for s in results:
        print(_row(s))
    print(f"\n{len(results)} of {len(pools)} pools", file=sys.stderr)
    return 0


def _strategy_table(pos: RangePosition, p: PoolSnapshot) -> str:
    lines = [f"\n{pos.shape.value} -{pos.depth:.0%} ${pos.size:,.0f} → {p.address[:12]}… "
             f"fee {p.fee_bps:.0f}bp  liq ${p.liquidity_usd:,.0f}  vol24 ${p.vol_24h_usd:,.0f}",
             "  scenario     price  in token  avg entry  pos PnL  fees/day  cover PnL"]
    scenarios = [("¼ range", 0.25), ("½ range", 0.5), ("bottom", 1.0), ("below", None)]
    for name, fraction in scenarios:
        drop = pos.depth * fraction if fraction else min(pos.depth + 0.1, 0.95)
        price = 1 - drop
        s = pos.state(price)
        pnl = s.value / pos.size - 1
        y = pos.daily_fee_yield(price, p.fee_bps, p.vol_24h_usd, p.fee_tvl_usd)
        cover = _days(-pnl / y) if y > 0 else "  out"
        lines.append(f"  {name:<10} {-drop:>+6.0%}  {s.token_share:>7.0%}  {s.avg_entry - 1:>+8.0%}  "
                     f"{pnl:>+7.1%}  {y:>7.2%}  {cover}")
    return "\n".join(lines)


def cmd_analyze(args: argparse.Namespace) -> int:
    st = load_settings()
    token = args.token.lower()
    query = ScreenerQuery(
        networks=(Network(args.network),) if args.network else st.networks,
        limit=50,
        min_liquidity=args.min_liq,
        rank_by="liquidity",
        exclude_scam=False,          # для конкретного токена хотим видеть и флаги Codex
        tokens=(token,),
    )
    try:
        with CodexScreener(st.codex_api_key or "") as screener:
            rows = screener.fetch_rows(query)
            parsed = [(r, p) for r in rows if (p := parse_pair(r, target=token)) and p.token == token]
            if not args.no_onchain:
                enriched = with_active_liquidity([p for _, p in parsed], screener)
                parsed = [(r, p) for (r, _), p in zip(parsed, enriched)]
    except CodexError as e:
        print(f"codex: {e}", file=sys.stderr)
        return 1

    if not parsed:
        print(f"no pools for {token} with liquidity >= ${args.min_liq:,.0f}", file=sys.stderr)
        return 1

    symbol = next((p.symbol for _, p in parsed if p.symbol), "?")
    print(f"{symbol} {token}: {len(parsed)} pools\n")

    # Риск-оценка Codex — предпросмотр, полноценный security (GMGN) будет на этапе 3
    risks = {(r.get("riskCoverage"), r.get("riskVerdict"), r.get("riskScore"), tuple(r.get("riskReasons") or ()))
             for r, _ in parsed}
    for coverage, verdict, score, reasons in sorted(risks, key=str):
        if coverage != "ANALYZED":
            # NEUTRAL без анализа ≠ «чисто»
            print(f"codex risk: not analyzed ({coverage or 'n/a'})")
            continue
        print(f"codex risk: {verdict or 'n/a'} score={score if score is not None else 'n/a'} "
              f"{', '.join(reasons) or '-'}")
    print()

    # Сортировка по объёму: пулы с «бумажной» ликвидностью без торгов (оценка по неликвидной
    # паре) иначе вытесняют реальные
    cfg = _params(st, args)
    results = sorted(screen([p for _, p in parsed], cfg, keep_skipped=True),
                     key=lambda s: (-s.pool.vol_24h_usd, -s.pool.liquidity_usd))
    shown, hidden = results[:args.top], results[args.top:]
    for s in shown:
        print(_row(s))
    if hidden:
        print(f"... +{len(hidden)} pools: liq ${sum(s.pool.liquidity_usd for s in hidden):,.0f}, "
              f"vol24 ${sum(s.pool.vol_24h_usd for s in hidden):,.0f} (--top {len(results)} to show)")
    total_liq = sum(s.pool.liquidity_usd for s in results)
    total_vol = sum(s.pool.vol_24h_usd for s in results)
    model = stress_model(cfg)
    if args.scenarios and isinstance(model, RangeStress):
        # PnL позиции зависит только от текущей цены: откат = подъём по той же таблице
        # Сценарии только для пулов, проходящих порог комиссии своей версии
        for s in [s for s in results if s.pool.fee_bps >= min_fee_for(s.pool, cfg)][:args.scenarios]:
            print(_strategy_table(model.pos, s.pool))
    print(f"\ntotal: liq ${total_liq:,.0f}, vol24 ${total_vol:,.0f}, "
          f"top pool makes {results[0].pool.vol_24h_usd / total_vol:.0%} of volume" if total_vol else "")
    return 0


def _num(x: float) -> str:
    # Мемы стоят 1e-6 ETH и дешевле: значащие цифры важнее фиксированных знаков
    return f"{x:,.2f}" if abs(x) >= 100 else f"{x:.4g}"


@dataclass(frozen=True, slots=True)
class PositionContext:
    """Всё, что известно о позиции помимо состояния с блокчейна."""
    q_usd: float | None = None                    # цена quote-актива в $
    stats: PairStats | None = None                # DexScreener
    minted: int | None = None                     # unix-время входа
    usd_at_entry: float | None = None             # цена токена на входе (Codex)
    earned: Earned | None = None                  # собранные комиссии
    collect_usd: tuple[float | None, ...] = ()    # цена токена на момент каждого клейма


def _position_report(v: PositionView, c: PositionContext) -> str:
    p, t, q = v.pos, v.target, v.quote
    lo, hi = v.range
    where = ("выше диапазона" if v.from_top > 0 else "ниже диапазона" if not p.in_range else "в диапазоне")
    q_usd = c.q_usd

    def money(amount_q: float) -> str:
        return f"{_num(amount_q)} {q.symbol}" + (f" (${amount_q * q_usd:,.2f})" if q_usd else "")

    entry = v.value_at_top
    ht, hq = v.holdings
    ft, fq = v.fees
    lines = [
        f"#{p.token_id} {p.network.value} v{p.version} {t.symbol}/{q.symbol} {p.fee_bps / 100:g}%  pool {p.pool[:12]}…",
        f"  диапазон  {_num(lo)} … {_num(hi)} {q.symbol} (глубина -{v.depth:.0%})",
        f"  цена      {_num(v.price)} {q.symbol}: {v.from_top:+.1%} от верхней границы, {where}",
        f"  состав    {_num(ht)} {t.symbol} + {_num(hq)} {q.symbol} = {money(v.value)}, в токене {v.token_share:.0%}",
        f"  PnL       {v.pnl_from_top:+.1%} без комиссий (вход на верхней границе: {money(entry)})",
        f"  несобр.   {_num(ft)} {t.symbol} + {_num(fq)} {q.symbol} = {money(v.fees_value)}",
    ]
    fees_total = v.fees_value
    if c.earned and (c.earned.collected0 or c.earned.collected1):
        ct, cq = v.split_raw(c.earned.collected0, c.earned.collected1)
        # Токенную часть клеймов оцениваем по цене на момент клейма (её и продавали), иначе — по текущей
        if c.earned.collects and q_usd and all(c.collect_usd):
            collected_q = sum(v.split_raw(x.amount0, x.amount1)[0] * u / q_usd + v.split_raw(x.amount0, x.amount1)[1]
                              for x, u in zip(c.earned.collects, c.collect_usd))
            basis = "по цене на момент клейма"
        else:
            collected_q, basis = ct * v.price + cq, "по текущей цене"
        n = len(c.earned.collects)
        lines.append(f"  собрано   {_num(ct)} {t.symbol} + {_num(cq)} {q.symbol} = {money(collected_q)}"
                     + (f", клеймов: {n}" if n else "") + f" ({basis})"
                     + ("" if c.earned.exact else " — неточно: менялась ликвидность или нативный ETH"))
        fees_total += collected_q
    if entry:
        # Считаем в quote (ETH/BNB): колебания quote не смазывают результат стратегии; в $ — по текущему курсу
        lines.append(f"  итог      комиссии {fees_total / entry:+.1%} + PnL {v.pnl_from_top:+.1%} = "
                     f"{(v.value + fees_total) / entry - 1:+.1%} ({money(v.value + fees_total - entry)})"
                     + (f" — vs HODL в {q.symbol}, $ по текущему курсу" if not is_stable(q.symbol) else ""))
    if c.minted:
        days = (time.time() - c.minted) / 86400
        line = f"  вход      {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(c.minted))} ({days * 24:.0f}ч назад)"
        if c.usd_at_entry and q_usd:
            line += f", токен тогда ${c.usd_at_entry:.4g}, верх диапазона {hi * q_usd / c.usd_at_entry - 1:+.0%} от неё"
        lines.append(line)
        if entry and days > 0:
            lines.append(f"  факт      комиссии {fees_total / entry / days:.1%}/день от входа с момента минта")
    s = c.stats
    if s:
        lines.append(f"  объём     $/ч: 1ч {s.vol_1h:,.0f} · 6ч {s.vol_6h / 6:,.0f} · 24ч {s.vol_24h / 24:,.0f}  "
                     f"pers(6ч) {s.persistence:.2f}  покупки/продажи 24ч {s.buys_24h}/{s.sells_24h}")
    share = p.fee_share
    if p.in_range and s and s.vol_6h:
        # Оценка по темпу последних 6ч: он ближе к текущему, чем среднее за сутки
        per_day = p.fee_bps / 1e4 * s.vol_6h * 4 * share
        lines.append(f"  доля      {share:.2%} активной ликвидности → ~${per_day:,.2f}/день по темпу 6ч"
                     + (f" ({per_day / (entry * q_usd):.1%} от входа)" if q_usd and entry else ""))
    else:
        lines.append(f"  доля      {share:.2%} активной ликвидности" + ("" if p.in_range else " (вне диапазона: 0)"))
    return "\n".join(lines)


def cmd_position(args: argparse.Namespace) -> int:
    st = load_settings()
    refs = [parse_position_url(u) for u in args.urls]
    views: list[PositionView] = []
    minted: dict[tuple[Network, int], int] = {}
    earned: dict[tuple[Network, int], Earned] = {}
    collect_ts: dict[tuple[Network, int], list[int]] = {}
    for network in dict.fromkeys(n for n, _, _ in refs):
        with ChainReader(network) as reader:
            for n, ver, i in refs:
                if n != network:
                    continue
                view = PositionView(reader.position(ver, i))
                views.append(view)
                if args.no_entry:
                    continue
                try:
                    block, minted[(n, i)] = reader.minted_at(ver, i)
                    earned[(n, i)] = e = reader.earned(view.pos, block)
                    collect_ts[(n, i)] = [reader.block_timestamp(x.block) for x in e.collects]
                except RuntimeError as e:
                    print(f"#{i}: история позиции недоступна ({e})", file=sys.stderr)

    # Цена токена в quote — с блокчейна; в $ нужна только цена quote (стейбл = $1, ETH/BNB — эталонная пара)
    # и исторические цены токена (вход, клеймы): всё одним запросом к Codex
    q_usd: dict[str, float | None] = {v.quote.symbol: 1.0 for v in views if is_stable(v.quote.symbol)}
    refs_q = list(dict.fromkeys(r for v in views if (r := quote_reference(v.quote.symbol))))
    keyed = [((v.pos.network, v.pos.token_id), v) for v in views]
    hist = [(k, v, ts) for k, v in keyed for ts in ([minted[k]] if k in minted else []) + collect_ts.get(k, [])]
    hist_usd: dict[tuple[tuple[Network, int], int], float | None] = {}
    if st.codex_api_key:
        try:
            with CodexScreener(st.codex_api_key) as codex:
                got = codex.prices([(a, n, None) for a, n in refs_q]
                                   + [(v.target.address, v.pos.network, ts) for _, v, ts in hist])
            ref_usd = dict(zip(refs_q, got))
            q_usd |= {v.quote.symbol: ref_usd.get(r) for v in views if (r := quote_reference(v.quote.symbol))}
            hist_usd = {(k, ts): price for (k, _, ts), price in zip(hist, got[len(refs_q):])}
        except CodexError as e:
            print(f"codex: {e} (без цен ETH/BNB и исторических цен)", file=sys.stderr)
    try:
        stats = pair_stats([(v.pos.pool, v.pos.network) for v in views])
    except httpx.HTTPError as e:
        print(f"dexscreener: {e} (без объёмов)", file=sys.stderr)
        stats = {}

    for k, v in keyed:
        ctx = PositionContext(
            q_usd=q_usd.get(v.quote.symbol), stats=stats.get(v.pos.pool.lower()), minted=minted.get(k),
            usd_at_entry=hist_usd.get((k, minted[k])) if k in minted else None, earned=earned.get(k),
            collect_usd=tuple(hist_usd.get((k, ts)) for ts in collect_ts.get(k, [])),
        )
        print(_position_report(v, ctx), end="\n\n")
    return 0


def cmd_dev(args: argparse.Namespace) -> int:
    st = load_settings()
    token = args.token.lower()
    networks = (Network(args.network),) if args.network else st.networks
    try:
        with CodexScreener(st.codex_api_key or "") as codex:
            found = [t for t in codex.token_stats([(token, n) for n in networks]) if t["address"] == token]
            if not found or not found[0]["created_at"]:
                print(f"{token}: токен не найден в Codex ({', '.join(n.value for n in networks)})", file=sys.stderr)
                return 1
            info = found[0]
            network: Network = info["network"]
            with ChainReader(network) as reader:
                head = int(reader._rpc("eth_blockNumber", []), 16)
                span = head - 100_000
                bps = 100_000 / max(reader.block_timestamp(head) - reader.block_timestamp(span), 1)
                report = dev_report(reader, token, info["created_at"],
                                    launches_lookback_blocks=int(args.days * 86400 * bps),
                                    outflow_blocks=int(args.days * 86400 * bps))
            if report.launches:
                report = enrich_launches(report, codex.token_stats([(t.address, network) for t in report.launches]))
    except (CodexError, RuntimeError) as e:
        hint = (" — этот RPC не отдаёт логи; задайте другой через RPC_<СЕТЬ> "
                "(например RPC_BSC=https://bsc-rpc.publicnode.com)" if "limit exceeded" in str(e) else "")
        print(f"dev: {e}{hint}", file=sys.stderr)
        return 1

    c = report.creation
    print(f"{info['symbol']} {token} ({network.value})")
    print(f"  запускатор  {c.launcher}" + (f" через {c.factory} (лаунчпад)" if c.factory else " (прямой деплой)"))
    print(f"  создание    tx {c.tx_hash}, блок {c.block}")
    print(f"  на старте   купил {report.bought_share:.2%} предложения, держит сейчас {report.holds_share:.2%}")
    if report.outflows:
        pools = sum(o.amount for o in report.outflows if o.is_contract) / report.total_supply
        print(f"  вывел       в контракты (пулы/роутеры) {pools:.2%}, на кошельки {report.to_wallets_share:.2%}"
              f" ({sum(o.to_wallet for o in report.outflows)} адресов)"
              + (f", сжёг {report.burned_share:.2%}" if report.burned_share else ""))
        for o in [o for o in report.outflows if o.to_wallet][:5]:
            print(f"              → {o.to} {o.amount / report.total_supply:.2%}")
    if report.launches:
        alive = [t for t in report.launches if t.alive]
        migrated = sum(bool(t.migrated) for t in report.launches)
        print(f"  запуски     {len(report.launches)} за {args.days:g} дн.: мигрировали {migrated}, "
              f"живых (mcap ≥ $50k) {len(alive)}")
        for t in sorted(report.launches, key=lambda t: -t.market_cap)[:args.top]:
            print(f"              {t.symbol[:14]:14} mcap ${t.market_cap:>12,.0f}  holders {t.holders:>6}  {t.address}")
    elif c.launch_event:
        print(f"  запуски     других запусков за {args.days:g} дн. не найдено")
    else:
        print("  запуски     событие лаунчпада не найдено — историю запусков смотреть в эксплорере")
    flags = report.flags()
    print("  флаги       " + ("; ".join(flags) if flags else "нет"))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="token_monitor")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    strat = argparse.ArgumentParser(add_help=False)
    strat.add_argument("--strategy", choices=[*(s.value for s in Shape), "fullrange"],
                       help="модель входа (override PARAM_STRATEGY)")
    strat.add_argument("--depth", type=float, help="глубина диапазона: 0.7 = до -70%%")
    strat.add_argument("--size", type=float, help="размер позиции, $")
    strat.add_argument("--no-onchain", action="store_true",
                       help="не читать активную ликвидность с блокчейна (оценка по TVL, завышает комиссии)")
    sc = sub.add_parser("screen", parents=[strat], help="top-N пулов из Codex → FAST/SLOW")
    sc.add_argument("--limit", type=int, help="override SCREEN_LIMIT")
    sc.add_argument("--all", action="store_true", help="показывать и SKIP")
    sc.set_defaults(func=cmd_screen)
    an = sub.add_parser("analyze", parents=[strat], help="все пулы токена → FAST/SLOW/SKIP + риск Codex")
    an.add_argument("token", help="адрес токена")
    an.add_argument("--network", choices=[n.value for n in Network])
    an.add_argument("--min-liq", type=float, default=1_000, help="нижняя граница ликвидности пула, $")
    an.add_argument("--top", type=int, default=10, help="сколько пулов показать (по объёму)")
    an.add_argument("--scenarios", type=int, default=0, metavar="N",
                    help="подробные сценарии для N самых торгуемых пулов")
    an.set_defaults(func=cmd_analyze)

    po = sub.add_parser("position", help="LP-позиции Uniswap по ссылкам app.uniswap.org/positions/...")
    po.add_argument("urls", nargs="+")
    po.add_argument("--no-entry", action="store_true", help="без истории: даты входа и собранных комиссий (быстрее)")
    po.set_defaults(func=cmd_position)

    dv = sub.add_parser("dev", help="проверка дева: запускатор, покупка на старте, продажи, другие запуски")
    dv.add_argument("token", help="адрес токена")
    dv.add_argument("--network", choices=[n.value for n in Network])
    dv.add_argument("--days", type=float, default=7, help="глубина поиска других запусков и выводов, дней")
    dv.add_argument("--top", type=int, default=10, help="сколько запусков показать")
    dv.set_defaults(func=cmd_dev)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
