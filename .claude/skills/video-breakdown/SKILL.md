---
name: video-breakdown
description: 참고 영상 한 편을 컷 단위로 역분석해서 각 장면이 실사 촬영·AI 생성·3D·코드 모션그래픽·화면 녹화 중 무엇인지 판정하고, 촬영/조명/편집 리듬/자막/음성·BGM·효과음을 분석해 재현(복제 제작) 계획과 AI 생성 프롬프트까지 한국어 보고서로 정리한다. 오픈소스 rea-video를 검토된 커밋으로 안전하게 설치해 쓰고, 도구 단독 판정의 약점(재압축 영상의 실사를 AI로 오판)을 Claude가 직접 프레임을 보고 보정한다. 사용자가 영상 파일을 주며 "이 영상 어떻게 만든 거야", "AI로 만든 거야 실사야", "이 광고/쇼츠 분석해줘", "똑같이 만들려면?", "역분석/리버스 엔지니어링", "rea-video로 돌려줘", 쇼핑 쇼츠·틱톡 광고 레퍼런스 분석을 요청하면 사용한다.
---

# video-breakdown: 영상 제작 방식 역분석 (rea-video + Claude 시각 검수)

사용자가 준 영상이 **컷마다 어떻게 만들어졌는지**(실사 / AI 영상 / 3D / 코드 렌더 / 화면 녹화 등) 판정하고, **어떻게 다시 만들지**까지 한국어로 보고한다.

