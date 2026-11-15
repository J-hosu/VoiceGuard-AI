import { useCallback, useEffect, useRef, useState } from 'react';
import { Platform } from 'react-native';
import { categorizeType, extractKeywords } from '@/core/utils/keywords';
import { riskLevelFromScore, type RiskLevel } from '@/core/utils/riskLevel';
import type {
  AgentAction,
  AnalysisEvent,
  CallResult,
  LiveChunk,
  RefinedTranscript,
  RefinedTurn,
  ScamStage,
  SpeakerSide,
  TranscriptTurn,
} from '@/data/models/types';
import type { Scenario } from '@/data/mock/mockScenarios';
import { createSttService, type SttService } from '@/data/services/sttService';
import { createAudioCapture, type AudioCaptureService } from '@/data/services/audioCapture';
import { analyzeSessionAudio, retranscribeSessionAudio } from '@/data/services/uploadAudio';
import { createAnalyzeConnection, type AnalyzeConnection } from '@/data/services/wsService';
import { useSettingsStore } from '@/state/settingsStore';

export interface CallSessionState {
  logId: number; // 백엔드가 발급한 통화 로그 id (call_started) — 0이면 아직 없음
  turns: TranscriptTurn[];
  score: number; // 현재 누적 위험도
  level: RiskLevel;
  matchedPatterns: string[];
  coreEvidence: string;
  elapsedSec: number;
  running: boolean;
  paused: boolean; // 분석 일시정지 상태 (오디오 캡처만 멈추고 WS 세션은 유지)
  finished: boolean;
  error: string | null; // 백엔드 연결/분석 오류 (실사용 시 표시)
  // ── v2 능동형 대응 루프 (state_update / agent_action) ──
  stage: ScamStage; // 지금까지 도달한 사기 단계
  stageIndex: number; // 0(없음)~6(금전 요구)
  phishingType: string; // 사기 유형 표시 문자열
  predictedNext: ScamStage | null; // 다음 예상 단계
  summary: string; // AI가 요약한 현재 상황
  actions: AgentAction[]; // 에이전트가 결정한 행동(받은 순서)
  // ── 정리본 (transcript_refined) ──
  refined: RefinedTranscript | null; // 한 번도 안 왔으면 null(STT_REFINE=off): 화면은 기존 '방금 들린 말' 그대로
  liveChunks: LiveChunk[]; // 실시간 조각 원본. 정리본이 덮은 구간의 조각은 정리본이 올 때 지운다.
}

/** 말풍선 하나의 최대 길이(글자). 이보다 길어지면 문장이 이어져도 새 말풍선을 만든다. */
const MAX_BUBBLE_CHARS = 90;

