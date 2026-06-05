import json
import threading
import time
from datetime import datetime, date, timedelta
import schedule
from loguru import logger
from sqlalchemy import select

from src.market.gamma_client import GammaClient
from src.market.markets import discover_and_save_weather_markets
from src.data.open_meteo import fetch_and_save_all_cities
from src.db.session import get_session
from src.db.repository import MarketGroupRepository, MarketSnapshotRepository
from src.db.models import MarketGroup, Market, MarketSnapshot, PaperTrade, DailySummary, LiveTrade
from src.strategy.paper_trader import run_paper_trade_engine, run_resolve_paper_trades
from src.strategy.live_trader import run_live_trade_engine, run_tail_engine
from src.notifications.telegram import get_notifier

_gamma = GammaClient()


_discovery_zero_count: int = 0  # G2-8: consecutive zero-event discovery counter

# ── G4-30: tail-exit runs on its OWN thread, decoupled from the single-threaded
# schedule.run_pending() loop. Heavy jobs (discover/snapshot/import) block that
# loop for minutes; measured tail tick drifted 5–47 min and missed the 45¢ window.
# The dedicated ticker guarantees DELIVERY of the tick on cadence; the per-position
# hot/normal gate in live_trader (decide_tail_exit) is untouched and still decides. ──
_TAIL_TICK_FLOOR_SECONDS: int = 60      # never tick faster than this (misconfig guard)
_tail_exit_stop = threading.Event()
_tail_exit_thread: "threading.Thread | None" = None


def _job_discover_markets() -> None:
    global _discovery_zero_count
    logger.info("JOB: market discovery")
    try:
        saved = discover_and_save_weather_markets(_gamma)
        if saved > 0:
            _discovery_zero_count = 0
            logger.info(f"Discovery added {saved} markets — triggering immediate snapshot")
            _job_snapshot_markets()
        else:
            _discovery_zero_count += 1
            logger.warning(
                f"[discovery] 0 new markets (cycle #{_discovery_zero_count})"
            )
            # G2-8: alert owner if discovery returns 0 for two consecutive cycles
            if _discovery_zero_count >= 2:
                _notifier_alert(
                    f"⚠️ Gamma discovery: 0 рынков найдено {_discovery_zero_count} цикла подряд. "
                    f"Gamma API может быть недоступен или изменился формат событий."
                )
                _discovery_zero_count = 0  # reset counter after alert
    except Exception as e:
        logger.error(f"Market discovery job failed: {e}")


def _notifier_alert(msg: str) -> None:
    """Send alert to owner only (not client). Used for operational issues."""
    try:
        notifier = get_notifier()
        notifier.send(msg)
    except Exception as exc:
        logger.warning(f"[notifier_alert] Failed to send alert: {exc}")


def _job_snapshot_markets() -> None:
    """Fetch current prices from Gamma API for all active groups."""
    logger.info("JOB: market snapshots (Gamma API)")
    session = get_session()
    snap_repo = MarketSnapshotRepository(session)

    try:
        groups: list[MarketGroup] = list(
            session.execute(
                select(MarketGroup).where(MarketGroup.is_active == True)  # noqa: E712
            ).scalars().all()
        )
        logger.info(f"Fetching prices for {len(groups)} active groups")

        saved = 0
        for group in groups:
            try:
                prices = _gamma.get_prices_for_group(group.group_id)
                if not prices:
                    continue

                markets: list[Market] = list(
                    session.execute(
                        select(Market).where(
                            Market.group_id == group.group_id,
                            Market.is_active == True,  # noqa: E712
                        )
                    ).scalars().all()
                )

                for market in markets:
                    price_yes = prices.get(market.market_id)
                    if price_yes is None:
                        continue
                    snap = MarketSnapshot(
                        market_id=market.market_id,
                        price_yes=price_yes,
                        price_no=round(1.0 - price_yes, 6),
                        mid=price_yes,
                        source="gamma",
                        snapshot_time=datetime.utcnow(),
                    )
                    snap_repo.insert(snap)
                    saved += 1

                time.sleep(0.2)
            except Exception as e:
                logger.error(f"Snapshot failed for group {group.group_id}: {e}")

        logger.info(f"Market snapshots: {saved} saved via Gamma API")
    finally:
        session.close()


def _job_deactivate_resolved_groups() -> None:
    logger.info("JOB: deactivate resolved groups")
    session = get_session()
    try:
        n = MarketGroupRepository(session).deactivate_resolved()
        if n:
            logger.info(f"Deactivated {n} resolved market groups (and their markets)")
    except Exception as e:
        logger.error(f"Deactivate-resolved job failed: {e}")
    finally:
        session.close()


def _job_fetch_open_meteo() -> None:
    logger.info("JOB: Open-Meteo multi-model forecast fetch")
    try:
        saved = fetch_and_save_all_cities()
        logger.info(f"Open-Meteo saved {saved} snapshots")
    except Exception as e:
        logger.error(f"Open-Meteo fetch job failed: {e}")


def _job_paper_trade_engine() -> None:
    from src.risk.risk_manager import risk_manager
    ok, reason = risk_manager.can_run_paper()
    if not ok:
        logger.info(f"JOB: paper trade engine SKIP — paper risk limit: {reason}")
        return
    logger.info("JOB: paper trade engine")
    try:
        run_paper_trade_engine()
    except Exception as exc:
        logger.exception("Paper trade engine job failed")



def _job_live_trade_engine() -> None:
    from src.config.settings import settings
    from src.risk.risk_manager import risk_manager
    mode = settings.trading_mode.lower()
    stopped = risk_manager.is_emergency_stopped()
    logger.info(f"JOB: live_trade_engine called — mode={mode} emergency_stop={stopped}")
    if mode != "live":
        logger.info("JOB: live_trade_engine SKIP — mode is not 'live'")
        return
    if stopped:
        logger.warning("JOB: live_trade_engine SKIP — emergency stop active")
        return
    try:
        run_live_trade_engine(gamma=_gamma)
        run_tail_engine()
    except Exception as e:
        logger.error(f"Live trade engine job failed: {e}")


def _job_tail_early_exit() -> None:
    """A2: tz-aware tail exit — take_profit (any time) / hard_floor + time_gate
    (post local peak). Ticks every 5 min; normal positions self-throttle to 10."""
    from src.config.settings import settings
    from src.strategy.live_trader import run_tail_early_exit
    if settings.trading_mode.lower() != "live":
        return
    try:
        run_tail_early_exit(gamma=_gamma)   # G4-18: gate SELL on live markets only
    except Exception as e:
        logger.error(f"Tail early-exit job failed: {e}")


def _classify_imported_token(
    net_usd: float,
    cid: str | None,
    redeemed_cids: set,
    pos: dict | None,
    gamma_resolved: bool | None,
    min_usd: float = 1.0,
) -> str:
    """
    G4-26: classify ONE on-chain token before assigning a DB status. Pure (no
    DB/network) so the rule is unit-testable. Returns "open" | "pending_redeem"
    | "skip". Priority:

      1. conditionId already has a REDEEM in /activity → skip (position closed).
      2. net BUY−SELL below the noise floor → skip (sold / flat).
      3. /positions known:
           redeemable=True OR curPrice≥0.99            → pending_redeem (won).
           live curPrice (≥0.02) and Gamma not resolved → open.
           live curPrice but Gamma resolved             → pending_redeem.
           curPrice≈0: Gamma resolved → pending_redeem; live → open; unknown →
             pending_redeem (don't risk a SELL on a maybe-dead market).
      4. not in /positions: fall back to Gamma (resolved→pending_redeem, live→open).
      5. SAFE DEFAULT when neither /positions nor Gamma can tell → pending_redeem,
         NEVER open (the invariant: resolved must never land in open).
    """
    if cid and cid in redeemed_cids:
        return "skip"
    if net_usd < min_usd:
        return "skip"
    if pos is not None:
        try:
            cur = float(pos.get("curPrice") or 0)
        except (TypeError, ValueError):
            cur = 0.0
        if pos.get("redeemable") or cur >= 0.99:
            return "pending_redeem"
        if cur >= 0.02:
            return "pending_redeem" if gamma_resolved is True else "open"
        # curPrice ≈ 0 — resolved-lost or deep-OTM-live; need the resolution signal.
        if gamma_resolved is True:
            return "pending_redeem"
        if gamma_resolved is False:
            return "open"
        return "pending_redeem"  # unknown → safe default
    # Not present in /positions.
    if gamma_resolved is True:
        return "pending_redeem"
    if gamma_resolved is False:
        return "open"
    return "pending_redeem"  # no /positions AND no Gamma → safe default


