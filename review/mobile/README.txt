모바일 앱 수정 전달 패치

이 저장소에는 React Native StockApp의 원본이 없다. StockApp-local-update.patch는 별도 로컬 앱용 패치이며 서버 PR 병합으로 앱에 자동 반영되지 않는다.
BASELINE-FILES.json은 패치 대상의 원본/수정본 SHA256이다. 대상은 app/src/tests/emulator-tests의 12개 파일이다.
현재 사용자 원본에 git apply --check를 실행해 충돌 없음만 확인했으며 쓰기는 하지 않았다.
운영 Firebase/결제/광고 설정, google-services.json, 서명키, 패키지명, node_modules, APK는 포함하지 않았다.
패치 적용 전 원본 대상 파일과 사용자 변경을 백업하고 해시를 대조해야 한다. 다르면 덮어쓰지 않고 충돌을 검토한다.

포함 내용
취소 요청 접수와 서버 확인 후 복구 구분, 인증 조회 실패 시 완료 확인 불가, 최근 인증과 서버시각으로 묶인 localhost Spark 접수 후보.
광고 promise/native 오류 처리와 세션 정리. 기존 광고·구독 정책은 유지한다.
실제 앱 accountDeletionTransport의 mode는 disabled이며 live endpoint/환경변수 활성화 스위치는 없다.
합성 및 에뮬레이터 검사는 로컬 테스트용이다. 운영 계정/자격증명을 넣어 실행하지 않는다.

안전한 읽기 확인 (StockApp 폴더에서):
  git apply --check <이 폴더>/StockApp-local-update.patch
실제 적용은 별도 승인·백업 이후 수행한다. 지금 PR 준비는 적용 승인이 아니다.
추가 변경 전 75개 검사/타입/lint는 이미 통과했으며 이번 전달에서는 최종 소스 해시 일치로 그 결과를 재사용한다.
TEST 1.0.6 APK에는 최신 화면과 모의 서비스가 들어 있다. 운영 서비스 모듈은 모의 구현으로 교체된다.
따라서 APK가 정상 작동해도 실제 회원 삭제/결제/로그인/광고/서버 연결이 검증된 것은 아니다.