/** 앞 조각이 문장 중간에서 끊겼는지: 문장 끝 기호로 끝나지 않고, 이어 붙여도 너무 길지 않을 때만 true. */
function continuesSentence(previous: string, next: string): boolean {
  return !/[.?!。…]["'”’)]?\s*$/.test(previous) && previous.length + next.length + 1 <= MAX_BUBBLE_CHARS;
}

/** 유한한 숫자면 그대로, 아니면(없음·NaN) fallback */
function finiteOr(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

// 정리본 경계(refined_until)는 실시간 조각의 끝이고, 조각 시각과 발화의 t_start는 서버가 받은 오디오 누적 길이를 같은
// 자리수(소수 셋째 자리)로 반올림한 값이다. 경계에서 바로 시작한 발화는 경계와 똑같은 값으로 오므로 약속대로
// t_start >= refined_until을 그대로 쓰고 부동소수 잡음만 흡수한다. 여유를 넓히면 안 된다: 서버는 조각 안 발화의 시작을
// 조각 끝보다 최소 0.01초 앞에 두어(live_refine.segment_times) 그 조각까지 덮은 정리본 앞으로 판정되게 하므로,
// 여유가 0.01초를 넘으면 같은 말이 정리본 말풍선과 '받아 적는 중'에 두 번 보인다.
const REFINED_EDGE_S = 1e-6;

/** 정리본이 아직 닿지 않은 실시간 조각인지(정리본 뒤에 '받아 적는 중'으로 보일 조각) */
export function isProvisional(chunk: LiveChunk, refinedUntil: number): boolean {
  return chunk.tStart >= refinedUntil - REFINED_EDGE_S;
}

/** speaker_sides 정리: 약속한 값(other/me) 말고는 모두 '아직 모름'으로 본다. */
function normalizeSides(raw: unknown): Record<string, SpeakerSide> {
  const sides: Record<string, SpeakerSide> = {};
  if (raw && typeof raw === 'object') {
    for (const [speaker, side] of Object.entries(raw as Record<string, unknown>)) {
      sides[speaker] = side === 'other' || side === 'me' ? side : 'unknown';
    }
  }
  return sides;
}

/**
 * STT(목/실제) → WS(목/실제) 오케스트레이션.
 * 발화 인식마다 WS로 전송하고, 응답으로 위험도/근거/자막을 갱신한다.
 * 강한 경고 임계값을 처음 넘으면 onDangerCross 콜백을 1회 호출한다.
 */
export function useCallSession(
  scenario: Scenario,
  opts: {
    speed?: number;
    onDangerCross?: (s: CallSessionState) => void;
    /** 에이전트가 새 행동을 결정했을 때 1회 호출된다(진동 등 알림용). */
    onAction?: (action: AgentAction) => void;
  } = {},
) {
  const dangerThreshold = useSettingsStore((s) => s.dangerThreshold);
  const [state, setState] = useState<CallSessionState>({
    logId: 0,
    turns: [],
    score: 0,
    level: 'safe',
    matchedPatterns: [],
    coreEvidence: '',
    elapsedSec: 0,
    running: false,
    paused: false,
    finished: false,
    error: null,
    stage: 'none',
    stageIndex: 0,
    phishingType: '',
    predictedNext: null,
    summary: '',
    actions: [],
    refined: null,
    liveChunks: [],
  });

  // 최신 state를 참조로 보관 — 종료 시 grace 대기 후 "그 시점의 최종" turns/logId/score를 읽기 위함.
  const latestRef = useRef(state);
  latestRef.current = state;

  // 세션은 start() 시점에 1회 생성(실제 WS 소켓이 렌더마다 새로 열리는 것 방지)
  const connRef = useRef<AnalyzeConnection | null>(null);
  const sttRef = useRef<SttService | null>(null);
  const captureRef = useRef<AudioCaptureService | null>(null);
  const speed = opts.speed ?? 1;

  // 웹 라이브(대본 없는 실시간) 세션은 오디오 스트리밍 경로를 사용한다.
  // (마이크 → 16kHz WAV 3초 청크 → 백엔드 Whisper 전사·채점)
  // 데모 시나리오(turns 존재)나 네이티브는 기존 STT(텍스트) 경로 유지.
  const audioMode = Platform.OS === 'web' && scenario.turns.length === 0;
  const turnCounter = useRef(0);
  const maxScore = useRef(0);
  const warned = useRef(false);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  const startedAt = useRef<number>(0);
  // 보낸 오디오 청크 수 vs 응답 받은 청크 수 — 종료 시 "전부 전사될 때까지" 대기용.
  const sentChunks = useRef(0);
  const recvChunks = useRef(0);
  const pausedAccumMs = useRef(0); // 일시정지 누적 시간(경과초 보정용)
  const pausedAtMs = useRef(0);
  const onDanger = useRef(opts.onDangerCross);
  onDanger.current = opts.onDangerCross;
  const onAction = useRef(opts.onAction);
  onAction.current = opts.onAction;
  const seenActions = useRef(new Set<number>()); // 이미 알린 행동 id (upsert 시 중복 알림 방지)
  const endedResolve = useRef<(() => void) | null>(null); // call_ended 대기 해제
  const lastLiveEnd = useRef(0); // 마지막 실시간 조각이 끝난 시각(초). t_start가 없는 조각의 대신 값

  const handleEvent = useCallback(
    (e: AnalysisEvent) => {
      // 오디오 청크 하나당 응답 하나(ack/detected/chunk_ack/error). chunk_index가 있으면 응답 1건으로 집계.
      if ('chunk_index' in e && (e as { chunk_index?: number }).chunk_index != null) {
        recvChunks.current += 1;
      }
      if (e.type === 'error' || e.type === 'audio_chunk_error') {
        setState((prev) => ({ ...prev, error: e.message }));
        return;
      }
      if (e.type === 'call_started') {
        // 백엔드가 통화 로그를 생성 → 종료 시 이 id로 상세를 조회한다.
        setState((prev) => ({ ...prev, logId: e.call.id }));
        return;
      }
      // v2: 상태 추적기가 통화 전체를 읽고 내린 판단. 위험도는 빠른 경로 점수와 융합된 값이다.
      if (e.type === 'state_update') {
        const score = e.risk_score;
        maxScore.current = Math.max(maxScore.current, score);
        setState((prev) => {
          const level = riskLevelFromScore(score, dangerThreshold);
          const next: CallSessionState = {
            ...prev,
            score,
            level,
            stage: e.stage,
            stageIndex: e.stage_index,
            phishingType: e.phishing_type,
            predictedNext: e.predicted_next,
            summary: e.summary,
            coreEvidence: e.summary || prev.coreEvidence,
          };
          if (!warned.current && level === 'danger') {
            warned.current = true;
            setTimeout(() => onDanger.current?.(next), 0);
          }
          return next;
        });
        return;
      }
      // v2: 에이전트 행동. 같은 id가 다시 오면(상태 변경) 교체하고, 처음 본 것만 알린다.
      if (e.type === 'agent_action') {
        const action = e.action;
        setState((prev) => {
          const exists = prev.actions.some((a) => a.id === action.id);
          const actions = exists
            ? prev.actions.map((a) => (a.id === action.id ? action : a))
            : [...prev.actions, action];
          return { ...prev, actions };
        });
        if (!seenActions.current.has(action.id)) {
          seenActions.current.add(action.id);
          if (action.status === 'delivered') onAction.current?.(action);
        }
        return;
      }
      if (e.type === 'call_ended') {
        endedResolve.current?.();
        endedResolve.current = null;
        return;
      }
      // 정리본: 서버가 (기본) 15초마다 지난 구간을 화자 구분과 함께 다시 받아 적어 보낸다. 새 발화만 오고, 화자 번호가 바뀐
      // 이전 발화는 relabels로, 화자별 쪽(상대방/나/아직 모름)은 매번 전부 온다. 이 이벤트가 한 번도 오지 않으면
      // (STT_REFINE=off) refined가 null로 남아 실시간 화면은 기존 '방금 들린 말' 그대로다.
      if (e.type === 'transcript_refined') {
        const added: RefinedTurn[] = [];
        for (const seg of e.segments ?? []) {
          const text = (seg.text ?? '').trim();
          // 시각이나 화자 번호가 없는 발화는 순서·말풍선 쪽을 정할 수 없어 뺀다.
          if (!text || !Number.isInteger(seg.speaker) || !Number.isFinite(seg.t_start)) continue;
          added.push({
            id: String(seg.id),
            tStart: seg.t_start,
            tEnd: Math.max(finiteOr(seg.t_end, seg.t_start), seg.t_start),
            speaker: seg.speaker,
            text,
            keywords: extractKeywords(text, 4),
          });
        }
        const relabels = new Map<string, number>();
        for (const r of e.relabels ?? []) {
          if (Number.isInteger(r.speaker)) relabels.set(String(r.id), r.speaker);
        }
        const sides = normalizeSides(e.speaker_sides);
        setState((prev) => {
          // 다른 통화의 정리본은 섞지 않는다.
          if (prev.logId > 0 && typeof e.log_id === 'number' && e.log_id !== prev.logId) return prev;
          const base = prev.refined ?? { turns: [], sides: {}, until: 0 };
          const addedIds = new Set(added.map((t) => t.id));
          const turns = base.turns
            .filter((t) => !addedIds.has(t.id))
            .map((t) => {
              const speaker = relabels.get(t.id);
              return speaker === undefined || speaker === t.speaker ? t : { ...t, speaker };
            })
            .concat(added)
            .sort((a, b) => a.tStart - b.tStart);
          // 경계는 앞으로만 간다(이미 말풍선이 된 구간을 다시 '받아 적는 중'으로 되돌리지 않는다).
          const until = Math.max(base.until, finiteOr(e.refined_until, base.until));
          return {
            ...prev,
            refined: { turns, sides: { ...base.sides, ...sides }, until },
            // 정리본이 덮은 구간의 실시간 조각은 다시 보일 일이 없으니 버린다.
            liveChunks: prev.liveChunks.filter((c) => isProvisional(c, until)),
          };
        });
        return;
      }
      // 오디오 청크 응답: 백엔드가 무음 경계 청크마다 "그 청크만" 독립 전사해 조각으로 내려준다.
      // 백엔드가 발화별 화자(화자A/화자B)를 라벨링하므로, 같은 화자의 연속 조각은 한 말풍선으로
      // 이어붙이고 화자가 바뀌면 새 말풍선을 만든다. (한 사람 말이 무음마다 여러 청크로 끊겨 와도
      // 자연스러운 대화 흐름으로 복원된다.)
      if (e.type === 'audio_analysis_ack' || e.type === 'audio_phishing_detected') {
        const score = e.risk_score;
        maxScore.current = Math.max(maxScore.current, score);
        const matched = e.type === 'audio_phishing_detected' ? e.matched_patterns : [];
        const evidence = e.type === 'audio_phishing_detected' ? e.core_evidence : '';
        const incoming = (e.transcripts ?? [])
          .map((tr) => {
            // 통화 시작부터의 시각(t_start/t_end). 없으면 앞 조각이 끝난 시각으로 둬서, 정리본이 그 뒤로 넘어가면 함께 지워지게 한다.
            const tStart = finiteOr(tr.t_start, lastLiveEnd.current);
            const tEnd = Math.max(finiteOr(tr.t_end, tStart), tStart);
            lastLiveEnd.current = Math.max(lastLiveEnd.current, tEnd);
            return {
              role: (tr.role ?? 'unknown').trim() || 'unknown',
              content: (tr.content ?? '').trim(),
              atSec: Math.round(tr.start_time ?? 0),
              tStart,
              tEnd,
            };
          })
          .filter((t) => t.content.length > 0);
        // 말풍선으로 합치기 전 조각을 시각과 함께 따로 남긴다. 정리본이 오면 정리본 뒤에 '받아 적는 중'으로 보인다.
        const chunks: LiveChunk[] = incoming.map((inc) => ({
          text: inc.content,
          tStart: inc.tStart,
          tEnd: inc.tEnd,
          keywords: extractKeywords(inc.content, 4),
        }));

        setState((prev) => {
          const level = riskLevelFromScore(score, dangerThreshold);
          const turns: TranscriptTurn[] = [...prev.turns];
          for (const inc of incoming) {
            const last = turns[turns.length - 1];
            // 실시간 음성 조각은 화자 구분 없이 모두 같은 화자(화자A)로 온다. 같은 화자라고 다 이어 붙이면 통화 전체가
            // 말풍선 하나("방금 들린 말")로 계속 길어진다. 그래서 앞 조각이 문장 중간에서 끊겼을 때(끝에 . ? ! 가 없고
            // 합쳐도 짧을 때)만 이어 붙이고, 나머지는 새 말풍선으로 둔다. 통화가 끝나면 전체 재전사로 화자가 다시 나뉜다.
            if (last && last.role === inc.role && inc.role !== 'unknown' && continuesSentence(last.content, inc.content)) {
              const mergedContent = `${last.content} ${inc.content}`.trim();
              turns[turns.length - 1] = {
                ...last,
                content: mergedContent,
                riskScore: score,
                keywords: extractKeywords(mergedContent, 4),
              };
            } else {
              turnCounter.current += 1;
              turns.push({
                turnIndex: turnCounter.current,
                role: inc.role,
                isMine: false,
                content: inc.content,
                atSec: inc.atSec,
                riskScore: score,
                keywords: extractKeywords(inc.content, 4),
              });
            }
          }
          const next: CallSessionState = {
            ...prev,
            turns,
            liveChunks: chunks.length > 0 ? [...prev.liveChunks, ...chunks] : prev.liveChunks,
            score,
            level,
            matchedPatterns: matched.length ? matched : prev.matchedPatterns,
            coreEvidence: evidence || prev.coreEvidence,
          };
          if (!warned.current && level === 'danger') {
            warned.current = true;
            setTimeout(() => onDanger.current?.(next), 0);
          }
          return next;
        });
        return;
      }
      if (e.type === 'analysis_ack' || e.type === 'phishing_detected') {
        const score = e.risk_score;
        maxScore.current = Math.max(maxScore.current, score);
        const matched = e.type === 'phishing_detected' ? e.matched_patterns : [];
        const evidence = e.type === 'phishing_detected' ? e.core_evidence : '';
        const turnIdx = e.message?.turn_index;

        setState((prev) => {
          const level = riskLevelFromScore(score, dangerThreshold);
          const turns = turnIdx
            ? prev.turns.map((t) => (t.turnIndex === turnIdx ? { ...t, riskScore: score } : t))
            : prev.turns;
          const next: CallSessionState = {
            ...prev,
            turns,
            score,
            level,
            matchedPatterns: matched.length ? matched : prev.matchedPatterns,
            coreEvidence: evidence || prev.coreEvidence,
          };
          if (!warned.current && level === 'danger') {
            warned.current = true;
            setTimeout(() => onDanger.current?.(next), 0);
          }
          return next;
        });
      }
    },
    [dangerThreshold],
  );

  // 경과초 타이머(일시정지 누적 시간 pausedAccumMs를 빼서 정지 중엔 멈춘 것처럼 보이게 함)
  const startTimer = () => {
    if (timer.current) return;
    timer.current = setInterval(() => {
      setState((prev) => ({
        ...prev,
        elapsedSec: Math.floor((Date.now() - startedAt.current - pausedAccumMs.current) / 1000),
      }));
    }, 1000);
  };

  // 오디오 캡처 시작(마이크→3초 WAV 청크→WS). start()와 resume()에서 재사용.
  const beginAudioCapture = (conn: AnalyzeConnection) => {
    const capture = createAudioCapture();
    captureRef.current = capture;
    capture.onError((m) => setState((prev) => ({ ...prev, error: m })));
    capture.onChunk((c) => {
      sentChunks.current += 1;
      conn.sendAudioChunk(c.base64, c.chunkIndex);
    });
    void capture.start();
  };

  const start = useCallback(() => {
    const conn = createAnalyzeConnection();
    connRef.current = conn;
    conn.onEvent(handleEvent);
    startedAt.current = Date.now();
    pausedAccumMs.current = 0;
    sentChunks.current = 0;
    recvChunks.current = 0;
    lastLiveEnd.current = 0;

    // ── 웹 실시간: 오디오 스트리밍 경로 ──
    if (audioMode) {
      conn.start(scenario.phone, { audio: true });
      beginAudioCapture(conn);
      startTimer();
      setState((prev) => ({ ...prev, running: true }));
      return;
    }

    // ── 네이티브/데모: 텍스트 STT 경로 ──
    const stt = createSttService(scenario, speed);
    sttRef.current = stt;
    conn.start(scenario.phone);

    stt.onTurn((t) => {
      turnCounter.current += 1;
      const idx = turnCounter.current;
      const turn: TranscriptTurn = {
        turnIndex: idx,
        role: t.role,
        isMine: t.isMine,
        content: t.content,
        atSec: t.atSec,
        keywords: t.isMine ? [] : extractKeywords(t.content, 4),
      };
      setState((prev) => ({ ...prev, turns: [...prev.turns, turn] }));
      conn.sendMessage(t.role, t.content, idx);
    });
    stt.onEnd(() => {
      setState((prev) => ({ ...prev, finished: true }));
    });

    startTimer();
    stt.start();
    setState((prev) => ({ ...prev, running: true }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [handleEvent, scenario, speed, audioMode]);

  const stop = useCallback(() => {
    sttRef.current?.stop();
    captureRef.current?.stop();
    connRef.current?.close();
    if (timer.current) clearInterval(timer.current);
    timer.current = null;
    setState((prev) => ({ ...prev, running: false }));
  }, []);

  // 분석 일시정지: 오디오 캡처(또는 데모 STT)만 멈추고 WS 세션은 유지 → 같은 통화로 이어서 재개.
  const pause = useCallback(() => {
    if (!connRef.current) return;
    captureRef.current?.stop();
    captureRef.current = null;
    sttRef.current?.stop();
    if (timer.current) {
      clearInterval(timer.current);
      timer.current = null;
    }
    pausedAtMs.current = Date.now();
    setState((prev) => ({ ...prev, paused: true, running: false }));
  }, []);

  const resume = useCallback(() => {
    const conn = connRef.current;
    if (!conn) return;
    pausedAccumMs.current += Date.now() - pausedAtMs.current;
    if (audioMode) beginAudioCapture(conn);
    else sttRef.current?.start();
    startTimer();
    setState((prev) => ({ ...prev, paused: false, running: true }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [audioMode]);

  // 정상 종료: 아직 3초 못 채운 마지막 오디오를 마저 보내고, "보낸 청크 전부의 전사 응답이
  // 도착할 때까지" 기다린 뒤(최대 MAX_WAIT_MS) WS를 닫는다. 대기 후의 "최종" state를 반환한다.
  // → 종료 시점에 아직 전사 안 된 마지막 발화까지 대화 내역에 남는다. (지연은 감수)
  const MAX_WAIT_MS = 20000; // 백엔드가 멈춰도 무한 대기하지 않도록 안전 상한
  const END_WAIT_MS = 15000; // call_ended 대기 상한
  const finish = useCallback(async (): Promise<CallSessionState> => {
    const capture = captureRef.current;
    capture?.flushFinal(); // 남은 partial 오디오를 마지막 청크로 전송(sentChunks 증가)
    // 전체 재전사용: stop() 전에 통화 전체 녹음을 확보한다.
    const sessionWav = capture?.getSessionWav() ?? null;
    capture?.stop();
    captureRef.current = null;
    sttRef.current?.stop();
    if (timer.current) {
      clearInterval(timer.current);
      timer.current = null;
    }
    setState((prev) => ({ ...prev, running: false }));

    // ── 전체 재전사 경로 ──
    // 실시간 청크는 조각마다 독립 전사라 화자 A/B가 청크 경계에서 틀린다("2003년"이 상대방으로,
    // 두 화자가 한 청크에 섞이는 등). 종료 시 통화 전체를 한 번에 전사하면 모델이 전체 목소리를
    // 비교해 화자를 정확히 가르므로, 결과 화면 대화 내역을 이 재전사 결과로 교체한다.
    if (sessionWav) {
      try {
        // 실시간 세션 로그가 있으면 그 로그에 덮어써(단일 기록) 재전사하고, 없으면 새 로그를 만든다.
        const existingLogId = latestRef.current.logId;
        const result =
          existingLogId > 0
            ? await retranscribeSessionAudio(existingLogId, sessionWav)
            : await analyzeSessionAudio(sessionWav);
        if (result.segments.length > 0) {
          const turns: TranscriptTurn[] = result.segments.map((seg, i) => {
            const content = seg.text.trim();
            return {
              turnIndex: i + 1,
              role: seg.speaker, // 화자A / 화자B
              isMine: seg.speaker === '화자B', // 화자B=나(우), 화자A=상대방(좌)
              content,
              atSec: Math.round(seg.start),
              riskScore: result.riskScore,
              keywords: extractKeywords(content, 4),
            };
          });
          maxScore.current = Math.max(maxScore.current, result.riskScore);
          const corrected: CallSessionState = {
            ...latestRef.current,
            turns,
            logId: result.logId,
            score: result.riskScore,
            level: riskLevelFromScore(result.riskScore, dangerThreshold),
            matchedPatterns: result.matchedPatterns.length ? result.matchedPatterns : latestRef.current.matchedPatterns,
            coreEvidence: result.coreEvidence || latestRef.current.coreEvidence,
            running: false,
          };
          connRef.current?.close();
          connRef.current = null;
          setState(corrected);
          return corrected;
        }
      } catch {
        // 재전사 실패 → 아래 실시간 누적 turns 폴백
      }
    }

    // ── 폴백: 재전사가 없거나(오디오 없음) 실패 → 기존처럼 청크 응답을 마저 기다린다 ──
    // 보낸 청크 수만큼 응답(전사)이 다 올 때까지 대기. 상한 초과 시 중단.
    const deadline = Date.now() + MAX_WAIT_MS;
    while (recvChunks.current < sentChunks.current && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
    // 서버에 종료를 알리고, 마지막 상태 확정과 통화 후 행동(call_ended)을 기다린다.
    const conn = connRef.current;
    if (conn && latestRef.current.logId > 0) {
      await new Promise<void>((resolve) => {
        const timeout = setTimeout(resolve, END_WAIT_MS);
        endedResolve.current = () => {
          clearTimeout(timeout);
          resolve();
        };
        conn.sendEnd();
      });
    }
    // 마지막 응답의 setState가 latestRef에 반영될 시간을 조금 더 준다.
    await new Promise((resolve) => setTimeout(resolve, 400));
    connRef.current?.close();
    connRef.current = null;
    return latestRef.current;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dangerThreshold]);

  const buildResult = useCallback(
    (id: number): CallResult => {
      const fullText = state.turns.map((t) => `${t.role}: ${t.content}`).join('\n');
      return {
        id,
        name: scenario.phone,
        category: categorizeType(state.matchedPatterns, fullText),
        finalScore: maxScore.current,
        matchedPatterns: state.matchedPatterns,
        coreEvidence: state.coreEvidence || '분석된 위험 근거가 없습니다.',
        keywords: extractKeywords(fullText, 6),
        turns: state.turns,
        source: scenario.source,
        createdAt: new Date().toISOString(),
        durationSec: state.elapsedSec,
      };
    },
    [scenario.phone, scenario.source, state],
  );

  // 행동 카드 반응(확인/닫기)과 후속 확인 답을 서버로 보낸다. 결과는 agent_action 이벤트로 돌아온다.
  const respondAction = useCallback((actionId: number, response: 'acknowledged' | 'dismissed') => {
    connRef.current?.sendActionResponse(actionId, response);
  }, []);

  const answerFollowup = useCallback((actionId: number, answer: string) => {
    connRef.current?.sendFollowupAnswer(actionId, answer);
  }, []);

  useEffect(() => {
    return () => {
      sttRef.current?.stop();
      captureRef.current?.stop();
      connRef.current?.close();
      if (timer.current) clearInterval(timer.current);
    };
  }, []);

  return { state, start, stop, pause, resume, finish, buildResult, respondAction, answerFollowup };
}
