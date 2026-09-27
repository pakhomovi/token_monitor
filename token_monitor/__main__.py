import argparse
import logging
import sys
from dataclasses import replace

from .config import Settings, load_settings
from .models import Network, PoolSnapshot
from .pipeline import Scored, screen
from .scoring import Params, RangeStress, stress_model
from .screener import CodexError, CodexScreener, ScreenerQuery, parse_pair
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
        min_fee_bps=st.screen_min_fee_bps,
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
        for s in [s for s in results if s.pool.fee_bps > 0][:args.scenarios]:
            print(_strategy_table(model.pos, s.pool))
    print(f"\ntotal: liq ${total_liq:,.0f}, vol24 ${total_vol:,.0f}, "
          f"top pool makes {results[0].pool.vol_24h_usd / total_vol:.0%} of volume" if total_vol else "")
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

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