def _import_apply_canonical(
    session, token_id, net_usd, shares, verdict,
    existing, cid, gid, city, bin_label, market_id, strat,
):
    """
    Ensure EXACTLY ONE canonical live row per token carries the full on-chain
    exposure with status=verdict. Idempotent: reuses an existing open/pending row
    instead of inserting. Other rows for the same token are demoted to not_filled
    so deployed/open_bets count the position ONCE (a UI position = one token),
    while the canonical row keeps the REAL exposure (Dallas $11.22, not collapsed).
    """
    canonical = next(
        (r for r in existing if r.status in ("open", "filled", "pending_redeem")), None
    )
    if canonical is None:
        promotable = [
            r for r in existing
            if r.status in ("not_filled", "cancelled", "pending", "expired")
        ]
        if promotable:
            canonical = max(promotable, key=lambda r: r.placed_at or datetime.min)
    if canonical is None and not existing:
        canonical = LiveTrade(
            market_id=market_id or "imported", bin_label=bin_label or "?",
            side="buy", status="pending", strategy_name=strat or "tail_no",
            group_id=gid, city=city, condition_id=cid, token_id=token_id,
        )
        session.add(canonical)
    if canonical is None:
        return

    canonical.status = verdict
    canonical.token_id = token_id
    canonical.condition_id = canonical.condition_id or cid
    canonical.group_id = canonical.group_id or gid
    canonical.city = canonical.city or city
    canonical.bin_label = canonical.bin_label or bin_label
    canonical.market_id = canonical.market_id or market_id or "imported"
    canonical.strategy_name = canonical.strategy_name or strat or "tail_no"
    canonical.stake_usd = round(net_usd, 4)          # ACTUAL exposure, not collapsed
    if shares:
        canonical.size_shares = round(shares, 4)
    if not canonical.order_id:
        canonical.order_id = f"imported-onchain:{token_id[:16]}"
    canonical.reconciled_with_polymarket = False     # let reconcile finalize PnL

    # Collapse duplicate live rows for this token → one counted position.
    for r in existing:
        if r is not canonical and r.status in ("open", "filled", "pending", "pending_redeem"):
            r.status = "not_filled"


def _job_import_onchain_positions(dry_run: bool | None = None) -> None:
    """
    G4-26: reconcile the DB from /activity as the source of truth for position
    EXISTENCE. Resurrect mislabeled not_filled/cancelled rows and create missing
    rows for real on-chain BUYs — but classify each token (open vs pending_redeem
    vs skip) via /positions.redeemable + Gamma BEFORE writing, never blindly.

    DRY-RUN by default (settings.onchain_import_dry_run): logs the full per-token
    classification every cycle and writes NOTHING, so it can be eye-checked
    against the UI first. Idempotent once applied (keyed by token, no blind insert).
    """
    from collections import defaultdict as _dd
    from src.config.settings import settings
    from src.market.polymarket_data import (
        get_activity, get_positions, open_cost_by_token,
        redeemed_condition_ids, buy_shares_by_token,
    )

    if settings.trading_mode.lower() != "live" or not settings.proxy_wallet_address:
        return
    if dry_run is None:
        dry_run = settings.onchain_import_dry_run
    min_usd = float(getattr(settings, "onchain_import_min_usd", 1.0))

    activity = get_activity(settings.proxy_wallet_address)
    # G4-28: empty /activity == data-api unavailable. Importing/demoting against it
    # would be based on a false "no positions" view — defer this cycle.
    if not activity:
        logger.warning(
            "[import] /activity unavailable (empty/failed) — "
            "skipping reconcile, no status change"
        )
        return
    open_cost = open_cost_by_token(activity)
    redeemed = redeemed_condition_ids(activity)
    buy_shares = buy_shares_by_token(activity)

    pos_by_cid: dict[str, dict] = {}
    pos_by_token: dict[str, dict] = {}
    try:
        for p in get_positions(settings.proxy_wallet_address) or []:
            c = p.get("conditionId")
            if c:
                pos_by_cid[c] = p
            a = str(p.get("asset") or "")
            if a:
                pos_by_token[a] = p
    except Exception as exc:
        logger.warning(f"[import] /positions unavailable: {exc}")

    gres_cache: dict[str, bool | None] = {}

    def _gres(gid: str | None) -> bool | None:
        if not gid:
            return None
        if gid not in gres_cache:
            try:
                prices = _gamma.get_prices_for_group(gid)
                gres_cache[gid] = (max(prices.values()) >= 0.99) if prices else None
            except Exception:
                gres_cache[gid] = None
        return gres_cache[gid]

    session = get_session()
    try:
        rows_by_token: dict[str, list] = _dd(list)
        for t in session.execute(
            select(LiveTrade).where(LiveTrade.token_id.isnot(None))
        ).scalars():
            rows_by_token[t.token_id].append(t)

        meta_by_token: dict[str, tuple] = {}
        for m in session.execute(select(Market)).scalars():
            if m.token_id_no:
                meta_by_token[m.token_id_no] = (m, "no")
            if m.token_id_yes:
                meta_by_token[m.token_id_yes] = (m, "yes")
        city_by_group = {
            g.group_id: g.city for g in session.execute(select(MarketGroup)).scalars()
        }

        verdict_counts: dict[str, int] = _dd(int)
        changed = 0
        for token_id, net in sorted(open_cost.items(), key=lambda x: -x[1]):
            existing = rows_by_token.get(token_id, [])
            cid = gid = city = bin_label = market_id = None
            strat = "tail_no"
            if existing:
                e = max(existing, key=lambda r: (r.order_id is not None, r.placed_at or datetime.min))
                cid, gid, city, bin_label, market_id = (
                    e.condition_id, e.group_id, e.city, e.bin_label, e.market_id
                )
                strat = e.strategy_name or "tail_no"
            elif token_id in meta_by_token:
                m, side_kind = meta_by_token[token_id]
                cid, gid, bin_label, market_id = (
                    m.condition_id, m.group_id, m.bin_label, m.market_id
                )
                city = city_by_group.get(gid)
                strat = "tail_no" if side_kind == "no" else "basket_narrow"

            # Orphan: a real on-chain BUY we can't map to any market/conditionId.
            # Without a conditionId it can't be redeemed or resolved → don't import
            # (would be a dead pending_redeem row auto_redeem can't process).
            if not cid:
                verdict_counts["skip_orphan"] += 1
                logger.info(
                    f"[import] token={str(token_id)[:14]}… net=${net:.2f} "
                    f"→ SKIP (orphan: no conditionId mapping in markets/live_trades)"
                )
                continue

            pos = pos_by_token.get(token_id) or pos_by_cid.get(cid)
            gres = _gres(gid)
            verdict = _classify_imported_token(net, cid, redeemed, pos, gres, min_usd)
            verdict_counts[verdict] += 1

            signal = (
                "redeemed" if (cid and cid in redeemed) else
                "positions" if pos is not None else
                "gamma" if gres is not None else "default"
            )
            logger.info(
                f"[import] token={str(token_id)[:14]}… city={city} bin={bin_label} "
                f"cid={str(cid)[:10]} net=${net:.2f} signal={signal} "
                f"redeemable={pos.get('redeemable') if pos else None} "
                f"curPrice={pos.get('curPrice') if pos else None} "
                f"gamma_resolved={gres} → {verdict}"
            )

            if verdict == "skip" or dry_run:
                continue
            _import_apply_canonical(
                session, token_id, net, buy_shares.get(token_id, 0.0), verdict,
                existing, cid, gid, city, bin_label, market_id, strat,
            )
            changed += 1

        mode = "DRY-RUN (no writes — verify vs UI, then set ONCHAIN_IMPORT_DRY_RUN=false)" \
            if dry_run else "APPLIED"
        logger.info(
            f"[import] {mode}: verdicts={dict(verdict_counts)} "
            f"over {len(open_cost)} on-chain tokens; rows changed={changed}"
        )
        if not dry_run and changed:
            session.commit()
    except Exception as exc:
        logger.error(f"[import] job failed: {exc}")
    finally:
        session.close()


def _job_auto_redeem() -> None:
    """
    G4-17: redeem won positions that reached resolution without an early-exit
    sell (no bid / market closed). Targets pending_redeem trades (Gamma win
    confirmed by reconcile). Initiates the on-chain redeem; the resulting REDEEM
    in /activity is then finalized by reconcile (source=redeem).
    """
    from src.config.settings import settings
    from src.blockchain.redeem import redeem_position
    from src.market.polymarket_data import get_positions

    if settings.trading_mode.lower() != "live" or not settings.auto_redeem_enabled:
        return

    session = get_session()
    try:
        pending: list[LiveTrade] = list(
            session.execute(
                select(LiveTrade).where(
                    LiveTrade.status == "pending_redeem",
                    LiveTrade.reconciled_with_polymarket == False,  # noqa: E712
                    LiveTrade.condition_id.isnot(None),
                )
            ).scalars().all()
        )
        if not pending:
            logger.debug("[auto_redeem] No pending_redeem positions")
            return

        logger.info(f"[auto_redeem] {len(pending)} pending_redeem position(s) to redeem")

        # Polymarket's own redeemable signal + negRisk flag, keyed by conditionId
        redeemable_by_cid: dict[str, dict] = {}
        try:
            for p in get_positions(settings.proxy_wallet_address, redeemable=True) or []:
                cid = p.get("conditionId")
                if cid:
                    redeemable_by_cid[cid] = p
        except Exception as exc:
            logger.warning(f"[auto_redeem] data-api positions unavailable: {exc}")

        # Redeem once per conditionId (a basket may have several legs on one cond)
        done_cids: set[str] = set()
        for t in pending:
            cid = t.condition_id
            if not cid or cid in done_cids:
                continue

            pos = redeemable_by_cid.get(cid)
            if redeemable_by_cid and pos is None:
                # data-api responded but this cond isn't (yet) redeemable — wait
                logger.info(
                    f"[auto_redeem] {t.city} {t.bin_label}: not yet redeemable "
                    f"per data-api — skip"
                )
                continue

            # negRisk flag: prefer data-api, default True (weather bins are NegRisk)
            neg_risk = True
            if pos is not None:
                neg_risk = bool(pos.get("negativeRisk", pos.get("negRisk", True)))
            size = float(pos.get("size")) if (pos and pos.get("size")) else (t.size_shares or 0.0)

            tx = redeem_position(
                condition_id=cid,
                size_shares=size,
                neg_risk=neg_risk,
                market_label=f"{t.city} {t.bin_label}",
            )
            if tx:
                done_cids.add(cid)

        # Let reconcile finalize PnL from the new REDEEM cash-flows
        if done_cids:
            logger.info(f"[auto_redeem] Redeemed {len(done_cids)} condition(s) — reconciling")
            _job_reconcile_pnl()

    except Exception as e:
        logger.error(f"Auto-redeem job failed: {e}")
    finally:
        session.close()


