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
from datetime import datetime, timedelta
from io import StringIO

# [추가] Firebase 라이브러리 (여러 사람에게 알림 보내기 위해 필요)
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False
    print("⚠️ firebase-admin 모듈이 없습니다. DB 연동 기능이 제한됩니다.")

# SSL 인증서 오류 방지
ssl._create_default_https_context = ssl._create_unverified_context

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

# --- [Git 강제 업로드 함수] ---
def git_push_updates(mode_name):
    """
    알림을 보내기 전에 데이터를 서버(GitHub)에 먼저 올리고, 
    반영될 때까지 충분히 기다려주는 역할을 합니다.
    """
    try:
        print(f"\n⬆️ [Git] 데이터 업로드 시작 ({mode_name})...")
        
        # 1. 깃 설정
        os.system("git config --global user.name 'GitHub Action'")
        os.system("git config --global user.email 'action@github.com'")
        
        # 2. 최신 파일들 장바구니에 담기
        os.system("git add todays_recommendation.json")
        os.system(f"git add {DAILY_DATA_DIR}/*.json")
        os.system(f"git add {WEEKLY_REPORT_DIR}/*.json")
        os.system("git add history_index.json")
        
        # 3. 포장하기 (Commit)
        commit_msg = f"Auto-update stock data ({mode_name}) - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        os.system(f"git commit -m '{commit_msg}' || echo '변경 사항 없음'")
        
        # 4. 발송하기 (Push)
        print("  - 서버로 전송 중...")
        push_result = os.system("git push origin master || git push origin main")
        
        if push_result == 0:
            print("✅ [Git] 업로드 성공!")
            # [중요] 웹사이트에 반영될 때까지 3분(180초) 대기
            print("⏳ 웹사이트 반영 대기 중 (180초)... 이 시간이 지나야 알림이 갑니다.")
            time.sleep(180) 
        else:
            print("⚠️ [Git] 업로드 중 문제가 있었거나 이미 최신 상태입니다.")
            
    except Exception as e:
        print(f"❌ [Git] 업로드 실패: {e}")

# --- [추가] 2. DB에서 모든 사용자 토큰 가져오기 ---
def get_all_user_tokens():
    if not FIREBASE_AVAILABLE:
        print("⚠️ [경고] firebase-admin 라이브러리 미설치. (관리자 토큰만 사용)")
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
                    print(f"❌ Firebase 키 파싱 실패: {e}")
                    return []
            else:
                print("⚠️ [경고] FIREBASE_CREDENTIALS 환경변수가 없습니다. (관리자 토큰만 사용)")
                return [] 
        
        # 'users' 목록에서 토큰만 쏙쏙 뽑아오기
        db = firestore.client()
        users_ref = db.collection('users')
        docs = users_ref.stream()
        
        for doc in docs:
            user_data = doc.to_dict()
            token = user_data.get('pushToken')
            if token and token.startswith("ExponentPushToken"):
                tokens.append(token)
                
        print(f"🔎 DB에서 사용자 {len(tokens)}명을 찾았습니다.")
        return tokens
        
    except Exception as e:
        print(f"❌ DB 조회 실패: {e}")
        return []