엔진은 오픈소스 [rea-video](https://github.com/JuneYaooo/rea-video)(Apache-2.0)다. 이 스킬은 실제 검토·실행 경험에서 얻은 교훈을 담고 있다 — 자세한 근거는 `references/lessons.md`.

## 반드시 지킬 원칙

1. **도구 판정을 그대로 전달하지 않는다.** 재압축된 영상(TikTok·카톡·위챗 등에서 받은 파일)에서는 도구만으로 실사를 "AI 100%"로 오판한다(실측: 실사 광고 14컷 전부 오판). **3단계 시각 검수는 생략 불가.**
2. **설치는 `scripts/setup.sh`로만.** 공식 `install_skill.sh`는 쓰지 않는다(시스템 Python의 opencv-python 삭제, `~/.claude`에 심볼릭 링크, 편집 모드 설치 → `git pull` 시 검토 없이 코드·지시문이 바뀜).
3. **영상을 외부로 보내지 않는다.** `--vlm`(외부 멀티모달 모델로 프레임 전송)은 사용자가 명시적으로 요청하고 엔드포인트·키를 직접 준 경우에만 쓴다. 기본은 오프라인 + Claude 시각 검수.
4. **유료 생성 호출 금지.** `remake-run`, `capsule-draft` 등 capsule-cinema 연동 명령은 실행하지 않는다. 재현은 계획·프롬프트까지만 제공.
5. **저작권:** 구조와 기법을 배우는 용도. 원본 영상·음악·인물·초상을 그대로 재사용하라고 권하지 않는다. 결과물을 올릴 때 플랫폼의 AI 생성물 표기 규정을 따르라고 안내한다.

## 0. 준비

```bash
CLI="$(bash .claude/skills/video-breakdown/scripts/setup.sh | tail -1)"   # 경로는 스킬 위치에 맞게
"$CLI" --help >/dev/null && echo ok
```

- 설치 위치: `~/.local/share/rea-video` (변경: `REA_VIDEO_DIR`). 첫 설치 2~3분, 이후엔 즉시.
- Python 3.10~3.12 필요. **3.13만 있으면 OCR 패키지가 설치 안 된다** → `uv python install 3.12` 또는 `PYTHON_BIN=python3.12`.
- 첫 분석 때 음원 분리·얼굴 인식 모델(약 70MB)을 GitHub에서 받는다(SHA-256 검증됨). 네트워크가 막혀 있으면 사용자에게 알린다.
- 다른 커밋으로 올리려면 먼저 diff를 검토한 뒤 `REA_VIDEO_REF=<commit>`로 실행한다.

## 1. 입력 확인

- 로컬 영상 파일 경로가 필요하다. URL만 받았다면 사용자가 직접 내려받아 파일로 달라고 한다(플랫폼 다운로드는 약관 문제).
- 결과 폴더: `OUT=~/video-breakdowns/<영상이름>` (작업 저장소 안에 두지 않는다 — 수십 MB).
- 화면 글자 언어 확인: OCR은 **중국어+영어(기본)와 일본어(`--ocr-lang ja`)만** 지원. **한국어 자막은 읽지 못한다** → 자막 분석은 3단계에서 Claude가 프레임을 보고 직접 받아 적는다.

## 2. 도구 분석 (영상 20초당 약 40~60초)

```bash
"$CLI" decompose "<영상>" -o "$OUT" --lang en          # 한국어 미지원 → en으로 받고 보고서는 한국어로 작성
"$CLI" summary "$OUT/breakdown.json"
```

- `breakdown.json`(수십만 자)과 `report.md` 전체를 읽지 않는다. `summary`와 필요한 절만 읽는다: `grep -n '^##' "$OUT/report.md"` → 해당 줄 범위만.
- 결과물: `report.html`, `report.md`, `storyboard.jpg`, `audio.png`, `shots/shot_XXX.jpg`(첫/중간/끝 프레임 시트), `stems/`(원음·목소리·배경음 분리), `packaging/overlays.ass`(자막 타임라인).

## 3. Claude 시각 검수 (필수)

```bash
"$CLI" review "$OUT"     # review_tasks.json(컷별 확인 항목·우선순위) + review_answers.json(답안 틀) 생성
```

1. `storyboard.jpg`를 보고 전체 인상을 잡는다.
2. **모든 컷**의 `shots/shot_XXX.jpg`를 본다(priority=high는 반드시). 애매하면 확대:
   `"$CLI" tiles "<영상>" --start 3.2 --end 4.0 --every 0.1 -o "$OUT/check.jpg"`
3. 판정 근거 체크리스트 — `references/lessons.md`의 「실사 vs AI 판별 단서」를 따른다. 핵심:
   - **실사 쪽 근거:** 배경 잡동사니·라벨·시계가 컷마다 일관되고 글자가 선명, 디스플레이 숫자 정상, 손·손목시계 자연스러움, 핸드헬드 흔들림·노출 변화, 생활감 있는 지저분함.
   - **AI 쪽 근거:** 손과 물체의 접촉이 어색(과일이 손에서 "떨어지듯 나타남"), 느리고 떠다니는 동작, 지나치게 고른 조명, 글자 깨짐, 형태 변형·질감 끓음, 컷 끝 정지, 24fps 소스 흔적(도구 증거).
   - 도구의 **강한 증거(AIGC 메타데이터 태그, 24→30 중복 프레임)는 쉽게 뒤집지 않는다.** 단 "깨끗한 화면/센서 노이즈 적음"만으로 AI라 한 판정은 재압축 때문일 수 있으니 화면 근거로 판단한다.
   - 확신이 없으면 확률을 나눈다(예: ai_video 0.55 / live_action 0.45)와 무엇이 부족한지 적는다.
4. 판정을 간단한 JSON으로 쓰고 헬퍼로 병합한다(`review_answers.json`을 손으로 편집하지 않아도 됨):

```bash
cat > "$OUT/verdicts.json" <<'EOF'
{"shots": {
  "shot_001": {"probs": {"live_action": 0.85, "ai_video": 0.15},
               "evidence": ["벽시계·수납함·우유통 라벨이 모든 컷에서 일관되고 선명"],
               "description": "식탁 위 휴대용 블렌더에 바나나를 넣는 손",
               "prompt": "(AI 컷일 때만) vertical 9:16, hand drops sliced strawberries into a navy portable blender on a marble counter, soft frontal light"}
}}
EOF
python3 -I .claude/skills/video-breakdown/scripts/fill_review.py "$OUT" "$OUT/verdicts.json"
"$CLI" apply-review "$OUT" --by claude --lang en
"$CLI" summary "$OUT/breakdown.json"          # 병합 후 결과로 보고
```

- AI 판정 컷에는 `prompt`(영어 영상 프롬프트: 구도·카메라 각도·주체 위치 → 동작과 끝 상태 → 카메라 움직임, 부정문 없이)를 채운다. 외부 VLM 없이는 재현 계획의 프롬프트가 "prompt missing"으로 비거나 description으로 대체되기 때문. 헬퍼가 `video_prompt`·`video_prompt_en` 둘 다에 넣는다.
- 재현할 계획이면 최상위 `cast`(객체: `style`, `characters`, `scenes`, `props`. 형식은 `scripts/fill_review.py` 상단 주석)도 채운다. 없으면 보고서의 「Cast, scenes and props」 절이 비어 있다.
- 병합 후 결론이 내 판단과 다르면 그 이유를 보고서에 적는다.

## 4. 소리 확인

- `audio.png`와 summary의 "Sound sources" 줄을 본다. 신호 기반 효과음 후보는 **신뢰도 낮음**(실측 약 40%만 진짜) → "가능성"으로 표현.
- 목소리 유형 표시(예: "sung")는 틀릴 수 있다. 실사 영상의 목소리는 현장음/사람 녹음일 가능성이 높다는 도구 추론을 우선한다.
- 직접 들을 수 없으므로 "음성은 사람 녹음 추정(확인 필요: `stems/vocals.wav` 청취)"처럼 확인 방법을 함께 준다.

## 5. 한국어 보고 (사용자 응답)

`references/report-template.md` 형식으로 쓴다. 요지:

1. **한 줄 결론** (예: "100% 휴대폰 실사, 6개 구도·컷 1.5초 간격, 사람 목소리 + 효과음 15개")
2. 컷 표: 번호 · 시간 · 판정(확률) · 근거 한 줄 · 화면 요약
3. 촬영·조명·색감·편집 리듬 (실사면 촬영 방법·장비, AI면 "생성 프롬프트용 묘사"라고 명시)
4. 자막·텍스트 효과·효과음 타이밍 (OCR 오인식은 보정해서 적고, 한국어 자막은 직접 읽은 내용)
5. 재현 계획: 실사 컷 → 촬영 계획 / AI 컷 → 생성 단위 묶음 + 영어 프롬프트 / 코드 컷 → Remotion 등
6. 불확실한 점과 확인 방법
7. 파일 경로: `report.html`(영문), 필요하면 `report.md`를 번역한 `report_ko.md`를 같은 폴더에 만든다.

보고서 이미지에서 한자 라벨이 □로 보이면 그 환경에 CJK 폰트가 없어서이다(도구 문제 아님) — 한 줄로만 언급한다.

## 하지 말 것

- 화풍만 보고 판단하지 않는다(카툰 3D는 AI일 수도 Blender일 수도 있다).
- 메타데이터가 없다는 것을 "AI 아님" 증거로 쓰지 않는다(플랫폼 재인코딩이 지운다).
- 코드 렌더 화면 속 글자를 "덧씌운 자막"으로 분류하지 않는다.
- 사용자의 실제 소재(제품 실물 컷 등)를 "AI로 생성 가능"이라고 쓰지 않는다.
- `install_skill.sh` 실행, `~/.claude/skills`에 rea-video 링크, `git pull` 후 무검토 재설치를 하지 않는다.
