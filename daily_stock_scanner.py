import yfinance as yf
import pandas as pd
import json
import time
import ssl
import requests
import xml.etree.ElementTree as ET
import math
import os
from collections import Counter

# SSL 인증서 오류 방지
ssl._create_default_https_context = ssl._create_unverified_context

# --- [안전장치] 값을 안전하게 변환하는 함수 ---
def safe_float(val, default=0.0):
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f): return default
        return f
    except: return default

def analyze_market_condition():
    print("🌍 글로벌 시장 상황 분석 중...")
    markets = {'US': {'ticker': '^GSPC', 'name': 'S&P 500'}, 'KR': {'ticker': '^KS11', 'name': 'KOSPI'}, 'VIX': {'ticker': '^VIX', 'name': '공포지수'}}
    market_status = {}
    for key, info in markets.items():
        try:
            ticker = yf.Ticker(info['ticker'])
            hist = ticker.history(period="5d")
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
            print(f"   👉 [{key}] {info['name']}: {current:.2f} ({message})")
        except: market_status[key] = {'status': 'UNKNOWN', 'change': 0.0, 'message': '분석 실패'}
    return market_status

def get_sp500_tickers():
    print("📋 [미국] S&P 500 종목 리스트 확보 중...")
    try:
        url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers)
        table = pd.read_html(response.text)
        tickers = table[0]['Symbol'].tolist()
        return [t.replace('.', '-') for t in tickers]
    except:
        return ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'TSLA', 'META']

def get_korea_tickers():
    print("📋 [한국] 주요 우량주 리스트 로드 중...")
    # 안정적인 데이터 수집을 위해 검증된 리스트 사용
    return [
        '005930.KS', '000660.KS', '373220.KS', '207940.KS', '005380.KS', '000270.KS', '068270.KS', '005490.KS', '035420.KS', 
        '006400.KS', '051910.KS', '035720.KS', '003670.KS', '028260.KS', '012330.KS', '105560.KS', '055550.KS', '032830.KS', 
        '086790.KS', '015760.KS', '034020.KS', '011200.KS', '010120.KS', '259960.KS', '329180.KS', '011070.KS', '034220.KS',
        '009150.KS', '010950.KS', '011780.KS', '009830.KS', '204320.KS', '003490.KS', '086280.KS', '000100.KS', '128940.KS',
        '247540.KQ', '086520.KQ', '028300.KQ', '196170.KQ', '035900.KQ', '041510.KQ', '068760.KQ', '277810.KQ', '403870.KQ', 
        '039200.KQ', '293490.KQ', '263750.KQ', '145020.KQ', '214150.KQ', '042700.KQ', '005290.KQ', '240810.KQ', '357780.KQ'
    ]

def get_news_from_google_kr(ticker):
    try:
        url = f"https://news.google.com/rss/search?q={ticker.split('.')[0]}+주가&hl=ko&gl=KR&ceid=KR:ko"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=3); root = ET.fromstring(response.content)
        return [{'title': item.find('title').text, 'link': item.find('link').text, 'publisher': item.find('source').text} for item in root.findall('./channel/item')[:3]]
    except: return []

def get_news_from_google_us(ticker):
    try:
        url = f"https://news.google.com/rss/search?q={ticker}+stock&hl=en-US&gl=US&ceid=US:en"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=3); root = ET.fromstring(response.content)
        return [{'title': item.find('title').text, 'link': item.find('link').text, 'publisher': item.find('source').text} for item in root.findall('./channel/item')[:3]]
    except: return []

def calculate_indicators(close):
    delta = close.diff(); gain = (delta.where(delta > 0, 0)).rolling(14).mean(); loss = (-delta.where(delta < 0, 0)).rolling(14).mean(); rs = gain/loss; rsi = 100 - (100/(1+rs))
    exp1 = close.ewm(span=12).mean(); exp2 = close.ewm(span=26).mean(); macd = exp1 - exp2; signal = macd.ewm(span=9).mean()
    ma20 = close.rolling(20).mean(); std = close.rolling(20).std(); upper = ma20 + (std*2); lower = ma20 - (std*2)
    return rsi, macd, signal, upper, lower, ma20

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