# --- [수정] 3. 여러 사람에게 알림 보내기 ---
def send_push_notification(title, message):
    # (1) 관리자(나)의 토큰
    admin_tokens = ["ExponentPushToken[hiUjiJITCNaVruAohWwGtG]"] 
    
    # (2) DB에서 가져온 다른 사람들 토큰
    db_tokens = get_all_user_tokens()
    
    # (3) 합치기 (중복 제거)
    user_push_tokens = list(set(admin_tokens + db_tokens))

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
    
    # 한 명씩 차례대로 전송 (안전하게)
    for token in user_push_tokens:
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
            requests.post(url, headers=headers, data=json.dumps(payload))
        except Exception as e:
            print(f"  ❌ 전송 에러 ({token}): {e}")

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
            hist = ticker.history(period="2y") 
            
            if target_date:
                hist.index = hist.index.tz_localize(None) 
                hist = hist[hist.index.strftime('%Y-%m-%d') <= target_date]

            if len(hist) < 2:
                market_status[key] = {'status': 'UNKNOWN', 'change': 0.0, 'current': 0.0, 'message': '데이터 없음'}
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
            market_status[key] = {'status': 'UNKNOWN', 'change': 0.0, 'message': '분석 실패'}
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
    tickers = []
    
    # 1. 네이버 금융 (코스피)
    try:
        print("🔍 [KR] 네이버 금융에서 코스피 종목 가져오는 중...")
        url = 'https://finance.naver.com/sise/sise_market_sum.naver?sosok=0&page=1'
        res = fetch_with_retry(url)
        if res:
            codes = re.findall(r'href="/item/main.naver\?code=(\d{6})"', res.text)
            seen = set()
            unique_codes = [x for x in codes if not (x in seen or seen.add(x))]
            for code in unique_codes[:50]: tickers.append(f"{code}.KS")
    except Exception as e: print(f"⚠️ 코스피 목록 실패: {e}")

    # 2. 네이버 금융 (코스닥)
    try:
        print("🔍 [KR] 네이버 금융에서 코스닥 종목 가져오는 중...")
        url = 'https://finance.naver.com/sise/sise_market_sum.naver?sosok=1&page=1'
        res = fetch_with_retry(url)
        if res:
            codes = re.findall(r'href="/item/main.naver\?code=(\d{6})"', res.text)
            seen = set()
            unique_codes = [x for x in codes if not (x in seen or seen.add(x))]
            for code in unique_codes[:50]: tickers.append(f"{code}.KQ")
    except Exception as e: print(f"⚠️ 코스닥 목록 실패: {e}")

    if not tickers:
        print("🚨 [KR] 한국 종목 목록 가져오기 실패. (네이버 & 다음 모두 실패)")
        return []
        
    return list(set(tickers)) # 중복 제거

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
            "op_margin": op_margin / 100.0 if op_margin else None, 
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
        try: hist = stock.history(period="2y")
        except: return None
        
        if target_date:
            target_dt = datetime.strptime(target_date, "%Y-%m-%d")
            hist.index = hist.index.tz_localize(None)
            hist = hist[hist.index.strftime('%Y-%m-%d') <= target_date]

        if len(hist) < 120: return None
        
        info = {}
        try: info = stock.info 
        except: pass
        
        op_margin = info.get('operatingMargins')
        rev_growth = info.get('revenueGrowth')
        per = info.get('forwardPE') or info.get('trailingPE')
        pbr = info.get('priceToBook')

        if market_type == 'KR':
            kr_fund = get_kr_fundamental(ticker)
            if kr_fund:
                op_margin = kr_fund['op_margin']
                per = kr_fund['per']
                pbr = kr_fund['pbr']
        
        if market_type == 'KR' and op_margin is not None and op_margin < 0: return None
        if market_type == 'US' and op_margin is not None and op_margin < -0.5: return None
        
        close = hist['Close']; volume = hist['Volume']; high = hist['High']; low = hist['Low']
        rsi, macd, signal, bb_upper, bb_lower, ma20, stoch_k, stoch_d = calculate_indicators(close, high, low)
        
        cur_p = close.iloc[-1]; cur_rsi = rsi.iloc[-1]; cur_low = bb_lower.iloc[-1]
        ma60 = close.rolling(60).mean().iloc[-1]
        ma120 = close.rolling(120).mean().iloc[-1]
        cur_k = stoch_k.iloc[-1]
        vol_ma20 = volume.rolling(20).mean().iloc[-1]; cur_vol = volume.iloc[-1]
        rvol = safe_float(cur_vol / vol_ma20, 1.0) if vol_ma20 > 0 else 1.0
        
        sector = info.get('sector', '기타')
        if market_type == 'KR' and sector == '기타':
            if ticker in ['005930.KS', '000660.KS']: sector = 'Technology'
            elif ticker in ['005380.KS', '000270.KS']: sector = 'Automotive'
            
        if pd.isna(cur_rsi) or pd.isna(cur_p) or cur_rsi > 80: return None
        
        score = 0; reasons = [] 
        
        if cur_rsi < 30: score += 40; reasons.append("RSI 과매도(강력)")
        elif cur_rsi < 45: score += 20; reasons.append("단기 과매도")
        elif cur_rsi < 60: score += 5; reasons.append("눌림목 구간")
        
        if cur_p <= cur_low * 1.05: score += 30; reasons.append("볼린저밴드 하단 근접")
        if not pd.isna(ma60) and cur_p >= ma60 * 0.98 and cur_p <= ma60 * 1.05: score += 20; reasons.append("60일선 지지")
        if macd.iloc[-1] > signal.iloc[-1]: score += 15; reasons.append("MACD 상승신호")
        if rvol >= 1.5: score += 20; reasons.append(f"거래량 폭발({rvol:.1f}배)")
        elif rvol >= 1.2: score += 10; reasons.append(f"거래량 증가")
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
        price_val = safe_float(round(cur_p, 2))
        
        hist_data = []
        for d, r in hist.iloc[-20:].iterrows():
            p = round(float(r['Close']), 2) if not math.isnan(r['Close']) else None
            b_u = round(float(bb_upper.loc[d]), 2) if not math.isnan(bb_upper.loc[d]) else None
            b_l = round(float(bb_lower.loc[d]), 2) if not math.isnan(bb_lower.loc[d]) else None
            hist_data.append({"time": d.strftime("%m-%d"), "price": p, "bb_upper": b_u, "bb_lower": b_l})

        return {
            "id": ticker, "rank": 0, "symbol": ticker.replace('.KS','').replace('.KQ',''), "name": name, "market": market_type,
            "currentPrice": price_val,
            "changePercent": safe_float(round(((cur_p - hist['Close'].iloc[-2]) / hist['Close'].iloc[-2]) * 100, 2)),
            "buyZoneTop": safe_float(round(cur_p * 1.02, 2), price_val), "buyZoneBottom": safe_float(round(cur_p * 0.98, 2), price_val),
            "targetPrice": safe_float(round(cur_p * 1.1, 2), price_val), "aiReason": " + ".join(reasons),
            "score": int(score), "rsi": safe_float(round(cur_rsi, 2)), "history": hist_data, "news": [],
            "financials": {
                "op_margin": safe_float(op_margin), 
                "rev_growth": safe_float(rev_growth), 
                "per": safe_float(per)
            },
            "sector": sector, "rvol": safe_float(round(rvol, 2))
        }
    except: return None

