import { getDeviceId } from '@/core/config/deviceId';
import {
  BASE_URL_WS,
  USE_MOCK_WS,
  WS_ANALYZE_PATH,
} from '@/core/config/env';
import type { AnalysisEvent, CallLog } from '@/data/models/types';
import { analyze } from './analyzer';

/**
 * 실시간 분석 연결 추상화.
 * - 흐름: connect → start → call_started → (발화마다) sendMessage → ack/detected
 * - USE_MOCK_WS=true(기본): 앱 내부 목 분석기 사용(백엔드 불필요)
 * - USE_MOCK_WS=false: 실제 WebSocket(ws/wss)로 백엔드 연결
 * - 앱이 닫지 않았는데 연결이 끝나거나 10초 안에 열리지 않으면 connection_closed를 한 번 보낸다.
 */
export interface AnalyzeConnection {
  onEvent(cb: (e: AnalysisEvent) => void): void;
  /** 통화 시작. audio:true면 오디오(실시간 스트리밍) 세션으로 시작한다. */
  start(name: string, opts?: { audio?: boolean }): void;
  /** 텍스트 발화 전송 (네이티브 온디바이스 STT 경로) */
  sendMessage(role: string, content: string, turnIndex: number): void;
  /** 오디오 청크 전송 (웹 실시간: WAV base64) */
  sendAudioChunk(base64: string, chunkIndex: number): void;
  /** 에이전트 행동 카드에 대한 반응 (확인/닫기) */
  sendActionResponse(actionId: number, response: 'acknowledged' | 'dismissed'): void;
  /** 후속 확인 질문에 대한 답 */
  sendFollowupAnswer(actionId: number, answer: string): void;
  /** 통화 종료 알림. 서버가 마지막 상태를 확정하고 call_ended를 보낸다. */
  sendEnd(): void;
  close(): void;
}

/** 백엔드 없이 앱 내부에서 분석 (Expo Go 데모 기본값) */
class MockAnalyzeConnection implements AnalyzeConnection {
  private cb: ((e: AnalysisEvent) => void) | null = null;
  private accumulated: string[] = [];
  private logId = Math.floor(Date.now() / 1000) % 100000;
  private closed = false;

  onEvent(cb: (e: AnalysisEvent) => void) {
    this.cb = cb;
  }

  private emit(e: AnalysisEvent) {
    if (!this.closed) this.cb?.(e);
  }

  start(name: string, _opts?: { audio?: boolean }) {
    const now = new Date().toISOString();
    const call: CallLog = {
      id: this.logId,
      device_id: getDeviceId(),
      name,
      status: 'normal',
      risk_score: 0,
      risk_level: 'low',
      detected_label: 0,
      core_evidence: '',
      created_at: now,
      updated_at: now,
    };
    setTimeout(() => this.emit({ type: 'call_started', call }), 120);
  }

  sendMessage(role: string, content: string, turnIndex: number) {
    this.accumulated.push(`${role}: ${content}`);
    const result = analyze(this.accumulated.join('\n'));
    const message = {
      id: turnIndex,
      log_id: this.logId,
      turn_index: turnIndex,
      role,
      content,
      created_at: new Date().toISOString(),
    };
    // 네트워크 지연 흉내
    setTimeout(() => {
      if (result.isPhishing) {
        this.emit({
          type: 'phishing_detected',
          log_id: this.logId,
          is_phishing: true,
          risk_score: result.riskScore,
          risk_level: result.riskLevel,
          matched_patterns: result.matchedPatterns,
          core_evidence: result.coreEvidence,
          notification: {},
          message,
        });
      } else {
        this.emit({
          type: 'analysis_ack',
          log_id: this.logId,
          is_phishing: false,
          risk_score: result.riskScore,
          risk_level: result.riskLevel,
          message,
        });
      }
    }, 180);
  }

  // 목은 오디오 전사 능력이 없으므로 청크는 무시한다(실제 백엔드에서만 동작).
  sendAudioChunk(_base64: string, _chunkIndex: number) {}

  // 목 분석기에는 대응 에이전트가 없다. 종료 알림에만 바로 응답한다.
  sendActionResponse(_actionId: number, _response: 'acknowledged' | 'dismissed') {}

  sendFollowupAnswer(_actionId: number, _answer: string) {}

  sendEnd() {
    this.emit({ type: 'call_ended', log_id: this.logId, final: {} });
  }

  close() {
    this.closed = true;
    this.cb = null;
  }
}