def _job_resolve_paper_trades() -> None:
    logger.info("JOB: resolve paper trades")
    try:
        run_resolve_paper_trades(gamma=_gamma)
    except Exception as e:
        logger.error(f"Resolve paper trades job failed: {e}")


def _job_reconcile_orders() -> None:
    """
    Non-blocking reconciliation: poll CLOB status for all open/pending orders.
    Runs every 2 minutes — replaces the blocking _poll_until_filled_or_timeout loop.
    Orders older than ORDER_TIMEOUT_MINUTES are cancelled and marked expired.
    """
    from datetime import timedelta
    from src.config.settings import settings
    from src.market.order_executor import get_order_status, cancel_order
    from src.market.polymarket_data import get_activity, buy_shares_by_token
    from src.notifications.telegram import get_notifier

    notifier = get_notifier()
    session = get_session()
    try:
        timeout = timedelta(minutes=settings.order_timeout_minutes)
        now = datetime.utcnow()

        pending_open = list(
            session.execute(
                select(LiveTrade).where(
                    LiveTrade.status.in_(["open", "pending"]),
                    LiveTrade.order_id.isnot(None),
                )
            ).scalars().all()
        )

        if not pending_open:
            return

        logger.debug(f"[reconcile] Checking {len(pending_open)} open/pending orders")

        # G4-19: /activity BUY is the ONLY proof of fill. CLOB 'matched' status is
        # a hint, not proof (phantom-fill bug: Lucknow had a matched order but no
        # BUY on Polymarket). Build BUY-shares-per-token once for this run.
        buy_shares: dict[str, float] = {}
        if settings.proxy_wallet_address:
            try:
                buy_shares = buy_shares_by_token(get_activity(settings.proxy_wallet_address))
            except Exception as exc:
                logger.warning(f"[reconcile] /activity fetch failed — fill check deferred: {exc}")

        def _has_matching_buy(tr: LiveTrade) -> bool:
            if not tr.token_id:
                return False
            bought = buy_shares.get(tr.token_id, 0.0)
            want = tr.size_shares or 0.0
            # tolerance: 2% or 0.5 shares, whichever is larger
            tol = max(0.5, want * 0.02)
            return bought >= (want - tol)

        for t in pending_open:
            placed_at = t.placed_at or now

            # Fill is confirmed by /activity BUY, not by CLOB status
            if _has_matching_buy(t):
                # G4-27: notify only on the pending/open → filled TRANSITION.
                # reconcile re-verifies the same position every cycle; without
                # this gate it re-sends "✅ Исполнен" for hours (Seattle/Dallas).
                first_fill = not t.fill_notified
                t.status = "filled"
                t.filled_price = t.target_price
                t.filled_at = now
                t.fill_notified = True
                session.commit()
                city = t.city or t.group_id or "?"
                logger.info(
                    f"[reconcile] FILLED (verified /activity BUY): {city} {t.bin_label} "
                    f"order={t.order_id[:12]}…"
                    + ("" if first_fill else " (already notified — no alert)")
                )
                if first_fill:
                    notifier.send(
                        f"✅ Исполнен [{t.strategy_name}]: {city} {t.bin_label} | "
                        f"${t.target_price:.4f} × {t.size_shares:.4f}"
                    )
                continue

            try:
                clob_status = get_order_status(t.order_id)
            except Exception as exc:
                logger.warning(f"[reconcile] Status check failed for {t.order_id[:12]}…: {exc}")
                continue

            if clob_status == "cancelled":
                t.status = "cancelled"
                t.cancelled_at = now
                session.commit()
                logger.info(
                    f"[reconcile] CANCELLED by exchange: {t.city or '?'} {t.bin_label}"
                )

            elif (now - placed_at) > timeout:
                # Timed out. CLOB may say 'matched' but if /activity has no BUY it
                # never really filled → not_filled (excluded from PnL/risk), not expired.
                try:
                    cancel_order(t.order_id)
                except Exception:
                    pass
                city = t.city or t.group_id or "?"
                if buy_shares and not _has_matching_buy(t):
                    t.status = "not_filled"
                    t.cancelled_at = now
                    session.commit()
                    logger.warning(
                        f"[reconcile] NOT_FILLED (no /activity BUY after timeout, "
                        f"clob={clob_status}): {city} {t.bin_label} order={t.order_id[:12]}…"
                    )
                else:
                    t.status = "expired"
                    t.cancelled_at = now
                    session.commit()
                    logger.warning(
                        f"[reconcile] EXPIRED (>{settings.order_timeout_minutes}m): "
                        f"{city} {t.bin_label} order={t.order_id[:12]}…"
                    )
                    notifier.send(f"⏰ Истёк [{t.strategy_name}]: {city} {t.bin_label}")

        # G2-5: After processing individual legs, detect partial basket fills
        _detect_partial_baskets(session, notifier)

    except Exception as exc:
        logger.error(f"[reconcile] Job failed: {exc}")
    finally:
        session.close()


def _detect_partial_baskets(session, notifier) -> None:
    """
    G2-5: Detect baskets where some legs filled and some expired.
    For each such basket: attempt one re-fill of expired legs at current ask.
    If re-fill also fails: mark basket_incomplete, alert owner.
    """
    from src.config.settings import settings as _settings
    from src.market.order_executor import place_limit_order, cancel_order
    from sqlalchemy import func as sa_func

    # Find basket_ids that have both filled and expired/cancelled legs
    baskets_with_mixed = session.execute(
        select(LiveTrade.basket_id, LiveTrade.status)
        .where(
            LiveTrade.basket_id.isnot(None),
            LiveTrade.status.in_(["filled", "expired", "cancelled"]),
        )
    ).all()

    if not baskets_with_mixed:
        return

    # Group by basket_id
    basket_status_map: dict[str, set[str]] = {}
    for bid, status in baskets_with_mixed:
        if bid:
            basket_status_map.setdefault(bid, set()).add(status)

    for basket_id, statuses in basket_status_map.items():
        has_filled = "filled" in statuses
        has_missing = bool(statuses & {"expired", "cancelled"})
        if not (has_filled and has_missing):
            continue

        all_legs: list[LiveTrade] = list(
            session.execute(
                select(LiveTrade).where(LiveTrade.basket_id == basket_id)
            ).scalars().all()
        )

        filled_legs = [l for l in all_legs if l.status == "filled"]
        missing_legs = [l for l in all_legs if l.status in ("expired", "cancelled")]

        if not missing_legs:
            continue

        city = (all_legs[0].city or basket_id[:8]) if all_legs else basket_id[:8]
        logger.warning(
            f"[reconcile/G2-5] Partial basket {basket_id[:8]}: "
            f"{len(filled_legs)} filled, {len(missing_legs)} missing — attempting re-fill"
        )

        # One retry: place new order for each missing leg at current ask
        refilled = 0
        for leg in missing_legs:
            from src.db.session import get_session as _gs
            from src.db.models import Market
            mkt = session.execute(
                select(Market).where(Market.market_id == leg.market_id)
            ).scalars().first()
            token_id = (mkt.token_id_yes if mkt else None) or leg.token_id or ""
            if not token_id or not leg.target_price or not leg.size_shares:
                continue

            order_id = place_limit_order(
                market_id=leg.market_id,
                token_id=token_id,
                side="BUY",
                price=min(0.99, leg.target_price + _settings.entry_tick),
                size=leg.size_shares,
                skip_risk_check=True,
            )
            if order_id:
                import uuid as _uuid
                from src.db.models import LiveTrade as _LT
                retry_trade = _LT(
                    id=str(_uuid.uuid4()),
                    group_id=leg.group_id,
                    market_id=leg.market_id,
                    bin_label=leg.bin_label,
                    target_price=leg.target_price,
                    size_shares=leg.size_shares,
                    stake_usd=leg.stake_usd,
                    basket_role=leg.basket_role,
                    strategy_name=leg.strategy_name,
                    city=leg.city,
                    status="open",
                    order_id=order_id,
                    side=leg.side,
                    placed_at=datetime.utcnow(),
                    condition_id=leg.condition_id,
                    token_id=leg.token_id,
                    basket_id=basket_id,
                )
                session.add(retry_trade)
                # Mark the old expired leg as basket_refill_attempted so we don't retry again
                leg.basket_role = "expired_refilled"
                session.commit()
                refilled += 1
                logger.info(f"[reconcile/G2-5] Re-placed leg {leg.bin_label} order={order_id[:12]}…")

        if refilled < len(missing_legs):
            still_missing = len(missing_legs) - refilled
            notifier.send(
                f"⚠️ Частичная корзина [{all_legs[0].strategy_name if all_legs else '?'}]: "
                f"{city} — {len(filled_legs)}/{len(all_legs)} бинов исполнено, "
                f"{still_missing} не удалось перезалить\n"
                f"basket_id={basket_id[:8]}"
            )
            logger.warning(
                f"[reconcile/G2-5] Basket {basket_id[:8]} incomplete: "
                f"{still_missing} legs still missing after retry"
            )
        elif refilled > 0:
            logger.info(
                f"[reconcile/G2-5] Basket {basket_id[:8]}: re-filled {refilled} missing leg(s)"
            )