# --- [6] 주간 종합 리포트 생성 (수익률 결산) ---
def generate_weekly_report(target_date_str):
    print(f"\n📊 [Weekly] {target_date_str} 기준 주간 성과 분석 시작...")
    
    daily_files = []
    end_date = datetime.strptime(target_date_str, "%Y-%m-%d")
    start_date = end_date - timedelta(days=14)
    
    if not os.path.exists(DAILY_DATA_DIR): 
        os.makedirs(DAILY_DATA_DIR)

    if not os.path.exists(WEEKLY_REPORT_DIR):
        os.makedirs(WEEKLY_REPORT_DIR)

    for f in sorted(os.listdir(DAILY_DATA_DIR)):
        if f.endswith('_daily.json'):
            file_date_str = f.split('_')[0]
            try:
                file_date = datetime.strptime(file_date_str, "%Y-%m-%d")
                if start_date <= file_date <= end_date: 
                    daily_files.append(f)
            except: pass
            
    print(f"📂 분석 대상 데일리 파일: {len(daily_files)}개")
    
    # 중복 제거 로직
    stocks_dict = {} 
    for file in daily_files:
        with open(f"{DAILY_DATA_DIR}/{file}", 'r', encoding='utf-8') as f:
            data = json.load(f)
            rec_date = file.split('_')[0]
            for stock in data.get('stocks', []):
                sid = stock['id']
                if sid not in stocks_dict:
                    stock['buyPrice'] = stock['currentPrice'] 
                    stock['recommendDate'] = rec_date
                    stocks_dict[sid] = stock

    aggregated_stocks = list(stocks_dict.values())
    print(f"🔎 총 {len(aggregated_stocks)}개의 유니크 종목 수익률 계산 중...")

    final_results = []
    for i, item in enumerate(aggregated_stocks):
        if i % 10 == 0: time.sleep(1)
        ticker = item['id']
        buy_price = item['buyPrice']
        try:
            stock_info = yf.Ticker(ticker)
            target_dt = datetime.strptime(target_date_str, "%Y-%m-%d")
            hist = stock_info.history(period="6mo")
            if hist.empty: continue
            hist.index = hist.index.tz_localize(None)
            hist_until_target = hist[hist.index.strftime('%Y-%m-%d') <= target_date_str] 
            if not hist_until_target.empty:
                current_price = float(hist_until_target['Close'].iloc[-1])
                return_rate = ((current_price - buy_price) / buy_price) * 100
                new_item = item.copy()
                new_item['currentPrice'] = round(current_price, 2)
                new_item['returnRate'] = round(return_rate, 2)
                final_results.append(new_item)
        except Exception as e: pass 

    us_results = [s for s in final_results if s['market'] == 'US']
    kr_results = [s for s in final_results if s['market'] == 'KR']
    us_results.sort(key=lambda x: x['returnRate'], reverse=True)
    kr_results.sort(key=lambda x: x['returnRate'], reverse=True)
    
    us_top10 = us_results[:10]
    kr_top10 = kr_results[:10]
    for i, item in enumerate(us_top10): item['rank'] = i + 1
    for i, item in enumerate(kr_top10): item['rank'] = i + 1
    top_performers = us_top10 + kr_top10
        
    ms = analyze_market_condition(target_date=target_date_str)
    out = {
        "market_status": ms, 
        "stocks": top_performers,
        "dominant_sectors": [], 
        "timestamp": f"{target_date_str} 08:00:00 (Weekly Report)"
    }
    
    output_path = f"{WEEKLY_REPORT_DIR}/{target_date_str}_weekly.json"
    with open(output_path, 'w', encoding='utf-8') as f: json.dump(out, f, indent=2, ensure_ascii=False, allow_nan=False)
    print(f"\n✅ 주간 종합 리포트 생성 완료: {output_path}")

