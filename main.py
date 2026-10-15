
import re
import requests
import streamlit as st
import pandas as pd
import plotly.graph_objects as go

# ==================================================
# 1. 기본 설정
# ==================================================
st.set_page_config(
    page_title="전국 고령화 단계구분도",
    page_icon="🗺️",
    layout="wide",
)

DATA_URL = (
    "https://raw.githubusercontent.com/hoyeon-max/project2//main/"
    "population_ts_yearly_gender.zip"
)

GEOJSON_URL = (
    "https://raw.githubusercontent.com/hoyeon-max/project2//main/"
    "sigungu_kr.geojson"
)

st.title("🗺️ 전국 인구 단계구분도")
st.caption("연도별 시군구 인구 지표를 지도에서 비교해 보세요.")

# 고령화율 구간은 모든 연도에 동일하게 적용
AGING_BOUNDS = [0, 19, 23, 28, 38, 50]
AGING_COLORS = [
    "#fff5eb",
    "#fdd0a2",
    "#fdae6b",
    "#e6550d",
    "#7f2704",
]

# 유소년 비율은 고령화율보다 낮으므로 별도 구간 적용
YOUTH_BOUNDS = [0, 5, 8, 11, 14, 25]
YOUTH_COLORS = [
    "#f7fbff",
    "#c6dbef",
    "#6baed6",
    "#2171b5",
    "#08306b",
]


# ==================================================
# 2. 행정구역 코드 처리
# ==================================================
def normalize_code(value):
    """행정구역 코드를 문자열 5자리로 정리합니다."""
    if value is None:
        return ""

    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass

    code = str(value).strip()
    code = re.sub(r"\.0$", "", code)

    if not code or code.lower() in {"nan", "none", "<na>"}:
        return ""

    if code.isdigit():
        code = code.zfill(5)

    code = code[:5]

    # 과거 행정구역 코드와 현재 경계 코드 연결
    # 강원도: 42 -> 51
    # 전라북도: 45 -> 52
    if code.startswith("42"):
        code = "51" + code[2:]
    elif code.startswith("45"):
        code = "52" + code[2:]

    # 과거 군위군 코드 -> 현재 대구 군위군 코드
    if code == "47720":
        code = "27720"

    return code


def get_age_number(column_name):
    """'계_65세', '계_100세 이상'에서 나이를 추출합니다."""
    match = re.search(r"계_(\d+)", str(column_name))
    if match:
        return int(match.group(1))
    return None


# ==================================================
# 3. 인구 데이터 읽기
# ==================================================
@st.cache_data(show_spinner="인구 데이터를 불러오는 중...")
def load_population():
    response = requests.get(DATA_URL, timeout=60)
    response.raise_for_status()

    # 압축 ZIP 안의 CSV 읽기
    from io import BytesIO

    df = pd.read_csv(
        BytesIO(response.content),
        compression="zip",
        dtype={"코드": "string"},
        low_memory=False,
    )

    required = {"연도", "시도", "시군구", "동", "코드"}
    missing = required - set(df.columns)

    if missing:
        raise ValueError(
            "인구 데이터에 필요한 열이 없습니다: "
            + ", ".join(sorted(missing))
        )

    df["연도"] = pd.to_numeric(df["연도"], errors="coerce")
    df = df[df["연도"].between(2015, 2026)].copy()

    if df.empty:
        raise ValueError("2015~2026년 인구 자료가 없습니다.")

    # 코드 정리 및 행정구역 개편 코드 변환
    df["코드"] = df["코드"].map(normalize_code)

    # 계_로 시작하는 열만 인구 합산에 사용
    # 남_와 여_ 열은 중복 합산하지 않습니다.
    total_cols = [
        col for col in df.columns
        if str(col).startswith("계_")
        and get_age_number(col) is not None
    ]

    if not total_cols:
        raise ValueError("'계_0세' 형식의 인구 열을 찾지 못했습니다.")

    elderly_cols = [
        col for col in total_cols
        if get_age_number(col) >= 65
    ]

    youth_cols = [
        col for col in total_cols
        if 0 <= get_age_number(col) <= 14
    ]

    if not elderly_cols:
        raise ValueError("65세 이상 인구 열을 찾지 못했습니다.")

    if not youth_cols:
        raise ValueError("0~14세 인구 열을 찾지 못했습니다.")

    for col in total_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    df["전체인구"] = df[total_cols].sum(axis=1)
    df["고령인구"] = df[elderly_cols].sum(axis=1)
    df["유소년인구"] = df[youth_cols].sum(axis=1)

    # 연도 및 시군구 코드별로 읍·면·동 인구 합산
    grouped = (
        df[df["코드"] != ""]
        .groupby(["연도", "코드"], as_index=False)
        .agg(
            전체인구=("전체인구", "sum"),
            고령인구=("고령인구", "sum"),
            유소년인구=("유소년인구", "sum"),
        )
    )

    grouped["고령화율"] = (
        grouped["고령인구"]
        .div(grouped["전체인구"].where(grouped["전체인구"] > 0))
        * 100
    )

    grouped["유소년비율"] = (
        grouped["유소년인구"]
        .div(grouped["전체인구"].where(grouped["전체인구"] > 0))
        * 100
    )

    return grouped