def _job_expire_stale_pending() -> None:
    """Mark pending limit orders older than 2 hours as expired (FIX 5)."""
    from datetime import timedelta
    logger.debug("JOB: expire stale pending orders")
    session = get_session()
    try:
        cutoff = datetime.utcnow() - timedelta(hours=2)
        stale = list(
            session.execute(
                select(LiveTrade).where(
                    LiveTrade.status == "pending",
                    LiveTrade.placed_at < cutoff,
                )
            ).scalars().all()
        )
        if stale:
            for t in stale:
                t.status = "expired"
            session.commit()
            logger.info(f"[expire_pending] Expired {len(stale)} stale pending orders (>2h)")
    except Exception as e:
        logger.error(f"Expire stale pending job failed: {e}")
    finally:
        session.close()


def _job_reconcile_pnl() -> None:
    """
    G3-2 / G4-13: Reconcile live trade PnL against Polymarket Data API.

    Resolution rule — only finalize PnL after one of:
      1. REDEEM in /activity for the trade's conditionId  →  source=redeem (true PnL)
      2. Gamma says market resolved AND we LOST (no REDEEM expected for losers)
         →  source=gamma_close (PnL = buy cost, negative)
      3. Gamma says market resolved AND we WON but no REDEEM yet
         →  status=pending_redeem; re-checked every reconcile run until REDEEM arrives

    This prevents the bug where pnl_map shows only the BUY cost before REDEEM,
    making a winning position look like a total loss (e.g. Seoul 27°C+ NO).

    Log: [resolve] {city} {bin} status={win|loss} pnl=${X} source={...} ui_value=${Y}
    """
    from collections import defaultdict as _dd
    from datetime import timedelta
    from src.config.settings import settings
    from src.market.polymarket_data import (
        get_activity, realized_pnl_by_token, buy_shares_by_token,
        redeem_payout_by_condition,
    )
    from src.notifications.telegram import get_notifier

    if not settings.proxy_wallet_address:
        logger.debug("[reconcile_pnl] No proxy_wallet_address — skip")
        return

    notifier = get_notifier()
    session = get_session()
    try:
        # Include pending_redeem so winning trades are re-checked each cycle
        unreconciled: list[LiveTrade] = list(
            session.execute(
                select(LiveTrade).where(
                    LiveTrade.token_id.isnot(None),
                    LiveTrade.group_id.isnot(None),
                    LiveTrade.reconciled_with_polymarket == False,  # noqa: E712
                    LiveTrade.status.in_(
                        ["filled", "open", "expired", "cancelled", "pending_redeem", "sold"]
                    ),
                )
            ).scalars().all()
        )

        if not unreconciled:
            logger.debug("[reconcile_pnl] No unreconciled trades")
            return

        logger.info(f"[reconcile_pnl] Checking {len(unreconciled)} trades against Data API")

        activity = get_activity(settings.proxy_wallet_address)
        # G4-28: an empty /activity is NOT proof that no BUY exists — the data-api
        # intermittently returns [] on timeout/error. Treating that as "no fill"
        # marked real positions not_filled and created the $1.71-vs-$110 wipeout.
        # A wallet that has open/unreconciled trades cannot truly have zero
        # activity, so empty == unavailable → defer, change NO status this cycle.
        if not activity:
            logger.warning(
                "[reconcile_pnl] /activity unavailable (empty/failed) — "
                "skipping reconcile, no status change"
            )
            return
        pnl_map = realized_pnl_by_token(activity)
        buy_shares = buy_shares_by_token(activity)
        _fill_timeout = timedelta(minutes=max(30, settings.order_timeout_minutes * 4))

        def _has_buy(tr: LiveTrade) -> bool:
            """/activity BUY is the only proof the position exists (size-matched)."""
            bought = buy_shares.get(tr.token_id or "", 0.0)
            want = tr.size_shares or 0.0
            tol = max(0.5, want * 0.02)
            return bought >= (want - tol)

        # --- Build REDEEM helpers from raw activity ---
        # G4-23: de-duplicate NegRisk twin-index REDEEM rows so the payout (and
        # thus the stored PnL written below) is net, not double-counted.
        redeem_payouts: dict[str, float] = redeem_payout_by_condition(activity)
        redeemed_cids: set[str] = set(redeem_payouts.keys())

        # Gamma caches — one API call per group_id
        resolved_cache: dict[str, bool] = {}
        winner_cache: dict[str, str | None] = {}

        def _is_resolved(gid: str) -> bool:
            if gid not in resolved_cache:
                resolved_cache[gid] = _gamma.is_resolved_event(gid)
            return resolved_cache[gid]

        def _winner_mid(gid: str) -> str | None:
            if gid not in winner_cache:
                winner_cache[gid] = _gamma.winning_market_id(gid)
            return winner_cache[gid]

        # ── SOLD positions (early-exit) — finalize per-lot from locked_pnl ──
        # Each sold lot is an independent close at a known SELL price. Its PnL
        # was computed at sell time (shares*sell_price - stake) and stored on
        # the row. Do NOT re-derive from pnl_map: the /activity SELL may not have
        # settled yet, and two lots sharing one token_id would aggregate/zero in
        # a way that flips the sign (the +$0.38 win → −$5.30 loss bug).
        sold_trades = [t for t in unreconciled if t.status == "sold"]
        for t in sold_trades:
            locked = t.pnl_usd if t.pnl_usd is not None else 0.0
            t.reconciled_with_polymarket = True
            t.resolved_at = t.resolved_at or datetime.utcnow()
            t.status = "resolved"
            outcome = "win" if locked > 0 else "loss"
            sign = "+" if locked >= 0 else ""
            logger.info(
                f"[resolve] {t.city or t.group_id} {t.bin_label} status={outcome} "
                f"pnl={sign}${abs(locked):.4f} source=sell ui_value=N/A"
            )
        if sold_trades:
            session.commit()

        # Group remaining (non-sold) by token_id for dedup
        by_token: dict[str, list[LiveTrade]] = _dd(list)
        for t in unreconciled:
            if t.status != "sold" and t.token_id:
                by_token[t.token_id].append(t)

        for token_id, trades in by_token.items():
            group_id = trades[0].group_id
            if not group_id:
                continue

            primary = max(
                trades,
                key=lambda t: (
                    t.status == "filled",
                    t.status == "open",
                    t.placed_at or datetime.min,
                ),
            )
            city = primary.city or group_id or "?"
            condition_id = primary.condition_id
            has_redeem = bool(condition_id and condition_id in redeemed_cids)

            # ── BUY gate: position must exist on /activity ──────────────────
            # No BUY in /activity = the order never really filled (phantom).
            # After a grace window, mark not_filled and exclude from PnL/risk.
            if not _has_buy(primary):
                oldest = min((t.placed_at or datetime.utcnow()) for t in trades)
                if (datetime.utcnow() - oldest) > _fill_timeout:
                    for t in trades:
                        t.status = "not_filled"
                        t.pnl_usd = None
                        # reconciled stays False → excluded from loss/deployed calcs
                    session.commit()
                    logger.warning(
                        f"[reconcile_pnl] NOT_FILLED {city} {primary.bin_label}: "
                        f"no /activity BUY for token={token_id[:12]}… "
                        f"(want {primary.size_shares} shares) — phantom, excluded"
                    )
                else:
                    logger.debug(
                        f"[reconcile_pnl] {city}: no /activity BUY yet "
                        f"(within grace window) — wait"
                    )
                continue

            # Position confirmed real → require Gamma resolution to finalize
            if not _is_resolved(group_id):
                continue

            # ── Gate 1: REDEEM confirmed ────────────────────────────────────
            if has_redeem:
                realized = pnl_map.get(token_id, 0.0)
                source = "redeem"
                ui_value = redeem_payouts.get(condition_id or "", None)

            else:
                # ── Gate 2: Determine win/loss from Gamma ──────────────────
                winner = _winner_mid(group_id)
                if winner is None:
                    # Gamma hasn't fully settled yet — wait
                    logger.debug(
                        f"[reconcile_pnl] {city}: Gamma resolved but no winner yet — skip"
                    )
                    continue

                is_tail_no = primary.strategy_name == "tail_no"
                # For tail_no we hold NO on primary.market_id;
                # we win if the Gamma winner is some OTHER market (our YES didn't happen).
                # For basket/YES trades we win if our market IS the winner.
                we_won = (winner != primary.market_id) if is_tail_no else (winner == primary.market_id)

                if we_won:
                    # ── Gate 3: WIN but no REDEEM yet → pending_redeem ────
                    if primary.status != "pending_redeem":
                        primary.status = "pending_redeem"
                        session.commit()
                        logger.info(
                            f"[reconcile_pnl] {city} {primary.bin_label}: "
                            f"WIN confirmed by Gamma (winner={winner[:8]}…), "
                            f"no REDEEM in /activity yet → pending_redeem"
                        )
                    continue  # re-check next cycle

                # LOSS confirmed — no REDEEM will arrive; use BUY cost from pnl_map
                realized = pnl_map.get(token_id, 0.0)  # negative (cost only)
                source = "gamma_close"
                ui_value = None

            # ── Write final PnL ─────────────────────────────────────────────
            primary.pnl_usd = round(realized, 4)
            primary.reconciled_with_polymarket = True
            primary.resolved_at = primary.resolved_at or datetime.utcnow()
            primary.status = "resolved"

            for t in trades:
                if t is not primary and not t.reconciled_with_polymarket:
                    t.pnl_usd = 0.0
                    t.reconciled_with_polymarket = True
                    t.status = "resolved"

            session.commit()

            outcome = "win" if realized > 0 else "loss"
            pnl_sign = "+" if realized >= 0 else ""
            ui_str = f"${ui_value:.4f}" if ui_value is not None else "N/A"
            logger.info(
                f"[resolve] {city} {primary.bin_label} "
                f"status={outcome} "
                f"pnl={pnl_sign}${abs(realized):.4f} "
                f"source={source} "
                f"ui_value={ui_str}"
                + (f" ({len(trades)-1} deduped)" if len(trades) > 1 else "")
            )
            outcome_em = "✅" if realized > 0 else "❌"
            notifier.send(
                f"{outcome_em} [{primary.strategy_name}] {city} {primary.bin_label} — "
                f"закрыта {pnl_sign}${abs(realized):.2f} "
                f"(source={source}, ui={ui_str})"
            )

    except Exception as exc:
        logger.error(f"[reconcile_pnl] Job failed: {exc}")
    finally:
        session.close()


