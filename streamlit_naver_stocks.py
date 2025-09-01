# -*- coding: utf-8 -*-
import os, re, io, time, json, requests
import pandas as pd
from bs4 import BeautifulSoup
import streamlit as st
import plotly.express as px
from collections import defaultdict
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

# ─────────────────────────────────────
# 기본 설정 및 상태 초기화
# ─────────────────────────────────────
st.set_page_config(page_title="네이버 재무 크롤러", layout="wide")
st.title("📊 네이버 증권 기업정보 뷰어")

if "data_cache" not in st.session_state:
    st.session_state.data_cache = {}

if "loaded" not in st.session_state:
    st.session_state.loaded = False

# ─────────────────────────────────────
# 유틸 함수
# ─────────────────────────────────────

def to_number(s):
    if s is None:
        return None
    s = str(s).strip()
    if s in ("", "-"):
        return None
    s = s.replace(",", "")
    m = re.fullmatch(r"\(([-+]?\d*\.?\d+)\)", s)
    if m:
        return -float(m.group(1))
    try:
        return float(s)
    except Exception:
        return None

def _clean_text(x: str) -> str:
    return re.sub(r"\s+", " ", (x or "").replace("\xa0", " ").strip())

def _extract_year_label(x: str) -> str:
    if not isinstance(x, str):
        x = str(x)
    m = re.search(r"(20\\d{2})(?:[./-]?(?:0?[1-9]|1[0-2]))?", x)
    return m.group(1) if m else x

def to_excel_bytes(df: pd.DataFrame, sheet_name: str = "Sheet1") -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False if df.index.name is None else True, sheet_name=sheet_name)
    buf.seek(0)
    return buf.read()

# ─────────────────────────────────────
# encparam / id 획득
# ─────────────────────────────────────

@st.cache_data(show_spinner=False)
def get_encparam_and_id(cmp_cd: str, page_key: str) -> dict:
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")
    driver = webdriver.Chrome(options=chrome_options)
    try:
        url = f"https://navercomp.wisereport.co.kr/v2/company/{page_key}.aspx?cmp_cd={cmp_cd}"
        driver.get(url)
        time.sleep(2.2)
        html = driver.page_source
        enc_match = re.search(r"encparam\\s*:\\s*['\"]?([a-zA-Z0-9+/=]+)['\"]?", html)
        id_match = re.search(r"cmp_cd\\s*=\\s*['\"]?([0-9]+)['\"]?", html)
        return {
            "cmp_cd": cmp_cd,
            "encparam": enc_match.group(1) if enc_match else None,
            "id": id_match.group(1) if id_match else None,
        }
    finally:
        driver.quit()

# ─────────────────────────────────────
# 주요재무정보(HTML 테이블)
# ─────────────────────────────────────

@st.cache_data(show_spinner=False)
def fetch_main_table(cmp_cd: str, encparam: str, cmp_id: str):
    url = "https://navercomp.wisereport.co.kr/v2/company/ajax/cF1001.aspx"
    headers = {
        'Accept': 'application/json, text/html, */*; q=0.01',
        'User-Agent': 'Mozilla/5.0',
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': f'https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={cmp_cd}',
    }
    params = {
        'cmp_cd': cmp_cd,
        'fin_typ': '0',
        'freq_typ': 'Y',
        'encparam': encparam,
        'id': cmp_id,
    }
    res = requests.get(url, headers=headers, params=params, timeout=20)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, 'html.parser')
    tables = soup.select("table.gHead01.all-width")
    target = next((tb for tb in tables if "연간" in tb.text or re.search(r"20\\d\\d", tb.text)), None)
    if not target:
        raise ValueError("주요재무정보 테이블 없음")

    thead_rows = target.select("thead tr")
    year_cells = thead_rows[-1].find_all(["th", "td"]) if thead_rows else []
    year_counter = defaultdict(int)
    years = []
    for th in year_cells:
        t = _clean_text(th.get_text(" "))
        if t and not re.search(r"주요재무정보|구분", t):
            year_counter[t] += 1
            suffix = f"_{year_counter[t]}" if year_counter[t] > 1 else ""
            years.append(t + suffix)

    rows = []
    for tr in target.select("tbody tr"):
        th = tr.find("th")
        if not th:
            continue
        metric = _clean_text(th.get_text(" "))
        tds = tr.find_all("td")
        values = []
        for i in range(len(years)):
            raw = tds[i].get("title") or _clean_text(tds[i].get_text(" ")) if i < len(tds) else None
            values.append(to_number(raw))
        rows.append([metric] + values)

    df_wide = pd.DataFrame(rows, columns=["지표"] + years).set_index("지표")
    df_long = df_wide.reset_index().melt(id_vars=["지표"], var_name="연도", value_name="값")
    return df_wide, df_long

# ─────────────────────────────────────
# Streamlit UI
# ─────────────────────────────────────

with st.sidebar:
    st.header("설정")
    cmp_cd = st.text_input("종목코드 (cmp_cd)", value="005930")
    modes = st.multiselect("섹션 선택", ["main", "fs", "profit", "value"], default=["main"])
    st.session_state.loaded = st.button("수집/표시하기")

if st.session_state.loaded:
    page_key_map = {"main": "c1010001", "fs": "c1030001", "profit": "c1040001", "value": "c1040001"}
    page_key = page_key_map.get("main")
    with st.spinner("encparam/id 추출 중..."):
        token = get_encparam_and_id(cmp_cd, page_key)
    encparam, cmp_id = token["encparam"], token["id"]
    if encparam and cmp_id:
        df_wide, df_long = fetch_main_table(cmp_cd, encparam, cmp_id)
        st.session_state.data_cache["df_wide"] = df_wide
        st.session_state.data_cache["df_long"] = df_long
    else:
        st.error("encparam 또는 id 추출 실패")

# ─────────────────────────────────────
# 테이블 및 차트 UI
# ─────────────────────────────────────

if "df_wide" in st.session_state.data_cache:
    st.subheader("📋 주요재무정보 (와이드)")
    df_wide = st.session_state.data_cache["df_wide"]
    st.dataframe(df_wide)
    st.download_button("엑셀 다운로드 (Wide)", to_excel_bytes(df_wide.reset_index()), file_name=f"{cmp_cd}_main_wide.xlsx")

if "df_long" in st.session_state.data_cache:
    st.subheader("📈 주요재무정보 차트")
    df_long = st.session_state.data_cache["df_long"]
    chart_df = df_long.copy()
    chart_df["연도"] = chart_df["연도"].map(_extract_year_label)
    metrics = sorted(chart_df["지표"].unique())
    sel_metrics = st.multiselect("지표 선택", metrics, default=metrics[:3])
    if sel_metrics:
        fig = px.line(chart_df[chart_df["지표"].isin(sel_metrics)], x="연도", y="값", color="지표", markers=True)
        st.plotly_chart(fig, use_container_width=True)
