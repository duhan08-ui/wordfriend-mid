# 단어친구 웹 (WordFriend Web)
# - 학습/퀴즈: 아이용. 저장소 안의 words.csv + audio/*.mp3 를 그대로 사용
# - 관리: 보호자용. 단어 목록 저장 시 새 단어의 원어민 MP3(edge-tts)를 자동 생성해
#   words.csv 와 함께 GitHub 에 한 번의 커밋으로 반영 → 웹/안드로이드 앱 모두에 적용됨

import asyncio
import base64
import csv
import io
import os
import random
import re
import tempfile

import requests
import streamlit as st

# 앱별 설정 (Secrets). 없으면 초등판 기본값 → 같은 코드로 초등판/중등판 둘 다 운영 가능
def _secret(key, default):
    try:
        return st.secrets.get(key, default)
    except Exception:   # secrets.toml 이 아예 없는 로컬 실행
        return default


APP_TITLE = _secret("APP_TITLE", "단어친구")
SUPA_TABLE = _secret("SUPA_TABLE", "wf_stats")   # 중등판: wf_stats_mid
MAX_TTS_PER_SAVE = int(_secret("MAX_TTS_PER_SAVE", 250))  # 한 번 저장에 만드는 발음 최대 수

st.set_page_config(page_title=APP_TITLE, page_icon="🦓", layout="centered")

PRAISES = ["Good job!", "Excellent!", "Great!", "Perfect!", "Wonderful!"]

SUPA_URL = "https://mztadmbkbrsvqmsqojcp.supabase.co"
SUPA_KEY = "sb_publishable_Za91TlCA4yg1crtS2-VHHg_r6BOi0an"

VOICES = [
    "en-US-JennyNeural",   # 여성, 또렷하고 자연스러움 (기본)
    "en-US-AnaNeural",     # 어린이 목소리
    "en-US-GuyNeural",     # 남성
    "en-GB-SoniaNeural",   # 영국식
]


# ---------- 공통 ----------

