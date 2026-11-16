/**
 * 파일 분석 공용 로직 (플랫폼 무관).
 *
 * 업로드 자체는 웹(실제 File/Blob)과 네이티브({uri,name,type})가 달라서
 * uploadAudio.ts(네이티브) / uploadAudio.web.ts(웹) 로 분기하고,
 * 엔드포인트 URL · MIME 추론 · 응답 파싱은 이 모듈을 공유한다.
 *
 * 백엔드: POST {BASE_URL_HTTP}/calls/analyze-audio (multipart, field=file)
 * 응답(API_SPEC 6. 녹음 파일 분석):
 *   { log_id, file_name, segments:[{chunk_id,start_time(초),end_time(초),speaker,text}],
 *     is_phishing, risk_score, risk_level, phishing_type, matched_patterns, core_evidence, notification }
 */
import { ANALYZE_AUDIO_PATH, BASE_URL_HTTP } from '@/core/config/env';
import { NETWORK_ERROR_TEXT } from '@/core/utils/format';
import type { BackendRiskLevel } from '@/data/models/types';

/** 서버가 받는 녹음 형식. backend/app/api/routes.py(analyze-audio)가 파일 이름 확장자로 이 셋만 받는다. */
export const ACCEPTED_AUDIO_EXTENSIONS = ['mp3', 'wav', 'm4a'] as const;

/** 파일 고르기 창에 넘기는 형식(MIME). 웹은 확장자도 함께 넘겨 브라우저마다 같은 파일만 고르게 한다. */
const ACCEPTED_AUDIO_MIME = ['audio/mpeg', 'audio/mp3', 'audio/wav', 'audio/x-wav', 'audio/wave', 'audio/mp4', 'audio/x-m4a', 'audio/m4a'];
export const AUDIO_PICKER_TYPES_WEB = [...ACCEPTED_AUDIO_EXTENSIONS.map((ext) => `.${ext}`), ...ACCEPTED_AUDIO_MIME];
export const AUDIO_PICKER_TYPES_NATIVE = ACCEPTED_AUDIO_MIME;

/** 파일 이름이 서버가 받는 확장자로 끝나는지 */
export function isAcceptedAudioName(fileName: string): boolean {
  const ext = fileName.toLowerCase().split('.').pop() ?? '';
  return fileName.includes('.') && (ACCEPTED_AUDIO_EXTENSIONS as readonly string[]).includes(ext);
}

/** 처리 시간이 길어 중간(Cloudflare 등)이 끊었을 때 보이는 문장 */
export const UPLOAD_TIMEOUT_TEXT = '처리 시간이 길어 중단됐어요. 2분 이하 파일로 다시 시도해 주세요.';
// 이보다 오래 걸린 뒤 온 JSON 아닌 오류(502 등)는 오래 처리하다 끊긴 것으로 본다.
const LONG_REQUEST_MS = 30000;

/**
 * 서버 문장 뒤에 붙은 예외 원문을 떼어 낸다(예: '오디오 파일을 전사하지 못했습니다: Invalid data found…' → '…못했습니다.').
 * 콜론 뒤가 한국어 안내면 그대로 둔다.
 */
function withoutExceptionText(detail: string): string {
  const m = detail.match(/^([\s\S]*?니다)\s*:\s*([\s\S]+)$/);
  return m && !/[가-힣]/.test(m[2]) ? `${m[1]}.` : detail;
}

/**
 * 실패 응답을 화면에 보일 문장으로 바꾼다.
 * 서버(FastAPI)가 보낸 JSON 오류는 detail 문장을 쓰고, 프록시가 보낸 HTML 오류 원문은 보이지 않는다(콘솔에만 남는다).
 */
export function uploadErrorMessage(status: number, raw: string, elapsedMs: number): string {
  try {
    const detail = (JSON.parse(raw) as { detail?: unknown })?.detail;
    if (typeof detail === 'string' && detail.trim()) return withoutExceptionText(detail.trim());
    if (detail !== undefined) return '파일을 처리하지 못했어요. 다시 시도해 주세요.';
  } catch {
    // JSON이 아니다(Cloudflare·프록시의 HTML 오류 페이지 등)
  }
  if (status === 413) return '파일이 너무 커요. 더 작은 파일로 다시 시도해 주세요.';
  if (status === 504 || status === 524 || elapsedMs >= LONG_REQUEST_MS) return UPLOAD_TIMEOUT_TEXT;
  return '서버에서 오류가 났어요. 잠시 뒤 다시 시도해 주세요.';
}

