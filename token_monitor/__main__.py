import argparse
import logging
import sys

from .config import load_settings
from .models import Network
from .pipeline import Scored, screen
from .screener import CodexError, CodexScreener, ScreenerQuery, parse_pair


def _row(s: Scored) -> str:
    p, m = s.pool, s.market.metrics
    return (f"{s.market.verdict.value.upper():<5} {p.network.value:<9} {p.symbol[:12]:<12} "
            f"{p.exchange[:16]:<16} fee {p.fee_bps:>5.1f}bp  liq ${p.liquidity_usd:>12,.0f}  "
            f"vol24 ${p.vol_24h_usd:>13,.0f}  "
            f"y24 {m.get('daily_yield_24h', 0):>6.2%}  pers {m.get('persistence', 0):>5.2f}  "
            f"{'; '.join(s.market.reasons)}  {p.address}")


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

    results = screen(pools, st.params, keep_skipped=args.all)
    for s in results:
        print(_row(s))
    print(f"\n{len(results)} of {len(pools)} pools", file=sys.stderr)
    return 0


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
    risks = {(r.get("riskVerdict"), r.get("riskScore"), tuple(r.get("riskReasons") or ())) for r, _ in parsed}
    for verdict, score, reasons in sorted(risks, key=str):
        print(f"codex risk: {verdict or 'n/a'} score={score if score is not None else 'n/a'} "
              f"{', '.join(reasons) or '-'}")
    print()

    results = screen([p for _, p in parsed], st.params, keep_skipped=True)
    for s in results:
        print(_row(s))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="token_monitor")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("screen", help="top-N пулов из Codex → FAST/SLOW")
    sc.add_argument("--limit", type=int, help="override SCREEN_LIMIT")
    sc.add_argument("--all", action="store_true", help="показывать и SKIP")
    sc.set_defaults(func=cmd_screen)
    an = sub.add_parser("analyze", help="все пулы токена → FAST/SLOW/SKIP + риск Codex")
    an.add_argument("token", help="адрес токена")
    an.add_argument("--network", choices=[n.value for n in Network])
    an.add_argument("--min-liq", type=float, default=1_000, help="нижняя граница ликвидности пула, $")
    an.set_defaults(func=cmd_analyze)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