def _audit_verdict(
    filled: bool, has_redeem: bool, sold: bool,
    stored_pnl: float, activity_pnl: float, tol: float = 0.01,
) -> str:
    """
    G4-22: three-way diagnostic verdict for one token's audit line.

    Pure function (no DB/network) so the rule is unit-testable. Returns one of
    "OK", "PENDING_REDEEM", or "DRIFT Δ=$X". The two auto-repaired classes
    (phantom / win-as-loss) are handled by the caller and short-circuit this.
    """
    delta = stored_pnl - activity_pnl
    if abs(delta) < tol:
        return "OK"
    if not filled:
        # No BUY on Polymarket: OK only if nothing was stored (honest pending).
        return "OK" if stored_pnl == 0 else f"DRIFT Δ=${delta:+.4f}"
    if not (has_redeem or sold):
        # Filled winner, resolved but neither redeemed nor sold → still hanging.
        return "PENDING_REDEEM"
    # Filled AND finalized but stored disagrees with /activity by ≥ tol.
    return f"DRIFT Δ=${delta:+.4f}"


def _job_audit_activity() -> None:
    """
    G4-19: audit every resolved/reconciled live trade against /activity and
    repair the two unambiguous discrepancy classes:

      • phantom  — no BUY in /activity for the token → status=not_filled, pnl=None
                   (e.g. Lucknow: recorded resolved with a local −cost on placement)
      • win-as-loss — REDEEM credit exists but stored pnl is negative → fix to the
                   /activity value +(payout−cost) (e.g. Seoul: +$0.74 stored −$6.03)

    Sold positions (per-lot locked_pnl, no REDEEM) are NOT touched.

    G4-22: the per-trade verdict is no longer binary OK/FIX. A drift no longer
    hides behind OK. Three explicit diagnostic verdicts (in addition to FIX:* for
    the two auto-repaired classes):
      • OK             — abs(stored − activity) < $0.01, OR honest pending
                         (not filled, nothing stored).
      • PENDING_REDEEM — filled winner, resolved but neither redeemed nor sold:
                         win recorded before REDEEM is realized (e.g. Seattle).
                         Surfaced as waiting, NOT OK, so hung positions are visible.
      • DRIFT Δ=$X     — filled AND finalized (redeem/sell) but stored ≠ /activity
                         truth by ≥ $0.01 (e.g. Milan/Austin/Atlanta/Chicago):
                         the local PnL estimate has diverged from the source of truth.

    Audit line for every trade:
      [audit] {city} {bin} stored=$X activity=$Y filled={b} redeem={b} sold={b} → {verdict}
    """
    from collections import defaultdict as _dd
    from src.config.settings import settings
    from src.market.polymarket_data import (
        get_activity, realized_pnl_by_token, buy_shares_by_token, redeemed_condition_ids,
    )

    if not settings.proxy_wallet_address:
        return

    session = get_session()
    try:
        resolved: list[LiveTrade] = list(
            session.execute(
                select(LiveTrade).where(
                    LiveTrade.token_id.isnot(None),
                    LiveTrade.status.in_(["resolved", "pending_redeem", "not_filled"]),
                )
            ).scalars().all()
        )
        if not resolved:
            logger.debug("[audit] No resolved live trades to audit")
            return

        activity = get_activity(settings.proxy_wallet_address)
        # G4-28: empty /activity == data-api unavailable, NOT "no fills". Auditing
        # against it would compute filled=False for everything and re-demote real
        # positions to not_filled (the wipeout). Defer — change nothing this cycle.
        if not activity:
            logger.warning(
                "[audit] /activity unavailable (empty/failed) — "
                "skipping reconcile, no status change"
            )
            return
        pnl_map = realized_pnl_by_token(activity)
        buy_shares = buy_shares_by_token(activity)
        redeemed = redeemed_condition_ids(activity)

        by_token: dict[str, list[LiveTrade]] = _dd(list)
        for t in resolved:
            by_token[t.token_id].append(t)

        fixes = 0
        for token_id, trades in by_token.items():
            want = max((t.size_shares or 0.0) for t in trades)
            bought = buy_shares.get(token_id, 0.0)
            tol = max(0.5, want * 0.02)
            filled = bought >= (want - tol)
            activity_pnl = pnl_map.get(token_id, 0.0)
            stored_pnl = sum((t.pnl_usd or 0.0) for t in trades)
            primary = max(trades, key=lambda t: (t.pnl_usd is not None, t.placed_at or datetime.min))
            cid = primary.condition_id or ""
            has_redeem = cid in redeemed
            sold = any((t.status or "") == "sold" for t in trades)

            verdict = None
            # --- Repairs (unchanged): act on the two unambiguous classes ---
            # Phantom: never filled on Polymarket
            if not filled and any(t.status != "not_filled" for t in trades):
                for t in trades:
                    t.status = "not_filled"
                    t.pnl_usd = None
                    t.reconciled_with_polymarket = False
                verdict = "FIX:not_filled(no BUY)"
                fixes += 1
            # Win recorded as loss: redeem credit exists but stored is negative
            elif filled and has_redeem and stored_pnl < 0 < activity_pnl:
                primary.pnl_usd = round(activity_pnl, 4)
                primary.status = "resolved"
                primary.reconciled_with_polymarket = True
                for t in trades:
                    if t is not primary:
                        t.pnl_usd = 0.0
                        t.reconciled_with_polymarket = True
                        t.status = "resolved"
                verdict = f"FIX:win {activity_pnl:+.4f}"
                fixes += 1

            # --- Three-way diagnostic verdict (G4-22): no silent OK on drift ---
            if verdict is None:
                verdict = _audit_verdict(
                    filled, has_redeem, sold, stored_pnl, activity_pnl
                )

            logger.info(
                f"[audit] {primary.city or primary.group_id} {primary.bin_label} "
                f"stored=${stored_pnl:+.4f} activity=${activity_pnl:+.4f} "
                f"filled={filled} redeem={has_redeem} sold={sold} → {verdict}"
            )

        if fixes:
            session.commit()
            logger.info(f"[audit] Applied {fixes} repair(s) from /activity")
    except Exception as exc:
        logger.error(f"[audit] Job failed: {exc}")
    finally:
        session.close()


