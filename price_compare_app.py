import re
import time
from datetime import datetime

import pandas as pd
import plotly.express as px
import requests
import streamlit as st
from curl_cffi import requests as cffi_requests

st.set_page_config(page_title="PChome vs MOMO 價格比較", layout="wide")

MAX_PAGES = 20
PLATFORM_COLORS = {"PChome": "#EF553B", "MOMO": "#636EFA"}
TIDY_COLS = ["平台", "品名", "價格", "商品編號", "連結", "頁碼"]

# --- PChome ---
PCHOME_URL = "https://ecshweb.pchome.com.tw/search/v3.3/all/results"
PCHOME_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9",
}

# --- MOMO ---
MOMO_HOME = "https://www.momoshop.com.tw/"
MOMO_URL = "https://apisearch.momoshop.com.tw/momoSearchCloud/moec/textSearch"
MOMO_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://www.momoshop.com.tw",
    "Referer": "https://www.momoshop.com.tw/",
}


# ----------------------------- 爬蟲:PChome -----------------------------
def fetch_pchome(keyword, pages, interval, on_progress):
    """逐頁抓取 PChome 搜尋結果。回傳 (原始 DataFrame, 警告清單)。"""
    frames, warnings = [], []
    with requests.Session() as s:
        s.headers.update(PCHOME_HEADERS)
        for p in range(1, pages + 1):
            on_progress((p - 1) / pages, f"抓取第 {p} / {pages} 頁…")
            try:
                resp = s.get(
                    PCHOME_URL,
                    params={"q": keyword, "page": p, "sort": "sale/dc"},
                    timeout=20,
                )
                resp.raise_for_status()
                prods = resp.json().get("prods") or []
            except Exception as e:
                if p == 1:
                    raise  # 第一頁就失敗,直接回報錯誤
                warnings.append(f"第 {p} 頁失敗,已保留前面的結果:{type(e).__name__}: {e}")
                break
            if not prods:  # 該頁沒資料,提前結束
                break
            df = pd.DataFrame(prods)
            df["頁碼"] = p
            frames.append(df)
            if p < pages:
                time.sleep(interval)  # 禮貌性間隔
    on_progress(1.0, "完成")
    return (pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()), warnings


def normalize_pchome(raw):
    if raw.empty:
        return pd.DataFrame(columns=TIDY_COLS)
    missing = [c for c in ("name", "price") if c not in raw.columns]
    if missing:
        raise ValueError(f"回傳資料缺少欄位 {missing},實際欄位:{list(raw.columns)}")
    out = pd.DataFrame({
        "平台": "PChome",
        "品名": raw["name"].astype(str),
        "價格": pd.to_numeric(raw["price"], errors="coerce"),
        "商品編號": raw["Id"].astype(str) if "Id" in raw.columns else "",
        "頁碼": raw["頁碼"],
    })
    out["連結"] = out["商品編號"].map(
        lambda c: f"https://24h.pchome.com.tw/prod/{c}" if c else ""
    )
    return out[TIDY_COLS]


# ----------------------------- 爬蟲:MOMO -----------------------------
def build_momo_payload(keyword, page):
    return {
        "host": "momoshop",
        "flag": "searchEngine",
        "data": {
            "searchValue": keyword,
            "searchType": "1",
            "cateLevel": "-1",
            "curPage": str(page),
            "priceS": "0",
            "priceE": "9999999",
            "showType": "chessboardType",
            "isFuzzy": "0",
            "rtnCateDatainfo": {
                "cateCode": "",
                "cateLv": "-1",
                "keyword": keyword,
                "curPage": str(page),  # 與外層一致
                "historyDoPush": "false",
                "timestamp": int(time.time() * 1000),  # 每頁用當下時間
            },
            "flag": 2018,
            "serviceCode": "MT01",
            "addressSearchData": {},
        },
    }


