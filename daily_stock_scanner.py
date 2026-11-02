import yfinance as yf
import pandas as pd
import json
import time
import ssl
import requests
import xml.etree.ElementTree as ET
import math
import os
import sys
import re
import argparse 
import random
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from scan_contract import (Observation, SourceDataError, TIMEZONES, number, price, utc_stamp, scan_market, merge_daily, daily_notification, cohort_from_snapshots, settle_cohort, build_report, write_json_once, write_latest)
from weekly_contract import REPORT_KIND, latest_publication, weekly_period, assert_publishable, weekly_cohort, settle_weekly, build_weekly_report, validate_frozen_report
from market_calendar import evaluation_session, is_market_session
from notification_policy import push_eligible
from korean_universe import load_korean_universe
from io import StringIO

# [추가] Firebase 라이브러리 (여러 사람에게 알림 보내기 위해 필요)
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False
    print("⚠️ firebase-admin 모듈이 없습니다. DB 연동 기능이 제한됩니다.")

# Keep standard TLS certificate validation enabled.

# 폴더 경로 상수 정의
DAILY_DATA_DIR = 'daily_data'
WEEKLY_REPORT_DIR = 'weekly_reports'

# --- [안전장치] ---
def safe_float(val, default=0.0):
    try:
        if val is None or val == "" or str(val).strip() == "-": return default
        f = float(val)
        if math.isnan(f) or math.isinf(f): return default
        return f
    except: return default

# --- [네트워크 요청 재시도 함수] ---
def fetch_with_retry(url, headers=None, retries=3, delay=2):
    """
    네트워크 요청 실패 시 재시도하는 함수
    """
    if headers is None:
        # 일반적인 브라우저 헤더 (차단 방지용)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5"
        }
    
    for i in range(retries):
        try:
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code == 200:
                return response
            else:
                print(f"⚠️ 요청 실패 ({response.status_code}): {url} (재시도 {i+1}/{retries})")
        except Exception as e:
            print(f"⚠️ 연결 에러: {e} (재시도 {i+1}/{retries})")
        
        time.sleep(delay + random.random()) # 랜덤 딜레이 추가
    
    return None

# Publishing is handled only by the workflow after successful generation.

# --- [추가] 2. DB에서 모든 사용자 토큰 가져오기 ---
def get_all_user_tokens():
    if not FIREBASE_AVAILABLE:
        print("⚠️ [경고] firebase-admin 라이브러리 미설치. 알림 발송을 생략합니다.")
        return []
    
    tokens = []
    try:
        # Firebase 접속 시도
        if not firebase_admin._apps:
            # GitHub Secrets에 등록된 키 사용
            fb_creds_json = os.environ.get('FIREBASE_CREDENTIALS')
            
            if fb_creds_json:
                try:
                    # JSON 문자열을 딕셔너리로 변환 후 인증
                    cred_dict = json.loads(fb_creds_json)
                    cred = credentials.Certificate(cred_dict)
                    firebase_admin.initialize_app(cred)
                    print("🔥 Firebase 인증 성공 (환경변수)")
                except Exception as e:
                    print(f"❌ Firebase 인증/구성 실패: {type(e).__name__}")
                    return []
            else:
                print("⚠️ [경고] FIREBASE_CREDENTIALS 환경변수가 없습니다. 알림 발송을 생략합니다.")
                return [] 
        
        # 'users' 목록에서 토큰만 쏙쏙 뽑아오기
        db = firestore.client()
        users_ref = db.collection('users')
        docs = users_ref.stream()
        
        for doc in docs:
            user_data = doc.to_dict()
            if not push_eligible(user_data):
                continue
            token = user_data.get('pushToken')
            if isinstance(token, str) and re.fullmatch(r'(?:Exponent|Expo)PushToken\[[^\s\[\]]+\]', token):
                tokens.append(token)
                
        print(f"🔎 DB에서 사용자 {len(tokens)}명을 찾았습니다.")
        return tokens
        
    except Exception as e:
        print(f"❌ DB 조회 실패: {type(e).__name__}")
        return []

