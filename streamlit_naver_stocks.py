# -*- coding: utf-8 -*-
"""
네이버 증권 기업정보(와이즈리포트) 스트림릿 앱 — 완전본 (값 파싱/차트/상태 유지 개선)
- cmp_cd(종목코드) 입력 → encparam/id 토큰 자동 획득(Selenium headless)
- 주요재무(HTML), 재무제표/수익성/가치(JSON) 표 + Plotly 차트
- 엑셀 다운로드(메모리) 제공, 세션 상태로 재수집 최소화

실행:
  streamlit run streamlit_naver_finance_app.py
필요 패키지:
  streamlit selenium beautifulsoup4 lxml html5lib pandas requests openpyxl plotly
"""

import io
import re
import time
import json
import requests
import pandas as pd
from bs4 import BeautifulSoup
import streamlit as st
import plotly.express as px

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

# ──────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────

def to_number(s):
    """네이버 숫자 문자열 → float.
    지원: 괄호음수, %, 억/조/만 단위, '원' 접미사, 콤마, 대시('-')."""
    if s is None:
        return None
    s = str(s).strip()
    if s == "" or s == "-":
        return None
    # 통일 전처리
    s = s.replace(",", "").replace("원", "")
    # 백분율(그대로 %값 유지: 23.5%)
    if s.endswith("%"):
        try:
            return float(s[:-1])
        except Exception:
            return None
    # 한국 단위 스케일
    scale = 1.0
    if s.endswith("억"):
        scale = 1e8
        s = s[:-1]
    elif s.endswith("조"):
        scale = 1e12
        s = s[:-1]
    elif s.endswith("만"):
        scale = 1e4
        s = s[:-1]
    # 괄호 음수
    m = re.fullmatch(r"\(([-+]?\d*\.?\d+)\)", s)
    if m:
        try:
            return -float(m.group(1)) * scale
        except Exception:
            return None
    try:
        return float(s) * scale
    except Exception:
        return None


def _clean_text(x: str) -> str:
    return re.sub(r"\s+", " ", (x or "").replace("\xa0", " ").strip())


# ──────────────────────────────────────────────
# encparam / id 토큰 추출
# ──────────────────────────────────────────────
@st.cache_data(show_spinner=False)
def get_encparam_and_id(cmp_cd: str, page_key: str) -> dict:
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")
    # 필요 시 아래 경로 지정 (Streamlit Cloud)
    # chrome_options.binary_location = "/usr/bin/chromium"

    driver = webdriver.Chrome(options=chrome_options)
    try:
        url = f"https://navercomp.wisereport.co.kr/v2/company/{page_key}.aspx?cmp_cd={cmp_cd}"
        driver.get(url)
        time.sleep(2.2)
        html = driver.page_source
        enc_match = re.search(r"encparam\s*:\s*['\"]?([a-zA-Z0-9+/=]+)['\"]?", html)
        id_match = re.search(r"cmp_cd\s*=\s*['\"]?([0-9]+)['\"]?", html)
        return {
            "cmp_cd": cmp_cd,
            "encparam": enc_match.group(1) if enc_match else None,
            "id": id_match.group(1) if id_match else None,
        }
    finally:
        driver.quit()