def _job_resolve_live_trades() -> None:
    """
    LEGACY: weather-based resolution for trades WITHOUT condition_id.
    For trades that were placed before G2-1 (no Polymarket matching keys).
    New trades use _job_reconcile_pnl instead.

    G4-19: NEVER touch trades that have a token_id — those are reconciled solely
    against /activity by _job_reconcile_pnl. This legacy local-PnL (−stake) path
    is the source of phantom losses (e.g. Lucknow) and must not write PnL for any
    trade with Polymarket matching keys.
    """
    session = get_session()
    notifier = get_notifier()
    try:
        unresolved: list[LiveTrade] = list(
            session.execute(
                select(LiveTrade).where(
                    LiveTrade.status == "filled",
                    LiveTrade.resolved_at.is_(None),
                    LiveTrade.token_id.is_(None),   # only pre-G2-1 trades w/o keys
                )
            ).scalars().all()
        )

        if not unresolved:
            logger.debug("[resolve_live] No unresolved filled trades")
            return

        logger.info(f"[resolve_live] Checking {len(unresolved)} unresolved live trades")

        # Batch by group_id to minimise Gamma API calls
        by_group: dict[str, list[LiveTrade]] = {}
        for t in unresolved:
            by_group.setdefault(t.group_id or t.market_id, []).append(t)

        for group_id, trades in by_group.items():
            try:
                prices = _gamma.get_prices_for_group(group_id)
                if not prices:
                    continue

                for t in trades:
                    price_yes = prices.get(t.market_id)
                    if price_yes is None:
                        continue

                    if price_yes > 0.99:
                        yes_won = True
                    elif price_yes < 0.01:
                        yes_won = False
                    else:
                        continue  # market still live

                    shares = t.size_shares or 0.0
                    stake = t.stake_usd or 0.0
                    bought_yes = (t.strategy_name != "tail_no")

                    if bought_yes:
                        pnl = (shares - stake) if yes_won else -stake
                    else:  # bought NO shares
                        pnl = (shares - stake) if (not yes_won) else -stake

                    t.pnl_usd = round(pnl, 4)
                    t.resolved_at = datetime.utcnow()
                    session.commit()

                    outcome_em = "✅" if pnl > 0 else "❌"
                    city = t.city or group_id[:8]
                    logger.info(
                        f"[resolve_live] {city} {t.bin_label} "
                        f"({'YES' if bought_yes else 'NO'} "
                        f"{'ВЫИГРАл' if pnl > 0 else 'ПРОИГРАл'}) "
                        f"PnL=${pnl:+.2f}"
                    )
                    side_tag = "YES" if bought_yes else "NO"
                    pnl_sign = "+" if pnl >= 0 else "−"
                    notifier.send(
                        f"{outcome_em} {city} {t.bin_label} {side_tag} — "
                        f"закрыта {pnl_sign}${abs(pnl):.2f}"
                    )

            except Exception as e:
                logger.error(f"[resolve_live] Error for group {group_id}: {e}")

    except Exception as e:
        logger.error(f"[resolve_live] Job failed: {e}")
    finally:
        session.close()


def _job_paper_trade_summary() -> None:
    session = get_session()
    try:
        today_start = datetime.combine(date.today(), datetime.min.time())

        open_trades = list(
            session.execute(select(PaperTrade).where(PaperTrade.status == "open"))
            .scalars().all()
        )
        resolved_today = list(
            session.execute(
                select(PaperTrade).where(
                    PaperTrade.resolved_at >= today_start,
                    PaperTrade.status != "open",
                )
            ).scalars().all()
        )

        wins = sum(1 for t in resolved_today if t.status == "resolved_win")
        basket_misses = sum(1 for t in resolved_today if t.status == "resolved_basket_miss")
        losses = len(resolved_today) - wins - basket_misses
        pnl = sum(t.pnl_usd or 0.0 for t in resolved_today)
        rate = f"{wins/len(resolved_today)*100:.0f}%" if resolved_today else "N/A"

        logger.info(
            f"SUMMARY: Open={len(open_trades)} | "
            f"Today: {len(resolved_today)} ({wins}W/{losses}L/{basket_misses}BM) "
            f"PnL=${pnl:+.2f} winrate={rate}"
        )
    except Exception as e:
        logger.error(f"Summary job failed: {e}")
    finally:
        session.close()


def _job_settle_paper() -> None:
    """
    G3-3: Settle open paper trades by Gamma outcome — NOT weather, NOT Data API.

    For each open PaperTrade: check if the event is resolved via Gamma. If yes,
    find the winning market_id (price ≥ 0.99), compute PnL from basket_json
    (shares × $1 - total_cost), and mark resolved.

    Paper trades are simulations — they have no /activity entry on Polymarket.
    Gamma prices are the canonical outcome for resolution.
    """
    import json as _json
    session = get_session()
    notifier = get_notifier()
    try:
        open_trades: list[PaperTrade] = list(
            session.execute(
                select(PaperTrade).where(PaperTrade.status == "open")
            ).scalars().all()
        )

        if not open_trades:
            logger.debug("[settle_paper] No open paper trades")
            return

        logger.info(f"[settle_paper] Checking {len(open_trades)} open paper trades via Gamma")
        resolved_count = 0

        for pt in open_trades:
            if not pt.group_id:
                continue
            if not _gamma.is_resolved_event(pt.group_id):
                continue

            winner_mid = _gamma.winning_market_id(pt.group_id)

            # Compute payout from basket_json
            try:
                basket = _json.loads(pt.basket_json)
            except Exception:
                basket = []

            basket_market_ids = [str(b.get("market_id", "")) for b in basket]
            total_cost = pt.virtual_stake_usd or sum(b.get("stake_usd", 0) for b in basket)

            # Diagnostic: log exact inputs to the match
            logger.info(
                f"[settle_paper] id={pt.id} group={pt.group_id} "
                f"winner_mid={winner_mid!r} "
                f"basket_market_ids={basket_market_ids} "
                f"total_cost=${total_cost:.4f}"
            )

            winning_bin = next(
                (b for b in basket if str(b.get("market_id")) == str(winner_mid)),
                None,
            )

            if winning_bin:
                shares = winning_bin.get("shares") or 0.0
                payout = float(shares) * 1.0
                pnl = round(payout - total_cost, 4)
                status = "resolved_win" if pnl > 0 else "resolved_partial" if payout > 0 else "resolved_loss"
                logger.info(
                    f"[settle_paper] MATCH: bin={winning_bin.get('bin_label')} "
                    f"shares={shares:.4f} payout=${payout:.4f} pnl=${pnl:+.4f}"
                )
            else:
                pnl = round(-total_cost, 4)
                status = "resolved_basket_miss" if winner_mid else "resolved_loss"
                logger.warning(
                    f"[settle_paper] NO MATCH: winner_mid={winner_mid!r} not in basket "
                    f"{basket_market_ids} → status={status} pnl=${pnl:+.2f}"
                )

            pt.pnl_usd = pnl
            pt.status = status
            pt.resolved_at = datetime.utcnow()
            pt.winning_market_id = winner_mid
            pt.resolved_via = "gamma_api"
            resolved_count += 1

            city = pt.city or pt.group_id or "?"
            outcome_em = "✅" if pnl > 0 else "❌"
            logger.info(
                f"[settle_paper] {city} → {status} PnL=${pnl:+.2f} winner={winner_mid}"
            )
            notifier.send(
                f"{outcome_em} [paper] {city} — {status} PnL=${pnl:+.2f}"
            )

        if resolved_count:
            session.commit()
            logger.info(f"[settle_paper] Settled {resolved_count} paper trades via Gamma")

    except Exception as exc:
        logger.error(f"[settle_paper] Job failed: {exc}")
        session.rollback()
    finally:
        session.close()


def _job_cleanup_snapshots() -> None:
    """Weekly cleanup: delete MarketSnapshot rows older than 7 days (P2-13)."""
    from sqlalchemy import delete as sa_delete
    session = get_session()
    try:
        cutoff = datetime.utcnow() - timedelta(days=7)
        result = session.execute(
            sa_delete(MarketSnapshot).where(MarketSnapshot.snapshot_time < cutoff)
        )
        session.commit()
        logger.info(f"[cleanup_snapshots] Deleted {result.rowcount} snapshot rows older than 7 days")
    except Exception as exc:
        logger.error(f"[cleanup_snapshots] Failed: {exc}")
        session.rollback()
    finally:
        session.close()