# ==================================================
# 4. 시군구 경계 GeoJSON 읽기
# ==================================================
@st.cache_data(show_spinner="시군구 경계 지도를 불러오는 중...")
def load_geojson():
    response = requests.get(GEOJSON_URL, timeout=60)
    response.raise_for_status()

    geojson = response.json()

    if geojson.get("type") != "FeatureCollection":
        raise ValueError("GeoJSON이 FeatureCollection 형식이 아닙니다.")

    features = []
    rows = []

    for feature in geojson.get("features", []):
        properties = feature.get("properties", {})
        code = normalize_code(properties.get("코드"))

        if not code:
            continue

        properties["코드"] = code
        feature["properties"] = properties
        features.append(feature)

        rows.append({
            "코드": code,
            "시군구": str(properties.get("시군구", "이름 없음")),
            "시도": str(properties.get("시도", "")),
        })

    if not features:
        raise ValueError("GeoJSON에 유효한 시군구 경계가 없습니다.")

    geojson["features"] = features

    geo_df = pd.DataFrame(rows).drop_duplicates("코드")

    return geojson, geo_df


# ==================================================
# 5. 데이터 불러오기
# ==================================================
try:
    population = load_population()
    geojson, geo_df = load_geojson()

except Exception as error:
    st.error("데이터를 불러오는 중 오류가 발생했습니다.")
    st.exception(error)
    st.stop()


# ==================================================
# 6. 연도 슬라이더와 지표 선택
# ==================================================
available_years = sorted(
    population["연도"].dropna().astype(int).unique().tolist()
)

if not available_years:
    st.error("선택할 수 있는 연도 데이터가 없습니다.")
    st.stop()

control1, control2 = st.columns([1, 1])

with control1:
    selected_year = st.slider(
        "📅 기준 연도",
        min_value=min(available_years),
        max_value=max(available_years),
        value=max(available_years),
        step=1,
    )

with control2:
    indicator_label = st.selectbox(
        "📊 지도 지표",
        ["고령화율 (65세 이상)", "유소년 비율 (0~14세)"],
    )

if indicator_label == "고령화율 (65세 이상)":
    indicator_col = "고령화율"
    indicator_title = "고령화율"
    indicator_unit = "%"
    bounds = AGING_BOUNDS
    colors = AGING_COLORS
    indicator_description = (
        "65세 이상 인구 ÷ 전체 인구 × 100"
    )
else:
    indicator_col = "유소년비율"
    indicator_title = "유소년 비율"
    indicator_unit = "%"
    bounds = YOUTH_BOUNDS
    colors = YOUTH_COLORS
    indicator_description = (
        "0~14세 인구 ÷ 전체 인구 × 100"
    )

year_data = population[
    population["연도"] == selected_year
].copy()

if year_data.empty:
    st.warning(f"{selected_year}년 인구 자료가 없습니다.")
    st.stop()


# ==================================================
# 7. 시도 선택
# ==================================================
province_names = sorted(
    name for name in geo_df["시도"].dropna().unique().tolist()
    if str(name).strip()
)

