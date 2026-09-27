import argparse
import logging
import sys
import time
from dataclasses import replace

from .config import Settings, load_settings
from .models import Network, PoolSnapshot
from .pipeline import Scored, screen
from .scoring import Params, RangeStress, min_fee_for, stress_model
from .screener import CodexError, CodexScreener, ScreenerQuery, is_stable, parse_pair
from .onchain import ChainReader, parse_position_url
from .positions import PositionView, quote_reference
from .strategy import RangePosition, Shape


def _days(d: float) -> str:
    return f"{d:>5.1f}d" if d < 1e3 else "    ∞"


def _row(s: Scored) -> str:
    p, m = s.pool, s.market.metrics
    covers = (f"cover ¼ {_days(m['cover_25'])} ½ {_days(m['cover_50'])} bottom {_days(m['cover_100'])}  "
              if "cover_25" in m else "")
    return (f"{s.market.verdict.value.upper():<5} {p.network.value:<9} {p.symbol[:12]:<12} "
            f"{p.exchange[:16]:<16} fee {p.fee_bps:>5.1f}bp  liq ${p.liquidity_usd:>12,.0f}  "
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
        y = pos.daily_fee_yield(price, p.fee_bps, p.vol_24h_usd, p.liquidity_usd)
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
    except CodexError as e:
        print(f"codex: {e}", file=sys.stderr)
        return 1

    parsed = [(r, p) for r in rows if (p := parse_pair(r, target=token)) and p.token == token]
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


def _position_report(v: PositionView, q_usd: float | None, vol24: float | None,
                     minted: int | None = None, usd_at_entry: float | None = None) -> str:
    p, t, q = v.pos, v.target, v.quote
    lo, hi = v.range
    where = ("выше диапазона" if v.from_top > 0 else "ниже диапазона" if not p.in_range else "в диапазоне")

    def money(amount_q: float) -> str:
        return f"{_num(amount_q)} {q.symbol}" + (f" (${amount_q * q_usd:,.2f})" if q_usd else "")

    ht, hq = v.holdings
    ft, fq = v.fees
    lines = [
        f"#{p.token_id} {p.network.value} v{p.version} {t.symbol}/{q.symbol} {p.fee_bps / 100:g}%  pool {p.pool[:12]}…",
        f"  диапазон  {_num(lo)} … {_num(hi)} {q.symbol} (глубина -{v.depth:.0%})",
        f"  цена      {_num(v.price)} {q.symbol}: {v.from_top:+.1%} от верхней границы, {where}",
        f"  состав    {_num(ht)} {t.symbol} + {_num(hq)} {q.symbol} = {money(v.value)}, в токене {v.token_share:.0%}",
        f"  PnL       {v.pnl_from_top:+.1%} без комиссий (если вход был на верхней границе: {money(v.value_at_top)})",
        f"  комиссии  {_num(ft)} {t.symbol} + {_num(fq)} {q.symbol} = {money(v.fees_value)} "
        f"({v.fees_value / v.value_at_top:.1%} от входа)" if v.value_at_top else "",
    ]
    if minted:
        days = (time.time() - minted) / 86400
        entry = f"  вход      {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(minted))} ({days * 24:.0f}ч назад)"
        if usd_at_entry and q_usd:
            # Совпадает ли верх диапазона с ценой на момент входа (quote в $ берём текущий)
            entry += f", токен тогда ${usd_at_entry:.4g}, верх диапазона {hi * q_usd / usd_at_entry - 1:+.0%} от неё"
        lines.append(entry)
        if v.value_at_top and days > 0:
            lines.append(f"  факт      комиссии {v.fees_value / v.value_at_top / days:.1%}/день от входа "
                         f"с момента минта (только несобранные: если собирали — больше)")
    share = p.fee_share
    if p.in_range and vol24:
        per_day = p.fee_bps / 1e4 * vol24 * share
        lines.append(f"  доля      {share:.2%} активной ликвидности → ~${per_day:,.2f}/день при vol24 ${vol24:,.0f}"
                     + (f" ({per_day / (v.value_at_top * q_usd):.1%} от входа)" if q_usd and v.value_at_top else ""))
    else:
        lines.append(f"  доля      {share:.2%} активной ликвидности" + ("" if p.in_range else " (вне диапазона: 0)"))
    return "\n".join(l for l in lines if l)


def cmd_position(args: argparse.Namespace) -> int:
    st = load_settings()
    refs = [parse_position_url(u) for u in args.urls]
    views: list[PositionView] = []
    minted: dict[tuple[Network, int], int] = {}
    for network in dict.fromkeys(n for n, _, _ in refs):
        with ChainReader(network) as reader:
            for n, ver, i in refs:
                if n != network:
                    continue
                views.append(PositionView(reader.position(ver, i)))
                if not args.no_entry:
                    try:
                        minted[(n, i)] = reader.minted_at(ver, i)[1]
                    except RuntimeError as e:
                        print(f"#{i}: дата входа недоступна ({e})", file=sys.stderr)

    # Цена токена в quote уже есть с блокчейна (slot0); в $ нужна только цена quote: стейбл = $1,
    # ETH/BNB — по одной эталонной паре. Всё остальное из Codex — ровно 2 запроса на любое число позиций
    q_usd: dict[str, float | None] = {v.quote.symbol: 1.0 for v in views if is_stable(v.quote.symbol)}
    refs_q = list(dict.fromkeys(r for v in views if (r := quote_reference(v.quote.symbol))))
    entries = [(v, ts) for v in views if (ts := minted.get((v.pos.network, v.pos.token_id)))]
    entry_usd: dict[tuple[Network, int], float | None] = {}
    vols: dict[str, float] = {}
    if st.codex_api_key:
        try:
            with CodexScreener(st.codex_api_key) as codex:
                got = codex.prices([(a, n, None) for a, n in refs_q]
                                   + [(v.target.address, v.pos.network, ts) for v, ts in entries])
                ref_usd = dict(zip(refs_q, got))
                q_usd |= {v.quote.symbol: ref_usd.get(quote_reference(v.quote.symbol)) for v in views
                          if quote_reference(v.quote.symbol)}
                entry_usd = {(v.pos.network, v.pos.token_id): p for (v, _), p in zip(entries, got[len(refs_q):])}
                for row in codex.pairs(list({(v.pos.pool, v.pos.network) for v in views})):
                    vols[row["pair"]["address"].lower()] = float(row.get("volumeUSD24") or 0)
        except CodexError as e:
            print(f"codex: {e} (без объёма и цен ETH/BNB)", file=sys.stderr)

    for v in views:
        key = (v.pos.network, v.pos.token_id)
        print(_position_report(v, q_usd.get(v.quote.symbol), vols.get(v.pos.pool.lower()),
                               minted.get(key), entry_usd.get(key)), end="\n\n")
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
    po.add_argument("--no-entry", action="store_true", help="не искать дату входа (быстрее)")
    po.set_defaults(func=cmd_position)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
