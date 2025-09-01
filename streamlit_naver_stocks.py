# -*- coding: utf-8 -*-
"""
네이버 증권 기업정보(와이즈리포트) 스트림릿 앱 — 완전본
- cmp_cd(종목코드) 입력 → encparam/id 토큰 자동 획득(Selenium headless)
- 주요재무정보(HTML 테이블), 재무제표/수익성/가치지표(JSON) 조회
- 화면 표 + Plotly 차트 + 엑셀 다운로드(메모리 내 생성)

필수 패키지(예):
  pip install streamlit selenium beautifulsoup4 lxml html5lib pandas requests openpyxl plotly

실행:
  streamlit run streamlit_naver_finance_app.py
"""

import os
import re
import io
import time
import json
import requests
import pandas as pd
from bs4 import BeautifulSoup
import streamlit as st
import plotly.express as px

# Selenium (encparam/id 추출)
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

# ──────────────────────────────────────────────────────────────
# 공통 유틸
# ──────────────────────────────────────────────────────────────

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
    """라벨에서 연/월을 정리. 연도만 있으면 그대로 반환."""
    if not isinstance(x, str):
        x = str(x)
    m = re.search(r"(20\d{2})(?:[./-]?(?:0?[1-9]|1[0-2]))?", x)
    return m.group(1) if m else x


# ──────────────────────────────────────────────────────────────
# encparam / id 토큰 추출 (Selenium)
# ──────────────────────────────────────────────────────────────
@st.cache_data(show_spinner=False)
def get_encparam_and_id(cmp_cd: str, page_key: str) -> dict:
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")

    # Streamlit Cloud 등에서 chromium 경로가 있을 수 있음
    # 자동 탐색이 실패하면 아래 주석 해제 후 경로 지정
    # chrome_options.binary_location = "/usr/bin/chromium"  # 필요 시

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


# ──────────────────────────────────────────────────────────────
# 주요재무정보(HTML) → df_wide, df_long
# ──────────────────────────────────────────────────────────────
@st.cache_data(show_spinner=False)
def fetch_main_table(cmp_cd: str, encparam: str, cmp_id: str):
    url = "https://navercomp.wisereport.co.kr/v2/company/ajax/cF1001.aspx"
    cookies = {
        'setC1010001': '%5B%7B...%7D%5D',
        'setC1030001': '%5B%7B...%7D%5D',
        'setC1040001': '%5B%7B...%7D%5D',
    }
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
    res = requests.get(url, headers=headers, cookies=cookies, params=params, timeout=20)
    res.raise_for_status()

    soup = BeautifulSoup(res.text, 'html.parser')
    tables = soup.select("table.gHead01.all-width")
    target = None
    for tb in tables:
        txt = _clean_text(tb.get_text(" "))
        if "연간" in txt or re.search(r"20\d\d", txt):
            target = tb
            break
    if not target:
        raise ValueError("연간 주요재무정보 테이블을 찾지 못했습니다.")

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
            if i < len(tds):
                raw = tds[i].get("title") or _clean_text(tds[i].get_text(" "))
                values.append(to_number(raw))
            else:
                values.append(None)
        rows.append([metric] + values)

    df_wide = pd.DataFrame(rows, columns=["지표"] + years).set_index("지표")
    df_long = (
        df_wide.reset_index()
        .melt(id_vars=["지표"], var_name="연도", value_name="값")
        .sort_values(["지표", "연도"]).reset_index(drop=True)
    )
    return df_wide, df_long


# ──────────────────────────────────────────────────────────────
# JSON 표 파싱 (재무제표/수익성/가치)
# ──────────────────────────────────────────────────────────────

def parse_json_table(js: dict) -> pd.DataFrame:
    data = js.get("DATA", [])
    labels_raw = js.get("YYMM", [])
    unit = js.get("UNIT", "")
    if not data:
        raise ValueError("DATA가 없습니다.")

    labels = [re.sub(r"<br\s*/?>", " ", l).strip() for l in labels_raw]
    year_keys = sorted([k for k in data[0] if re.match(r"^DATA\d+$", k)], key=lambda x: int(x[4:]))
    if len(labels) < len(year_keys):
        labels += [f"DATA{i+1}" for i in range(len(labels), len(year_keys))]

    rows = [[r.get("ACC_NM", "")] + [r.get(k, "") for k in year_keys] for r in data]
    df = pd.DataFrame(rows, columns=["항목"] + labels[:len(year_keys)])
    df.insert(1, "단위", unit)

    # 숫자화 + YoY
    num = df[labels[:len(year_keys)]].replace(",", "", regex=True).apply(pd.to_numeric, errors="coerce")
    if num.shape[1] >= 2:
        last, prev = num.iloc[:, -1], num.iloc[:, -2]
        yoy = ((last - prev) / prev * 100).where(prev != 0)
        df["전년대비 (YoY, %)"] = yoy.round(1)
    else:
        df["전년대비 (YoY, %)"] = pd.NA
    return df


