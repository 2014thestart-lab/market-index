# LLM / GPU Market Index

세 개의 일별 지수를 **API 키 없이** 수집·렌더링하는 파이프라인.

| 차트 | 출력 | 소스 |
|---|---|---|
| LLM Token Expenditure Index | `out/token_expenditure_index.png` | OpenRouter rankings × models |
| Token Usage (OpenRouter) | `out/token_usage.png` | OpenRouter rankings |
| GPU Rental Index | `out/gpu_rental_index.png` | Vast.ai marketplace |

---

## 가장 중요한 제약: 히스토리는 소급되지 않는다

키 없이 접근 가능한 `openrouter.ai/api/frontend/v1/rankings/models`는
`view=week` / `view=month`를 받지만, **완전한 데이터가 있는 날짜는 최신 1일뿐**이다.
그 이전 날짜들은 뒤늦게 도착한 일부 행만 조각으로 돌아온다. 실측 결과:

```
2026-09-05   models=530   tokens=382,864,509,702,129   <- 완전
2026-09-04   models=  7   tokens=     13,699,312,915   <- 조각
2026-09-03   models=  8   tokens=     13,876,466,971   <- 조각
```

이 조각을 완전한 날과 섞으면 지수가 1 → 50까지 튀는 쓰레기가 나온다.
그래서 `collect.py`는 날짜별 모델 수를 세어 임계치(`COMPLETE_MIN_MODELS=100`)
이상인 날만 `complete=1`로 표시하고, `render.py`는 그 날짜만 사용한다.
한 번 `complete=1`이 된 날은 이후 실행에서 강등되지 않는다.

**결론: 첫날은 데이터 1개다. 하루에 하루씩 쌓인다.**
업로드하신 원본이 9개월치를 갖고 있는 이유도 같다 — 소급이 아니라 축적이다.
차트는 2일치부터 그려지고, 우측 상단 WoW 배지는 8일치부터 나온다.

과거를 소급하려면 유료 경로뿐이다. OpenRouter Data API
(`/api/v1/datasets/rankings-daily`)는 `start_date`/`end_date`를 받아
과거 구간을 그대로 주지만 API 키가 필요하다. 키가 생기면 `collect.py`의
URL만 갈아끼우면 나머지는 그대로 돌아간다.

---

## 지수 산식

**Token Expenditure Index** — 총지출액이 아니라 **사용량 가중 혼합 단가**다.

```
blended $/Mtok = Σ(prompt_tok × prompt_price + completion_tok × completion_price)
                 ─────────────────────────────────────────────────────────────── × 10⁶
                                    Σ(total tokens)

index = blended / blended[첫날]      (첫날 = 1.0)
```

트래픽이 저가 모델로 이동하면 내려가고, 고가 모델로 몰리면 올라간다.
원본 차트에서 토큰 사용량이 20배 늘었는데 지수는 1.0 근처에 머무는 형태와 일치한다.

- 현재 커버리지: **토큰 물량의 99.1%**가 가격 매칭됨 (매 실행 시 콘솔에 출력)
- 매칭은 `model_permaslug` → `canonical_slug` → `model_id` 순, 실패 시 `-YYYYMMDD` 접미사 제거 후 재시도
- `MAX_UNIT_USD_PER_MTOK=200` 초과 모델은 제외 (이미지·rerank 등 토큰당 단가가 무의미한 것들)
- ⚠️ 과거 시점의 가격표는 구할 수 없어 **최신 가격 스냅샷을 과거로 소급 적용**한다.
  `model_price` 테이블이 며칠 쌓이면 `load_expenditure()`를 as-of 조인으로 바꿔라.
  (코드에 주석으로 표시해 둠)

**GPU Rental Index** — Vast.ai 렌더블 오퍼의 GPU당 시간단가(`dph_total / num_gpus`)를
받아 min / p25 / median / mean을 저장. 차트는 median을 쓰고 SXM·PCIE를 세대별로
평균낸 뒤 첫날 = 1.0으로 정규화한다. 원본과 동일하게 Y축 3개를 독립으로 쓴다.

---

## 실행

```bash
pip install -r requirements.txt
python scripts/collect.py     # 수집 (멱등, 재실행 안전)
python scripts/render.py      # 렌더
python scripts/render.py --demo   # 합성 데이터로 스타일만 미리보기
```

`--demo`는 `out/preview_*.png`를 만들고 푸터에 SYNTHETIC 워터마크를 박는다.
스타일 확인용이며 `.gitignore`에 들어가 있다.

## 자동화 (권장: GitHub Actions)

`.github/workflows/daily.yml`이 매일 **01:20 UTC (10:20 KST)** 에 돌면서
수집 → 렌더 → `data/market.db`와 `out/*.png`를 리포지토리에 커밋한다.

이 시각을 고른 이유: OpenRouter가 전일자를 확정한 뒤라 매 실행이 정확히
새로운 완전일 하나를 잡는다.

서버가 필요 없고, SQLite가 git에 버전 관리되어 히스토리가 통째로 남고,
차트 PNG는 커밋될 때마다 README에 바로 임베드된다. 로컬 cron은
PC가 꺼져 있으면 그날 데이터가 영구 유실되므로 권하지 않는다 —
소급이 안 되는 이 파이프라인에서는 치명적이다.

**세팅:** 리포 생성 → 파일 푸시 → Settings ▸ Actions ▸ General ▸
Workflow permissions를 **Read and write**로 변경 → Actions 탭에서
`daily-collect`를 `Run workflow`로 한 번 수동 실행해 시드.

## 스키마

```
token_usage(date, model_permaslug, variant, prompt_tokens, completion_tokens,
            reasoning_tokens, request_count, complete, collected_at)
model_price(date, model_id, canonical_slug, prompt_usd, completion_usd, collected_at)
gpu_price  (date, gpu_class, n_offers, min_dph, p25_dph, median_dph, mean_dph, collected_at)
```

전부 UPSERT라 하루에 여러 번 돌려도 안전하다. 수집기는 세 소스 중
일부가 실패해도 나머지를 커밋하고, 셋 다 죽었을 때만 exit 1을 낸다.

## 튜닝 포인트

- `collect.py`의 `GPU_CLASSES` — 추적 GPU 추가 (`H200`, `RTX 5090`, `MI300X` 등)
- `render.py`의 `FOOTER` — 워터마크 문구
- `render.py` 상단 팔레트 상수 — 색상
- 차트는 90일 미만이면 `MM-DD`, 이상이면 `YY.MM`으로 X축 라벨이 자동 전환된다

## 출처 표기

OpenRouter 데이터를 재배포·인용할 때는 다음을 명시할 것:
`Source: OpenRouter (openrouter.ai/rankings)`
