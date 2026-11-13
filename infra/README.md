# infra — 영상 분석 Modal 앱 (B 인프라)

`modal_app.py`가 Modal 앱 `ok-muncheol-vision`을 정의한다. 처리 로직은 `workers/modal_worker.py`(테스트: `tests/test_modal_worker.py`)에 있다.
BE는 `app/modal_dispatch.py`로 웹 엔드포인트를 부르고, 워커는 `POST /internal/analysis/{task_id}`로 결과를 보낸다(계약 문서 「영상 분석(Modal)」).

## 처리 순서

1. BE가 `analyze` 엔드포인트에 job을 보낸다(프록시 인증). job은 `task_id`·`case_id`·`video_url`·`callback_url`·`callback_token`과, 있으면 `preview_upload_url`·`frame_uploads`다.
2. `analyze`가 모양을 확인하고 `run_job`(GPU T4)을 spawn한 뒤 `{"call_id"}`를 바로 돌려준다.
3. `run_job`이 다음 순서로 처리한다.
   - 영상을 받는다.
   - H.264 미리보기를 만들어 PUT한다. 분석과 나란히 돌고, 실패해도 분석은 계속한다.
   - 정지원 `analyze_video_sync`로 분석한다.
   - 근거 프레임 JPG를 칸에 PUT한다.
   - 콜백을 보낸다. 5xx·429·연결 실패면 4번까지 다시 보낸다.

Modal에는 Supabase·DB 키가 없다. 시크릿에는 VLM용 `OPENAI_API_KEY` 하나뿐이다.

워커 이미지에 들어가는 저장소 코드는 `workers/`와 `app/__init__.py`·`app/schemas/`(카드 스키마, `workers/vision/fact_card.py`가 쓴다)뿐이다. `workers/`가 그 밖의 저장소 모듈을 import하면 로컬 테스트는 지나가도 운영에서 `ModuleNotFoundError`가 난다(2026-11-13 첫 실행). `tests/test_modal_worker.py`가 이것을 막는다.

## 처음 한 번 (사용자)

1. modal.com 계정을 만든다(Starter, 월 $30 무료 크레딧).
   - T4 GPU 함수를 배포하려면 Settings → Usage & Billing에 결제 수단(카드)을 등록해야 한다. 없으면 배포가 `Please add a payment method to use T4 GPU functions`로 거절된다. CPU 함수는 카드 없이 돈다.
2. CLI를 설치하고 로그인한다.

   ```bash
   brew install pipx && pipx install modal
   modal setup        # 브라우저 로그인
   ```

3. 대시보드 → Secrets → Custom에서 시크릿 `ok-muncheol-vision`을 만든다. 키는 `OPENAI_API_KEY` 하나다.
   - Modal 전용 OpenAI 키를 새로 만들어 사용 한도를 건다.
   - 대화창(`!` 명령)에 키를 붙여 넣지 않는다.
4. 대시보드 → Settings → Proxy Tokens(문서 이름은 Proxy Auth Tokens)에서 토큰을 만든다.
   - Token ID(`wk-…`)가 `Modal-Key`, Token Secret(`ws-…`)이 `Modal-Secret`이다.
   - Secret은 만들 때 한 번만 보인다. 대화창에 붙여 넣지 말고, 클립보드에서 바로 `vercel env add … --sensitive`로 넣는다.

## 배포 (CLI 로그인 뒤)

1. 모델 8개를 볼륨 맨 위에 올린다. 구글 드라이브가 두 폴더로 나눠 받았다.

   ```bash
   D=../데이터/LG_CNS_KG-Project1-vision_models
   for f in video_model_v3.pt video_model_j3.pt type_head.pt motion_movement_B.joblib \
            scene_location_type.joblib scene_road_priority.joblib yolo11s.pt; do
     modal volume put ok-muncheol-models "$D/$f" "/$f"
   done
   modal volume put ok-muncheol-models "$D 2/video_model_tr.pt" /video_model_tr.pt
   modal volume ls ok-muncheol-models   # 8개 확인
   ```

2. 저장소 루트에서 `modal deploy infra/modal_app.py`를 실행한다.
   - 처음에는 이미지 빌드(torch·Hugging Face 본체)로 몇 분 걸린다.
   - 빌드 중 로컬 연결이 끊기면(DNS 오류 등) 배포만 실패하고, 서버에서 끝난 빌드 단계는 캐시에 남는다. 노트북이 잠들지 않게 `caffeinate -is modal deploy infra/modal_app.py`로 다시 돌린다.
   - 출력된 `analyze` 주소가 `MODAL_ANALYZE_URL`이다. 모양은 `https://<워크스페이스>--ok-muncheol-vision-analyze.<지역>.modal.run`이고, 인증 없이 부르면 401이다.
3. Vercel BE Production에 `MODAL_ANALYZE_URL`·`MODAL_PROXY_KEY`·`MODAL_PROXY_SECRET`을 Sensitive로 넣고 다시 배포한다.
   - 콜백 주소는 Vercel 운영 도메인이 기본이다(`PUBLIC_BACKEND_URL`로 바꿀 수 있다).
4. 확인한다.
   - 사건 하나에 영상을 올리고 `POST /cases/{id}/analysis`를 보낸다.
   - Modal 대시보드의 `run_job` 로그에서 "분석 완료, 근거 프레임 n장"과 "콜백 200"을 본다.
   - 2026-11-13 첫 운영 실행(AI Hub 샘플 742KB): 접수부터 카드 v1 저장까지 차가운 시작 포함 69초였고 콜백은 200이었다. 근거 프레임 사진은 BE가 `frame_uploads` 칸을 넘길 때만 올라간다.

## 비용 (2026-11 Starter 기준)

- 쓴 초만큼만 과금한다. 요청이 없으면 컨테이너가 꺼진다(scale to zero). 그래서 서버를 직접 켜고 끄지 않는다.
- T4는 초당 $0.000164(시간당 약 $0.59)다. 월 $30이면 T4만으로 약 50시간이다. CPU·메모리도 함께 붙어서 실제로는 그보다 조금 짧다.
- 분석 한 건이 쓰는 시간
  - 차가운 시작: 모델 약 3.5GB 로드 + 분석 + VLM + 대기 60초로, 대략 2~3분이다.
  - 이어서 들어온 건: 1분 안쪽이다.
  - $30이면 수백 건 이상이다.
- 아끼는 장치
  - `max_containers=1`: GPU 컨테이너는 하나만 띄운다.
  - `scaledown_window=60`: 대기 60초 뒤 끈다.
  - `timeout=900`: 한 건이 15분을 넘지 않는다. BE 작업 시간 초과와 같다.
  - 남은 크레딧은 대시보드 Usage에서 본다.
- VLM(OpenAI gpt-4o) 비용은 Modal 크레딧과 별도다.