def _job_daily_summary() -> None:
    """Build and store a full daily PnL report; send to Telegram if configured."""
    logger.info("JOB: daily summary")
    notifier = get_notifier()
    session = get_session()
    try:
        yesterday = date.today() - timedelta(days=1)
        day_start = datetime.combine(yesterday, datetime.min.time())
        day_end = datetime.combine(date.today(), datetime.min.time())

        # ── Paper trades (simulation) ──────────────────────────────────────
        resolved = list(
            session.execute(
                select(PaperTrade).where(
                    PaperTrade.resolved_at >= day_start,
                    PaperTrade.resolved_at < day_end,
                    PaperTrade.status != "open",
                )
            ).scalars().all()
        )
        open_trades = list(
            session.execute(select(PaperTrade).where(PaperTrade.status == "open"))
            .scalars().all()
        )

        wins = sum(1 for t in resolved if t.status == "resolved_win")
        losses = sum(1 for t in resolved if t.status == "resolved_loss")
        basket_misses = sum(1 for t in resolved if t.status == "resolved_basket_miss")
        total_pnl = sum(t.pnl_usd or 0.0 for t in resolved)

        all_resolved = list(
            session.execute(
                select(PaperTrade).where(PaperTrade.status != "open")
            ).scalars().all()
        )
        paper_hwm = sum(t.pnl_usd or 0.0 for t in all_resolved)

        # HWM includes live PnL (P2-18 fix: was paper-only)
        all_live_resolved = list(
            session.execute(
                select(LiveTrade).where(LiveTrade.pnl_usd.isnot(None))
            ).scalars().all()
        )
        live_hwm = sum(t.pnl_usd or 0.0 for t in all_live_resolved)
        hwm = paper_hwm + live_hwm

        # ── Live trades — grouped by placed_at date (G2-2) ────────────────
        # Use placed_at (entry date) not resolved_at: resolution can lag 1-2 days
        live_resolved = list(
            session.execute(
                select(LiveTrade).where(
                    LiveTrade.placed_at >= day_start,
                    LiveTrade.placed_at < day_end,
                    LiveTrade.pnl_usd.isnot(None),
                )
            ).scalars().all()
        )
        live_pnl = sum(t.pnl_usd or 0.0 for t in live_resolved)
        # win/loss by sign of pnl_usd (redemption-cost), not by cash-flow direction (G2-2)
        live_wins = sum(1 for t in live_resolved if (t.pnl_usd or 0.0) > 0)
        live_losses = sum(1 for t in live_resolved if (t.pnl_usd or 0.0) < 0)
        live_staked = sum(t.stake_usd or 0.0 for t in live_resolved)
        live_roi = (live_pnl / live_staked * 100) if live_staked > 0 else 0.0
        all_reconciled = all(t.reconciled_with_polymarket for t in live_resolved)

        # By strategy for live trades
        live_by_strat: dict[str, dict] = {}
        for t in live_resolved:
            s = t.strategy_name or "unknown"
            if s not in live_by_strat:
                live_by_strat[s] = {"pnl": 0.0, "wins": 0, "losses": 0}
            live_by_strat[s]["pnl"] += t.pnl_usd or 0.0
            if (t.pnl_usd or 0.0) > 0:
                live_by_strat[s]["wins"] += 1
            else:
                live_by_strat[s]["losses"] += 1

        live_best = max(live_resolved, key=lambda t: t.pnl_usd or 0.0, default=None)
        live_worst = min(live_resolved, key=lambda t: t.pnl_usd or 0.0, default=None)

        # ── Persist daily summary ──────────────────────────────────────────
        summary_data = {
            "date": str(yesterday),
            "paper_pnl": total_pnl,
            "paper_trades": len(resolved),
            "paper_wins": wins,
            "paper_losses": losses,
            "paper_basket_misses": basket_misses,
            "live_pnl": live_pnl,
            "live_trades": len(live_resolved),
            "live_wins": live_wins,
            "live_losses": live_losses,
            "live_staked": live_staked,
            "live_roi_pct": live_roi,
            "open": len(open_trades),
            "hwm": hwm,
        }

        existing = session.get(DailySummary, yesterday)
        if existing:
            existing.total_pnl = total_pnl
            existing.trades_count = len(resolved)
            existing.wins = wins
            existing.losses = losses
            existing.basket_misses = basket_misses
            existing.open_positions = len(open_trades)
            existing.hwm = hwm
            existing.summary_json = json.dumps(summary_data)
            existing.sent_at = None
        else:
            session.add(DailySummary(
                date=yesterday,
                total_pnl=total_pnl,
                trades_count=len(resolved),
                wins=wins,
                losses=losses,
                basket_misses=basket_misses,
                open_positions=len(open_trades),
                hwm=hwm,
                summary_json=json.dumps(summary_data),
            ))
        session.commit()

        # ── Build Telegram messages (G3-6: per-strategy breakdown) ──────────
        owner_msg = f"📅 <b>Итоги дня: {yesterday}</b> (по дате открытия)\n\n"

        if live_resolved:
            live_staked_total = sum(t.stake_usd or 0.0 for t in live_resolved)
            live_received = live_staked_total + live_pnl
            live_rate = f"{live_wins / len(live_resolved) * 100:.0f}%"
            reconcile_flag = "✅" if all_reconciled else "⚠️ не сверено"
            owner_msg += (
                f"💼 <b>LIVE</b>\n"
                f"Вложено: ${live_staked_total:.2f} | Получено: ${live_received:.2f} | "
                f"Итог: ${live_pnl:+.2f} | Сверено: {reconcile_flag}\n"
            )
            # Per-strategy breakdown (G3-6)
            strat_order = ["basket_wide", "basket_narrow", "tail_no"]
            for strat in strat_order + [s for s in live_by_strat if s not in strat_order]:
                if strat not in live_by_strat:
                    continue
                sv = live_by_strat[strat]
                short = {"basket_wide": "basket", "basket_narrow": "basket_n", "tail_no": "tail"}.get(strat, strat)
                owner_msg += f"  ↳ {short}: ${sv['pnl']:+.2f} ({sv['wins']}W/{sv['losses']}L)\n"
            if live_best and live_best.pnl_usd:
                owner_msg += f"🔝 {live_best.city or '?'} {live_best.bin_label}: ${live_best.pnl_usd:+.2f}\n"
            if live_worst and live_worst.pnl_usd and live_worst is not live_best:
                owner_msg += f"💸 {live_worst.city or '?'} {live_worst.bin_label}: ${live_worst.pnl_usd:+.2f}\n"
        else:
            owner_msg += "💼 <b>LIVE</b>: сделок за день нет\n"

        rate_str = f"{wins / len(resolved) * 100:.0f}%" if resolved else "N/A"
        owner_msg += f"\n📝 <b>PAPER</b> (тест)\n"
        if resolved:
            owner_msg += (
                f"PnL: ${total_pnl:+.2f} ({len(resolved)} сделок)\n"
                f"✅ {wins}W / ❌ {losses}L / 🚫 {basket_misses}BM | Winrate: {rate_str}\n"
            )
        else:
            owner_msg += "Сделок за день нет\n"

        # Client message: only if all trades reconciled with Polymarket (G2-2 gate)
        client_msg: str | None = None
        if live_resolved and all_reconciled:
            live_staked_total = sum(t.stake_usd or 0.0 for t in live_resolved)
            live_received = live_staked_total + live_pnl
            live_rate = f"{live_wins / len(live_resolved) * 100:.0f}%"
            client_msg = (
                f"📅 <b>День {yesterday}</b> (по дате открытия сделок)\n\n"
                f"💼 Вложено: ${live_staked_total:.2f}   Получено: ${live_received:.2f}\n"
                f"💰 Итог: ${live_pnl:+.2f}\n"
                f"Сделок: {len(live_resolved)} | Побед: {live_wins} | Поражений: {live_losses}\n"
                f"Сверено с Polymarket: ✅"
            )

        # Send to owner (all modes)
        sent = notifier.send(owner_msg)
        # Send to client (live mode only, reconciled only)
        from src.config.settings import settings as _s
        if client_msg and _s.trading_mode.lower() == "live":
            _send_to_clients(client_msg)

        if sent:
            if existing:
                existing.sent_at = datetime.utcnow()
            else:
                ds = session.get(DailySummary, yesterday)
                if ds:
                    ds.sent_at = datetime.utcnow()
            session.commit()

        logger.info(
            f"Daily summary {yesterday}: paper {len(resolved)} trades PnL=${total_pnl:+.2f} "
            f"({wins}W/{losses}L/{basket_misses}BM) | "
            f"live {len(live_resolved)} trades PnL=${live_pnl:+.2f} ROI={live_roi:+.1f}%"
        )
    except Exception as e:
        logger.error(f"Daily summary job failed: {e}")
    finally:
        session.close()


def _send_to_clients(msg: str) -> None:
    """Send msg to client chat IDs (G2-4). Silently skips if not configured."""
    from src.config.settings import settings
    raw = settings.client_chat_ids or ""
    chat_ids = [c.strip() for c in raw.split(",") if c.strip()]
    if not chat_ids:
        return
    try:
        import requests as _req
        token = settings.telegram_bot_token
        for cid in chat_ids:
            _req.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": cid, "text": msg, "parse_mode": "HTML"},
                timeout=10,
            )
    except Exception as exc:
        logger.warning(f"[send_to_clients] Failed: {exc}")


def _restore_trading_mode() -> None:
    """G2-4: Restore trading_mode from bot_state DB if it was changed via /mode command."""
    from src.db.models import BotState
    session = get_session()
    try:
        state = session.get(BotState, "trading_mode")
        if state and state.value in ("live", "paper"):
            from src.config.settings import settings
            if settings.trading_mode != state.value:
                settings.trading_mode = state.value
                logger.info(f"[scheduler] Restored trading_mode={state.value} from bot_state DB")
    except Exception as e:
        logger.warning(f"[scheduler] Could not restore trading_mode: {e}")
    finally:
        session.close()


def _check_emergency_stop_on_startup() -> None:
    """
    Auto-clear emergency stops that do NOT require manual intervention:
      - 'manual'     → user restarted intentionally → clear
      - 'daily_loss' → new trading day → clear; same day → keep
      - 'total_loss' → always require /reset_stop (never auto-clear)
    """
    from src.risk.risk_manager import risk_manager

    if not risk_manager.is_emergency_stopped():
        return

    stop_type = risk_manager.get_stop_type()
    notifier = get_notifier()

    if stop_type == "manual":
        risk_manager.clear_emergency_stop()
        logger.info("[startup] Emergency stop cleared (reason=manual, bot restarted intentionally)")
        notifier.send(
            "✅ Emergency stop сброшен после перезапуска (причина: manual). "
            "Бот возобновляет live торговлю."
        )

    elif stop_type == "daily_loss":
        triggered_at = risk_manager.get_stop_triggered_at()
        if triggered_at is None or triggered_at.date() < date.today():
            risk_manager.clear_emergency_stop()
            logger.info("[startup] Emergency stop cleared (reason=daily_loss, new trading day)")
            notifier.send("✅ Emergency stop сброшен — новый торговый день, дневной лимит обнулён.")
        else:
            logger.warning("[startup] Emergency stop ACTIVE (daily_loss today) — live jobs disabled")

    else:  # total_loss or unknown
        logger.critical(
            "[startup] Emergency stop ACTIVE (reason=%s) — live jobs disabled. "
            "Use /reset_stop in Telegram to resume.",
            stop_type or "total_loss",
        )