# --- [수정] 3. 여러 사람에게 알림 보내기 ---
def send_push_notification(title, message):
    # Only tokens already accepted by the DB eligibility filter are recipients.
    # No hardcoded recipient may bypass deletion exclusion or failed DB lookup.
    db_tokens = get_all_user_tokens()
    user_push_tokens = list(dict.fromkeys(db_tokens))

    if not user_push_tokens:
        print(f"⚠️ 전송할 토큰이 없습니다.")
        return

    url = "https://exp.host/--/api/v2/push/send"
    headers = {
        "host": "exp.host",
        "accept": "application/json",
        "accept-encoding": "gzip, deflate",
        "content-type": "application/json"
    }

    print(f"📨 알림 발송 시작: '{title}' (총 {len(user_push_tokens)}명에게)")
    
    # Check service acceptance; this does not prove delivery to a device.
    failures = 0
    for index, token in enumerate(user_push_tokens, 1):
        payload = {
            "to": token,
            "title": title,
            "body": message,
            "data": { "title": title, "message": message }, # 앱이 내용을 읽을 수 있게 데이터 추가
            "sound": "default",
            "priority": "high",
            "channelId": "default", 
            "badge": 1,
            "_displayInForeground": True
        }

        try:
            response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=15)
            response.raise_for_status()
            body = response.json()
            ticket = body.get('data') if isinstance(body, dict) else None
            if isinstance(ticket, list) and len(ticket) == 1:
                ticket = ticket[0]
            if (not isinstance(ticket, dict) or ticket.get('status') != 'ok' or
                    not isinstance(ticket.get('id'), str) or not ticket['id'] or body.get('errors')):
                raise SourceDataError('push_ticket_rejected')
        except Exception as e:
            failures += 1
            print(f"  ❌ 알림 접수 실패 #{index}: {type(e).__name__}")
    if failures:
        # No automatic retry: an uncertain response may already have been accepted.
        raise SourceDataError(f'push_submission_failed:{failures}/{len(user_push_tokens)}')

# --- [어제 추천 종목 가져오기] ---
def get_latest_recommendation_ids():
    if not os.path.exists(DAILY_DATA_DIR): return set()
    files = sorted([f for f in os.listdir(DAILY_DATA_DIR) if f.endswith('_daily.json')], reverse=True)
    if not files: return set()
    try:
        with open(f"{DAILY_DATA_DIR}/{files[0]}", 'r', encoding='utf-8') as f:
            data = json.load(f)
            return {s['id'] for s in data.get('stocks', [])}
    except:
        return set()

# --- [어제 추천 종목 가져오기 (날짜 기준, 백필용)] ---
def get_previous_recommendation_ids(target_date_str):
    if not os.path.exists(DAILY_DATA_DIR): return set()
    
    target_date = datetime.strptime(target_date_str, "%Y-%m-%d")
    files = sorted([f for f in os.listdir(DAILY_DATA_DIR) if f.endswith('_daily.json')], reverse=True)
    
    for f in files:
        file_date_str = f.split('_')[0]
        try:
            file_date = datetime.strptime(file_date_str, "%Y-%m-%d")
            if file_date < target_date:
                with open(f"{DAILY_DATA_DIR}/{f}", 'r', encoding='utf-8') as file:
                    data = json.load(file)
                    return {s['id'] for s in data.get('stocks', [])}
        except:
            continue
    return set()