/** 선택한 오디오 파일 정보 (DocumentPicker asset에서 추출) */
export interface PickedAudio {
  uri: string;
  name: string;
  mime?: string;
}

/** 발화 세그먼트 (start/end 단위: 초). 파일 분석은 화자분리 없음 → speaker='speaker_a' */
export interface AudioSegment {
  speaker: string;
  text: string;
  start: number; // 초
  end: number; // 초
}

/** 백엔드 파일 분석 결과 (정규화) */
export interface AudioAnalysisResult {
  logId: number;
  segments: AudioSegment[];
  isPhishing: boolean;
  riskScore: number;
  riskLevel: BackendRiskLevel;
  phishingType: string;
  matchedPatterns: string[];
  coreEvidence: string;
}

export const ANALYZE_AUDIO_URL = `${BASE_URL_HTTP}${ANALYZE_AUDIO_PATH}`;

export function guessMime(fileName: string): string {
  const ext = fileName.toLowerCase().split('.').pop();
  switch (ext) {
    case 'mp3':
      return 'audio/mpeg';
    case 'm4a':
      return 'audio/mp4';
    case 'aac':
      return 'audio/aac';
    case 'ogg':
      return 'audio/ogg';
    case 'flac':
      return 'audio/flac';
    case 'wav':
    default:
      return 'audio/wav';
  }
}

/**
 * 녹음을 분석 API에 올리고 결과를 받는다(녹음 파일 분석·통화 후 전체 재전사 공용).
 * 요청 자체가 실패하면(인터넷 끊김 등) 친절한 문장으로, timeoutMs가 지나면 처리 시간 초과 문장으로 실패한다.
 */
export async function postForAnalysis(url: string, form: FormData, timeoutMs = 0): Promise<AudioAnalysisResult> {
  const startedAt = Date.now();
  const controller = timeoutMs > 0 && typeof AbortController !== 'undefined' ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
  let res: Response;
  try {
    res = await fetch(url, { method: 'POST', body: form, signal: controller?.signal });
  } catch (e) {
    // eslint-disable-next-line no-console
    console.warn('[VoiceGuard] 분석 요청 실패', e);
    throw new Error(controller?.signal.aborted ? UPLOAD_TIMEOUT_TEXT : NETWORK_ERROR_TEXT);
  } finally {
    if (timer) clearTimeout(timer);
  }
  return parseAnalyzeResponse(res, Date.now() - startedAt);
}

/** fetch 응답을 검사하고 정규화된 분석 결과로 변환한다. elapsedMs는 요청부터 응답까지 걸린 시간(오류 문장 고르기용). */
export async function parseAnalyzeResponse(res: Response, elapsedMs = 0): Promise<AudioAnalysisResult> {
  const raw = await res.text().catch(() => '');
  // 디버깅: 백엔드 원본 응답을 콘솔에 남긴다(웹 F12 Console에서 확인).
  // eslint-disable-next-line no-console
  console.log('[VoiceGuard] analyze-audio 응답', res.status, raw.slice(0, 3000));

  if (!res.ok) {
    throw new Error(uploadErrorMessage(res.status, raw, elapsedMs));
  }

  let data: {
    log_id?: number;
    segments?: { start_time?: number; end_time?: number; speaker?: unknown; text?: string }[];
    is_phishing?: boolean;
    risk_score?: number;
    risk_level?: BackendRiskLevel;
    phishing_type?: string;
    matched_patterns?: string[];
    core_evidence?: string;
  };
  try {
    data = JSON.parse(raw);
  } catch {
    // 성공 코드인데 JSON이 아니다(주소 설정이 틀려 웹 페이지가 온 경우 등). 원문은 위 콘솔 로그에 있다.
    throw new Error('서버 응답을 읽지 못했어요. 잠시 뒤 다시 시도해 주세요.');
  }

  const segments: AudioSegment[] = (data.segments ?? [])
    .map((s) => ({
      speaker: s.speaker != null ? String(s.speaker) : 'unknown',
      text: (s.text ?? '').trim(),
      start: s.start_time ?? 0,
      end: s.end_time ?? 0,
    }))
    .filter((s) => s.text.length > 0);

  return {
    logId: data.log_id ?? 0,
    segments,
    isPhishing: Boolean(data.is_phishing),
    riskScore: data.risk_score ?? 0,
    riskLevel: data.risk_level ?? 'low',
    phishingType: data.phishing_type ?? '',
    matchedPatterns: data.matched_patterns ?? [],
    coreEvidence: data.core_evidence ?? '',
  };
}