def slug(en: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", en.lower()).strip("_")


def load_words():
    words = []
    if not os.path.exists("words.csv"):
        return words
    with open("words.csv", encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row or row[0].strip() in ("", "en"):
                continue
            row = [c.strip() for c in row] + ["", "", ""]
            words.append({"en": row[0], "ko": row[1], "emoji": row[2], "group": row[3]})
    return words


def audio_path(en: str):
    p = os.path.join("audio", slug(en) + ".mp3")
    return p if os.path.exists(p) else None


def play(en: str, autoplay=False):
    p = audio_path(en)
    if p:
        st.audio(p, autoplay=autoplay)
    else:
        st.caption("🔇 아직 발음 파일이 없어요 (관리 페이지에서 저장하면 생성됩니다)")


def big_card(word):
    st.markdown(
        f"""
        <div style="background:#fff;border-radius:28px;padding:36px 16px;text-align:center;
                    box-shadow:0 4px 14px rgba(0,0,0,.08);margin-bottom:12px;">
          <div style="font-size:64px;line-height:1.1;">{word['emoji']}</div>
          <div style="font-size:52px;font-weight:800;color:#3E3A39;">{word['en']}</div>
          <div style="font-size:26px;color:#8A8580;margin-top:6px;">{word['ko']}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ---------- 학습 ----------

def page_learn(words):
    st.subheader("📖 단어 배우기")
    seen = []
    for w in words:
        if w["group"] and w["group"] not in seen:
            seen.append(w["group"])
    groups = ["🌈 전체"] + seen
    g = st.selectbox("범위", groups)
    cur = [w for w in words if g == "🌈 전체" or w["group"] == g]
    if not cur:
        st.info("단어가 없어요.")
        return

    key = f"learn_idx_{g}"
    idx = st.session_state.get(key, 0) % len(cur)
    w = cur[idx]

    st.caption(f"{idx + 1} / {len(cur)}")
    big_card(w)
    play(w["en"], autoplay=True)

    c1, c2 = st.columns(2)
    if c1.button("◀ 이전", use_container_width=True):
        st.session_state[key] = (idx - 1) % len(cur)
        st.rerun()
    if c2.button("다음 ▶", use_container_width=True, type="primary"):
        st.session_state[key] = (idx + 1) % len(cur)
        st.rerun()


# ---------- 퀴즈 ----------

def make_questions(words, mode):
    pool = random.sample(words, min(10, len(words)))
    qs = []
    for w in pool:
        m = mode if mode != "MIX" else random.choice(["LISTEN", "EN_KO", "KO_EN"])
        use_en = m == "KO_EN"
        answer = w["en"] if use_en else w["ko"]
        wrong = list({(x["en"] if use_en else x["ko"]) for x in words} - {answer})
        choices = random.sample(wrong, min(3, len(wrong))) + [answer]
        random.shuffle(choices)
        qs.append({"w": w, "mode": m, "choices": choices, "answer": answer})
    return qs


def page_quiz(words):
    st.subheader("🎯 퀴즈 놀이")
    if len(words) < 4:
        st.info("퀴즈를 하려면 단어가 4개 이상 필요해요.")
        return

    ss = st.session_state
    if "quiz" not in ss:
        mode = st.radio(
            "어떤 퀴즈로 할까?",
            ["LISTEN", "EN_KO", "KO_EN", "MIX"],
            format_func=lambda m: {
                "LISTEN": "🎧 듣고 뜻 고르기",
                "EN_KO": "👀 영어 보고 뜻 고르기",
                "KO_EN": "🇰🇷 한글 보고 영어 고르기",
                "MIX": "🌈 섞어서!",
            }[m],
        )
        if st.button("시작! 🚀", type="primary", use_container_width=True):
            ss.quiz = {"qs": make_questions(words, mode), "i": 0, "score": 0,
                       "wrong": [], "picked": None}
            st.rerun()
        return

    q = ss.quiz
    if q["i"] >= len(q["qs"]):
        st.markdown(f"## {q['score']} / {len(q['qs'])}")
        st.markdown("완벽해요! 🏆" if q["score"] == len(q["qs"]) else "참 잘했어요! 🌟")
        for w in {x["en"]: x for x in q["wrong"]}.values():
            st.write(f"🔍 **{w['en']}** — {w['ko']}")
            play(w["en"])
        if st.button("한 번 더! 🔁", type="primary", use_container_width=True):
            del ss.quiz
            st.rerun()
        return

    item = q["qs"][q["i"]]
    w = item["w"]
    st.caption(f"{q['i'] + 1} / {len(q['qs'])}")

    if item["mode"] == "LISTEN":
        st.markdown("### 🔊 잘 듣고 뜻을 골라요")
        play(w["en"], autoplay=q["picked"] is None)
    elif item["mode"] == "EN_KO":
        st.markdown(f"### {w['en']}  <span style='font-size:16px;color:#8A8580'>무슨 뜻일까요?</span>", unsafe_allow_html=True)
        play(w["en"], autoplay=q["picked"] is None)
    else:
        st.markdown(f"### {w['ko']}  <span style='font-size:16px;color:#8A8580'>영어로 뭘까요?</span>", unsafe_allow_html=True)

    if q["picked"] is None:
        for c in item["choices"]:
            if st.button(c, use_container_width=True, key=f"c_{q['i']}_{c}"):
                q["picked"] = c
                if c == item["answer"]:
                    q["score"] += 1
                else:
                    q["wrong"].append(w)
                st.rerun()
    else:
        ok = q["picked"] == item["answer"]
        if ok:
            st.success(f"⭕ 정답! **{item['answer']}**")
            p = audio_path(random.choice(PRAISES))
            if p:
                st.audio(p, autoplay=True)
        else:
            st.error(f"❌ 정답은 **{item['answer']}** 이에요")
        if not ok:
            play(w["en"], autoplay=True)
        if st.button("다음 문제 ▶", type="primary", use_container_width=True):
            q["i"] += 1
            q["picked"] = None
            st.rerun()


# ---------- 관리 (보호자) ----------

def gh(path, method="GET", **kw):
    r = requests.request(
        method,
        f"https://api.github.com/repos/{st.secrets['GH_REPO']}/{path}",
        headers={
            "Authorization": f"Bearer {st.secrets['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30,
        **kw,
    )
    r.raise_for_status()
    return r.json()


def commit_files(files: dict, message: str):
    """여러 파일(경로→bytes)을 GitHub에 '한 번의 커밋'으로 반영"""
    branch = st.secrets.get("GH_BRANCH", "main")
    head = gh(f"git/ref/heads/{branch}")["object"]["sha"]
    base_tree = gh(f"git/commits/{head}")["tree"]["sha"]
    tree = []
    for path, data in files.items():
        blob = gh("git/blobs", "POST",
                  json={"content": base64.b64encode(data).decode(), "encoding": "base64"})
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    new_tree = gh("git/trees", "POST", json={"base_tree": base_tree, "tree": tree})
    commit = gh("git/commits", "POST",
                json={"message": message, "tree": new_tree["sha"], "parents": [head]})
    gh(f"git/refs/heads/{branch}", "PATCH", json={"sha": commit["sha"]})


def gen_mp3(text: str, voice: str) -> bytes:
    import edge_tts

    async def _run(path):
        await edge_tts.Communicate(text, voice).save(path)

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        path = f.name
    try:
        asyncio.run(_run(path))
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.unlink(path)


def check_admin() -> bool:
    if st.session_state.get("auth"):
        return True
    pw = st.text_input("비밀번호", type="password", key="admin_pw")
    if st.button("확인", key="admin_ok"):
        if pw == st.secrets.get("ADMIN_PASSWORD", ""):
            st.session_state.auth = True
            st.rerun()
        st.error("비밀번호가 달라요")
    return False


def kst(iso: str) -> str:
    """서버 UTC 시각을 한국 시간 문자열로"""
    try:
        from datetime import datetime, timedelta, timezone
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso[:16].replace("T", " ")


def page_report(words):
    st.subheader("📊 학습 리포트")
    if not check_admin():
        return

    try:
        r = requests.get(
            f"{SUPA_URL}/rest/v1/{SUPA_TABLE}?select=*&order=updated_at.desc",
            headers={"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}"},
            timeout=10,
        )
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        st.error(f"서버에서 기록을 불러오지 못했어요: {e}")
        return

    if not rows:
        st.info("아직 올라온 기록이 없어요. 아이 폰에서 학습하면 자동으로 올라옵니다.")
        return

    labels = {f"{x.get('child_name') or '기기'} ({x['device_id'][:6]}…)": x for x in rows}
    pick = st.selectbox("기기", list(labels.keys()))
    row = labels[pick]
    st.caption(f"마지막 기록: {kst(row.get('updated_at', ''))} (한국 시간)")

    stats = row.get("stats") or {}
    meta = stats.get("meta") or {}
    if meta:
        st.caption(
            f"📲 이 폰 적용 상태: 앱 v{meta.get('appVer','?')} · 단어 {meta.get('words',0)}개 · "
            f"배움 {meta.get('seen',0)}개 · 발음 {meta.get('audio',0)}개 · 마지막 동기화 {meta.get('lastSync','-')}"
        )
    days = stats.get("days") or {}
    keys = sorted(days.keys(), reverse=True)[:14]

    # ---- 주간 요약: 최근 7일 vs 그 전 7일 ----
    from datetime import datetime, timedelta, timezone
    def day_val(d0, field):
        return (days.get(d0.isoformat()) or {}).get(field, 0)
    # 서버는 UTC로 돌아서 date.today()를 쓰면 한국 새벽(0~9시)에 날짜가 하루 밀림 → KST 기준 오늘
    t = (datetime.now(timezone.utc) + timedelta(hours=9)).date()
    wk = [t - timedelta(days=i) for i in range(7)]
    prev = [t - timedelta(days=i) for i in range(7, 14)]
    def summary(ds):
        play = sum(day_val(d, "playSec") for d in ds)
        r = sum(day_val(d, "right") for d in ds)
        w = sum(day_val(d, "wrong") for d in ds)
        rate = round(r / (r + w) * 100) if (r + w) else 0
        return play // 60, rate, r + w
    p1, rate1, n1 = summary(wk)
    p0, rate0, n0 = summary(prev)

    c1, c2, c3 = st.columns(3)
    c1.metric("최근 7일 학습", f"{p1}분", delta=f"{p1 - p0:+d}분")
    c2.metric("최근 7일 정답률", f"{rate1}%" if n1 else "-",
              delta=(f"{rate1 - rate0:+d}%p" if n1 and n0 else None))
    c3.metric("최근 7일 문제 수", n1, delta=n1 - n0)

    # ---- 그래프 ----
    import pandas as pd
    ordered = sorted(days.keys())[-14:]
    if ordered:
        df = pd.DataFrame({
            "날짜": [k[5:] for k in ordered],
            "학습(분)": [round(days[k].get("playSec", 0) / 60, 1) for k in ordered],
        }).set_index("날짜")
        st.markdown("##### ⏱ 학습 시간 (최근 14일)")
        st.bar_chart(df, height=220)

        rated = [k for k in ordered
                 if days[k].get("right", 0) + days[k].get("wrong", 0) > 0]
        if rated:
            df2 = pd.DataFrame({
                "날짜": [k[5:] for k in rated],
                "정답률(%)": [
                    round(days[k]["right"] / (days[k]["right"] + days[k].get("wrong", 0)) * 100)
                    if (days[k].get("right", 0) + days[k].get("wrong", 0)) else 0
                    for k in rated
                ],
            }).set_index("날짜")
            st.markdown("##### 🎯 정답률 추이")
            st.line_chart(df2, height=220)

    # ---- 라이트너 상자 분포 ----
    wstats_all = stats.get("words") or {}
    tried = [s for s in wstats_all.values() if s.get("right", 0) + s.get("wrong", 0) > 0]
    if tried:
        dist = {f"상자{b}": sum(1 for s in tried if s.get("box", 0) == b) for b in range(5)}
        weak_n = dist["상자0"] + dist["상자1"]
        st.markdown(f"##### 📦 단어 상태 (약한 단어 {weak_n}개)")
        st.bar_chart(pd.DataFrame([dist]).T.rename(columns={0: "단어 수"}), height=200)
        st.caption("상자0~1 = 자주 나오게 되는 약한 단어 · 상자4 = 완전히 익힌 단어")

    # ===== 이번 주(월~일) 요약 =====
    from datetime import datetime, timedelta, timezone
    _kst = timezone(timedelta(hours=9))
    _today = datetime.now(_kst).date()
    _monday = _today - timedelta(days=(_today.weekday()))  # 월=0
    _wk_play = _wk_star = _wk_days = _wk_r = _wk_w = 0
    _wk_bar = []
    _dow_names = ["월","화","수","목","금","토","일"]
    for _i in range(7):
        _d = _monday + timedelta(days=_i)
        _key = _d.strftime("%Y-%m-%d")
        _o = days.get(_key) or {}
        _p = _o.get("playSec", 0); _r = _o.get("right", 0); _w = _o.get("wrong", 0)
        if _p > 0 or _r > 0 or _w > 0: _wk_days += 1
        _wk_play += _p; _wk_star += _o.get("stars", 0); _wk_r += _r; _wk_w += _w
        _wk_bar.append({"요일": _dow_names[_i], "공부(분)": round(_p/60, 1)})
    # 지금까지 모은 별 (전체 누적) — 앱 홈의 '이번 주 별'은 월요일에 0으로 리셋되므로 여기서만 볼 수 있음
    _star_all = sum((_o or {}).get("stars", 0) for _o in days.values())
    _star_today = (days.get(_today.isoformat()) or {}).get("stars", 0)

    st.markdown("##### ⭐ 모은 별")
    _sc1, _sc2, _sc3 = st.columns(3)
    _sc1.metric("오늘", f"⭐{_star_today}")
    _sc2.metric("이번 주 (월~일)", f"⭐{_wk_star}")
    _sc3.metric("지금까지 모두", f"⭐{_star_all}")

    st.markdown("##### 📅 이번 주 요약 (월~일)")
    _wc1, _wc2, _wc3, _wc4 = st.columns(4)
    _wc1.metric("공부 시간", f"{_wk_play//60}분")
    _wc2.metric("받은 별", f"⭐{_wk_star}")
    _wc3.metric("학습한 날", f"{_wk_days}일")
    _wc4.metric("정답/오답", f"{_wk_r}/{_wk_w}")
    import pandas as _pd
    st.bar_chart(_pd.DataFrame(_wk_bar).set_index("요일"), height=200)

    # ===== 세션(시각별) — 머문/공부 둘 다 =====
    _sess_rows = []
    for k in sorted(days.keys(), reverse=True)[:7]:
        for s in (days[k].get("sessions") or []):
            _elapsed = max(0, int((s.get("endMs", 0) - s.get("startMs", 0)) / 1000))
            _sess_rows.append({
                "날짜": k[5:],
                "시작": s.get("start", ""),
                "종료": s.get("end", ""),
                "머문(분)": round(_elapsed / 60, 1),
                "공부(분)": round(s.get("playSec", 0) / 60, 1),
                "⭕": s.get("right", 0),
                "❌": s.get("wrong", 0),
                "🎤": s.get("speak", 0),
            })
    if _sess_rows:
        st.markdown("##### ⏰ 학습 시간대 (세션별 · 머문/공부)")
        st.dataframe(_sess_rows, use_container_width=True, hide_index=True)

    st.markdown("##### 📅 일자별 기록")
    st.dataframe(
        [
            {
                "날짜": k,
                "접속(분)": round(days[k].get("appSec", 0) / 60, 1),
                "학습(분)": round(days[k].get("playSec", 0) / 60, 1),
                "⭕": days[k].get("right", 0),
                "❌": days[k].get("wrong", 0),
                "🎤": days[k].get("speak", 0),
                "⭐": days[k].get("stars", 0),
            }
            for k in keys
        ],
        use_container_width=True, hide_index=True,
    )

    wmap = {w["en"]: w for w in words}
    wstats = stats.get("words") or {}
    # 상태 우선: 🔴(box 낮음) 먼저 → 그 안에서 틀린 횟수 많은 순 (위에서부터 도와줄 순서)
    weak = sorted(
        ((en, s) for en, s in wstats.items() if s.get("wrong", 0) > 0),
        key=lambda x: (x[1].get("box", 0), -x[1].get("wrong", 0)),
    )[:15]
    def _box_label(b):
        return {0: "🔴 아직 어려워요", 1: "🔴 조금 어려워요", 2: "🟡 외우는 중",
                3: "🟢 거의 외웠어요"}.get(int(b or 0), "🟢 다 외웠어요")

    st.markdown("##### ❗ 많이 틀린 단어 TOP")
    st.caption("위에서부터 지금 도와주면 좋은 순서예요. 🔴 = 아직 어려운 단어, 🟢 = 다 외운 단어.")
    if not weak:
        st.caption("틀린 단어가 아직 없어요 👍")
    else:
        st.dataframe(
            [
                {
                    "단어": en,
                    "뜻": wmap.get(en, {}).get("ko", ""),
                    "상태": _box_label(s.get("box", 0)),
                    "틀림": s.get("wrong", 0),
                    "맞힘": s.get("right", 0),
                    "최근": s.get("last", ""),
                }
                for en, s in weak
            ],
            use_container_width=True, hide_index=True,
        )


def page_admin(words):
    st.subheader("⚙️ 단어 관리 (보호자)")

    if not check_admin():
        return

    st.markdown("한 줄에 한 단어씩 · 형식: `영어,뜻,이모지,그룹` — 그룹은 아이 화면에 그대로 보이니 읽기 쉬운 이름으로 (예: 중1-1 학교생활) · "
                "**뜻에 쉼표(,) 금지** — 여러 뜻은 `·` 로 (예: `lie,거짓말·눕다,🤥,중2-2 친구·관계`)")
    cur_text = "\n".join(
        ",".join([w["en"], w["ko"], w["emoji"], w["group"]]).rstrip(",") for w in words)
    text = st.text_area("단어 목록 (이 내용 전체가 그대로 저장됩니다)", cur_text, height=320)

    # 현재 입력칸 기준 단어 수 (저장 전 검증용)
    parsed = []
    for _line in text.splitlines():
        _p = [x.strip() for x in re.split(r"[,\t]", _line.strip(), maxsplit=3)]
        if len(_p) >= 2 and _p[0] and _p[1] and _p[0] != "en":
            parsed.append(_p + ["", ""])
    if parsed:
        from collections import Counter
        _g = Counter((p[3] or "그룹없음") for p in parsed)
        _detail = " · ".join(f"{k} {v}" for k, v in _g.items())
        st.caption(f"📚 지금 목록: **{len(parsed)}개** — {_detail}")
    else:
        st.caption("📚 지금 목록: 0개")
    # 쉼표가 4칸을 넘는 줄은 앱/웹에서 뜻·그룹이 잘려 들어감 → 저장 전에 경고
    _bad = [_l.strip() for _l in text.splitlines()
            if _l.strip() and len(re.split(r"[,\t]", _l.strip())) > 4]
    if _bad:
        st.warning(f"⚠ 쉼표가 너무 많은 줄 {len(_bad)}개 — 뜻 안의 쉼표는 `·` 로 바꿔 주세요: "
                   + " / ".join(_bad[:5]) + (" …" if len(_bad) > 5 else ""))

    # 서버 발음 파일 상태 — 저장 후 페이지가 새로고침돼도 여기서 항상 확인 가능
    _targets = [p[0] for p in parsed] + PRAISES
    _have = sum(1 for t in _targets if audio_path(t))
    _miss = len(_targets) - _have
    if _miss == 0 and _targets:
        st.success(f"🔊 발음 파일: 전부 준비됨 ✅ ({_have}/{len(_targets)})")
    else:
        st.info(f"🔊 발음 파일: {_have}/{len(_targets)} · 부족 {_miss}개 — 저장을 누르면 부족분만 자동 생성돼요")

    voice = st.selectbox("발음 목소리", VOICES)
    regen = st.checkbox("모든 발음을 이 목소리로 다시 생성 (목소리를 바꿨을 때 체크)")
    if regen:
        st.warning(f"⚠ 전체 {len(_targets)}개를 처음부터 다시 만듭니다 (10분 이상 소요). "
                   "새 단어만 추가하는 경우라면 체크를 해제하세요!")
        if len(_targets) > MAX_TTS_PER_SAVE:
            st.error(f"단어가 {len(_targets)}개라 전체 다시 만들기는 30분 이상 걸리고, 도중에 창을 닫으면 "
                     "처음부터 다시 해야 해요. 가능하면 PC에서 창을 켜 둔 채로 진행하세요.")

    if st.button("💾 저장 → 웹/앱에 적용", type="primary", use_container_width=True):
        new_words = []
        for line in text.splitlines():
            parts = [p.strip() for p in re.split(r"[,\t]", line.strip(), maxsplit=3)]
            if len(parts) >= 2 and parts[0] and parts[1] and parts[0] != "en":
                parts += ["", ""]
                new_words.append({"en": parts[0], "ko": parts[1],
                                  "emoji": parts[2], "group": parts[3]})
        if not new_words:
            st.error("저장할 단어가 없어요. 형식: 영어,뜻")
            return

        buf = io.StringIO()
        buf.write("en,ko,emoji,group\n")
        for w in new_words:
            buf.write(",".join([w["en"], w["ko"], w["emoji"], w["group"]]) + "\n")
        files = {"words.csv": buf.getvalue().encode("utf-8")}

        texts = [w["en"] for w in new_words] + PRAISES
        todo = [t for t in texts if regen or not audio_path(t)]
        # 한 번에 너무 많이 만들면(수백 개) 도중에 끊겼을 때 전부 날아감 → 나눠서 저장
        # (목소리 전체 교체(regen)는 나누면 옛 목소리와 섞이므로 예외 — 한 번에 전부)
        remain = 0 if regen else max(0, len(todo) - MAX_TTS_PER_SAVE)
        if not regen:
            todo = todo[:MAX_TTS_PER_SAVE]
        if regen:
            import time as _time
            files["audio/_v.txt"] = str(int(_time.time())).encode("utf-8")
        prog = st.progress(0.0, text="원어민 발음 생성 중…")
        for i, t in enumerate(todo):
            try:
                files[f"audio/{slug(t)}.mp3"] = gen_mp3(t, voice)
            except Exception as e:
                st.warning(f"{t} 발음 생성 실패: {e}")
            prog.progress((i + 1) / max(len(todo), 1), text=f"발음 생성: {t}")
        prog.empty()

        try:
            commit_files(files, f"words: {len(new_words)}개 저장 (신규 발음 {len(todo)}개)")
        except Exception as e:
            st.error(f"GitHub 저장 실패: {e}")
            return

        st.success(
            f"저장 완료! 단어 {len(new_words)}개 · 발음 {len(todo)}개 생성. "
            "웹은 1~2분 안에 자동 재시작되며, 앱은 다음 실행(또는 단어 관리의 서버 동기화) 때 반영됩니다.")
        if remain:
            st.warning(f"🔊 아직 발음 {remain}개가 남았어요. 1~2분 뒤 페이지를 새로고침하고 "
                       "💾 저장을 한 번 더 누르면 이어서 만들어요 (이미 만든 건 건너뜀).")

    st.divider()
    st.markdown("#### ⚙️ 앱 원격 설정 — 재빌드 없이 즉시 반영")
    import json as _cjson
    _cfg = {}
    if os.path.exists("config.json"):
        try:
            _cfg = _cjson.load(open("config.json", encoding="utf-8"))
        except Exception:
            pass
    st.markdown("**학습 흐름 (새 단어 묶음제)**")
    st.caption("새 단어 N개 배우기 → 그 N개 섞어서 퀴즈 → 다 맞으면 ⭐ 지급 · "
               "틀리면 틀린 단어 배우고 다시 → 하루 상한까지")
    bc1, bc2 = st.columns(2)
    _batch = bc1.number_input("새 단어 묶음 크기 (= 퀴즈 문제 수)", 3, 50,
                              int(_cfg.get("batchSize", 10)))
    _cap = bc2.number_input("하루 별 상한 (0=무제한)", 0, 200, int(_cfg.get("dailyStarCap", 10)))
    _bstar = int(_cfg.get("batchStar", 5))  # (미사용 · 모드별 1개 고정)
    st.caption("퀴즈는 5가지(듣기·영어·한글·섞어서·철자)이고 각 만점마다 ⭐1개 → 한 묶음에서 최대 ⭐5개. "
               f"하루 상한 {int(_cap)}개면 하루 최대 {int(_cap)//5 if _cap else '∞'}묶음까지 별을 받아요 (그 뒤엔 공부만).")

    _stage = st.checkbox("📘 단원제 (단원을 순서대로 · 한 단원 익히면 다음 단원 해금)",
                         value=bool(_cfg.get("stageMode", 1)))
    if _stage:
        _ratio = st.slider("다음 단원 열리는 기준 (현재 단원 마스터 비율)",
                           50, 100, int(float(_cfg.get("stageUnlockRatio", 0.7)) * 100), step=5,
                           format="%d%%")
    else:
        _ratio = int(float(_cfg.get("stageUnlockRatio", 0.7)) * 100)
        st.caption("단원제를 끄면 배운 단어 전체에서 묶음이 나와요.")
    cc4, cc5, cc6 = st.columns(3)
    _retry = cc4.number_input("따라 말하기 재시도", 1, 9, int(_cfg.get("speakRetries", 3)))
    _slow = cc5.number_input("느린 발음 속도", 0.4, 1.0, float(_cfg.get("slowSpeed", 0.65)), step=0.05)
    _len = cc6.selectbox("말하기 판정 관대함", [0, 1, 2, 3],
        index=int(_cfg.get("speakLeniency", 2)),
        format_func=lambda v: {0:"0 엄격", 1:"1 보통", 2:"2 관대(기본)", 3:"3 매우 관대"}[v])
    st.caption("말하기 인식이 자꾸 실패하면 관대함을 3으로 올려보세요. 아무 소리나 통과되면 1로 낮추세요.")
    _abc = st.checkbox("🔤 홈에 '알파벳 소리' 버튼 보이기 (파닉스용 · 중등판은 보통 끔)",
                       value=bool(_cfg.get("showAbc", 0)))
    _child = st.text_input("👤 아이 이름 (학습 리포트·관리자 앱에 표시 · 비우면 폰 기종 이름)",
                           value=str(_cfg.get("childName", "")), max_chars=20)
    st.caption("저장하면 아이 폰이 다음에 앱을 열 때 조용히 자동 적용돼요 (아이 화면에 안내 없음). "
               "적용 확인은 각 폰의 관리자 대시보드 상단에서.")
    if st.button("⚙️ 설정 저장 → 앱에 적용", use_container_width=True):
        # 이 화면에 없는 키도 그대로 보존 (예: quizAll, starMinCorrect) → 저장할 때 설정이 사라지지 않게
        body = _cjson.dumps({
            **_cfg,
            "showAbc": 1 if _abc else 0,
            "childName": _child.strip(),
            "batchSize": int(_batch),
            "batchStar": int(_bstar),
            "dailyStarCap": int(_cap),
            "stageMode": 1 if _stage else 0,
            "stageUnlockRatio": round(_ratio / 100, 2),
            "speakRetries": int(_retry),
            "speakLeniency": int(_len),
            "slowSpeed": round(float(_slow), 2),
            "ttsSlow": float(_cfg.get("ttsSlow", 0.45)),
            "ttsNormal": float(_cfg.get("ttsNormal", 0.8)),
            "weakFirst": int(_cfg.get("weakFirst", 7)),
        }).encode("utf-8")
        try:
            commit_files({"config.json": body}, "config: 원격 설정 변경")
            st.success("설정 저장 완료! 폰은 다음에 앱을 열 때 자동 적용됩니다.")
        except Exception as e:
            st.error(f"저장 실패: {e}")

    st.divider()
    st.markdown("#### 📲 앱 업데이트 배포")
    cur_v, cur_code = "없음", 0
    if os.path.exists("app/version.json"):
        try:
            import json as _json
            v = _json.load(open("app/version.json", encoding="utf-8"))
            cur_code = int(v.get("versionCode") or 0)
            cur_v = f"v{v.get('versionName')} (code {cur_code})"
        except Exception:
            pass
    st.caption(f"서버에 올라온 버전: {cur_v} · 올리면 앱의 관리자 화면 > 🔄 앱 업데이트에서 받을 수 있어요")

    c1, c2 = st.columns(2)
    vname = c1.text_input("versionName (예: 2.2)")
    vcode = c2.number_input(
        "versionCode — build.gradle의 versionCode와 같게, 이전보다 크게",
        min_value=1, step=1, value=max(cur_code + 1, 1),
    )
    if cur_code and vcode <= cur_code:
        st.warning(f"지금 서버 코드({cur_code})보다 커야 폰에서 업데이트 버튼이 켜져요")
    apk = st.file_uploader("서명된 APK 파일", type=["apk"])

    if st.button("📲 APK 업로드", use_container_width=True):
        if not apk or not vname.strip():
            st.error("APK 파일과 versionName 을 입력해 주세요")
        else:
            import json as _json
            data = apk.getvalue()
            meta = _json.dumps({"versionCode": int(vcode), "versionName": vname.strip(),
                                "size": len(data)}).encode("utf-8")
            try:
                with st.spinner(f"업로드 중… ({len(data)//1024} KB)"):
                    commit_files({"app/wordfriend.apk": data, "app/version.json": meta},
                                 f"app: v{vname.strip()} (code {int(vcode)})")
                st.success(f"업로드 완료! 앱에서 [관리자 → 🔄 앱 업데이트] 로 설치할 수 있어요")
            except Exception as e:
                st.error(f"GitHub 저장 실패: {e}")


# ---------- 메인 ----------

words = load_words()
st.markdown(f"## 🦓 {APP_TITLE}")
tab = st.sidebar.radio("메뉴", ["📖 단어 배우기", "🎯 퀴즈 놀이", "📊 학습 리포트", "⚙️ 단어 관리"])
if tab.startswith("📖"):
    page_learn(words)
elif tab.startswith("🎯"):
    page_quiz(words)
elif tab.startswith("📊"):
    page_report(words)
else:
    page_admin(words)
