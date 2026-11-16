/**
 * 파일 분석 — 웹(브라우저) 업로드 구현.
 *
 * 브라우저에서는 네이티브의 {uri,name,type} 형식이 통하지 않는다.
 * DocumentPicker가 준 uri(blob:/data:/http:)를 실제 Blob으로 받아 File로 감싼 뒤
 * FormData에 append 해야 서버가 정상적인 multipart 파일로 인식한다.
 * (Metro가 웹 번들에서 uploadAudio.ts 대신 이 파일을 우선 해석한다)
 */
import { getDeviceId } from '@/core/config/deviceId';
import { BASE_URL_HTTP } from '@/core/config/env';
import {
  ANALYZE_AUDIO_URL,
  guessMime,
  postForAnalysis,
  type AudioAnalysisResult,
  type PickedAudio,
} from './uploadAudio.shared';

export type { AudioAnalysisResult, AudioSegment, PickedAudio } from './uploadAudio.shared';

/**
 * 통화 후 전체 녹음을 올려 다시 받아 적을 때 기다리는 상한. 넘기면 실시간으로 받은 전사로 결과를 낸다.
 * 인터넷이 끊긴 채로 요청이 매달려 '분석 마무리 중'에서 멈추지 않게 한다(Cloudflare도 100초에 요청을 끊는다).
 */
const SESSION_UPLOAD_TIMEOUT_MS = 100000;

export async function analyzeAudioFile(file: PickedAudio): Promise<AudioAnalysisResult> {
  // 선택한 파일의 uri를 실제 바이너리(Blob)로 로드한다.
  let blob: Blob;
  try {
    const srcRes = await fetch(file.uri);
    if (!srcRes.ok) throw new Error(String(srcRes.status));
    blob = await srcRes.blob();
  } catch {
    throw new Error('선택한 파일을 읽을 수 없습니다. 다시 선택해 주세요.');
  }
  const type = file.mime || blob.type || guessMime(file.name);
  const uploadFile = new File([blob], file.name, { type });

  const form = new FormData();
  form.append('file', uploadFile);
  form.append('device_id', String(getDeviceId()));

  return postForAnalysis(ANALYZE_AUDIO_URL, form);
}

/**
 * 실시간 통화 종료 시, 세션 전체 녹음(WAV Blob)을 통째로 분석 API에 올려 재전사한다.
 * 청크별 전사와 달리 통화 전체를 한 번에 보므로 화자 A/B가 정확하게 분리된다.
 */
export async function analyzeSessionAudio(audio: Blob): Promise<AudioAnalysisResult> {
  const uploadFile = new File([audio], 'session.wav', { type: 'audio/wav' });
  const form = new FormData();
  form.append('file', uploadFile);
  form.append('device_id', String(getDeviceId()));

  return postForAnalysis(ANALYZE_AUDIO_URL, form, SESSION_UPLOAD_TIMEOUT_MS);
}

/**
 * 기존 통화 로그(logId)에 세션 전체 녹음을 재전사해 덮어쓴다.
 * 새 로그를 만들지 않고 실시간 세션 로그 하나에 정확한 화자 A/B 결과를 반영한다.
 */
export async function retranscribeSessionAudio(logId: number, audio: Blob): Promise<AudioAnalysisResult> {
  const uploadFile = new File([audio], 'session.wav', { type: 'audio/wav' });
  const form = new FormData();
  form.append('file', uploadFile);

  return postForAnalysis(`${BASE_URL_HTTP}/calls/${logId}/retranscribe-audio`, form, SESSION_UPLOAD_TIMEOUT_MS);
}