def setup_scheduler() -> None:
    from src.config.settings import settings
    from src.risk.risk_manager import risk_manager

    _restore_trading_mode()
    _check_emergency_stop_on_startup()  # auto-clear manual/daily_loss stops before registering jobs

    # Log emergency stop state but ALWAYS register live jobs — runtime check is in the job
    if risk_manager.is_emergency_stopped():
        logger.warning(
            "⚠️ Emergency stop is ACTIVE — live job registered but will self-skip until "
            "/reset_stop clears the flag (no restart needed)."
        )

    schedule.every(30).minutes.do(_job_discover_markets)
    schedule.every(10).minutes.do(_job_snapshot_markets)
    schedule.every(60).minutes.do(_job_deactivate_resolved_groups)
    schedule.every(60).minutes.do(_job_fetch_open_meteo)
    schedule.every(15).minutes.do(_job_paper_trade_engine)
    schedule.every(15).minutes.do(_job_live_trade_engine)   # runtime emergency_stop check inside
    # G4-30: tail-exit is NOT registered here — it runs on its own thread
    # (start_tail_exit_thread) so the blocking run_pending() loop can't starve it.
    schedule.every(10).minutes.do(_job_auto_redeem)          # G4-17: redeem stuck wins on-chain
    schedule.every(60).minutes.do(_job_resolve_paper_trades)
    schedule.every(15).minutes.do(_job_settle_paper)   # G3-3: Gamma-based paper settlement
    schedule.every(60).minutes.do(_job_paper_trade_summary)
    schedule.every(2).minutes.do(_job_reconcile_orders)
    schedule.every(15).minutes.do(_job_import_onchain_positions)  # G4-26: DB⟵/activity truth
    schedule.every(15).minutes.do(_job_reconcile_pnl)
    schedule.every(30).minutes.do(_job_audit_activity)      # G4-19: /activity audit + repair
    schedule.every(30).minutes.do(_job_expire_stale_pending)
    schedule.every(30).minutes.do(_job_resolve_live_trades)
    schedule.every().sunday.at("04:00").do(_job_cleanup_snapshots)
    schedule.every().day.at("00:05").do(_job_daily_summary)
    mode = settings.trading_mode.upper()
    logger.info(
        f"Scheduler ready [{mode}]: discover=30 min | snapshots=10 min | "
        "open-meteo=60 min | deactivate=60 min | "
        "paper-trade=15 min | live-trade=15 min (always registered, runtime-gated) | "
        "resolve-paper=60 min | resolve-live=30 min | expire-pending=30 min | "
        "tail-exit=dedicated thread (hot 5 min / normal 10 min, decoupled) | "
        "summary=60 min | daily=00:05 UTC"
    )


def _reset_phantom_paper_losses() -> None:
    """
    One-time cleanup: cancel paper trades whose pnl_usd equals exactly -virtual_stake_usd.
    These are the signature of a basket_miss where the winning_market_id was not found in
    the basket_json — a known settle bug. Sets them to cancelled/pnl=0 so they don't
    inflate the paper_total_loss counter.
    """
    session = get_session()
    try:
        rows: list[PaperTrade] = list(
            session.execute(
                select(PaperTrade).where(
                    PaperTrade.pnl_usd.isnot(None),
                    PaperTrade.status.in_(["resolved_basket_miss", "resolved_loss"]),
                )
            ).scalars().all()
        )
        cleaned = 0
        for pt in rows:
            # Flat loss = stake rounded to 4 decimals — signature of basket_miss path
            if pt.pnl_usd is not None and abs(pt.pnl_usd + (pt.virtual_stake_usd or 0)) < 0.01:
                pt.status = "cancelled"
                pt.pnl_usd = 0.0
                pt.resolved_via = "reset_phantom"
                cleaned += 1
        if cleaned:
            session.commit()
            logger.info(f"[reset_phantom] Cancelled {cleaned} phantom paper losses (pnl reset to $0)")
        else:
            logger.info("[reset_phantom] No phantom paper losses found")
    except Exception as exc:
        logger.warning(f"[reset_phantom] Failed: {exc}")
        session.rollback()
    finally:
        session.close()


def _void_legacy_paper_ledger() -> None:
    """
    G4-21: void paper trades settled BEFORE the ledger fix (G3-3 Gamma settler,
    resolved_via='gamma_api'). Pre-fix settlements (resolved_via NULL /
    'polymarket_api' / 'weather_api') used inflated stakes and the old loss path —
    they pollute paper_loss_calc and falsely pause the paper engine.

    Marks them status='voided_legacy' (excluded from paper_loss_calc). Idempotent:
    re-running skips already-voided rows. Going forward, clean sims settle via
    _job_settle_paper (gamma_api) and count normally.
    """
    session = get_session()
    try:
        rows: list[PaperTrade] = list(
            session.execute(
                select(PaperTrade).where(
                    PaperTrade.pnl_usd.isnot(None),
                    PaperTrade.pnl_usd < 0,
                    PaperTrade.status.like("resolved%"),
                    PaperTrade.status != "voided_legacy",
                    (PaperTrade.resolved_via.is_(None))
                    | (PaperTrade.resolved_via.notin_(["gamma_api"])),
                )
            ).scalars().all()
        )
        voided = 0
        total = 0.0
        for pt in rows:
            total += pt.pnl_usd or 0.0
            pt.status = "voided_legacy"
            voided += 1
        if voided:
            session.commit()
            logger.info(
                f"[void_legacy_paper] Voided {voided} pre-ledger-fix paper losses "
                f"(${total:.2f}) — excluded from paper_loss_calc"
            )
        else:
            logger.info("[void_legacy_paper] No legacy paper trades to void")
    except Exception as exc:
        logger.warning(f"[void_legacy_paper] Failed: {exc}")
        session.rollback()
    finally:
        session.close()


def _compute_tail_tick_seconds() -> int:
    """Base tick for the dedicated tail-exit ticker = the hot poll interval.

    The per-position cadence gate in live_trader spaces hot positions at
    tail_poll_hot_minutes (5) and normal at tail_poll_normal_minutes (10).
    Ticking the thread at the hot interval lets the gate deliver both: hot →
    every tick (~5 min), normal → every other tick (~10 min). Floored so a
    misconfigured 0 can't busy-spin.
    """
    from src.strategy.config import strategy_settings as ss
    return max(
        _TAIL_TICK_FLOOR_SECONDS,
        int(getattr(ss, "tail_poll_hot_minutes", 5)) * 60,
    )


def _tail_exit_loop(base_seconds: float) -> None:
    """G4-30: dedicated tail-exit ticker, independent of schedule.run_pending().

    Waits `base_seconds` between ticks on an interruptible Event (immune to the
    main loop blocking on a 90 s discover job). This fixes tick DELIVERY only —
    it calls the same _job_tail_early_exit(); no exit branch / decide_tail_exit
    logic is touched. Stop via _tail_exit_stop.set().
    """
    logger.info(
        f"[tail_exit] dedicated ticker started — base tick {base_seconds:.0f}s "
        f"(own thread, decoupled from scheduler)"
    )
    while not _tail_exit_stop.wait(base_seconds):
        try:
            _job_tail_early_exit()
        except Exception:
            logger.exception("[tail_exit] dedicated ticker iteration failed")
    logger.info("[tail_exit] dedicated ticker stopped")


def start_tail_exit_thread(base_seconds: "float | None" = None) -> "threading.Thread":
    """Start the decoupled tail-exit ticker and return the thread (daemon)."""
    global _tail_exit_thread
    _tail_exit_stop.clear()
    base = base_seconds if base_seconds is not None else _compute_tail_tick_seconds()
    _tail_exit_thread = threading.Thread(
        target=_tail_exit_loop, args=(base,), name="tail-exit-ticker", daemon=True
    )
    _tail_exit_thread.start()
    return _tail_exit_thread


def stop_tail_exit_thread(timeout: float = 5.0) -> None:
    """Signal the ticker to stop and join it (best-effort)."""
    _tail_exit_stop.set()
    if _tail_exit_thread is not None:
        _tail_exit_thread.join(timeout)


def run_initial_jobs() -> None:
    logger.info("Running initial data collection on startup...")
    _reset_phantom_paper_losses()    # cancel flat -stake losses from broken settle path
    _void_legacy_paper_ledger()      # G4-21: void pre-ledger-fix paper losses (phantom $65)
    _job_expire_stale_pending()
    _job_discover_markets()
    _job_fetch_open_meteo()
    _job_snapshot_markets()
    _job_resolve_paper_trades()
    _job_settle_paper()         # G3-3: Gamma paper settlement on startup
    # Order matters after downtime: sell at bid FIRST (best outcome), then
    # reconcile marks remaining wins pending_redeem, then redeem the no-bid ones.
    _job_tail_early_exit()     # G4-15: capture early-exit window missed overnight
    _job_import_onchain_positions()  # G4-26: DB⟵/activity (dry-run until verified)
    _job_reconcile_pnl()       # G3-2: finalize sells + mark wins pending_redeem
    _job_auto_redeem()         # G4-17: redeem stuck wins (no bid / market closed)
    _job_audit_activity()      # G4-19: repair phantom/win-as-loss vs /activity
    _job_resolve_live_trades()
    _job_paper_trade_engine()
    _job_live_trade_engine()
    _job_paper_trade_summary()


def run_forever() -> None:
    setup_scheduler()
    run_initial_jobs()
    # G4-30: tail-exit on its own thread so heavy jobs in this loop can't starve it.
    start_tail_exit_thread()
    logger.info("Entering scheduler loop — Ctrl+C / Shift+F5 to stop")
    while True:
        schedule.run_pending()
        time.sleep(30)