@st.cache_data(show_spinner=False)
def fetch_json_mode(cmp_cd: str, mode: str, encparam: str) -> pd.DataFrame:
    url = "https://navercomp.wisereport.co.kr/v2/company/cF3002.aspx" if mode == "fs" else \
          "https://navercomp.wisereport.co.kr/v2/company/cF4002.aspx"

    rpt_map = {
        "fs": "1",      # 재무제표
        "profit": "1",  # 수익성
        "value": "5"     # 가치
    }

    cookies = {
        'setC1010001': '%5B%7B...%7D%5D',
        'setC1030001': '%5B%7B...%7D%5D',
        'setC1040001': '%5B%7B...%7D%5D',
    }
    headers = {
        'Accept': 'application/json, text/html, */*; q=0.01',
        'User-Agent': 'Mozilla/5.0',
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': f'https://navercomp.wisereport.co.kr/v2/company/c1040001.aspx?cmp_cd={cmp_cd}',
    }
    params = {
        'cmp_cd': cmp_cd,
        'frq': '0',
        'rpt': rpt_map[mode],
        'finGubun': 'MAIN',
        'frqTyp': '0',
        'cn': '',
        'encparam': encparam,
    }

    res = requests.get(url, params=params, headers=headers, cookies=cookies, timeout=20)
    res.raise_for_status()
    try:
        js = res.json()
    except json.JSONDecodeError:
        return pd.DataFrame({"메시지": ["JSON이 아니므로 표시할 수 없습니다.", res.text[:500] + "..."]})
    return parse_json_table(js)


# ──────────────────────────────────────────────────────────────
# 차트 변환 유틸
# ──────────────────────────────────────────────────────────────

def melt_for_chart_from_main(df_long: pd.DataFrame) -> pd.DataFrame:
    out = df_long.copy()
    out["연도"] = out["연도"].map(_extract_year_label)
    return out


def melt_for_chart_from_json(df_json: pd.DataFrame) -> pd.DataFrame:
    if df_json.empty:
        return df_json
    cols = list(df_json.columns)
    value_cols = [c for c in cols if c not in ("항목", "단위", "전년대비 (YoY, %)")]
    out = df_json.melt(id_vars=[c for c in ["항목", "단위"] if c in df_json.columns],
                       value_vars=value_cols, var_name="기간", value_name="값")
    out["기간"] = out["기간"].map(_extract_year_label)
    out["값"] = pd.to_numeric(out["값"].astype(str).str.replace(",", "", regex=False), errors="coerce")
    return out


# ──────────────────────────────────────────────────────────────
# 엑셀 다운로드 헬퍼
# ──────────────────────────────────────────────────────────────

def to_excel_bytes(df: pd.DataFrame, sheet_name: str = "Sheet1") -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False if df.index.name is None else True, sheet_name=sheet_name)
    buf.seek(0)
    return buf.read()


# ──────────────────────────────────────────────────────────────
# Streamlit UI
# ──────────────────────────────────────────────────────────────

st.set_page_config(page_title="네이버 재무 크롤러", layout="wide")

st.title("📊 네이버 증권 기업정보 뷰어")
st.caption("cmp_cd를 입력하고 원하는 섹션을 선택하면, 표와 차트를 바로 확인하고 엑셀로 내려받을 수 있습니다.")

with st.sidebar:
    st.header("설정")
    cmp_cd = st.text_input("종목코드 (cmp_cd)", value="066570", help="예: 삼성전자 005930, LG전자 066570 등")
    modes = st.multiselect(
        "불러올 섹션",
        options=["main", "fs", "profit", "value"],
        default=["main", "fs", "profit", "value"],
        help="main=주요재무정보(HTML), fs=재무제표, profit=수익성, value=가치지표",
    )
    run = st.button("수집/표시하기", type="primary")