# ──────────────────────────────────────────────
# 주요재무정보(HTML) 파싱
# ──────────────────────────────────────────────
@st.cache_data(show_spinner=False)
def fetch_main_table(cmp_cd: str, encparam: str, cmp_id: str):
    url = "https://navercomp.wisereport.co.kr/v2/company/ajax/cF1001.aspx"
    headers = {"User-Agent": "Mozilla/5.0"}
    params = {
        "cmp_cd": cmp_cd,
        "fin_typ": "0",
        "freq_typ": "Y",
        "encparam": encparam,
        "id": cmp_id,
    }
    res = requests.get(url, headers=headers, params=params, timeout=20)
    res.raise_for_status()

    soup = BeautifulSoup(res.text, "html.parser")
    target = None
    for tb in soup.select("table.gHead01.all-width"):
        txt = _clean_text(tb.get_text(" "))
        if "연간" in txt or re.search(r"20\d\d", txt):
            target = tb
            break
    if not target:
        return pd.DataFrame(), pd.DataFrame()

    # 연도 헤더
    thead_rows = target.select("thead tr")
    year_cells = thead_rows[-1].find_all(["th", "td"]) if thead_rows else []
    years = []
    for th in year_cells:
        t = _clean_text(th.get_text(" "))
        if t and not re.search(r"주요재무정보|구분", t):
            years.append(t)

    # 본문 파싱
    rows = []
    for tr in target.select("tbody tr"):
        th = tr.find("th")
        if not th:
            continue
        metric = _clean_text(th.get_text(" "))
        tds = tr.find_all("td")
        values = []
        for i in range(len(years)):
            raw = None
            if i < len(tds):
                raw = (
                    tds[i].get("title")
                    or tds[i].get("data-value")
                    or _clean_text(tds[i].get_text(" "))
                )
            values.append(to_number(raw))
        rows.append([metric] + values)

    # 헤더/열 길이 보정
    max_len = max((len(r) - 1) for r in rows) if rows else 0
    if len(years) > max_len:
        years = years[-max_len:]
    elif len(years) < max_len:
        years = years + [f"COL{i+1}" for i in range(max_len - len(years))]

    df_wide = pd.DataFrame(rows, columns=["지표"] + years).set_index("지표")
    df_long = df_wide.reset_index().melt(id_vars=["지표"], var_name="기간", value_name="값")
    if not df_long.empty:
        df_long["값"] = pd.to_numeric(df_long["값"], errors="coerce")
    return df_wide, df_long


# ──────────────────────────────────────────────
# JSON 기반 섹션 파싱
# ──────────────────────────────────────────────

def parse_json_table(json_data: dict) -> pd.DataFrame:
    data = json_data.get("DATA", [])
    labels_raw = json_data.get("YYMM", [])
    unit = json_data.get("UNIT", "")
    if not data:
        return pd.DataFrame()

    labels = [re.sub(r"<br\s*/?>", " ", l).strip() for l in labels_raw]
    year_keys = sorted([k for k in data[0] if re.match(r"^DATA\d+$", k)], key=lambda x: int(x[4:]))
    if len(labels) < len(year_keys):
        labels += [f"DATA{i+1}" for i in range(len(labels), len(year_keys))]

    rows = [[r.get("ACC_NM", "")] + [r.get(k, "") for k in year_keys] for r in data]
    df = pd.DataFrame(rows, columns=["항목"] + labels[:len(year_keys)])
    df.insert(1, "단위", unit)
    return df


@st.cache_data(show_spinner=False)
def fetch_json_mode(cmp_cd: str, mode: str, encparam: str) -> pd.DataFrame:
    url = "https://navercomp.wisereport.co.kr/v2/company/cF3002.aspx" if mode == "fs" else \
          "https://navercomp.wisereport.co.kr/v2/company/cF4002.aspx"
    rpt_map = {"fs": "1", "profit": "1", "value": "5"}
    params = {
        "cmp_cd": cmp_cd,
        "frq": "0",
        "rpt": rpt_map[mode],
        "finGubun": "MAIN",
        "frqTyp": "0",
        "cn": "",
        "encparam": encparam,
    }
    res = requests.get(url, params=params, timeout=20)
    res.raise_for_status()
    try:
        return parse_json_table(res.json())
    except json.JSONDecodeError:
        return pd.DataFrame()


# ──────────────────────────────────────────────
# 엑셀 다운로드 헬퍼
# ──────────────────────────────────────────────

def to_excel_bytes(df: pd.DataFrame, sheet_name: str = "Sheet1") -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False if df.index.name is None else True, sheet_name=sheet_name)
    buf.seek(0)
    return buf.read()


# ──────────────────────────────────────────────
# Streamlit UI (세션 상태로 재수집 최소화)
# ──────────────────────────────────────────────

st.set_page_config(page_title="네이버 재무 크롤러", layout="wide")
st.title("📊 네이버 증권 기업정보 뷰어")

with st.sidebar:
    cmp_cd = st.text_input("종목코드", value="066570")
    modes = st.multiselect("섹션 선택", ["main", "fs", "profit", "value"], default=["main", "fs", "profit", "value"])
    col1, col2 = st.columns(2)
    with col1:
        run = st.button("불러오기/갱신", type="primary")
    with col2:
        clear = st.button("캐시 초기화")

