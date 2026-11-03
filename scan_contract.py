"""Pure data contracts. No network, credentials, publishing or notification effects."""
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import math
import os
from pathlib import Path
import sys
import tempfile

from scan_diagnostics import emit_scan_failure

TIMEZONES = {"US": "America/New_York", "KR": "Asia/Seoul"}
UNIVERSE_ERROR_CODES = frozenset({
    'universe_unavailable', 'incomplete_us_universe', 'incomplete_korean_universe',
    'kr_universe_fetch_failed', 'kr_universe_invalid_json', 'kr_universe_invalid_contract',
    'kr_universe_incomplete', 'kr_universe_invalid_order',
})


class SourceDataError(ValueError):
    pass


@dataclass
class Observation:
    stock: dict | None
    quote_as_of: str


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def price(value, market):
    value = number(value)
    if value is None or value <= 0:
        return None
    digits = 0 if market == "KR" else (4 if value < 1 else 2)
    rounded = float(Decimal(str(value)).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))
    return rounded if rounded > 0 else None


def utc_stamp(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def score_inputs_available(stock):
    """Independent rules may be absent; optional financials are not a prerequisite.

    Legacy rows expose RSI/RVOL only. New rows also expose the actual other
    calculated inputs, so missing RSI does not hide a valid MA/MACD evaluation.
    """
    rsi, rvol = number(stock.get('rsi')), number(stock.get('rvol'))
    inputs = stock.get('scoreInputs')
    inputs = inputs if isinstance(inputs, dict) else {}
    stoch = number(inputs.get('stochK'))
    return ((rsi is not None and 0 <= rsi <= 100) or
            (rvol is not None and rvol >= 0) or
            any(number(inputs.get(key)) is not None and number(inputs[key]) > 0
                for key in ('bbLower', 'ma60', 'ma120')) or
            (number(inputs.get('macd')) is not None and number(inputs.get('signal')) is not None) or
            (stoch is not None and 0 <= stoch <= 100))


def rank_stocks(rows, limit=10):
    """Stable ties: exact score descending, then ticker ascending."""
    ordered = sorted(deepcopy(rows), key=lambda s: (-(number(s.get("score")) or 0), s["id"]))[:limit]
    for i, stock in enumerate(ordered, 1):
        stock["rank"] = i
        stock["analysisMethod"] = "rules_v1"
        if (number(stock.get("score")) or 0) < 40:
            stock["aiReason"] = "기준 점수 미달: 참고 후보 + " + stock.get("aiReason", "")
    return ordered


def scan_market(market, get_universe, analyze, previous, generated_at):
    old_stocks = [s for s in previous.get("stocks", []) if s.get("market") == market]
    old_meta = previous.get("markets", {}).get(market, {})
    errors, filtered, observations, candidates = 0, 0, [], []
    universe_error = None
    try:
        universe = sorted(set(get_universe()))
        if not universe:
            raise SourceDataError("universe_unavailable")
    except Exception as exc:
        universe, errors = [], 1
        # Publish a bounded diagnostic, never arbitrary HTTP bodies or exception text.
        universe_error = str(exc) if isinstance(exc, SourceDataError) and str(exc) in UNIVERSE_ERROR_CODES else 'universe_unavailable'
        emit_scan_failure(exc, market=market, ticker=None, stage='universe',
                          generated_at=generated_at, source_error_type=SourceDataError)
    for ticker in universe:
        diagnostic_stage = 'stock_analysis'
        try:
            observation = analyze(ticker, market)
            diagnostic_stage = 'observation_validation'
            if not isinstance(observation, Observation):
                raise SourceDataError("invalid_observation")
            diagnostic_stage = 'quote_date_validation'
            quote_date = date.fromisoformat(observation.quote_as_of[:10])
            diagnostic_stage = 'quote_freshness'
            age = (date.fromisoformat(generated_at[:10]) - quote_date).days
            if age < 0 or age > 4:
                raise SourceDataError("stale_quote")
            if observation.stock is None:
                filtered += 1
            else:
                diagnostic_stage = 'price_validation'
                if price(observation.stock.get("currentPrice"), market) is None:
                    raise SourceDataError("invalid_price")
                diagnostic_stage = 'score_validation'
                score = number(observation.stock.get('score'))
                if score is None or score < 0 or not score_inputs_available(observation.stock):
                    raise SourceDataError('score_inputs_unavailable')
                candidates.append(observation.stock)
            observations.append(observation.quote_as_of)
        except Exception as exc:
            errors += 1
            emit_scan_failure(exc, market=market, ticker=ticker, stage=diagnostic_stage,
                              generated_at=generated_at, source_error_type=SourceDataError)
    if errors and old_stocks:
        # A partial scan must not silently replace the full last-good universe.
        rows = deepcopy(old_stocks)
        meta = {**old_meta, "generatedAt": old_meta.get("generatedAt"),
                "quoteAsOf": old_meta.get("quoteAsOf"), "stale": True,
                "status": "partial" if observations else "error"}
    else:
        rows = rank_stocks(candidates)
        for row in rows:
            row["generatedAt"] = generated_at
            row["scoreAsOf"] = generated_at
            row["recommendationId"] = f"{market}:{row['id']}:{generated_at}"
        meta = {"generatedAt": generated_at, "quoteAsOf": min(observations) if observations else None,
                "stale": bool(errors), "status": ("partial" if observations else "error") if errors else ("fresh" if rows else "empty")}
    meta.update(timezone=TIMEZONES[market], attemptedAt=generated_at, errorCount=errors,
                universeError=universe_error,
                attemptedCount=len(universe), observedCount=len(observations), filteredCount=filtered,
                quotePrecision="session-date", freshnessLimitCalendarDays=4)
    return rows, meta


def merge_daily(previous, updates, generated_at):
    markets = deepcopy(previous.get("markets", {}))
    stocks = []
    for market in TIMEZONES:
        if market in updates:
            rows, markets[market] = updates[market]
        else:
            rows = [s for s in previous.get("stocks", []) if s.get("market") == market]
            markets.setdefault(market, {"status": "unverified", "quoteAsOf": None, "generatedAt": None,
                                      "timezone": TIMEZONES[market], "stale": True, "errorCount": 0})
        stocks.extend(deepcopy(rows))
    return {"schemaVersion": 2, "stocks": stocks, "markets": markets,
            "generatedAt": generated_at, "timestamp": generated_at, "analysisMethod": "rules_v1",
            "methodology": "규칙 기반 기술·재무 스크리닝. 기준 가격은 예측·수익 보장이 아닙니다.",
            "market_status": deepcopy(previous.get("market_status", {})), "dominant_sectors": []}


def daily_notification(updates):
    parts = []
    for market, (rows, meta) in updates.items():
        name = "미국" if market == "US" else "한국"
        if meta["status"] in {"error", "partial", "stale"}:
            parts.append(f"{name}: 데이터 수집 오류 {meta['errorCount']}건. 최신 추천 확인 불가")
        elif not rows:
            parts.append(f"{name}: 이번 규칙 조건에 해당하는 종목 없음")
        else:
            parts.append(f"{name} 규칙 기반 후보: " + ", ".join(s["symbol"] for s in rows[:2]))
    return {"title": "DailyPick10 데이터 업데이트", "body": " / ".join(parts)}


def cohort_from_snapshots(snapshots, start_date=None, end_date=None):
    """Keep all recommendation events; carried-forward events are counted once."""
    cohort, seen = [], set()
    for source_file, payload in snapshots:
        for stock in payload.get("stocks", []):
            market = stock.get("market")
            if market not in TIMEZONES:
                continue
            key = stock.get("recommendationId") or f"legacy:{source_file}:{market}:{stock.get('id')}"
            event_date = (stock.get("generatedAt") or source_file.split("_")[0])[:10]
            if (start_date and event_date < start_date) or (end_date and event_date > end_date):
                continue
            if key in seen:
                continue
            seen.add(key)
            item = deepcopy(stock)
            item.update(recommendationId=key, buyPrice=number(stock.get("currentPrice")),
                        recommendDate=(stock.get("generatedAt") or source_file.split("_")[0])[:10],
                        sourceFile=source_file, returnRate=None, returnStatus="unavailable")
            cohort.append(item)
    return cohort


def settle_cohort(cohort, get_exit_quote, target_date):
    """Descriptive price change only, not an executable fixed-horizon backtest."""
    results = []
    for source in cohort:
        item = deepcopy(source)
        item["returnRate"] = None
        item["returnStatus"] = "unavailable"
        entry = number(item.get("buyPrice"))
        if item.get("priceBasis") != "unadjusted_close":
            item["returnStatus"] = "unverified_legacy_basis"
        elif entry is None or entry <= 0:
            item["returnStatus"] = "missing_entry_price"
        else:
            try:
                quote = get_exit_quote(item, target_date)
                exit_price = number(quote.get("price"))
                exit_date = date.fromisoformat(quote["quoteAsOf"][:10])
                entry_date = date.fromisoformat(item["recommendDate"][:10])
                if exit_date > date.fromisoformat(target_date) or exit_date < entry_date:
                    raise SourceDataError("invalid_exit_date")
                if exit_price is None or exit_price <= 0:
                    raise SourceDataError("missing_exit_price")
                if quote.get("hasSplit"):
                    item["returnStatus"] = "corporate_action_requires_adjustment"
                else:
                    item.update(currentPrice=price(exit_price, item["market"]), exitQuoteAsOf=quote["quoteAsOf"],
                                returnRate=round((exit_price - entry) / entry * 100, 4),
                                returnStatus="observed", holdingCalendarDays=(exit_date - entry_date).days)
            except Exception:
                item["returnStatus"] = "price_fetch_failed"
        results.append(item)
    return results


def summarize_results(rows, market):
    all_rows = [s for s in rows if s.get("market") == market]
    observed = [s for s in all_rows if number(s.get("returnRate")) is not None]
    return {"totalCount": len(all_rows), "observedCount": len(observed), "missingCount": len(all_rows) - len(observed),
            "averageObservedReturn": sum(s["returnRate"] for s in observed) / len(observed) if observed else None,
            "complete": len(all_rows) == len(observed), "weighting": "equal_recommendation_event"}


def build_report(rows, report_date, generated_at):
    return {"schemaVersion": 2, "reportDate": report_date, "timestamp": generated_at, "generatedAt": generated_at,
            "stocks": rows, "market_status": {}, "dominant_sectors": [],
            "performance": {m: summarize_results(rows, m) for m in TIMEZONES},
            "winnerRankings": {m: [s["recommendationId"] for s in sorted(
                [s for s in rows if s.get("market") == m and number(s.get("returnRate")) is not None],
                key=lambda s: (-s["returnRate"], s["recommendationId"]))[:10]] for m in TIMEZONES},
            "methodology": "최근 14일 추천 건별 관측 가격 변화. 보유기간은 건별로 다르며, 미관측 건은 평균에서 제외하고 개수를 표시합니다. 수수료·슬리피지·배당·실제 체결 미반영. 검증된 백테스트가 아닙니다.",
            "legacyHistoryRecalculated": False}


def _publish_json(path, data, exclusive):
    path = Path(path)
    serialized = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        # Unique staging in the same directory avoids cross-filesystem moves and
        # collisions between concurrent writers. The public name is never partial.
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         dir=path.parent, prefix='.' + path.name + '.',
                                         suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        if exclusive:
            # Atomic create-without-replacement. Existing historical bytes win,
            # including when another writer publishes after the existence check.
            if sys.platform == 'win32':
                # Windows rename fails if the destination exists. Unlike POSIX
                # rename it cannot overwrite a report; no hardlink permission needed.
                os.rename(temporary, path)
            else:
                os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_json_once(path, data):
    _publish_json(path, data, exclusive=True)


def write_latest(path, data):
    _publish_json(path, data, exclusive=False)