def fetch_momo(keyword, pages, interval, on_progress):
    """逐頁抓取 MOMO 搜尋結果。回傳 (原始 DataFrame, 警告清單)。"""
    rows, warnings = [], []
    session = cffi_requests.Session(impersonate="chrome")  # 模擬 Chrome 的 TLS 指紋與 UA
    try:
        session.get(MOMO_HOME, headers=MOMO_HEADERS, timeout=20)  # 先取得 cookie
        for p in range(1, pages + 1):
            on_progress((p - 1) / pages, f"抓取第 {p} / {pages} 頁…")
            try:
                resp = session.post(
                    MOMO_URL, headers=MOMO_HEADERS, json=build_momo_payload(keyword, p), timeout=20
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}:{resp.text[:200]}")
                try:
                    body = resp.json()
                except ValueError:
                    raise RuntimeError(f"回傳不是 JSON,可能被擋:{resp.text[:200]}")
            except Exception as e:
                if p == 1:
                    raise
                warnings.append(f"第 {p} 頁失敗,已保留前面的結果:{type(e).__name__}: {e}")
                break

            products = (body.get("rtnSearchData") or {}).get("goodsInfoList") or []
            if not products:
                break
            for item in products:
                rows.append({
                    "品名": item.get("goodsName", ""),
                    "價格": item.get("goodsPrice", ""),
                    "商品編號": item.get("goodsCode", ""),
                    "頁碼": p,
                })
            if p < pages:
                time.sleep(interval)
    finally:
        session.close()
    on_progress(1.0, "完成")
    return pd.DataFrame(rows), warnings


def parse_price(series):
    """取出字串中的第一組數字(如 '$1,273' -> 1273),避免價格區間被串成一個超大數字。"""
    num = series.astype(str).str.extract(r"(\d[\d,]*)", expand=False)
    return pd.to_numeric(num.str.replace(",", "", regex=False), errors="coerce")


def normalize_momo(raw):
    if raw.empty:
        return pd.DataFrame(columns=TIDY_COLS)
    out = pd.DataFrame({
        "平台": "MOMO",
        "品名": raw["品名"].astype(str),
        "價格": parse_price(raw["價格"]),
        "商品編號": raw["商品編號"].astype(str),
        "頁碼": raw["頁碼"],
    })
    out["連結"] = out["商品編號"].map(
        lambda c: f"https://www.momoshop.com.tw/goods/GoodsDetail.jsp?i_code={c}" if c else ""
    )
    return out[TIDY_COLS]


def finalize(df):
    """清理單一平台資料:移除空品名、無價格列,並去除重複商品。"""
    df = df[df["品名"].str.strip() != ""]
    df = df.dropna(subset=["價格"])
    if df.empty:
        return df
    has_code = df["商品編號"].str.strip() != ""
    if has_code.all():
        df = df.drop_duplicates(subset=["商品編號"])
    else:
        df = df.drop_duplicates(subset=["品名", "價格"])
    return df.reset_index(drop=True)


PLATFORMS = {
    "PChome": (fetch_pchome, normalize_pchome),
    "MOMO": (fetch_momo, normalize_momo),
}


# ----------------------------- 分析輔助 -----------------------------
def guess_brand(name):
    """由品名粗略推測品牌:優先取開頭【】內文字,否則取第一個詞。"""
    name = str(name).strip()
    m = re.match(r"^[【\[]([^】\]]+)[】\]]", name)
    if m:
        return m.group(1).strip()
    token = re.split(r"[\s/|｜]", name, maxsplit=1)[0]
    return token[:10] if token else "未知"