# --- [7] 주간 수익률 결산 알림 (토요일 5PM) ---
def send_weekly_summary_notification():
    print(f"\n📢 [Weekly Summary] 주간 수익률 결산 알림 전송 시작...")
    today_str = (datetime.utcnow() + timedelta(hours=9)).strftime("%Y-%m-%d")
    report_file_path = f"{WEEKLY_REPORT_DIR}/{today_str}_weekly.json"
    
    if not os.path.exists(report_file_path):
        generate_weekly_report(today_str)
        update_history_index()

    title = "📊 주간 수익률 결산 도착"
    body = "지난 2주간의 추천 종목 성과 분석이 완료되었습니다.\n지금 앱에서 한국/미국 Top 10 수익률을 확인해보세요!"
    
    # [수정] 데이터 업로드 후 알림 전송
    git_push_updates("weekly")
    send_push_notification(title, body)

# --- [수정] 인덱스 업데이트 ---
def update_history_index():
    if not os.path.exists(WEEKLY_REPORT_DIR): return
    hl = []
    for f in sorted(os.listdir(WEEKLY_REPORT_DIR), reverse=True):
        if f.endswith('_weekly.json'): 
            d_str = f.split('_')[0]
            hl.append({"date": d_str, "file": f"{WEEKLY_REPORT_DIR}/{f}"})
    with open('history_index.json', 'w', encoding='utf-8') as f: json.dump(hl, f, indent=2, ensure_ascii=False)