if run:
    if not cmp_cd or not re.fullmatch(r"\d{6}", cmp_cd):
        st.error("종목코드는 6자리 숫자여야 합니다. 예: 005930")
    else:
        page_key_map = {"main": "c1010001", "fs": "c1030001", "profit": "c1040001", "value": "c1040001"}
        entry = modes[0] if modes else "main"
        page_key = page_key_map.get(entry, "c1010001")

        with st.spinner("토큰 준비 중..."):
            token = get_encparam_and_id(cmp_cd, page_key)
        encparam, cmp_id = token.get("encparam"), token.get("id")

        colA, colB, colC = st.columns([1,1,1])
        with colA:
            st.metric(label="종목코드", value=cmp_cd)
        with colB:
            st.metric(label="토큰(encparam)", value=(encparam[:10] + "…") if encparam else "없음")
        with colC:
            st.metric(label="ID", value=cmp_id or "없음")

        if not encparam or not cmp_id:
            st.warning("토큰을 찾지 못했습니다. 잠시 후 다시 시도하거나 섹션을 바꿔 시도해 보세요.")

        for mode in modes:
            st.markdown("---")
            st.subheader(f"📁 {mode.upper()} 결과")

            if mode == "main":
                if encparam and cmp_id:
                    try:
                        with st.spinner("주요재무정보(HTML) 불러오는 중…"):
                            df_wide, df_long = fetch_main_table(cmp_cd, encparam, cmp_id)
                        tabs = st.tabs(["와이드", "롱(연도별)"])
                        with tabs[0]:
                            st.dataframe(df_wide, use_container_width=True)
                            xls = to_excel_bytes(df_wide.reset_index(), sheet_name="main_wide")
                            st.download_button(
                                "엑셀 다운로드 (와이드)", data=xls,
                                file_name=f"{cmp_cd}_main_wide.xlsx",
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            )
                        with tabs[1]:
                            st.dataframe(df_long, use_container_width=True)
                            xls2 = to_excel_bytes(df_long, sheet_name="main_long")
                            st.download_button(
                                "엑셀 다운로드 (롱)", data=xls2,
                                file_name=f"{cmp_cd}_main_long.xlsx",
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            )
                            st.markdown("#### 📈 차트")
                            chart_df = melt_for_chart_from_main(df_long)
                            avail_metrics = sorted(chart_df["지표"].unique().tolist())
                            sel_metrics = st.multiselect(
                                "지표 선택",
                                options=avail_metrics,
                                default=avail_metrics[:3],
                                key=f"main_metrics_{cmp_cd}",
                            )
                            if sel_metrics:
                                plot_df = chart_df[chart_df["지표"].isin(sel_metrics)].copy()
                                fig = px.line(plot_df, x="연도", y="값", color="지표", markers=True)
                                st.plotly_chart(fig, use_container_width=True)
                            else:
                                st.info("차트에 표시할 지표를 선택하세요.")
                    except Exception as e:
                        with st.expander("오류 상세 보기"):
                            st.exception(e)
                        st.stop()
                else:
                    st.info("토큰이 없어 main 섹션을 건너뜁니다.")
            else:
                if encparam:
                    try:
                        with st.spinner(f"{mode} 데이터(JSON) 불러오는 중…"):
                            df = fetch_json_mode(cmp_cd, mode, encparam)
                        st.dataframe(df, use_container_width=True)
                        xls = to_excel_bytes(df, sheet_name=mode)
                        st.download_button(
                            "엑셀 다운로드", data=xls,
                            file_name=f"{cmp_cd}_{mode}.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        )

                        st.markdown("#### 📈 차트")
                        json_long = melt_for_chart_from_json(df)
                        if not json_long.empty:
                            choices = sorted(json_long["항목"].dropna().unique().tolist())
                            sel_items = st.multiselect(
                                "항목 선택", options=choices, default=choices[:3], key=f"{mode}_items_{cmp_cd}"
                            )
                            chart_type = st.radio(
                                "차트 종류", options=["line", "bar"], horizontal=True, key=f"{mode}_charttype_{cmp_cd}",
                            )
                            filtered = json_long[json_long["항목"].isin(sel_items)] if sel_items else json_long.head(0)
                            if not filtered.empty:
                                if chart_type == "line":
                                    fig = px.line(filtered, x="기간", y="값", color="항목", markers=True)
                                else:
                                    fig = px.bar(filtered, x="기간", y="값", color="항목", barmode="group")
                                st.plotly_chart(fig, use_container_width=True)
                            else:
                                st.info("차트에 표시할 항목을 선택하세요.")
                        else:
                            st.info("차트로 변환할 데이터가 없습니다.")
                    except Exception as e:
                        with st.expander("오류 상세 보기"):
                            st.exception(e)
                        st.stop()
                else:
                    st.info("encparam이 없어 JSON 섹션을 건너뜁니다.")
else:
    st.info("좌측 사이드바에서 종목코드와 섹션을 선택한 뒤 ‘수집/표시하기’를 눌러 주세요.")
