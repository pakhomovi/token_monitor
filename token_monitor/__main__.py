import argparse
import logging
import sys

from .config import load_settings
from .pipeline import Scored, screen
from .screener import CodexError, CodexScreener, ScreenerQuery


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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="token_monitor")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("screen", help="top-N пулов из Codex → FAST/SLOW")
    sc.add_argument("--limit", type=int, help="override SCREEN_LIMIT")
    sc.add_argument("--all", action="store_true", help="показывать и SKIP")
    sc.set_defaults(func=cmd_screen)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