def add_price_band(prices, step):
    """依固定寬度切出價格區間(回傳字串 Series),兩個平台共用同一組區間才能比較。"""
    max_p = int(prices.max())
    edges = list(range(0, (max_p // step + 2) * step, step))
    labels = [f"{a:,}–{b - 1:,}" for a, b in zip(edges[:-1], edges[1:])]
    band = pd.cut(prices, bins=edges, labels=labels, right=False)
    return band.astype(str)


def band_sort_key(label):
    return int(label.split("–")[0].replace(",", ""))


# ------------------------------- 介面 -------------------------------
st.title("PChome vs MOMO 價格比較")

with st.sidebar:
    st.header("搜尋條件")
    with st.form("search_form"):
        keyword = st.text_input("產品名稱", value="行動電源")
        chosen = st.multiselect("比較平台", list(PLATFORMS.keys()), default=list(PLATFORMS.keys()))
        pages = st.number_input("每個平台爬取頁數", min_value=1, max_value=MAX_PAGES, value=2, step=1)
        interval = st.slider("每頁間隔(秒)", 0.0, 10.0, 1.5, 0.5)
        submitted = st.form_submit_button("開始爬取", type="primary")

if submitted:
    if not keyword.strip():
        st.warning("請輸入產品名稱")
    elif not chosen:
        st.warning("請至少選擇一個平台")
    else:
        bar = st.progress(0.0, text="準備中…")
        results, msgs = [], []
        n = len(chosen)
        for k, name in enumerate(chosen):
            fetch_fn, norm_fn = PLATFORMS[name]

            def cb(ratio, text, k=k, name=name):
                bar.progress((k + ratio) / n, text=f"[{name}] {text}")

            try:
                raw, warns = fetch_fn(keyword.strip(), int(pages), interval, cb)
                msgs += [("warning", f"{name}:{w}") for w in warns]
                tidy = finalize(norm_fn(raw))
                if tidy.empty:
                    msgs.append(("warning", f"{name}:沒有搜尋到可用的商品資料"))
                else:
                    results.append(tidy)
            except Exception as e:
                msgs.append(("error", f"{name} 爬取失敗:{type(e).__name__}: {e}"))
        bar.empty()

        st.session_state["msgs"] = msgs
        if results:
            st.session_state["data"] = pd.concat(results, ignore_index=True)
            st.session_state["meta"] = {
                "keyword": keyword.strip(),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        else:
            st.session_state.pop("data", None)

for level, text in st.session_state.get("msgs", []):
    getattr(st, level)(text)

raw_all = st.session_state.get("data")
if raw_all is None:
    st.info("請在左側輸入產品名稱、選擇平台與頁數,按「開始爬取」。")
    st.stop()

meta = st.session_state["meta"]

# --- 資料整理 ---
df = raw_all.copy()
df["價格"] = df["價格"].astype(int)
df["排名"] = df.groupby("平台").cumcount() + 1  # 各平台內依搜尋結果順序編號

with st.sidebar:
    st.header("圖表設定")
    equalize = st.checkbox(
        "各平台取相同筆數(以較少者為準)",
        value=False,
        help="兩個平台每頁商品數不同,勾選後可在相同樣本數下比較。",
    )

if equalize and df["平台"].nunique() > 1:
    cut = int(df.groupby("平台").size().min())
    df = df[df["排名"] <= cut].reset_index(drop=True)

with st.sidebar:
    max_rank = int(df["排名"].max())
    if max_rank > 5:
        show_n = st.slider("折線圖顯示筆數(每平台)", 5, max_rank, min(70, max_rank))
    else:
        show_n = max_rank
    default_step = 500 if df["價格"].max() > 2000 else 100
    band_step = st.number_input("價格區間寬度(元)", min_value=10, value=default_step, step=10)
    top_brands = st.slider("旭日圖/柱狀圖顯示前 N 個品牌(其餘歸「其他」)", 3, 20, 8)

df["價格區間"] = add_price_band(df["價格"], int(band_step))
df["品牌(推測)"] = df["品名"].apply(guess_brand)
top = df["品牌(推測)"].value_counts().head(top_brands).index
df["品牌分組"] = df["品牌(推測)"].where(df["品牌(推測)"].isin(top), "其他")

platforms_in_data = [p for p in PLATFORMS if p in set(df["平台"])]
band_order = sorted(df["價格區間"].unique(), key=band_sort_key)

st.caption(f"關鍵字:{meta['keyword']}　|　抓取時間:{meta['time']}")

# --- 統計摘要 ---
stats = (
    df.groupby("平台")["價格"]
    .agg(["count", "mean", "median", "max", "min"])
    .rename(columns={"count": "商品數", "mean": "平均價格", "median": "中位數", "max": "最高價", "min": "最低價"})
    .round(0)
    .astype(int)
    .reset_index()
)
st.dataframe(stats, hide_index=True)

if len(stats) == 2:
    ordered = stats.sort_values("平均價格")
    lo, hi = ordered.iloc[0], ordered.iloc[1]
    diff = int(hi["平均價格"] - lo["平均價格"])
    if diff == 0:
        st.info("兩個平台的平均價格相同。")
    else:
        st.info(f"平均價格較低的平台:{lo['平台']}(比 {hi['平台']} 低 {diff:,} 元)。僅反映本次搜尋結果,商品組成不同時不代表同款比價。")

tab_line, tab_pie, tab_sun, tab_bar, tab_data = st.tabs(
    ["折線圖", "圓餅圖", "旭日圖", "柱狀圖", "資料表"]
)

with tab_line:
    view = df[df["排名"] <= show_n]
    fig = px.line(
        view,
        x="排名",
        y="價格",
        color="平台",
        markers=True,
        hover_name="品名",
        color_discrete_map=PLATFORM_COLORS,
        labels={"排名": "商品排名(各平台依搜尋結果順序)", "價格": "價格"},
        title=f"「{meta['keyword']}」售價走勢比較",
    )
    for i, (name, sub) in enumerate(df.groupby("平台")):
        mean_p = sub["價格"].mean()
        fig.add_hline(
            y=mean_p,
            line_dash="dash",
            line_color=PLATFORM_COLORS.get(name, "gray"),
            annotation_text=f"{name} 平均 {mean_p:,.0f}",
            annotation_position="top left" if i % 2 == 0 else "bottom right",
        )
    fig.update_layout(height=520)
    st.plotly_chart(fig, key="chart_line")

with tab_pie:
    st.caption("各平台的價格區間分布(區間定義相同,可直接對照)。")
    cols = st.columns(len(platforms_in_data))
    for col, name in zip(cols, platforms_in_data):
        counts = df.loc[df["平台"] == name, "價格區間"].value_counts()
        order = [b for b in band_order if b in counts.index]
        pie_df = pd.DataFrame({"價格區間": order, "商品數": [int(counts[o]) for o in order]})
        fig = px.pie(
            pie_df,
            names="價格區間",
            values="商品數",
            title=f"{name} 價格區間分布",
        )
        # sort 是 go.Pie 的 trace 屬性,要用 update_traces 設定(px.pie 沒有此參數)
        fig.update_traces(sort=False, textinfo="percent+label")
        fig.update_layout(height=480, showlegend=False)
        with col:
            st.plotly_chart(fig, key=f"chart_pie_{name}")

with tab_sun:
    st.caption("層級:平台 → 價格區間 → 品牌(推測)。品牌為依品名開頭文字粗略推測,僅供參考。")
    fig = px.sunburst(
        df,
        path=["平台", "價格區間", "品牌分組"],
        color="平台",
        color_discrete_map=PLATFORM_COLORS,
        title="平台 → 價格區間 → 品牌(推測)",
    )
    fig.update_traces(textinfo="label+percent parent")
    fig.update_layout(height=680)
    st.plotly_chart(fig, key="chart_sunburst")

with tab_bar:
    # 1) 價格統計比較
    stats_long = stats.melt(
        id_vars="平台",
        value_vars=["平均價格", "中位數", "最高價", "最低價"],
        var_name="指標",
        value_name="價格",
    )
    fig = px.bar(
        stats_long,
        x="指標",
        y="價格",
        color="平台",
        barmode="group",
        color_discrete_map=PLATFORM_COLORS,
        title="價格統計比較",
    )
    fig.update_traces(texttemplate="%{y:,.0f}", textposition="outside")
    fig.update_layout(height=450)
    st.plotly_chart(fig, key="chart_bar_stats")

    # 2) 各價格區間商品數
    band_counts = df.groupby(["價格區間", "平台"]).size().reset_index(name="商品數")
    fig = px.bar(
        band_counts,
        x="價格區間",
        y="商品數",
        color="平台",
        barmode="group",
        color_discrete_map=PLATFORM_COLORS,
        category_orders={"價格區間": band_order},
        title="各價格區間商品數",
    )
    fig.update_layout(height=450)
    st.plotly_chart(fig, key="chart_bar_band")

    # 3) 品牌(推測)商品數
    brand_order = df["品牌分組"].value_counts().index.tolist()
    brand_counts = df.groupby(["品牌分組", "平台"]).size().reset_index(name="商品數")
    fig = px.bar(
        brand_counts,
        x="品牌分組",
        y="商品數",
        color="平台",
        barmode="group",
        color_discrete_map=PLATFORM_COLORS,
        category_orders={"品牌分組": brand_order},
        title="品牌(推測)商品數",
    )
    fig.update_layout(height=450)
    st.plotly_chart(fig, key="chart_bar_brand")

with tab_data:
    show_cols = [c for c in ["平台", "排名", "品名", "價格", "品牌(推測)", "價格區間", "連結", "頁碼"] if c in df.columns]
    st.dataframe(
        df[show_cols],
        hide_index=True,
        column_config={"連結": st.column_config.LinkColumn("連結")},
    )
    st.download_button(
        "下載資料 (CSV)",
        data=df[show_cols].to_csv(index=False).encode("utf-8-sig"),
        file_name=f"compare_{meta['keyword']}_{datetime.now():%Y%m%d}.csv",
        mime="text/csv",
    )