# --- [백필 실행 함수] ---
def run_backfill(start_date, end_date):
    print(f"\n⏪ Backfill Mode: {start_date} ~ {end_date}")
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    if not os.path.exists(DAILY_DATA_DIR): os.makedirs(DAILY_DATA_DIR)
    
    current_dt = start_dt
    while current_dt <= end_dt:
        if current_dt.weekday() >= 5: 
            current_dt += timedelta(days=1)
            continue
        target_str = current_dt.strftime("%Y-%m-%d")
        print(f"\n📅 [Backfill] 처리 중: {target_str}")
        
        ms = analyze_market_condition(target_date=target_str)
        
        # 임시로 ALL 실행 (로직은 daily와 동일하게 호출)
        # (여기서는 생략, 실제로는 analyze_stock 호출 필요)
        
        current_dt += timedelta(days=1)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', type=str, default='daily', help='Execution mode: daily, weekly, weekly_summary, or backfill')
    parser.add_argument('--target', type=str, default='ALL', help='Target market: US, KR, or ALL')
    parser.add_argument('--date', type=str, default=None, help='Target date')
    parser.add_argument('--start', type=str, default=None, help='Backfill start date')
    parser.add_argument('--end', type=str, default=None, help='Backfill end date')
    args = parser.parse_args()
    
    if args.date:
        today_str = args.date
    else:
        today_str = (datetime.utcnow() + timedelta(hours=9)).strftime("%Y-%m-%d")
        
    print(f"🚀 AI 주식 분석기 가동 (모드: {args.mode}, 타겟: {args.target}, 날짜: {today_str})")

    if args.mode == 'daily':
        if not os.path.exists(DAILY_DATA_DIR): os.makedirs(DAILY_DATA_DIR)
        prev_stock_ids = get_latest_recommendation_ids()
        
        existing_stocks = []
        try:
            with open('todays_recommendation.json', 'r', encoding='utf-8') as f:
                existing_stocks = json.load(f).get('stocks', [])
        except: pass

        ms = analyze_market_condition(target_date=today_str)
        final_stocks = []
        
        if args.target in ['US', 'ALL']:
            sp500 = get_sp500_tickers()
            nasdaq100 = get_nasdaq100_tickers()
            us_tickers = list(set(sp500 + nasdaq100))
            
            if not us_tickers:
                print("❌ [미국] 종목 목록을 가져오지 못했습니다. (네트워크/소스 오류)")
                # 미국 종목 추가 안 함 -> final_stocks에 US 없음
            else:
                print(f"\n🇺🇸 미국 분석 (대상: {len(us_tickers)}개)...")
                usc = []
                for i, t in enumerate(us_tickers): 
                    d = analyze_stock(t, 'US', target_date=today_str)
                    if d: usc.append(d)
                
                # ✅ [수정] 10개 미만일 때 차순위로 강제 채우기 로직 (미국)
                usc.sort(key=lambda x: x['score'], reverse=True)
                
                # 1. 40점 이상인 종목들 먼저 선택
                high_score_stocks = [s for s in usc if s['score'] >= 40]
                
                ust = []
                if len(high_score_stocks) >= 10:
                    ust = high_score_stocks[:10]
                else:
                    # 2. 부족하면 40점 미만 종목으로 채우기 (이미 usc는 점수순 정렬됨)
                    ust = high_score_stocks[:] # 40점 이상 복사
                    needed = 10 - len(ust)
                    
                    # 40점 미만인 애들 중에서 needed만큼 가져옴
                    fillers = [s for s in usc if s['score'] < 40][:needed]
                    
                    for f in fillers:
                        f['aiReason'] = "AI 점수 우수 (매수 조건 근접) + " + f['aiReason']
                        ust.append(f)
                
                # 최종 점수순 재정렬 (혹시 섞였을까봐) 및 10개 자르기
                ust.sort(key=lambda x: x['score'], reverse=True)
                ust = ust[:10]

                for i, item in enumerate(ust): item['rank'] = i + 1
                process_news_for_list(ust)
                final_stocks.extend(ust)
        else:
            us_kept = [s for s in existing_stocks if s['market'] == 'US']
            final_stocks.extend(us_kept)

        if args.target in ['KR', 'ALL']:
            kr = get_korea_tickers()
            
            if not kr:
                print("❌ [한국] 종목 목록을 가져오지 못했습니다. (네트워크/소스 오류)")
            else:
                krc = []
                print(f"\n🇰🇷 한국 분석 (대상: {len(kr)}개)...")
                for i, t in enumerate(kr): 
                    d = analyze_stock(t, 'KR', target_date=today_str)
                    if d: krc.append(d)
                
                # ✅ [수정] 10개 미만일 때 차순위로 강제 채우기 로직 (한국)
                krc.sort(key=lambda x: x['score'], reverse=True)
                
                # 1. 40점 이상인 종목들 먼저 선택
                high_score_stocks_kr = [s for s in krc if s['score'] >= 40]
                
                krt = []
                if len(high_score_stocks_kr) >= 10:
                    krt = high_score_stocks_kr[:10]
                else:
                    # 2. 부족하면 40점 미만 종목으로 채우기
                    krt = high_score_stocks_kr[:] 
                    needed = 10 - len(krt)
                    
                    fillers_kr = [s for s in krc if s['score'] < 40][:needed]
                    
                    for f in fillers_kr:
                        f['aiReason'] = "AI 점수 우수 (매수 조건 근접) + " + f['aiReason']
                        krt.append(f)
                
                krt.sort(key=lambda x: x['score'], reverse=True)
                krt = krt[:10]

                for i, item in enumerate(krt): item['rank'] = i + 1
                process_news_for_list(krt)
                final_stocks.extend(krt)
        else:
            kr_kept = [s for s in existing_stocks if s['market'] == 'KR']
            final_stocks.extend(kr_kept)
        
        all_sectors = [s['sector'] for s in final_stocks if s['sector'] != '기타']
        dominant_sectors = [item[0] for item in Counter(all_sectors).most_common(2)]
        
        noti_title = "🔔 DailyPick10 알림"
        market_name = "미국" if args.target == 'US' else ("한국" if args.target == 'KR' else "전체")
        
        # [중요] 추천 종목 유무와 관계없이 알림 메시지 생성
        target_market_stocks = [s for s in final_stocks if s['market'] == args.target] if args.target != 'ALL' else final_stocks

        if target_market_stocks:
            new_stocks = [s['symbol'] for s in target_market_stocks if s['id'] not in prev_stock_ids]
            if new_stocks:
                highlight = ", ".join(new_stocks[:2])
                noti_body = f"오늘의 {market_name} 추천: {highlight} 등 (신규 {len(new_stocks)}건)"
            else:
                top = ", ".join([s['symbol'] for s in target_market_stocks[:2]])
                noti_body = f"오늘의 {market_name} 추천: {top} 등 (순위 변동)"
        else:
            # 🚨 종목을 하나도 못 가져온 경우 (네트워크 오류 등)
            if not final_stocks and args.target == 'ALL':
                 noti_body = f"⚠️ 데이터 수집 실패: 서버 연결 상태를 확인해주세요."
            elif not final_stocks:
                 noti_body = f"오늘의 {market_name} 추천 종목이 없습니다. (시장 관망 필요 📉)"
            else:
                 noti_body = f"오늘의 {market_name} 추천 종목이 없습니다."

        out = {
            "market_status": ms, "stocks": final_stocks, "dominant_sectors": dominant_sectors, 
            "timestamp": f"{today_str} {datetime.now().strftime('%H:%M:%S')}",
            "notification": { "title": noti_title, "body": noti_body }
        }
        
        with open('todays_recommendation.json', 'w', encoding='utf-8') as f: json.dump(out, f, indent=2, ensure_ascii=False, allow_nan=False)
        with open(f"{DAILY_DATA_DIR}/{today_str}_daily.json", 'w', encoding='utf-8') as f: json.dump(out, f, indent=2, ensure_ascii=False, allow_nan=False)

        # [수정] Git 업로드 먼저 수행 후 알림 발송
        git_push_updates("daily")
        
        # 조건 완화: 날짜 지정 여부와 상관없이 알림 내용이 있으면 무조건 발송
        if noti_body: 
            send_push_notification(noti_title, noti_body)
    
    elif args.mode == 'weekly':
        generate_weekly_report(today_str)
        update_history_index()
    
    elif args.mode == 'weekly_summary':
        send_weekly_summary_notification()
    
    elif args.mode == 'backfill':
        if args.start and args.end: run_backfill(args.start, args.end)
    
    elif args.mode == 'update_index':
        update_history_index()

    elif args.mode == 'test_push':
        send_push_notification("🔔 테스트 알림", "이것은 강제 전송 테스트 메시지입니다!")

    print(f"\n✅ 완료되었습니다.")

if __name__ == "__main__": main()