# --- [1] 시장 상황 분석 ---
def analyze_market_condition(target_date=None):
    print("🌍 글로벌 시장 상황 분석 중...")
    markets = {'US': {'ticker': '^GSPC', 'name': 'S&P 500'}, 'KR': {'ticker': '^KS11', 'name': 'KOSPI'}, 'VIX': {'ticker': '^VIX', 'name': '공포지수'}}
    market_status = {}
    for key, info in markets.items():
        try:
            ticker = yf.Ticker(info['ticker'])
            hist = ticker.history(period="2y", auto_adjust=False) 
            
            if target_date:
                hist.index = hist.index.tz_localize(None) 
                hist = hist[hist.index.strftime('%Y-%m-%d') <= target_date]

            if len(hist) < 2:
                market_status[key] = {'status': 'UNKNOWN', 'change': None, 'current': None, 'message': '데이터 없음'}
                continue
                
            current = hist['Close'].iloc[-1]; prev = hist['Close'].iloc[-2]
            change_pct = ((current - prev) / prev) * 100
            status, message = "NEUTRAL", ""
            
            if key == 'VIX':
                if current >= 30: status, message = "PANIC", "극도의 공포 😱"
                elif current >= 20: status, message = "BAD", "공포 구간 😨"
                elif current <= 15: status, message = "VERY_GOOD", "시장 과열 🤑"
                else: status, message = "NEUTRAL", "안정적 😌"
            else:
                if change_pct >= 1.0: status, message = "VERY_GOOD", "강한 상승 🔥"
                elif change_pct >= 0.2: status, message = "GOOD", "상승세 📈"
                elif change_pct > -0.5: status, message = "NEUTRAL", "보합세 ➖"
                elif change_pct > -1.5: status, message = "BAD", "하락세 ☁️"
                else: status, message = "PANIC", "폭락 경고 ⛈️"
                
            market_status[key] = {'name': info['name'], 'current': safe_float(round(current, 2)), 'change': safe_float(round(change_pct, 2)), 'status': status, 'message': message}
        except Exception as e: 
            print(f"⚠️ {key} 지수 분석 실패: {e}")
            market_status[key] = {'status': 'UNKNOWN', 'change': None, 'current': None, 'message': '데이터 수집 실패'}
    return market_status

# --- [2] 종목 리스트 (다중 백업 소스 적용 및 하드코딩 제거) ---
def get_sp500_tickers():
    tickers = []
    
    # 1순위: 위키피디아 시도
    try:
        print("🔍 [S&P 500] 1순위: 위키피디아 시도...")
        url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
        response = fetch_with_retry(url)
        if response:
            table = pd.read_html(StringIO(response.text))
            tickers = table[0]['Symbol'].tolist()
            return [t.replace('.', '-') for t in tickers]
    except Exception as e:
        print(f"⚠️ 위키피디아 실패: {e}")

    # 2순위: GitHub Datasets 시도 (오픈 데이터셋 사용)
    try:
        print("🔄 [S&P 500] 2순위: GitHub 데이터셋 시도...")
        url = 'https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv'
        response = fetch_with_retry(url)
        if response:
            df = pd.read_csv(StringIO(response.text))
            tickers = df['Symbol'].tolist()
            return [t.replace('.', '-') for t in tickers]
    except Exception as e:
        print(f"⚠️ GitHub 데이터셋 실패: {e}")

    # 3순위: Slickcharts 시도
    try:
        print("🔄 [S&P 500] 3순위: Slickcharts 시도...")
        url = 'https://www.slickcharts.com/sp500'
        response = fetch_with_retry(url)
        if response:
            tables = pd.read_html(StringIO(response.text))
            tickers = tables[0]['Symbol'].tolist()
            return [t.replace('.', '-') for t in tickers]
    except Exception as e:
        print(f"⚠️ Slickcharts 실패: {e}")

    # 4순위: StockAnalysis.com 시도
    try:
        print("🔄 [S&P 500] 4순위: StockAnalysis 시도...")
        url = 'https://stockanalysis.com/list/sp-500-stocks/'
        response = fetch_with_retry(url)
        if response:
            dfs = pd.read_html(StringIO(response.text))
            for df in dfs:
                if 'Symbol' in df.columns:
                    tickers = df['Symbol'].tolist()
                    return [t.replace('.', '-') for t in tickers]
    except Exception as e:
        print(f"⚠️ StockAnalysis 실패: {e}")

    print("🚨 [S&P 500] 모든 데이터 소스 연결 실패. 종목을 가져올 수 없습니다.")
    return []