province_options = ["전국"] + province_names

selected_province = st.selectbox(
    "📍 표시할 지역",
    province_options,
    index=0,
)

# GeoJSON 코드와 인구 코드를 기준으로 결합
map_df = geo_df.merge(
    year_data,
    on="코드",
    how="left",
    validate="one_to_one",
)

# 전국 지도 지표는 전국 인구 합계로 계산
national_total = year_data["전체인구"].sum()

if national_total > 0:
    if indicator_col == "고령화율":
        national_numerator = year_data["고령인구"].sum()
    else:
        national_numerator = year_data["유소년인구"].sum()

    national_rate = national_numerator / national_total * 100
else:
    national_rate = float("nan")

# 지표별 최고·최저 지역
valid_all = map_df.dropna(subset=[indicator_col]).copy()

if valid_all.empty:
    st.error(
        "인구 데이터와 지도 경계 코드가 연결되지 않았습니다. "
        "코드 열의 형식과 행정구역 코드를 확인해 주세요."
    )
    st.stop()

highest = valid_all.loc[valid_all[indicator_col].idxmax()]
lowest = valid_all.loc[valid_all[indicator_col].idxmin()]


# ==================================================
# 8. 지도 위 지표 카드 3개
# ==================================================
card1, card2, card3 = st.columns(3)

with card1:
    st.metric(
        f"전국 {indicator_title}",
        f"{national_rate:.2f}%" if pd.notna(national_rate) else "자료 없음",
    )

with card2:
    st.metric(
        f"{indicator_title} 최고",
        str(highest["시군구"]),
        f"{highest[indicator_col]:.2f}%",
    )

with card3:
    st.metric(
        f"{indicator_title} 최저",
        str(lowest["시군구"]),
        f"{lowest[indicator_col]:.2f}%",
    )

st.caption(
    f"선택 연도: {selected_year}년 · 산출 기준: {indicator_description}"
)


# ==================================================
# 9. 지도 데이터 필터링
# ==================================================
if selected_province == "전국":
    display_geo_df = geo_df.copy()
    display_map_df = map_df.copy()
    display_geojson = geojson
else:
    display_geo_df = geo_df[
        geo_df["시도"] == selected_province
    ].copy()

    display_map_df = map_df[
        map_df["시도"] == selected_province
    ].copy()

    selected_codes = set(display_geo_df["코드"])

    display_geojson = {
        "type": "FeatureCollection",
        "features": [
            feature
            for feature in geojson["features"]
            if feature.get("properties", {}).get("코드") in selected_codes
        ],
    }

matched = display_map_df.dropna(subset=[indicator_col]).copy()
unmatched = display_map_df[
    display_map_df[indicator_col].isna()
].copy()

# 지도 범위에 사용할 코드가 없는 경우 안내
if display_map_df.empty or not display_geojson["features"]:
    st.warning("선택한 지역에 표시할 경계 데이터가 없습니다.")
    st.stop()


# ==================================================
# 10. 단계별 색상표 생성
# ==================================================
def make_discrete_colorscale(boundaries, palette):
    """
    구간 경계에서 색이 부드럽게 섞이지 않고
    계단식으로 바뀌도록 Plotly 색상표를 생성합니다.
    """
    maximum = boundaries[-1]
    scale = [[0, palette[0]]]

    for i, boundary in enumerate(boundaries[1:-1], start=1):
        position = boundary / maximum
        scale.append([position, palette[i - 1]])
        scale.append([position, palette[i]])

    scale.append([1, palette[-1]])
    return scale


colorscale = make_discrete_colorscale(bounds, colors)
zmax = bounds[-1]

# ==================================================
# 11. Plotly 단계구분도
# ==================================================
fig = go.Figure()