# 키 구성 및 이벤트
data_key = f"payload::{cmp_cd}::" + ",".join(modes)
if clear:
    st.cache_data.clear()
    if data_key in st.session_state:
        del st.session_state[data_key]

if run:
    st.session_state[data_key] = {"cmp_cd": cmp_cd, "modes": modes}

if data_key in st.session_state:
    cmp_cd = st.session_state[data_key]["cmp_cd"]
    modes = st.session_state[data_key]["modes"]

    page_key_map = {"main": "c1010001", "fs": "c1030001", "profit": "c1040001", "value": "c1040001"}
    entry = modes[0] if modes else "main"
    page_key = page_key_map.get(entry, "c1010001")

    # 토큰(캐시됨)
    token = get_encparam_and_id(cmp_cd, page_key)
    encparam, cmp_id = token.get("encparam"), token.get("id")

    st.caption(f"토큰(encparam): {'없음' if not encparam else encparam[:10]+'…'}  |  id: {cmp_id or '없음'}")

    for mode in modes:
        st.markdown("---")
        st.subheader(f"{mode.upper()} 결과")
        try:
            if mode == "main" and encparam and cmp_id:
                df_wide, df_long = fetch_main_table(cmp_cd, encparam, cmp_id)
                tabs = st.tabs(["와이드", "롱", "차트"])
                with tabs[0]:
                    st.dataframe(df_wide, use_container_width=True)
                    st.download_button(
                        "엑셀 다운로드 (와이드)", data=to_excel_bytes(df_wide.reset_index()), file_name=f"{cmp_cd}_main_wide.xlsx"
                    )
                with tabs[1]:
                    st.dataframe(df_long, use_container_width=True)
                    st.download_button(
                        "엑셀 다운로드 (롱)", data=to_excel_bytes(df_long), file_name=f"{cmp_cd}_main_long.xlsx"
                    )
                with tabs[2]:
                    st.markdown("#### 📈 차트")
                    if not df_long.empty:
                        plot_df = df_long.dropna(subset=["값"]).copy()
                        if plot_df.empty:
                            st.info("그릴 값이 없습니다. 원시 데이터에 숫자가 있는지 확인하세요.")
                        else:
                            x_col = "기간" if "기간" in plot_df.columns else plot_df.columns[1]
                            metrics = sorted(plot_df["지표"].unique().tolist())
                            sel = st.multiselect("지표 선택", options=metrics, default=metrics[:5], key=f"main_metrics_{cmp_cd}")
                            if sel:
                                plot_df = plot_df[plot_df["지표"].isin(sel)]
                            fig = px.line(plot_df, x=x_col, y="값", color="지표", markers=True)
                            st.plotly_chart(fig, use_container_width=True)
                    else:
                        st.info("차트로 표시할 데이터가 없습니다.")

            elif mode in ["fs", "profit", "value"] and encparam:
                df = fetch_json_mode(cmp_cd, mode, encparam)
                st.dataframe(df, use_container_width=True)
                if not df.empty:
                    st.download_button(
                        "엑셀 다운로드", data=to_excel_bytes(df), file_name=f"{cmp_cd}_{mode}.xlsx"
                    )
                    st.markdown("#### 📈 차트")
                    df_long = df.melt(id_vars=[c for c in ["항목", "단위"] if c in df.columns], var_name="기간", value_name="값")
                    # 숫자 변환(콤마/문자 제거)
                    df_long["값"] = pd.to_numeric(df_long["값"].astype(str).str.replace(",", "", regex=False), errors="coerce")
                    df_long = df_long.dropna(subset=["값"])
                    if df_long.empty:
                        st.info("차트로 표시할 숫자 값이 없습니다.")
                    else:
                        fig = px.line(df_long, x="기간", y="값", color="항목", markers=True)
                        st.plotly_chart(fig, use_container_width=True)
        except Exception as e:
            st.error(f"{mode} 섹션 처리 중 오류: {e}")
else:
    st.info("좌측에서 종목코드와 섹션을 선택하고 ‘불러오기/갱신’을 눌러주세요.")