def get_nasdaq100_tickers():
    tickers = []
    
    # 1순위: 위키피디아 시도
    try:
        print("🔍 [NASDAQ 100] 1순위: 위키피디아 시도...")
        url = 'https://en.wikipedia.org/wiki/Nasdaq-100'
        response = fetch_with_retry(url)
        if response:
            tables = pd.read_html(StringIO(response.text))
            for table in tables:
                if 'Ticker' in table.columns:
                    return [str(t).replace('.', '-') for t in table['Ticker'].tolist()]
                elif 'Symbol' in table.columns:
                    return [str(t).replace('.', '-') for t in table['Symbol'].tolist()]
    except Exception as e:
        print(f"⚠️ 위키피디아 실패: {e}")

    # 2순위: Slickcharts 시도
    try:
        print("🔄 [NASDAQ 100] 2순위: Slickcharts 시도...")
        url = 'https://www.slickcharts.com/nasdaq100'
        response = fetch_with_retry(url)
        if response:
            tables = pd.read_html(StringIO(response.text))
            tickers = tables[0]['Symbol'].tolist()
            return [t.replace('.', '-') for t in tickers]
    except Exception as e:
        print(f"⚠️ Slickcharts 실패: {e}")

    # 3순위: StockAnalysis.com 시도
    try:
        print("🔄 [NASDAQ 100] 3순위: StockAnalysis 시도...")
        url = 'https://stockanalysis.com/list/nasdaq-100-stocks/'
        response = fetch_with_retry(url)
        if response:
            dfs = pd.read_html(StringIO(response.text))
            for df in dfs:
                if 'Symbol' in df.columns:
                    tickers = df['Symbol'].tolist()
                    return [t.replace('.', '-') for t in tickers]
    except Exception as e:
        print(f"⚠️ StockAnalysis 실패: {e}")

    print("🚨 [NASDAQ 100] 모든 데이터 소스 연결 실패. 종목을 가져올 수 없습니다.")
    return []

def get_korea_tickers():
    # Same market-cap top 50 per exchange. Never use list prices for scoring.
    print("🔍 [KR] 네이버 시가총액 목록 확인: 코스피 50 + 코스닥 50")
    try:
        tickers = load_korean_universe(fetch_with_retry)
    except Exception:
        # Static warning only; structured allowlisted detail is stored in markets.KR.
        print("::warning title=KR universe unavailable::Korean market-cap list validation failed; latest recommendations are unavailable.")
        raise
    print(f"✅ [KR] 한국 대상 종목 {len(tickers)}개 확인")
    return tickers

# --- [신규] 네이버 금융 재무 데이터 크롤링 ---
def get_kr_fundamental(ticker):
    try:
        code = ticker.split('.')[0] # 005930.KS -> 005930
        url = f"https://finance.naver.com/item/main.naver?code={code}"
        
        # 네이버 금융은 EUC-KR 사용
        response = fetch_with_retry(url)
        if not response: return None
        
        dfs = pd.read_html(StringIO(response.content.decode('euc-kr', 'replace')))
        
        fin_df = None
        for df in dfs:
            if df.shape[1] > 1 and '영업이익률' in str(df.iloc[:, 0].values):
                fin_df = df
                break
        
        if fin_df is None: return None

        fin_df.set_index(fin_df.columns[0], inplace=True)
        target_col = fin_df.columns[-1] 

        def get_val(row_name):
            try:
                rows = fin_df[fin_df.index.str.contains(row_name, na=False)]
                if len(rows) > 0:
                    val = rows.iloc[0][target_col]
                    return safe_float(val, None) 
                return None
            except: return None

        op_margin = get_val('영업이익률')
        per = get_val('PER')
        pbr = get_val('PBR')
        
        return {
            "op_margin": op_margin / 100.0 if op_margin is not None else None, 
            "per": per,
            "pbr": pbr,
            "rev_growth": None 
        }

    except Exception as e:
        return None

# --- [3] 뉴스 수집 ---
def get_news_from_google_kr(ticker):
    try:
        url = f"https://news.google.com/rss/search?q={ticker.split('.')[0]}+주가&hl=ko&gl=KR&ceid=KR:ko"
        response = fetch_with_retry(url); 
        if not response: return []
        root = ET.fromstring(response.content)
        return [{'title': item.find('title').text, 'link': item.find('link').text, 'publisher': item.find('source').text} for item in root.findall('./channel/item')[:3]]
    except: return []

