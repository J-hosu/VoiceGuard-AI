/**
 * 음성·대화 처리 안내 문구(온보딩·설정·결과 화면·실시간 시작 화면 공용).
 *
 * 서버 코드가 실제로 하는 일에 맞춘 문장이다. 아래 동작이 바뀌면 이 문구도 함께 고친다.
 * - 음성: 실시간 조각·녹음 파일·통화 후 전체 녹음 모두 서버로 올라온다. 서버는 임시 파일에 써서 받아 적은 뒤 바로 지우고
 *   (backend/app/services/audio_transcriber.py transcribe_audio_bytes, backend/app/api/routes.py의 os.unlink),
 *   음성 대신 해시만 남긴다(backend/app/services/evidence.py record_audio).
 * - 리턴제로: STT_FILE_ENGINE=rtzr(녹음 파일·통화 후 전체 재전사, 화자 구분)나 STT_REFINE=rtzr(통화 중 화자 구분 정리)를
 *   켠 서버만 음성을 리턴제로로 보낸다(backend/app/services/audio_transcriber.py).
 * - 글: 받아 적은 발화는 서버 DB(call_messages)에 남고, 상태 추적기가 위험 판단을 위해 Claude로 보낸다
 *   (backend/app/repository.py insert_call_message, backend/app/services/state_tracker.py).
 * - 문자 검사·문서 점검: 붙여 넣은 글과 사진을 Claude로 보낸다(backend/app/services/message_analyzer.py, document_analyzer.py).
 */
export const PRIVACY_NOTICE =
  '음성은 우리 서버로 보내 글로 받아 적고, 음성 파일은 받아 적은 뒤 바로 지웁니다. ' +
  '화자 구분을 켠 서버에서는 외부 음성 인식(리턴제로)으로도 보냅니다. ' +
  '받아 적은 글은 서버에 저장되고, 위험 판단을 위해 AI(Claude)에 보냅니다.';

/** 문자 검사·문서 점검 안내(설정 화면에서 위 문구에 덧붙인다) */
export const PRIVACY_NOTICE_CHECKS = '문자 검사와 문서 점검에 올린 글과 사진도 AI(Claude)에 보냅니다.';

/** 실시간 분석 시작 화면의 한 줄 요약 */
export const PRIVACY_NOTICE_SHORT = '음성은 서버로 보내 받아 적고, 받아 적은 글은 저장해 AI(Claude)로 판단해요.';