# 자료가 있는 시군구
if not matched.empty:
    fig.add_trace(
        go.Choropleth(
            geojson=display_geojson,
            featureidkey="properties.코드",
            locations=matched["코드"].astype(str),
            z=matched[indicator_col],
            customdata=matched[["시군구", "시도"]].to_numpy(),
            zmin=0,
            zmax=zmax,
            colorscale=colorscale,
            autocolorscale=False,
            marker={
                "line": {
                    "color": "white",
                    "width": 0.5,
                }
            },
            colorbar={
                "title": {"text": f"{indicator_title} (%)"},
                "tickmode": "array",
                "tickvals": bounds,
                "ticktext": [
                    f"{value}%"
                    for value in bounds[:-1]
                ] + [f"{bounds[-1]}% 이상"],
                "thickness": 18,
                "len": 0.75,
            },
            hovertemplate=(
                "<b>%{customdata[0]}</b><br>"
                "시도: %{customdata[1]}<br>"
                f"{indicator_title}: "
                "%{z:.2f}%"
                "<extra></extra>"
            ),
            name=indicator_title,
        )
    )

# 자료가 없는 시군구는 회색으로 표시
if not unmatched.empty:
    fig.add_trace(
        go.Choropleth(
            geojson=display_geojson,
            featureidkey="properties.코드",
            locations=unmatched["코드"].astype(str),
            z=[0] * len(unmatched),
            zmin=0,
            zmax=1,
            colorscale=[
                [0, "#bdbdbd"],
                [1, "#bdbdbd"],
            ],
            showscale=False,
            marker={
                "line": {
                    "color": "white",
                    "width": 0.5,
                }
            },
            customdata=unmatched[["시군구", "시도"]].to_numpy(),
            hovertemplate=(
                "<b>%{customdata[0]}</b><br>"
                "시도: %{customdata[1]}<br>"
                "자료 없음: 코드 연결 확인 필요"
                "<extra></extra>"
            ),
            name="자료 없음",
        )
    )

fig.update_geos(
    visible=False,
    fitbounds="locations",
    showcountries=False,
    showcoastlines=False,
    showland=False,
    showframe=False,
    bgcolor="rgba(0,0,0,0)",
)

fig.update_layout(
    title={
        "text": (
            f"{selected_year}년 {selected_province} "
            f"시군구별 {indicator_title}"
        ),
        "x": 0.02,
        "xanchor": "left",
    },
    height=760,
    margin={"l": 0, "r": 0, "t": 60, "b": 0},
    paper_bgcolor="white",
    plot_bgcolor="white",
    font={"family": "Arial, sans-serif"},
    legend={"orientation": "h", "y": 1.02, "x": 0.02},
)

st.plotly_chart(fig, use_container_width=True)

# 회색 지역이 있는 경우 지도 아래 안내
if not unmatched.empty:
    st.warning(
        f"회색 지역 {len(unmatched):,}곳은 선택한 연도의 인구 코드와 "
        "경계 코드가 일치하지 않아 지표를 표시할 수 없습니다. "
        "행정구역 개편이나 코드 누락 여부를 확인해 주세요."
    )
else:
    st.caption("모든 표시 지역에 인구 데이터가 연결되었습니다.")

st.caption(
    "색이 진할수록 해당 지표의 비율이 높습니다. "
    "색 구간 경계는 연도를 바꾸어도 동일하게 유지됩니다."
)


# ==================================================
# 12. 지역별 수치 표
# ==================================================
with st.expander("시군구별 상세 데이터 보기"):
    table = display_map_df[
        [
            "시도",
            "시군구",
            "코드",
            "전체인구",
            "고령인구",
            "유소년인구",
            "고령화율",
            "유소년비율",
        ]
    ].copy()

    table = table.sort_values(
        ["시도", "시군구"],
        na_position="last",
    )

    st.dataframe(
        table.style.format(
            {
                "전체인구": "{:,.0f}",
                "고령인구": "{:,.0f}",
                "유소년인구": "{:,.0f}",
                "고령화율": "{:.2f}%",
                "유소년비율": "{:.2f}%",
            },
            na_rep="자료 없음",
        ),
        use_container_width=True,
        hide_index=True,
    )

    csv_data = table.to_csv(index=False).encode("utf-8-sig")

    st.download_button(
        "상세 데이터 CSV 다운로드",
        data=csv_data,
        file_name=f"{selected_year}_{selected_province}_인구지표.csv",
        mime="text/csv",
    )