def get_news_from_google_us(ticker):
    try:
        url = f"https://news.google.com/rss/search?q={ticker}+stock&hl=en-US&gl=US&ceid=US:en"
        response = fetch_with_retry(url);
        if not response: return []
        root = ET.fromstring(response.content)
        return [{'title': item.find('title').text, 'link': item.find('link').text, 'publisher': item.find('source').text} for item in root.findall('./channel/item')[:3]]
    except: return []

def analyze_news_sentiment(news_list):
    analyzed = []
    pos = ['surge', 'jump', 'soar', 'gain', 'profit', 'buy', 'growth', 'record', 'upgrade', '급등', '상승', '호재', '증가', '개선', '매수', '체결', '성장', '최고']
    neg = ['drop', 'fall', 'plunge', 'loss', 'miss', 'sell', 'crash', 'downgrade', 'lawsuit', '급락', '하락', '악재', '감소', '부진', '매도', '적자', '우려', '소송']
    for n in news_list:
        t = n.get('title', '').strip(); 
        if not t: continue
        t_l = t.lower(); sent = 'neutral'
        p_sc = sum(1 for w in pos if w in t_l); n_sc = sum(1 for w in neg if w in t_l)
        if p_sc > n_sc: sent = 'positive'
        elif n_sc > p_sc: sent = 'negative'
        analyzed.append({'title': t, 'link': n.get('link',''), 'sentiment': sent, 'publisher': n.get('publisher','News')})
    return analyzed

def process_news_for_list(stock_list):
    if not stock_list: return
    print(f"\n📰 Top {len(stock_list)} 뉴스 수집 중...")
    for item in stock_list:
        ticker = item['id']; mkt = item['market']
        print(f"   [{mkt}] {ticker}...", end=' '); raw = []
        try:
            if mkt == 'KR': raw = get_news_from_google_kr(ticker)
            else: raw = get_news_from_google_us(ticker)
        except: pass
        print(f"{len(raw)}개"); item['news'] = analyze_news_sentiment(raw[:3])

# --- [4] 지표 계산 ---
def calculate_indicators(close, high, low):
    delta = close.diff(); gain = (delta.where(delta > 0, 0)).rolling(14).mean(); loss = (-delta.where(delta < 0, 0)).rolling(14).mean(); rs = gain/loss; rsi = 100 - (100/(1+rs))
    exp1 = close.ewm(span=12).mean(); exp2 = close.ewm(span=26).mean(); macd = exp1 - exp2; signal = macd.ewm(span=9).mean()
    ma20 = close.rolling(20).mean(); std = close.rolling(20).std(); upper = ma20 + (std*2); lower = ma20 - (std*2)
    lowest_low = low.rolling(window=14).min()
    highest_high = high.rolling(window=14).max()
    stoch_k = 100 * ((close - lowest_low) / (highest_high - lowest_low))
    stoch_d = stoch_k.rolling(window=3).mean()
    return rsi, macd, signal, upper, lower, ma20, stoch_k, stoch_d