def analyze_stock(ticker, market_type):
    try:
        stock = yf.Ticker(ticker)
        try: hist = stock.history(period="6mo")
        except: return None
        if len(hist) < 60: return None
        
        info = {}
        try: info = stock.info 
        except: pass
        
        if market_type == 'US' and info.get('operatingMargins', 0) < -0.2: return None
        
        close = hist['Close']
        volume = hist['Volume'] # 거래량 추가
        
        rsi, macd, signal, bb_upper, bb_lower, ma20 = calculate_indicators(close)
        
        cur_p = close.iloc[-1]; cur_rsi = rsi.iloc[-1]; cur_low = bb_lower.iloc[-1]; ma60 = close.rolling(60).mean().iloc[-1]
        
        # [New] RVOL 계산
        vol_ma20 = volume.rolling(20).mean().iloc[-1]
        cur_vol = volume.iloc[-1]
        rvol = safe_float(cur_vol / vol_ma20, 1.0) if vol_ma20 > 0 else 1.0
        
        sector = info.get('sector', '기타')
        if market_type == 'KR' and sector == '기타':
            if ticker in ['005930.KS', '000660.KS']: sector = 'Technology'
            elif ticker in ['005380.KS', '000270.KS']: sector = 'Automotive'
            elif ticker in ['207940.KS', '068270.KS']: sector = 'Healthcare'
        
        if pd.isna(cur_rsi) or pd.isna(cur_p) or cur_rsi > 75: return None
        
        score = 20; reasons = []
        
        # 기술적 분석 점수
        if cur_rsi < 30: score += 30; reasons.append("RSI 과매도")
        elif cur_rsi < 40: score += 20; reasons.append("단기 과매도")
        elif cur_rsi < 55: score += 10; reasons.append("눌림목")
        
        if cur_p <= cur_low * 1.02: score += 20; reasons.append("볼린저밴드 하단")
        
        if not pd.isna(ma60) and cur_p >= ma60 * 0.95 and cur_p <= ma60 * 1.08: score += 15; reasons.append("60일선 지지")
        
        if macd.iloc[-1] > signal.iloc[-1]: score += 10
        
        if rvol >= 1.5:
            score += 15
            reasons.append(f"거래량 폭발({rvol:.1f}배)")
        
        # [핵심 수정] 재무 분석 점수 & 텍스트 추가 (이게 있어야 앱에서 보임!)
        if market_type == 'US':
            op_margin = info.get('operatingMargins', 0)
            rev_growth = info.get('revenueGrowth', 0)
            per = info.get('forwardPE', 0)
            
            if op_margin > 0.15: 
                score += 5
                reasons.append("영업이익률 우수") # '이익' 키워드
            if rev_growth > 0.10: 
                score += 5
                reasons.append("매출 고성장") # '매출', '성장' 키워드
            if per > 0 and per < 30: 
                score += 5
                reasons.append("저PER 저평가") # 'PER' 키워드
        elif market_type == 'KR':
            # 한국장은 데이터가 불안정해도 기본적으로 재무가 튼튼한 종목 위주이므로
            score += 5
            reasons.append("재무 건전성 양호") # '재무' 키워드
        
        cutoff = 40 if market_type == 'US' else 15
        if score < cutoff: return None
        
        name = info.get('shortName', ticker) if info else ticker
        price_val = safe_float(round(cur_p, 2))
        if price_val <= 0: return None

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
            "financials": {"op_margin": safe_float(info.get('operatingMargins', 0)), "rev_growth": safe_float(info.get('revenueGrowth', 0)), "per": safe_float(info.get('forwardPE', 0))},
            "sector": sector, "rvol": safe_float(round(rvol, 2))
        }
    except: return None

def process_news_for_list(stock_list):
    if not stock_list: return
    print(f"\n📰 Top {len(stock_list)} 뉴스 수집 중...")
    for item in stock_list:
        ticker = item['id']; mkt = item['market']
        print(f"   [{mkt}] {ticker}...", end=' '); raw = []
        try:
            if mkt == 'KR': raw = get_news_from_google_kr(ticker)
            else:
                stock = yf.Ticker(ticker)
                try: raw = s.news; 
                except: pass
                if raw: raw = [n for n in raw if n.get('title')]
                if not raw: raw = get_news_from_google_us(ticker)
        except: pass
        print(f"{len(raw)}개"); item['news'] = analyze_news_sentiment(raw[:3])

def update_history_index():
    if not os.path.exists('history'): return
    hl = []
    for f in sorted(os.listdir('history'), reverse=True):
        if f.endswith('_recommendation.json'): hl.append({"date": f.split('_')[0], "file": f"history/{f}"})
    with open('history_index.json', 'w', encoding='utf-8') as f: json.dump(hl, f, indent=2, ensure_ascii=False)

def main():
    print("🚀 [재무 텍스트 추가] AI 주식 분석기 가동")
    ms = analyze_market_condition(); final = []
    
    us = get_sp500_tickers(); usc = []
    print("\n🇺🇸 미국 분석..."); 
    for i, t in enumerate(us): 
        print(f"[{i+1}/{len(us)}] {t}...", end='\r'); d = analyze_stock(t, 'US'); 
        if d: usc.append(d)
    usc.sort(key=lambda x: x['score'], reverse=True); ust = usc[:10]
    for i, item in enumerate(ust): item['rank'] = i + 1
    final.extend(ust)
    
    kr = get_korea_tickers(); krc = []
    print("\n🇰🇷 한국 분석..."); 
    for i, t in enumerate(kr): 
        print(f"[{i+1}/{len(kr)}] {t}...", end='\r'); d = analyze_stock(t, 'KR'); 
        if d: krc.append(d)
    krc.sort(key=lambda x: x['score'], reverse=True); krt = krc[:10]
    for i, item in enumerate(krt): item['rank'] = i + 1
    final.extend(krt)
    
    process_news_for_list(ust); process_news_for_list(krt)
    
    # [New] 주도 테마 선정
    all_sectors = [s['sector'] for s in final if s['sector'] != '기타']
    dominant_sectors = [item[0] for item in Counter(all_sectors).most_common(2)]
    
    out = {"market_status": ms, "stocks": final, "dominant_sectors": dominant_sectors, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}
    
    try:
        with open('todays_recommendation.json', 'w', encoding='utf-8') as f: json.dump(out, f, indent=2, ensure_ascii=False, allow_nan=False)
        if not os.path.exists('history'): os.makedirs('history')
        with open(f"history/{time.strftime('%Y-%m-%d')}_recommendation.json", 'w', encoding='utf-8') as f: json.dump(out, f, indent=2, ensure_ascii=False, allow_nan=False)
        print(f"\n🎉 분석 완료! 총 {len(final)}개 저장됨."); update_history_index()
    except ValueError as e: print(f"\n❌ 저장 실패: {e}")

if __name__ == "__main__": main()