/** 연결이 열리기를 기다리는 상한. 넘기면 서버에 연결하지 못한 것으로 알린다(멈춘 서버에 매달려 '듣는 중'으로 남지 않게). */
const CONNECT_TIMEOUT_MS = 10000;

/** 실제 WebSocket 연결 */
class RealAnalyzeConnection implements AnalyzeConnection {
  private ws: WebSocket;
  private cb: ((e: AnalysisEvent) => void) | null = null;
  private queue: object[] = [];
  private ready = false;
  private opened = false; // 한 번이라도 열렸는지: 못 열었으면 '연결하지 못함', 열렸다 닫히면 '끊김'
  private done = false; // 앱이 닫았거나 끊김을 이미 알렸다(그 뒤로는 보내지도 알리지도 않는다)
  private connectTimer: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    this.ws = new WebSocket(`${BASE_URL_WS}${WS_ANALYZE_PATH}`);
    this.connectTimer = setTimeout(() => this.lost(), CONNECT_TIMEOUT_MS);
    this.ws.onopen = () => {
      if (this.done) return;
      this.clearConnectTimer();
      this.opened = true;
      this.ready = true;
      this.queue.forEach((m) => this.ws.send(JSON.stringify(m)));
      this.queue = [];
    };
    this.ws.onmessage = (ev) => {
      try {
        const data = JSON.parse(String(ev.data)) as AnalysisEvent;
        this.cb?.(data);
      } catch {
        this.cb?.({ type: 'error', message: '응답을 해석할 수 없습니다.' });
      }
    };
    // 브라우저는 오류 뒤에 늘 close를 보내지만, 어느 쪽이 먼저 와도 끊김은 한 번만 알린다.
    this.ws.onerror = () => this.lost();
    this.ws.onclose = () => this.lost();
  }

  private clearConnectTimer() {
    if (this.connectTimer) clearTimeout(this.connectTimer);
    this.connectTimer = null;
  }

  /** 앱이 닫지 않았는데 연결이 끝났다(또는 제때 열리지 않았다). 한 번만 알리고 그 뒤로는 아무것도 보내지 않는다. */
  private lost() {
    if (this.done) return;
    this.done = true;
    this.ready = false;
    this.queue = [];
    this.clearConnectTimer();
    try {
      this.ws.close();
    } catch {
      // ignore
    }
    this.cb?.({ type: 'connection_closed', opened: this.opened });
  }

  private send(msg: object) {
    if (this.done) return;
    if (this.ready) this.ws.send(JSON.stringify(msg));
    else this.queue.push(msg);
  }

  onEvent(cb: (e: AnalysisEvent) => void) {
    this.cb = cb;
  }

  start(name: string, opts?: { audio?: boolean }) {
    if (opts?.audio) {
      // 오디오(실시간 스트리밍) 세션: 이후 audio_chunk(WAV) 프레임을 전송한다.
      this.send({ type: 'start', device_id: getDeviceId(), name, file_type: 'realtime', audio_format: 'wav' });
    } else {
      // 텍스트 발화 경로도 file_type을 명시한다(백엔드 start 검증 요구).
      this.send({ type: 'start', device_id: getDeviceId(), name, file_type: 'realtime' });
    }
  }

  sendMessage(role: string, content: string, turnIndex: number) {
    this.send({ type: 'message', role, content, turn_index: turnIndex });
  }

  sendAudioChunk(base64: string, chunkIndex: number) {
    // API_SPEC 7: base64 JSON 방식(chunk_index로 응답 상관관계 추적 용이).
    this.send({ type: 'audio_chunk', chunk_index: chunkIndex, audio_format: 'wav', audio_base64: base64 });
  }

  sendActionResponse(actionId: number, response: 'acknowledged' | 'dismissed') {
    this.send({ type: 'action_response', action_id: actionId, response });
  }

  sendFollowupAnswer(actionId: number, answer: string) {
    this.send({ type: 'followup_answer', action_id: actionId, answer });
  }

  sendEnd() {
    this.send({ type: 'end' });
  }

  close() {
    // 앱이 닫는 것이므로 뒤따라 오는 onclose는 끊김으로 알리지 않는다.
    this.done = true;
    this.ready = false;
    this.clearConnectTimer();
    try {
      this.ws.close();
    } catch {
      // ignore
    }
    this.cb = null;
  }
}

export function createAnalyzeConnection(): AnalyzeConnection {
  return USE_MOCK_WS ? new MockAnalyzeConnection() : new RealAnalyzeConnection();
}
