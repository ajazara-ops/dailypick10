DailyPick10 — 7일 탈퇴 로컬 구현 검토본
상태: draft PR용. 운영 비활성 / Spark 유지 / 규칙·IAM·스케줄 미적용.

이번 코드가 해결하는 것
신청 서버시각부터 604800초를 유지하며 재로그인/중복 신청은 기한을 늘리지 않는다.
취소는 cancel_requested로 접수하고, 서버가 동의·Auth 상태·기한·삭제 선점 여부를 확인한 뒤 cancelled로 확정한다.
서버 확인 전 서비스 차단을 해제하지 않는다. 인증 철회·UID 세대 불일치·조회 실패는 자동 삭제/복구를 허용하지 않는다.
Auth와 프로필 삭제의 중간 실패는 단계 기록으로 재시도한다. 이미 시작한 영구 삭제가 복구 가능하다고 안내하지 않는다.
앱에서 인증된 완료 조회가 실패하면 완료 확인 불가이며, 공개 비로그인 영수증 경로는 추가하지 않는다.

안전 경계
account_deletion.DeletionService의 기본 모드는 disabled이며 synthetic 어댑터만 받는다.
SparkIntentBridge도 기본 비활성이다. emulator_only는 demo-dailypick10-deletion과 정확한 localhost 포트, 살아 있는 Emulator Hub를 강제한다.
운영 서버 바인딩, 운영 credential 로딩, 임의 UID 삭제 CLI, 새 workflow 또는 스케줄은 이 PR에 없다.
기존 .github/workflows/daily_scan.yml 및 identity-check.yml과 추천/알림 코드를 변경하지 않는다.
runtime-plan.json.example은 운영 연결이 비어 있는 검토 자료다. 환경변수 하나로 live를 켤 수 있는 구현이 아니다.

검사 근거
최종 소스 15개가 직전 검증 해시와 일치하는 것을 확인했다. 기존 고유 검사 75개와 타입·lint 통과 증거를 재사용한다.
75개 구성: 앱/모의 흐름 28 + 새 서버 15 + 기존 서버 경계 14 + 실제 localhost SDK·규칙 13 + localhost Firestore 트랜잭션/모의 Auth 부작용 5.
마지막 5개는 실제 회원 삭제 검사가 아니다. 운영 IAM/인덱스/쿼터/스케줄·실기기·실제 결제/광고는 미검증이다.
review/VALIDATION.json은 전달 시점의 추가 검사와 APK 검증 범위를 기록한다. 이전 75개를 반복 합산하지 않는다.

재현 경로
순수 합성 서버 검사(저장소 루트):
  python -m unittest discover -s tests -p test_spark_safe_cancel.py -v
  python -m unittest discover -s tests -p test_spark_deadline.py -v
  python -m unittest discover -s tests -p test_deletion_boundary.py -v
에뮬레이터 모듈은 기존 별도 시험 환경용이며 자동 실행되지 않는다. 필요한 firebase_admin/google-cloud-firestore 등 도구 설치·실행 승인은 별도다.
앱 연결 검사를 재현할 때는 이 서버 저장소와 패치를 적용한 앱을 각각 형제 폴더 server/, mobile/에 둔다.
spark_intake_runner.py 및 emulator_app_runner.py는 이 형제 폴더 구조와 명시적 demo/localhost 환경을 요구한다.
실제 에뮬레이터가 없거나 credential 환경변수가 감지되면 실행을 거부하며 운영으로 폴백하지 않는다.

모바일 전달
이 GitHub 저장소에는 React Native 원본 앱이 없다. review/mobile/StockApp-local-update.patch는 별도 로컬 StockApp용이다.
원본 변경과 서버 PR 병합은 별개다. 패치는 검토/전달만 하며 이번 작업에서 원본에 적용하지 않았다.
APK는 실제 앱 컴포넌트와 모의 서비스를 넣은 별도 TEST 패키지이며 저장소에는 업로드하지 않는다.
운영 적용에 필요한 규칙 범위·권한·중단/복원은 FIREBASE-RULES-CHANGE.txt 참조.