# --- [5] 개별 종목 분석 ---
def analyze_stock(ticker, market_type, target_date=None):
    try:
        stock = yf.Ticker(ticker)
        try: hist = stock.history(period="2y", auto_adjust=False)
        except Exception as exc: raise SourceDataError("quote_fetch_failed") from exc
        
        if target_date:
            target_dt = datetime.strptime(target_date, "%Y-%m-%d")
            hist.index = hist.index.tz_localize(None)
            hist = hist[hist.index.strftime('%Y-%m-%d') <= target_date]

        if hist.empty: raise SourceDataError("missing_quote_history")
        quote_as_of = hist.index[-1].strftime("%Y-%m-%d")
        if number(hist['Close'].iloc[-1]) is None or hist['Close'].iloc[-1] <= 0:
            raise SourceDataError("invalid_quote_price")
        # A valid short trading history fails strategy eligibility, not data collection.
        if len(hist) < 120: return Observation(None, quote_as_of)
        
        info = {}
        try: info = stock.info 
        except: pass
        
        op_margin = info.get('operatingMargins')
        rev_growth = info.get('revenueGrowth')
        per = info.get('forwardPE') if info.get('forwardPE') is not None else info.get('trailingPE')
        pbr = info.get('priceToBook')

        if market_type == 'KR':
            kr_fund = get_kr_fundamental(ticker)
            if isinstance(kr_fund, dict):
                # Missing primary fields must not erase available fallback data.
                # In particular, losing a known negative margin bypasses the KR
                # loss filter below. A real zero remains a valid primary value.
                if number(kr_fund.get('op_margin')) is not None: op_margin = kr_fund['op_margin']
                if number(kr_fund.get('per')) is not None: per = kr_fund['per']
                if number(kr_fund.get('pbr')) is not None: pbr = kr_fund['pbr']
        
        op_margin, rev_growth, per, pbr = map(number, (op_margin, rev_growth, per, pbr))
        if market_type == 'KR' and op_margin is not None and op_margin < 0: return Observation(None, quote_as_of)
        if market_type == 'US' and op_margin is not None and op_margin < -0.5: return Observation(None, quote_as_of)
        
        close = hist['Close']; volume = hist['Volume']; high = hist['High']; low = hist['Low']
        rsi, macd, signal, bb_upper, bb_lower, ma20, stoch_k, stoch_d = calculate_indicators(close, high, low)
        
        cur_p = close.iloc[-1]; cur_rsi = rsi.iloc[-1]; cur_low = bb_lower.iloc[-1]
        ma60 = close.rolling(60).mean().iloc[-1]
        ma120 = close.rolling(120).mean().iloc[-1]
        cur_k = stoch_k.iloc[-1]
        vol_ma20 = volume.rolling(20).mean().iloc[-1]; cur_vol = volume.iloc[-1]
        rvol = number(cur_vol / vol_ma20) if number(vol_ma20) is not None and vol_ma20 > 0 else None
        if rvol is not None and rvol < 0: rvol = None
        
        sector = info.get('sector', '기타')
        if market_type == 'KR' and sector == '기타':
            if ticker in ['005930.KS', '000660.KS']: sector = 'Technology'
            elif ticker in ['005380.KS', '000270.KS']: sector = 'Automotive'
            
        if number(cur_p) is None or cur_p <= 0: raise SourceDataError("invalid_quote_price")
        cur_rsi = number(cur_rsi)
        if cur_rsi is not None and cur_rsi > 80: return Observation(None, quote_as_of)
        
        score = 0; reasons = [] 
        
        if cur_rsi is not None and cur_rsi < 30: score += 40; reasons.append("RSI 과매도(강력)")
        elif cur_rsi is not None and cur_rsi < 45: score += 20; reasons.append("단기 과매도")
        elif cur_rsi is not None and cur_rsi < 60: score += 5; reasons.append("눌림목 구간")
        
        if cur_p <= cur_low * 1.05: score += 30; reasons.append("볼린저밴드 하단 근접")
        if not pd.isna(ma60) and cur_p >= ma60 * 0.98 and cur_p <= ma60 * 1.05: score += 20; reasons.append("60일선 지지")
        if macd.iloc[-1] > signal.iloc[-1]: score += 15; reasons.append("MACD 상승신호")
        if rvol is not None and rvol >= 1.5: score += 20; reasons.append(f"거래량 폭발({rvol:.1f}배)")
        elif rvol is not None and rvol >= 1.2: score += 10; reasons.append(f"거래량 증가")
        if cur_k < 20: score += 15; reasons.append("스토캐스틱 과매도")
        if not pd.isna(ma120) and cur_p >= ma120: score += 10; reasons.append("장기 상승 추세")

        if market_type == 'US':
            if op_margin and op_margin > 0.15: score += 10; reasons.append("이익률 우수")
            if rev_growth and rev_growth > 0.10: score += 10; reasons.append("고성장주")
            if per and per > 0 and per < 30: score += 10; reasons.append("저평가(PER)")
        elif market_type == 'KR':
            if per and per > 0 and per < 20: score += 5; reasons.append("적정 PER")
            if pbr and pbr > 0 and pbr < 1.5: score += 5; reasons.append("저PBR")
            if op_margin and op_margin > 0: score += 5; reasons.append("흑자 기업")
        
        name = info.get('shortName', ticker) if info else ticker
        price_val = price(cur_p, market_type)
        
        hist_data = []
        for d, r in hist.iloc[-20:].iterrows():
            p = round(float(r['Close']), 2) if not math.isnan(r['Close']) else None
            b_u = round(float(bb_upper.loc[d]), 2) if not math.isnan(bb_upper.loc[d]) else None
            b_l = round(float(bb_lower.loc[d]), 2) if not math.isnan(bb_lower.loc[d]) else None
            hist_data.append({"time": d.strftime("%m-%d"), "price": p, "bb_upper": b_u, "bb_lower": b_l})

        return Observation({
            "id": ticker, "rank": 0, "symbol": ticker.replace('.KS','').replace('.KQ',''), "name": name, "market": market_type,
            "currentPrice": price_val,
            "changePercent": safe_float(round(((cur_p - hist['Close'].iloc[-2]) / hist['Close'].iloc[-2]) * 100, 2)),
            "buyZoneTop": price(cur_p * 1.02, market_type), "buyZoneBottom": price(cur_p * 0.98, market_type),
            "targetPrice": price(cur_p * 1.1, market_type), "targetMethod": "reference_close_plus_10_percent", "aiReason": " + ".join(reasons),
            "score": int(score), "rsi": round(cur_rsi, 2) if cur_rsi is not None else None, "history": hist_data, "news": [],
            "financials": {
                "op_margin": number(op_margin), 
                "rev_growth": number(rev_growth), 
                "per": number(per)
            },
            "sector": sector, "rvol": round(rvol, 2) if rvol is not None else None,
            "scoreInputs": {"bbLower": number(cur_low), "ma60": number(ma60), "ma120": number(ma120),
                            "macd": number(macd.iloc[-1]), "signal": number(signal.iloc[-1]), "stochK": number(cur_k)},
            "quoteAsOf": quote_as_of, "timezone": TIMEZONES[market_type],
            "priceBasis": "unadjusted_close", "analysisMethod": "rules_v1"
        }, quote_as_of)
    except Exception as exc:
        raise SourceDataError("stock_observation_failed") from exc

# Weekly TOP10 uses the exact calendar-resolved close; older reports stay intact.
def exit_quote(item, target_date):
    start = item['recommendDate']
    end = (datetime.strptime(target_date, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
    hist = yf.Ticker(item['id']).history(start=start, end=end, auto_adjust=False, actions=True)
    if hist.empty:
        raise SourceDataError('missing_exit_quote')
    hist = hist[hist.index.strftime('%Y-%m-%d') <= target_date]
    if hist.empty:
        raise SourceDataError('missing_exit_quote')
    quote_date = hist.index[-1].strftime('%Y-%m-%d')
    if quote_date != target_date:
        raise SourceDataError('missing_exact_session_close')
    held_period = hist[hist.index.strftime('%Y-%m-%d') > start]
    return {'price': float(hist['Close'].iloc[-1]), 'quoteAsOf': quote_date,
            'hasSplit': bool((held_period['Stock Splits'] != 0).any()) if 'Stock Splits' in hist else None}


def generate_weekly_report(publication_date=None, now=None):
    generated_at = now or utc_stamp()
    publication_date = publication_date or latest_publication(generated_at)
    period = weekly_period(publication_date)
    assert_publishable(period, generated_at)
    output_path = Path(WEEKLY_REPORT_DIR) / f'{publication_date}_v3_weekly.json'
    if output_path.exists():
        validate_frozen_report(json.loads(output_path.read_text('utf-8')), period)
        return output_path  # Frozen entry/exit prices, including unevaluated rows.
    snapshots = []
    unreadable_files = 0
    for file in sorted(Path(DAILY_DATA_DIR).glob('*_daily.json')):
        try:
            payload = json.loads(file.read_text('utf-8'))
            if not isinstance(payload, dict) or not isinstance(payload.get('stocks'), list):
                raise ValueError('invalid_snapshot')
            snapshots.append((file.name, payload))
        except (ValueError, OSError):
            unreadable_files += 1
    cohort, diagnostics = weekly_cohort(snapshots, period, is_market_session)
    diagnostics['unreadableSnapshotCount'] = unreadable_files
    results, sessions = settle_weekly(cohort, period, evaluation_session, exit_quote)
    write_json_once(output_path, build_weekly_report(results, period, generated_at, sessions, diagnostics))
    return output_path


def update_history_index(now=None):
    # Hide future v3 reports even if a file has been copied into the directory.
    generated_at = now or utc_stamp()
    by_date = {}
    for file in sorted(Path(WEEKLY_REPORT_DIR).glob('*_weekly.json')):
        day = file.name.split('_')[0]
        priority = 3 if '_v3_' in file.name else 2 if '_v2_' in file.name else 1
        row = {'date': day, 'file': file.as_posix()}
        if priority == 3:
            try:
                report = json.loads(file.read_text('utf-8'))
                period = weekly_period(day)
                validate_frozen_report(report, period)
                assert_publishable(period, generated_at)
                row.update(publishAt=period['publishAt'], reportKind=REPORT_KIND)
            except (ValueError, OSError):
                continue
        if day not in by_date or priority > by_date[day][0]:
            by_date[day] = (priority, row)
    write_latest('history_index.json', sorted((value[1] for value in by_date.values()), key=lambda row: row['date'], reverse=True))


def us_universe():
    sp500, nasdaq = get_sp500_tickers(), get_nasdaq100_tickers()
    if not sp500 or not nasdaq:
        raise SourceDataError('incomplete_us_universe')
    return sorted(set(sp500 + nasdaq))


def market_session_date(market):
    return datetime.now(ZoneInfo(TIMEZONES[market])).strftime('%Y-%m-%d')


def generate_daily(target, requested_date=None):
    generated_at = utc_stamp()
    previous_path = Path('todays_recommendation.json')
    previous = json.loads(previous_path.read_text('utf-8')) if previous_path.exists() else {}
    updates = {}
    for market in (['US', 'KR'] if target == 'ALL' else [target]):
        session_date = market_session_date(market)
        if requested_date and requested_date != session_date:
            raise ValueError('Historical daily regeneration requires point-in-time data and is disabled')
        rows, meta = scan_market(market, us_universe if market == 'US' else get_korea_tickers,
                                lambda ticker, m: analyze_stock(ticker, m, target_date=session_date), previous, generated_at)
        if meta['status'] in {'fresh', 'empty'}:
            process_news_for_list(rows)
        updates[market] = (rows, meta)
    out = merge_daily(previous, updates, generated_at)
    indexes = analyze_market_condition()
    for key, value in indexes.items():
        if key in updates or key == 'VIX':
            out['market_status'][key] = value
    out['dominant_sectors'] = [v for v, _ in Counter(s.get('sector') for s in out['stocks'] if s.get('sector') not in (None, '기타')).most_common(2)]
    out['notification'] = daily_notification(updates)
    stamp = generated_at.replace(':', '').replace('+0000', 'Z')
    filename = f"{generated_at[:10]}_{stamp[11:]}_{target}_daily.json"
    write_json_once(Path(DAILY_DATA_DIR) / filename, out)
    write_latest('todays_recommendation.json', out)
    write_latest('.pending_notification.json', out['notification'])
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['daily', 'weekly', 'weekly_summary', 'notify', 'update_index'], default='daily')
    parser.add_argument('--target', choices=['US', 'KR', 'ALL'], default='ALL')
    parser.add_argument('--date')
    parser.add_argument('--notify', action='store_true', help='Explicitly allow push delivery; used only after successful publishing')
    args = parser.parse_args()
    if args.mode == 'daily':
        generate_daily(args.target, args.date)
    elif args.mode in {'weekly', 'weekly_summary'}:
        generate_weekly_report(args.date)
        update_history_index()
        write_latest('.pending_notification.json', {'title': '주간 상승률 TOP 10',
                     'body': '한 주 추천의 중복 종목을 모아 2주 뒤 금요일 종가로 비교했습니다. 시장별 상승률 상위 10개와 미평가 수를 확인하세요.'})
    elif args.mode == 'update_index':
        update_history_index()
    elif args.mode == 'notify':
        if not args.notify:
            raise ValueError('Push delivery requires --notify')
        payload = json.loads(Path('.pending_notification.json').read_text('utf-8'))
        send_push_notification(payload['title'], payload['body'])


if __name__ == '__main__':
    main()
